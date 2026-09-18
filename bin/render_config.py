#!/usr/bin/env python3
"""Validate the environment and render every runtime config file.

Run once by entrypoint.sh before supervisord starts.  Anything wrong with the
environment must be caught here, loudly, and must stop the container: a DHCP
server that comes up half-configured on somebody's production L2 segment is a
much worse outcome than a container that refuses to boot.

Nothing here writes USER_PASSWORD to disk.  The plaintext is turned into a
SHA-512 crypt hash exactly once (openssl passwd -6, fed over stdin so it never
appears in a process argument list) and only the hash is persisted.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys

# PXE_TEMPLATE_DIR / PXE_OUTPUT_ROOT exist so the generator can be exercised
# outside the image (see README, "Verifying without a target rack"). Both
# default to the in-image layout, so production behaviour is unchanged.
TEMPLATE_DIR = os.environ.get("PXE_TEMPLATE_DIR", "/opt/pxe/templates")
OUTPUT_ROOT = os.environ.get("PXE_OUTPUT_ROOT", "")

# Sane for an unattended install: the machine must keep its lease across the
# whole install and the first reboot, otherwise the "one server, one IP, in
# power-on order" property falls apart.
VALID_LIFETIME = 86400

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SIZING_POLICIES = ("all", "scaled")
BOOT_TRANSPORTS = ("http", "tftp")


class ConfigError(Exception):
    pass


_errors: list[str] = []


def err(msg: str) -> None:
    _errors.append(msg)


def warn(msg: str) -> None:
    print(f"[config] WARNING: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[config] {msg}")


def env(name: str, default: str | None = None) -> str | None:
    """Return a stripped env var, treating empty/whitespace as unset."""
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


# --------------------------------------------------------------------------
# template rendering
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def render(template_name: str, values: dict) -> str:
    with open(os.path.join(TEMPLATE_DIR, template_name), encoding="utf-8") as fh:
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
        raise ConfigError(
            f"template {template_name} has unfilled placeholders: "
            + ", ".join(sorted(set(missing)))
        )
    return out


def write(path: str, content: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    info(f"wrote {path}")


# --------------------------------------------------------------------------
# host interface discovery
# --------------------------------------------------------------------------


def host_addresses() -> list[tuple[str, ipaddress.IPv4Address]]:
    """[(ifname, addr), ...] for every IPv4 address on the host, minus loopback."""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"could not enumerate host interfaces via `ip`: {exc}")

    found = []
    for line in out.splitlines():
        parts = line.split()
        # "2: eth0    inet 192.168.101.254/24 brd ... scope global eth0"
        if len(parts) < 4 or parts[2] != "inet":
            continue
        ifname = parts[1]
        if ifname == "lo":
            continue
        try:
            addr = ipaddress.ip_address(parts[3].split("/")[0])
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv4Address):
            found.append((ifname, addr))
    return found


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def parse_env() -> dict:
    cfg: dict = {}

    # ---- SUBNET ------------------------------------------------------------
    subnet = None
    raw_subnet = env("SUBNET")
    if not raw_subnet:
        err("SUBNET is required (CIDR, e.g. 192.168.101.0/24)")
    else:
        try:
            subnet = ipaddress.ip_network(raw_subnet, strict=False)
        except ValueError as exc:
            err(f"SUBNET={raw_subnet!r} is not a valid CIDR: {exc}")
        else:
            if not isinstance(subnet, ipaddress.IPv4Network):
                err(f"SUBNET={raw_subnet!r} is not IPv4; only IPv4 DHCP is supported")
                subnet = None
            elif str(subnet) != raw_subnet:
                info(f"SUBNET {raw_subnet} normalised to {subnet}")
    cfg["subnet"] = subnet

    def in_subnet(name: str, value: str, addr: ipaddress.IPv4Address) -> bool:
        if subnet is None:
            return True  # already reported
        if addr not in subnet:
            err(f"{name}={value} is outside SUBNET {subnet}")
            return False
        return True

    def parse_ip(name: str, value: str) -> ipaddress.IPv4Address | None:
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            err(f"{name}={value!r} is not a valid IPv4 address")
            return None
        if not isinstance(addr, ipaddress.IPv4Address):
            err(f"{name}={value!r} is not IPv4")
            return None
        return addr

    # ---- SERVER_IP ---------------------------------------------------------
    server_ip = None
    raw_server = env("SERVER_IP")
    if not raw_server:
        err("SERVER_IP is required (this host's IP on the PXE network)")
    else:
        server_ip = parse_ip("SERVER_IP", raw_server)
        if server_ip is not None:
            in_subnet("SERVER_IP", raw_server, server_ip)
    cfg["server_ip"] = server_ip

    # ---- DHCP_RANGE --------------------------------------------------------
    start = end = None
    raw_range = env("DHCP_RANGE")
    if not raw_range:
        err("DHCP_RANGE is required (e.g. 192.168.101.1-192.168.101.45)")
    elif raw_range.count("-") != 1:
        err(f"DHCP_RANGE={raw_range!r} must be exactly 'START-END'")
    else:
        lo, hi = (p.strip() for p in raw_range.split("-"))
        start = parse_ip("DHCP_RANGE start", lo)
        end = parse_ip("DHCP_RANGE end", hi)
        if start is not None:
            in_subnet("DHCP_RANGE start", lo, start)
        if end is not None:
            in_subnet("DHCP_RANGE end", hi, end)
        if start is not None and end is not None and int(start) > int(end):
            err(f"DHCP_RANGE start {start} is greater than end {end}")
            start = end = None
    cfg["range_start"] = start
    cfg["range_end"] = end
    if start is not None and end is not None:
        size = int(end) - int(start) + 1
        cfg["range_size"] = size
        cfg["hostname_width"] = len(str(size))

    # ---- identity ----------------------------------------------------------
    user_name = env("USER_NAME")
    if not user_name:
        err("USER_NAME is required")
    elif not USERNAME_RE.match(user_name):
        err(
            f"USER_NAME={user_name!r} is not a valid Linux user name "
            "(lowercase letters, digits, '-', '_'; must not start with a digit)"
        )
    cfg["user_name"] = user_name

    # Read straight from os.environ: a password of "  " is a real (bad) password,
    # not an unset variable, and we should not silently normalise it away.
    password = os.environ.get("USER_PASSWORD")
    if not password:
        err("USER_PASSWORD is required")
    cfg["_password"] = password

    # ---- optional ----------------------------------------------------------
    gateway = None
    raw_gw = env("GATEWAY")
    if raw_gw:
        gateway = parse_ip("GATEWAY", raw_gw)
        if gateway is not None:
            in_subnet("GATEWAY", raw_gw, gateway)
    cfg["gateway"] = gateway

    dns: list[str] = []
    raw_dns = env("DNS")
    if raw_dns:
        for item in raw_dns.split(","):
            item = item.strip()
            if not item:
                continue
            addr = parse_ip("DNS entry", item)
            if addr is not None:
                dns.append(str(addr))
        if not dns:
            err(f"DNS={raw_dns!r} contained no usable IPv4 address")
    cfg["dns"] = dns

    sizing = env("DISK_SIZING_POLICY", "all")
    if sizing not in SIZING_POLICIES:
        err(
            f"DISK_SIZING_POLICY={sizing!r} must be one of "
            + ", ".join(SIZING_POLICIES)
        )
    cfg["sizing_policy"] = sizing

    transport = (env("BOOT_TRANSPORT", "tftp") or "tftp").lower()
    if transport not in BOOT_TRANSPORTS:
        err(f"BOOT_TRANSPORT={transport!r} must be one of " + ", ".join(BOOT_TRANSPORTS))
    cfg["boot_transport"] = transport

    cfg["ssh_pubkey"] = env("SSH_PUBKEY")

    # ---- ISO ---------------------------------------------------------------
    iso_path = env("ISO_PATH", "/iso/ubuntu-26.04-live-server-amd64.iso")
    if not os.path.isfile(iso_path):
        err(
            f"ISO_PATH={iso_path} does not exist inside the container. "
            "Mount the installer ISO read-only, e.g. "
            f"-v /path/to/ubuntu-26.04-live-server-amd64.iso:{iso_path}:ro"
        )
    cfg["iso_path"] = iso_path
    cfg["iso_name"] = os.path.basename(iso_path)

    # ---- DHCP interface ----------------------------------------------------
    iface = env("DHCP_INTERFACE")
    addrs = []
    try:
        addrs = host_addresses()
    except ConfigError as exc:
        err(str(exc))

    if iface:
        names = {n for n, _ in addrs}
        if names and iface not in names:
            warn(
                f"DHCP_INTERFACE={iface} has no IPv4 address right now "
                f"(interfaces with addresses: {', '.join(sorted(names)) or 'none'}). "
                "Continuing, but Kea will fail to open a socket if it stays that way."
            )
    elif subnet is not None:
        candidates = sorted({n for n, a in addrs if a in subnet})
        if not candidates:
            err(
                f"could not auto-detect DHCP_INTERFACE: no host interface has an "
                f"IPv4 address inside {subnet}. Run the container with "
                "--network host, or set DHCP_INTERFACE explicitly."
            )
        elif len(candidates) > 1:
            err(
                "DHCP_INTERFACE is ambiguous: "
                f"{', '.join(candidates)} all have an address inside {subnet}. "
                "Set DHCP_INTERFACE explicitly so we do not answer DHCP on the "
                "wrong link."
            )
        else:
            iface = candidates[0]
            info(f"auto-detected DHCP_INTERFACE={iface}")
    cfg["interface"] = iface

    # SERVER_IP must really be on this host: it is the TFTP next-server and the
    # base of every URL we hand out, and nginx binds to it.
    if server_ip is not None and addrs and server_ip not in {a for _, a in addrs}:
        err(
            f"SERVER_IP={server_ip} is not assigned to any interface of this host "
            f"(found: {', '.join(str(a) for _, a in addrs) or 'none'}). "
            "PXE clients would be told to fetch from an address nobody answers on."
        )
    elif server_ip is not None and iface:
        owners = {n for n, a in addrs if a == server_ip}
        if owners and iface not in owners:
            warn(
                f"SERVER_IP={server_ip} lives on {', '.join(sorted(owners))} but DHCP "
                f"will be served on {iface}; make sure they are on the same L2 segment."
            )

    return cfg


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------


def crypt_sha512(password: str) -> str:
    """SHA-512 crypt hash via openssl.

    Python's `crypt` module was removed in 3.13 and Ubuntu 26.04 ships 3.14,
    so shelling out to openssl is the portable option here.  The password goes
    in on stdin, never on argv.
    """
    proc = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=password,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ConfigError(f"openssl passwd -6 failed: {proc.stderr.strip()}")
    digest = proc.stdout.strip()
    if not digest.startswith("$6$"):
        raise ConfigError(f"openssl produced an unexpected hash: {digest[:16]!r}...")
    return digest


# --------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------


def _indent_json(value, spaces: int) -> str:
    """json.dumps, with every line after the first shifted right by `spaces`."""
    pad = " " * spaces
    lines = json.dumps(value, indent=2).splitlines()
    return "\n".join([lines[0]] + [pad + ln for ln in lines[1:]])


def build_kea(cfg: dict, paths: dict) -> str:
    option_data = []
    if cfg["gateway"] is not None:
        option_data.append({"name": "routers", "data": str(cfg["gateway"])})
    if cfg["dns"]:
        option_data.append(
            {"name": "domain-name-servers", "data": ", ".join(cfg["dns"])}
        )

    return render(
        "kea-dhcp4.conf.tmpl",
        {
            "DHCP_INTERFACE": cfg["interface"],
            "SERVER_IP": cfg["server_ip"],
            "SUBNET": cfg["subnet"],
            "POOL_START": cfg["range_start"],
            "POOL_END": cfg["range_end"],
            "BOOT_FILE": "shimx64.efi",
            "LEASE_FILE": paths["lease_file"],
            "VALID_LIFETIME": VALID_LIFETIME,
            "RENEW_TIMER": VALID_LIFETIME // 2,
            "REBIND_TIMER": VALID_LIFETIME * 7 // 8,
            # json.dumps keeps this a well-formed JSON array whether it holds
            # two entries or none; re-indent it to sit under "option-data".
            "SUBNET_OPTION_DATA": _indent_json(option_data, 8),
        },
    )


def build_grub_cfg(cfg: dict) -> str:
    server_ip = cfg["server_ip"]
    if cfg["boot_transport"] == "http":
        kernel_url = f"(http,{server_ip})/boot/vmlinuz"
        initrd_url = f"(http,{server_ip})/boot/initrd"
    else:
        # relative to the TFTP root we were loaded from
        kernel_url = "/boot/vmlinuz"
        initrd_url = "/boot/initrd"

    return render(
        "grub.cfg.tmpl",
        {
            "SERVER_IP": server_ip,
            "ISO_NAME": cfg["iso_name"],
            "KERNEL_URL": kernel_url,
            "INITRD_URL": initrd_url,
            "BOOT_TRANSPORT": cfg["boot_transport"],
            "MENU_TIMEOUT": 3,
        },
    )


def main() -> int:
    paths = {
        "tftp_root": os.environ.get("TFTP_ROOT", "/srv/tftp"),
        "http_root": os.environ.get("HTTP_ROOT", "/srv/http"),
        "runtime_dir": os.environ.get("RUNTIME_DIR", "/run/pxe"),
        # Not prefixed: this goes *inside* the Kea config, and Kea 3.x refuses
        # a memfile lease path outside its permitted lease directory.
        "lease_file": "/var/lib/kea/kea-leases4.csv",
        "kea_conf": OUTPUT_ROOT + "/etc/kea/kea-dhcp4.conf",
        "nginx_conf": OUTPUT_ROOT + "/etc/nginx/nginx.conf",
        "supervisord_conf": OUTPUT_ROOT + "/etc/supervisor/supervisord.conf",
        "tftpd_options": OUTPUT_ROOT + "/etc/pxe/tftpd-hpa.options",
    }

    cfg = parse_env()

    if _errors:
        print("", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        print("Refusing to start: invalid configuration", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        for e in _errors:
            print(f"  * {e}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    password_hash = crypt_sha512(cfg["_password"])
    info("generated SHA-512 crypt hash for USER_NAME "
         f"{cfg['user_name']} (plaintext is not written anywhere)")

    http_port = 8080

    # ---- kea ---------------------------------------------------------------
    write(paths["kea_conf"], build_kea(cfg, paths))

    # ---- nginx -------------------------------------------------------------
    write(
        paths["nginx_conf"],
        render(
            "nginx.conf.tmpl",
            {
                "SERVER_IP": cfg["server_ip"],
                "HTTP_ROOT": paths["http_root"],
                "ISO_NAME": cfg["iso_name"],
                "ISO_PATH": cfg["iso_path"],
                "APP_PORT": http_port,
            },
        ),
    )

    # ---- grub --------------------------------------------------------------
    write(os.path.join(paths["tftp_root"], "grub", "grub.cfg"), build_grub_cfg(cfg))

    # ---- tftpd -------------------------------------------------------------
    write(
        paths["tftpd_options"],
        render("tftpd-hpa.options.tmpl", {"TFTP_ROOT": paths["tftp_root"]}),
    )

    # ---- supervisord -------------------------------------------------------
    write(
        paths["supervisord_conf"],
        render(
            "supervisord.conf.tmpl",
            {
                "KEA_CONF": paths["kea_conf"],
                "TFTPD_OPTIONS": paths["tftpd_options"],
            },
        ),
    )

    # ---- runtime config for the autoinstall HTTP service -------------------
    runtime = {
        "server_ip": str(cfg["server_ip"]),
        "range_start": str(cfg["range_start"]),
        "range_end": str(cfg["range_end"]),
        "range_size": cfg["range_size"],
        "hostname_prefix": "node",
        "hostname_width": cfg["hostname_width"],
        "user_name": cfg["user_name"],
        "password_hash": password_hash,
        "ssh_pubkey": cfg["ssh_pubkey"],
        "sizing_policy": cfg["sizing_policy"],
        "iso_name": cfg["iso_name"],
        "tftp_root": paths["tftp_root"],
        "template_dir": TEMPLATE_DIR,
        "listen_port": http_port,
        "locale": env("INSTALL_LOCALE", "en_US.UTF-8"),
        "keyboard_layout": env("INSTALL_KEYBOARD_LAYOUT", "us"),
    }
    runtime_path = os.path.join(paths["runtime_dir"], "config.json")
    write(runtime_path, json.dumps(runtime, indent=2) + "\n", mode=0o600)

    info(
        "summary: subnet=%s pool=%s-%s (%d hosts, node%s-node%s) iface=%s "
        "server=%s transport=%s iso=%s"
        % (
            cfg["subnet"],
            cfg["range_start"],
            cfg["range_end"],
            cfg["range_size"],
            str(1).zfill(cfg["hostname_width"]),
            str(cfg["range_size"]).zfill(cfg["hostname_width"]),
            cfg["interface"],
            cfg["server_ip"],
            cfg["boot_transport"],
            cfg["iso_name"],
        )
    )
    if cfg["gateway"] is None:
        info("GATEWAY unset -> DHCP option 'routers' will not be offered")
    if not cfg["dns"]:
        warn(
            "DNS is unset, so no 'domain-name-servers' option is offered -- and "
            "that costs every installed machine ~2 minutes on EVERY boot.\n"
            "                   netplan generates "
            "`systemd-networkd-wait-online --any --dns -o routable -i <iface>` "
            "and --dns is never\n"
            "                   satisfied with zero resolvers, so the unit runs "
            "to its 2-minute timeout (measured: 2min 1.176s).\n"
            "                   The server does not have to be reachable, only "
            "configured -- setting DNS to this container's own IP is enough "
            "to fix the boot delay."
        )
    if not cfg["ssh_pubkey"]:
        info("SSH_PUBKEY unset -> password login only")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as exc:
        print(f"[config] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
