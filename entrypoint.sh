#!/bin/bash
#
# Bring up the Ubuntu 26.04 network-install appliance:
#   1. validate the environment and render every config file
#   2. publish the signed EFI boot chain into the TFTP root
#   3. pull vmlinuz/initrd out of the ISO
#   4. prove the generated Kea config parses
#   5. hand over to supervisord
#
# Any failure before step 5 must stop the container.  A half-configured DHCP
# server on somebody's L2 segment is worse than no container at all.
set -euo pipefail

TFTP_ROOT="${TFTP_ROOT:-/srv/tftp}"
HTTP_ROOT="${HTTP_ROOT:-/srv/http}"
RUNTIME_DIR="${RUNTIME_DIR:-/run/pxe}"
ISO_PATH="${ISO_PATH:-/iso/ubuntu-26.04-live-server-amd64.iso}"
BOOT_TRANSPORT="${BOOT_TRANSPORT:-tftp}"
PXE_BOOT_SRC=/usr/share/pxe-boot

say()  { echo "[entrypoint] $*"; }
die()  { echo "[entrypoint] FATAL: $*" >&2; exit 1; }

say "Ubuntu 26.04 LTS PXE autoinstall appliance starting"

# /run/kea holds Kea's lock and PID files; /run is image content in Docker,
# but recreate it anyway so a tmpfs-mounted /run does not break start-up.
mkdir -p "$TFTP_ROOT/grub" "$HTTP_ROOT/boot" "$RUNTIME_DIR" \
         /var/lib/kea /run/kea /etc/pxe /var/log/supervisor

# ---------------------------------------------------------------------------
# 1. environment validation + config rendering
# ---------------------------------------------------------------------------
say "validating environment and rendering configuration ..."
/opt/pxe/bin/render_config.py || die "configuration is invalid (see the errors above)"

# ---------------------------------------------------------------------------
# 2. signed boot chain
#
# Baked into the image at build time from shim-signed / grub-efi-amd64-signed
# for this very release.  shim looks for its second stage as "grubx64.efi"
# next to itself, which is why grubnetx64 is published under that name.
# ---------------------------------------------------------------------------
for f in shimx64.efi grubx64.efi; do
    [ -f "$PXE_BOOT_SRC/$f" ] || die "$PXE_BOOT_SRC/$f is missing -- rebuild the image"
    install -m 0644 "$PXE_BOOT_SRC/$f" "$TFTP_ROOT/$f"
done
say "published signed boot chain: shimx64.efi -> grubx64.efi (grubnetx64$( [ -f "$PXE_BOOT_SRC/grub-version" ] && echo " $(cat "$PXE_BOOT_SRC/grub-version")" ))"

# ---------------------------------------------------------------------------
# 3. kernel + initrd out of the ISO
#
# xorriso reads the ISO as a plain file: no loop device, no mount, so the
# container needs no extra privileges for this.
# ---------------------------------------------------------------------------
[ -f "$ISO_PATH" ] || die "ISO not found at $ISO_PATH"

say "inspecting $ISO_PATH ..."
casper_files="$(xorriso -indev "$ISO_PATH" -lsl /casper/ 2>/dev/null \
                 | sed -n "s/.*'\(.*\)'\$/\1/p" || true)"
[ -n "$casper_files" ] || die "could not list /casper inside $ISO_PATH -- is it an Ubuntu live-server ISO?"

pick() {
    # $1.. = candidate names, in order of preference
    for want in "$@"; do
        while IFS= read -r have; do
            [ "$have" = "$want" ] && { echo "$want"; return 0; }
        done <<< "$casper_files"
    done
    return 1
}

kernel_name="$(pick vmlinuz vmlinuz.efi vmlinuz-generic linux)" \
    || die "no kernel found in /casper (saw: $(echo "$casper_files" | tr '\n' ' '))"
initrd_name="$(pick initrd initrd.img initrd.lz initrd.gz)" \
    || die "no initrd found in /casper (saw: $(echo "$casper_files" | tr '\n' ' '))"

say "extracting /casper/$kernel_name and /casper/$initrd_name ..."
rm -f "$HTTP_ROOT/boot/vmlinuz" "$HTTP_ROOT/boot/initrd"
xorriso -osirrox on -indev "$ISO_PATH" \
        -extract "/casper/$kernel_name" "$HTTP_ROOT/boot/vmlinuz" \
        -extract "/casper/$initrd_name" "$HTTP_ROOT/boot/initrd" \
    || die "xorriso failed to extract the boot files"

for f in vmlinuz initrd; do
    [ -s "$HTTP_ROOT/boot/$f" ] || die "extracted $HTTP_ROOT/boot/$f is empty"
    chmod 0644 "$HTTP_ROOT/boot/$f"
done
say "extracted vmlinuz ($(du -h "$HTTP_ROOT/boot/vmlinuz" | cut -f1)) and initrd ($(du -h "$HTTP_ROOT/boot/initrd" | cut -f1))"

if [ "$BOOT_TRANSPORT" = "tftp" ]; then
    # GRUB will fetch them from the TFTP root instead of over HTTP.
    mkdir -p "$TFTP_ROOT/boot"
    install -m 0644 "$HTTP_ROOT/boot/vmlinuz" "$TFTP_ROOT/boot/vmlinuz"
    install -m 0644 "$HTTP_ROOT/boot/initrd"  "$TFTP_ROOT/boot/initrd"
    say "BOOT_TRANSPORT=tftp: also copied kernel/initrd into the TFTP root"
fi

# nginx workers run as www-data and have to be able to read the ISO mount.
if ! su -s /bin/sh -c "test -r '$ISO_PATH'" www-data 2>/dev/null; then
    echo "[entrypoint] WARNING: $ISO_PATH is not readable by www-data; nginx will" >&2
    echo "[entrypoint]          return 403 for /iso/. Fix the permissions on the" >&2
    echo "[entrypoint]          host file (chmod a+r) and restart." >&2
fi
chmod 0755 "$HTTP_ROOT" "$HTTP_ROOT/boot" "$TFTP_ROOT" "$TFTP_ROOT/grub"

# ---------------------------------------------------------------------------
# 4. prove the Kea config is valid before letting the daemon near the network
# ---------------------------------------------------------------------------
say "checking the generated Kea configuration ..."
if ! kea-dhcp4 -t /etc/kea/kea-dhcp4.conf; then
    echo "--- /etc/kea/kea-dhcp4.conf ---" >&2
    cat -n /etc/kea/kea-dhcp4.conf >&2
    die "generated Kea configuration failed 'kea-dhcp4 -t'"
fi
say "Kea configuration OK"

# ---------------------------------------------------------------------------
# 5. run
#
# The plaintext password has already been turned into a SHA-512 crypt hash by
# render_config.py; drop it so no supervised process inherits it.
# ---------------------------------------------------------------------------
unset USER_PASSWORD

say "starting supervisord (kea-dhcp4, tftpd-hpa, nginx, autoinstall-http)"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
