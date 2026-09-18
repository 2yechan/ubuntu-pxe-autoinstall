#!/usr/bin/env python3
"""Per-machine autoinstall seed renderer and install-completion callback.

Speaks plain HTTP on 127.0.0.1 behind nginx (which proxies /autoinstall/,
/done/ and /health to it).  Python standard library only.

Routes
------
  GET  /autoinstall/<mac>/user-data       rendered subiquity autoinstall config
  GET  /autoinstall/<mac>/meta-data       nocloud meta-data (instance-id)
  GET  /autoinstall/<mac>/vendor-data     empty, on purpose
  GET  /autoinstall/<mac>/network-config  empty, on purpose
  GET|POST /done/<mac>                    install finished -> stop PXE-installing
  GET  /health                            liveness + current config summary

The node number comes from the *client address*, not from the MAC: Kea hands
out the pool iteratively from DHCP_RANGE's first address, so the machine that
powered on first is holding the first address and becomes node 1.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import os
import re
import socketserver
import sys
import threading
import urllib.parse

CONFIG_PATH = os.path.join(os.environ.get("RUNTIME_DIR", "/run/pxe"), "config.json")

MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")
_write_lock = threading.Lock()


def log(level: str, msg: str) -> None:
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    stream = sys.stderr if level in ("ERROR", "WARN") else sys.stdout
    print(f"{ts} [autoinstall] {level:<5} {msg}", file=stream, flush=True)


class BadRequest(Exception):
    """Client error; carries the status code to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class Config:
    def __init__(self, data: dict):
        self.server_ip = data["server_ip"]
        self.range_start = ipaddress.IPv4Address(data["range_start"])
        self.range_end = ipaddress.IPv4Address(data["range_end"])
        self.range_size = int(data["range_size"])
        self.hostname_prefix = data["hostname_prefix"]
        self.hostname_width = int(data["hostname_width"])
        self.user_name = data["user_name"]
        self.password_hash = data["password_hash"]
        self.ssh_pubkey = data.get("ssh_pubkey") or None
        self.sizing_policy = data["sizing_policy"]
        self.iso_name = data["iso_name"]
        self.tftp_root = data["tftp_root"]
        self.template_dir = data["template_dir"]
        self.listen_port = int(data["listen_port"])
        self.locale = data.get("locale", "en_US.UTF-8")
        self.keyboard_layout = data.get("keyboard_layout", "us")

    def node_index(self, client_ip: str) -> int:
        """1-based position of `client_ip` inside DHCP_RANGE."""
        try:
            addr = ipaddress.IPv4Address(client_ip)
        except ValueError:
            raise BadRequest(400, f"{client_ip!r} is not an IPv4 address")
        index = int(addr) - int(self.range_start) + 1
        if index < 1 or index > self.range_size:
            raise BadRequest(
                400,
                f"client address {client_ip} is outside DHCP_RANGE "
                f"{self.range_start}-{self.range_end}; cannot derive a node "
                "number. Is another DHCP server answering on this segment?",
            )
        return index

    def hostname(self, index: int) -> str:
        return f"{self.hostname_prefix}{index:0{self.hostname_width}d}"


_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def render(template_dir: str, name: str, values: dict) -> str:
    with open(os.path.join(template_dir, name), encoding="utf-8") as fh:
        text = fh.read()
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            missing.append(key)
            return m.group(0)
        return str(values[key])

    out = _PLACEHOLDER.sub(sub, text)
    if missing:
        raise RuntimeError(
            f"template {name} has unfilled placeholders: {', '.join(sorted(set(missing)))}"
        )
    return out


# ---------------------------------------------------------------------------
# MAC handling
# ---------------------------------------------------------------------------


def normalise_mac(raw: str) -> str:
    """'52-54-00-AB-CD-EF' / '525400abcdef' -> '52:54:00:ab:cd:ef'.

    The canonical spelling is GRUB's: lowercase, colon-separated, because the
    file we write has to be found by `[ -f "$prefix/grub.cfg-$net_default_mac" ]`.

    Strict validation doubles as the path-traversal guard for the filename we
    are about to build out of this.
    """
    cleaned = raw.strip().lower().replace(":", "").replace("-", "").replace(".", "")
    if not MAC_HEX_RE.match(cleaned):
        raise BadRequest(400, f"{raw!r} is not a MAC address")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------


def build_user_data(cfg: Config, mac: str, client_ip: str, hostname: str) -> str:
    if cfg.ssh_pubkey:
        ssh_block = (
            "    authorized-keys:\n"
            f"      - {json.dumps(cfg.ssh_pubkey)}\n"
        )
    else:
        ssh_block = ""

    late_commands = [
        _sources_list_fixup(),
        _completion_callback(cfg.server_ip, mac),
    ]
    # JSON is valid YAML, so json.dumps gives us correctly escaped argv-form
    # commands without hand-rolling any YAML quoting.
    late_block = "\n".join(f"    - {json.dumps(c)}" for c in late_commands)

    return render(
        cfg.template_dir,
        "user-data.tmpl",
        {
            "HOSTNAME": hostname,
            "CLIENT_IP": client_ip,
            "MAC": mac,
            "SERVER_IP": cfg.server_ip,
            "LOCALE": cfg.locale,
            "KEYBOARD_LAYOUT": cfg.keyboard_layout,
            "DISK_SIZING_POLICY": cfg.sizing_policy,
            "USER_NAME": cfg.user_name,
            "PASSWORD_HASH": cfg.password_hash,
            "SSH_AUTHORIZED_KEYS_BLOCK": ssh_block,
            "LATE_COMMANDS_BLOCK": late_block,
            "GENERATED_AT": datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        },
    )


def _sources_list_fixup() -> list:
    """Undo the deliberately-broken mirror we used to force an offline install.

    Runs in the installer environment (not in-target), so it writes through
    /target.  The codename is read from the installed system rather than
    hard-coded, so this keeps working if you point ISO_PATH at a different
    release.
    """
    script = r"""
set -e
. /target/etc/os-release
cat > /target/etc/apt/sources.list.d/ubuntu.sources <<EOF
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: ${VERSION_CODENAME} ${VERSION_CODENAME}-updates ${VERSION_CODENAME}-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: ${VERSION_CODENAME}-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
""".strip()
    return ["sh", "-c", script]


def _completion_callback(server_ip: str, mac: str) -> list:
    """Tell the container this machine is done, so it stops PXE-installing it.

    Never fails the install: a machine that installed fine but could not phone
    home should still reboot into its new system.  It will just PXE-install
    again next boot, which is recoverable; a failed install is not.
    """
    url = f"http://{server_ip}/done/{mac}"
    script = f"""
URL="{url}"
for attempt in 1 2 3 4 5; do
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -m 15 -X POST "$URL" && exit 0
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O- --timeout=15 --post-data='' "$URL" && exit 0
    else
        python3 -c 'import sys,urllib.request as u; u.urlopen(u.Request(sys.argv[1], data=b"", method="POST"), timeout=15).read()' "$URL" && exit 0
    fi
    sleep 3
done
echo "WARNING: could not reach $URL; this host will PXE-install again on next boot" >&2
exit 0
""".strip()
    return ["sh", "-c", script]


def write_host_grub_cfg(cfg: Config, mac: str, hostname: str, client_ip: str) -> str:
    """Drop grub/grub.cfg-<mac> so this machine boots its disk from now on."""
    content = render(
        cfg.template_dir,
        "grub.cfg-host.tmpl",
        {
            "MAC": mac,
            "HOSTNAME": hostname,
            "CLIENT_IP": client_ip,
            "TIMESTAMP": datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        },
    )
    target = os.path.join(cfg.tftp_root, "grub", f"grub.cfg-{mac}")
    tmp = target + ".tmp"
    with _write_lock:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ubuntu-pxe-autoinstall/1.0"
    protocol_version = "HTTP/1.1"
    config: Config  # injected below

    # -- helpers ------------------------------------------------------------

    def client_ip(self) -> str:
        """Real client address.

        nginx is the only thing that can reach us (it proxies from localhost),
        so X-Real-IP is trustworthy here; fall back to the socket peer for
        direct requests, which is what happens when you curl the service from
        inside the container while debugging.
        """
        forwarded = self.headers.get("X-Real-IP")
        if forwarded:
            return forwarded.strip()
        return self.client_address[0]

    def respond(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Silence BaseHTTPRequestHandler's own stderr logging; we log richer
        # lines ourselves and nginx already logs the access line.
        pass

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        cfg = self.config
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        client = self.client_ip()

        # Drain any request body so keep-alive stays in sync.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        try:
            parts = [p for p in path.split("/") if p]

            if parts == ["health"]:
                self.respond(
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "range": f"{cfg.range_start}-{cfg.range_end}",
                            "hosts": cfg.range_size,
                            "iso": cfg.iso_name,
                            "server_ip": cfg.server_ip,
                        },
                        indent=2,
                    )
                    + "\n",
                    "application/json",
                )
                return

            if len(parts) == 3 and parts[0] == "autoinstall":
                self._serve_seed(method, parts[1], parts[2], client)
                return

            if len(parts) == 2 and parts[0] == "done":
                if method not in ("GET", "POST", "HEAD"):
                    raise BadRequest(405, f"{method} not allowed on {path}")
                self._serve_done(parts[1], client)
                return

            raise BadRequest(404, f"no such resource: {path}")

        except BadRequest as exc:
            log("ERROR", f"{client} {method} {path} -> {exc.status}: {exc.message}")
            self.respond(exc.status, f"{exc.message}\n")
        except Exception as exc:  # pragma: no cover - defensive
            log("ERROR", f"{client} {method} {path} -> 500: {exc!r}")
            self.respond(500, f"internal error: {exc}\n")

    # -- handlers -----------------------------------------------------------

    def _serve_seed(self, method: str, raw_mac: str, resource: str, client: str) -> None:
        cfg = self.config
        mac = normalise_mac(raw_mac)

        if resource == "vendor-data":
            body = render(cfg.template_dir, "vendor-data.tmpl", {})
        elif resource == "network-config":
            body = render(cfg.template_dir, "network-config.tmpl", {})
        elif resource in ("user-data", "meta-data"):
            index = cfg.node_index(client)
            hostname = cfg.hostname(index)
            if resource == "meta-data":
                body = render(
                    cfg.template_dir,
                    "meta-data.tmpl",
                    {
                        "INSTANCE_ID": f"ubuntu-autoinstall-{mac.replace(':', '')}",
                        "HOSTNAME": hostname,
                    },
                )
            else:
                body = build_user_data(cfg, mac, client, hostname)
            log(
                "INFO",
                f"{client} {method} {resource} -> node #{index} hostname={hostname} "
                f"mac={mac}",
            )
            self.respond(200, body)
            return
        else:
            raise BadRequest(404, f"unknown seed resource {resource!r}")

        log("INFO", f"{client} {method} {resource} -> 200 (static) mac={mac}")
        self.respond(200, body)

    def _serve_done(self, raw_mac: str, client: str) -> None:
        cfg = self.config
        mac = normalise_mac(raw_mac)

        # Best effort: a completion callback from outside the pool is odd but
        # must not stop us from marking the host as installed.
        try:
            hostname = cfg.hostname(cfg.node_index(client))
        except BadRequest as exc:
            hostname = f"unknown({client})"
            log("WARN", f"/done/{mac} from {client}: {exc.message}")

        target = write_host_grub_cfg(cfg, mac, hostname, client)
        log(
            "INFO",
            f"{client} install complete: mac={mac} hostname={hostname} "
            f"-> wrote {target}; this host will now boot from local disk",
        )
        self.respond(
            200,
            f"ok: {hostname} ({mac}) recorded as installed; "
            f"next PXE boot chainloads the local disk\n",
        )


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 45 machines can hit the seed endpoint within a few seconds of each other.
    request_queue_size = 128


def main() -> int:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = Config(json.load(fh))
    except (OSError, KeyError, ValueError) as exc:
        log("ERROR", f"cannot load {CONFIG_PATH}: {exc}")
        return 1

    Handler.config = cfg
    server = ThreadedHTTPServer(("127.0.0.1", cfg.listen_port), Handler)
    log(
        "INFO",
        f"listening on 127.0.0.1:{cfg.listen_port} | pool {cfg.range_start}-"
        f"{cfg.range_end} ({cfg.range_size} hosts) -> "
        f"{cfg.hostname(1)}..{cfg.hostname(cfg.range_size)} | user={cfg.user_name} "
        f"| sizing-policy={cfg.sizing_policy}"
        + ("" if cfg.ssh_pubkey else " | no SSH_PUBKEY (password login only)"),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("INFO", "shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
