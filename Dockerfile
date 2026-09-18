# syntax=docker/dockerfile:1
#
# Ubuntu 26.04 LTS (resolute) network unattended-install appliance.
#
# The base image is pinned to the *same* release as the OS being installed so
# that the signed boot chain we hand out over TFTP (shim -> grubnetx64) is
# exactly the one Canonical ships for 26.04.  Mixing releases here is how you
# end up with a shim that refuses to load GRUB under Secure Boot.
FROM ubuntu:26.04

# Which shim binary to hand out.  Verified contents of shim-signed
# 1.59+15.8-0ubuntu2 (see README "Verified against 26.04"):
#   dualsigned     - 2 PE signatures: Microsoft UEFI CA + Canonical.  Superset
#                    of signed.latest, so it also boots on machines that only
#                    have Canonical's CA enrolled.  Default.
#   signed.latest  - 1 PE signature: Microsoft UEFI CA.
#   signed.previous- the *previous* shim release, Microsoft-signed.  Only of
#                    interest if the current shim is revoked by your firmware.
ARG SHIM_VARIANT=dualsigned

ENV DEBIAN_FRONTEND=noninteractive

# supervisor lives in universe; the ubuntu:26.04 base image normally enables it
# already, but do not take that on faith.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i -E '/^Components:/{ /universe/! s/$/ universe/ }' /etc/apt/sources.list.d/ubuntu.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i -E 's/^(deb .*[[:space:]]main)([[:space:]]|$)/\1 universe\2/' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        kea-dhcp4-server \
        tftpd-hpa \
        nginx \
        supervisor \
        xorriso \
        openssl \
        iproute2 \
        procps \
        python3 \
        ; \
    rm -rf /var/lib/apt/lists/*

# Signed EFI boot chain.
#
# We deliberately `apt-get download` + `dpkg-deb -x` instead of `apt-get
# install`:  shim-signed/grub-efi-amd64-signed drag in grub-efi-amd64,
# grub2-common, mokutil, sbsigntool, os-prober ... whose maintainer scripts
# want a real ESP and a real firmware environment.  We only need three files,
# so unpack the archives and throw the rest away.
#
# Note on naming: shim hard-codes the name of its second stage -- it loads
# "grubx64.efi" from the directory it was itself loaded from.  grubnetx64 is
# therefore published under that name, not its own.
RUN set -eux; \
    apt-get update; \
    mkdir -p /tmp/efi/debs /tmp/efi/x /usr/share/pxe-boot; \
    cd /tmp/efi/debs; \
    apt-get download shim-signed grub-efi-amd64-signed; \
    for d in *.deb; do dpkg-deb -x "$d" /tmp/efi/x; done; \
    \
    shim=""; \
    for c in "/tmp/efi/x/usr/lib/shim/shimx64.efi.${SHIM_VARIANT}" \
             /tmp/efi/x/usr/lib/shim/shimx64.efi.dualsigned \
             /tmp/efi/x/usr/lib/shim/shimx64.efi.signed.latest \
             /tmp/efi/x/usr/lib/shim/shimx64.efi.signed; do \
        if [ -f "$c" ]; then shim="$c"; break; fi; \
    done; \
    [ -n "$shim" ] || { echo "FATAL: no signed shim found in shim-signed" >&2; ls -la /tmp/efi/x/usr/lib/shim/ >&2; exit 1; }; \
    echo "using shim: $shim"; \
    cp "$shim" /usr/share/pxe-boot/shimx64.efi; \
    \
    grubnet=/tmp/efi/x/usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed; \
    [ -f "$grubnet" ] || { echo "FATAL: grubnetx64.efi.signed not found" >&2; find /tmp/efi/x/usr/lib/grub -type f >&2; exit 1; }; \
    cp "$grubnet" /usr/share/pxe-boot/grubx64.efi; \
    \
    cp /tmp/efi/x/usr/lib/grub/x86_64-efi-signed/version /usr/share/pxe-boot/grub-version 2>/dev/null || true; \
    rm -rf /tmp/efi /var/lib/apt/lists/*; \
    ls -la /usr/share/pxe-boot/

COPY bin/       /opt/pxe/bin/
COPY templates/ /opt/pxe/templates/
COPY entrypoint.sh /opt/pxe/entrypoint.sh
RUN chmod +x /opt/pxe/entrypoint.sh /opt/pxe/bin/*.py /opt/pxe/bin/*.sh

# TFTP root, HTTP root, generated configs, kea leases.
RUN mkdir -p /srv/tftp/grub /srv/http/boot /etc/kea /etc/pxe \
             /var/lib/kea /run/kea /run/pxe /var/log/supervisor

# ISO mount point (bind-mounted read-only at run time).
VOLUME /iso

ENV ISO_PATH=/iso/ubuntu-26.04-live-server-amd64.iso \
    TFTP_ROOT=/srv/tftp \
    HTTP_ROOT=/srv/http \
    RUNTIME_DIR=/run/pxe \
    PYTHONUNBUFFERED=1

# host networking is required (DHCP needs L2 broadcast); EXPOSE is documentation
# only in that mode.
EXPOSE 67/udp 69/udp 80/tcp

ENTRYPOINT ["/opt/pxe/entrypoint.sh"]
