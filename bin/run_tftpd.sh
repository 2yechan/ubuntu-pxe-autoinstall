#!/bin/sh
# Thin wrapper so the tftpd knobs live in a config file (rendered from
# templates/tftpd-hpa.options.tmpl) instead of being buried in supervisord's
# command line.
set -eu

OPTIONS_FILE="${1:-/etc/pxe/tftpd-hpa.options}"

if [ ! -r "$OPTIONS_FILE" ]; then
    echo "run_tftpd: cannot read $OPTIONS_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
. "$OPTIONS_FILE"

echo "run_tftpd: serving $TFTP_DIRECTORY on $TFTP_ADDRESS as $TFTP_USERNAME ($TFTP_OPTIONS)"

# $TFTP_OPTIONS is deliberately unquoted: it is a list of flags.
# shellcheck disable=SC2086
exec /usr/sbin/in.tftpd \
    --user "$TFTP_USERNAME" \
    --address "$TFTP_ADDRESS" \
    $TFTP_OPTIONS \
    "$TFTP_DIRECTORY"
