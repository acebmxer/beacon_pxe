#!/bin/sh
# Watches the generated dnsmasq config and restarts the dnsmasq container when
# it changes. Runs in the lightweight docker:cli image (has `docker` + `stat`),
# so we poll mtime rather than depend on inotify-tools.
set -eu

CONF=/dnsmasq/dnsmasq.conf
TARGET=beacon_dnsmasq
LAST=""

echo "[reload] watching $CONF -> restarts $TARGET on change"
while true; do
  if [ -f "$CONF" ]; then
    NOW=$(stat -c %Y "$CONF" 2>/dev/null || echo "")
    if [ -n "$NOW" ] && [ "$NOW" != "$LAST" ]; then
      if [ -n "$LAST" ]; then
        echo "[reload] config changed, restarting $TARGET"
        docker restart "$TARGET" >/dev/null 2>&1 || echo "[reload] restart failed"
      fi
      LAST="$NOW"
    fi
  fi
  sleep 3
done
