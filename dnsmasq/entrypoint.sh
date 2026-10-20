#!/bin/sh
# Run dnsmasq and restart it whenever the web app rewrites its config.
#
# The web app regenerates /dnsmasq/dnsmasq.conf (a shared volume) when Server
# Settings are saved. dnsmasq reads its main config only at start (SIGHUP
# re-reads host files, not interface= or dhcp-range=), so the process is
# restarted here instead. This used to be the job of a separate `reload`
# container with the Docker socket mounted; doing it in-container needs no
# Docker access at all. Polls mtime (busybox has no inotify tools).
set -eu

CONF=/dnsmasq/dnsmasq.conf
PID=""

mtime() { stat -c %Y "$CONF" 2>/dev/null || echo ""; }

start() {
  dnsmasq --keep-in-foreground --conf-file="$CONF" &
  PID=$!
}

stop() {
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
}

trap 'stop; exit 0' TERM INT

LAST=$(mtime)
start
while true; do
  # Backgrounded sleep + wait so a docker stop is handled straight away.
  sleep 3 & wait $! || true
  if ! kill -0 "$PID" 2>/dev/null; then
    # dnsmasq exited on its own (bad config, interface gone): exit with its
    # status so Docker's restart policy brings the container back, as before.
    rc=0
    wait "$PID" || rc=$?
    echo "[dnsmasq] exited with status $rc"
    exit "$rc"
  fi
  NOW=$(mtime)
  if [ "$NOW" != "$LAST" ]; then
    echo "[dnsmasq] config changed, restarting dnsmasq"
    stop
    LAST="$NOW"
    start
  fi
done
