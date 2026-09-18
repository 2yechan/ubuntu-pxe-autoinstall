#!/bin/sh
# Start nginx under supervisord, surviving a hard kill of a previous master.
#
# nginx's workers are children of the master but do not exit when it dies: if
# the master is killed with SIGKILL (OOM, a stray kill -9), the workers are
# reparented to PID 1 and keep the listening socket open.  supervisord then
# respawns a master that can never bind, and nginx crash-loops for ever while
# the orphans quietly keep serving.  Clear them out before starting.
set -u

if pgrep -x nginx >/dev/null 2>&1; then
    echo "run_nginx: orphaned nginx processes from a previous master; terminating"
    pkill -x nginx 2>/dev/null || true
    i=0
    while pgrep -x nginx >/dev/null 2>&1 && [ $i -lt 20 ]; do
        i=$((i + 1))
        sleep 0.5
    done
    if pgrep -x nginx >/dev/null 2>&1; then
        echo "run_nginx: they did not go quietly; SIGKILL"
        pkill -9 -x nginx 2>/dev/null || true
        sleep 1
    fi
fi

rm -f /run/nginx.pid
exec /usr/sbin/nginx -g "daemon off;"
