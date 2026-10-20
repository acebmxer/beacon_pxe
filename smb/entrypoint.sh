#!/bin/sh
# Run Samba (smbd) in the foreground, serving /srv/install read-only to WinPE.
# nmbd (NetBIOS name service) isn't needed: WinPE connects by IP, not name.
set -eu

# smbd needs these runtime dirs; the alpine package doesn't always create them.
mkdir -p /var/lib/samba/private /var/run/samba /var/log/samba

# Optional diagnostics capture share. Off unless ENABLE_DIAG_CAPTURE=true, since
# it is guest-writable. smb.conf `include`s capture.conf; write the [capture]
# stanza into it only when enabled, otherwise leave it empty so nothing is shared.
if [ "${ENABLE_DIAG_CAPTURE:-false}" = "true" ]; then
    mkdir -p /srv/capture
    chmod 0777 /srv/capture 2>/dev/null || true
    cat > /etc/samba/capture.conf <<'EOF'
[capture]
   comment = Beacon diagnostics (Windows Setup logs)
   path = /srv/capture
   browseable = yes
   read only = no
   guest ok = yes
   guest only = yes
   # Write as the unprivileged 'nobody', never root: this is a guest-writable
   # share reachable by any host on the boot LAN, so files a client drops here
   # must not land root-owned. The dir stays world-writable (below) so the forced
   # user can create files in it regardless of the bind mount's owner.
   force user = nobody
   create mask = 0664
   directory mask = 0775
EOF
    echo "[smb] diagnostics capture share ENABLED (guest-writable /srv/capture)"
else
    : > /etc/samba/capture.conf
    echo "[smb] diagnostics capture share disabled (set ENABLE_DIAG_CAPTURE=true to enable)"
fi

echo "[smb] starting smbd; 'install' -> /srv/install (read-only, guest)"

# Listen only on the boot interface set in Server Settings. The web app writes
# it into the generated dnsmasq.conf (interface=...), read here from the shared
# dnsmasqconf volume. smbd binds its addresses at start, so it is restarted when
# the interface or its addresses change (checked every few seconds).
CONF_DIR=/dnsmasq
CONF="$CONF_DIR/dnsmasq.conf"
PID=""

boot_iface() {
  sed -n 's/^interface=//p' "$CONF" 2>/dev/null | head -n 1
}

# What smbd's binding depends on: the interface name and its IPv4 addresses.
binding_state() {
  if [ ! -d "$CONF_DIR" ]; then
    echo "unmounted"
    return 0
  fi
  iface=$(boot_iface)
  echo "$iface $(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"
}

write_interfaces() {
  iface=""
  if [ -d "$CONF_DIR" ]; then
    iface=$(boot_iface)
  fi
  if [ -n "$iface" ]; then
    printf '   interfaces = lo %s\n   bind interfaces only = yes\n' "$iface" \
      > /etc/samba/interfaces.conf
    echo "[smb] listening on $iface only"
  elif [ ! -d "$CONF_DIR" ]; then
    : > /etc/samba/interfaces.conf
    echo "[smb] listening on every interface (this docker-compose.yml doesn't share the dnsmasq config with smb; use the current one to limit SMB to the boot interface)"
  else
    : > /etc/samba/interfaces.conf
    echo "[smb] listening on every interface (no boot interface set)"
  fi
}

start() {
  # Without --no-process-group: smbd gets its own process group, so when it is
  # stopped for a restart its shutdown signal doesn't also reach this script.
  smbd --foreground --debug-stdout &
  PID=$!
}

stop() {
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
}

trap 'stop; exit 0' TERM INT

# A fresh install starts before the web app has written its first config; wait
# for it rather than listening everywhere in the meantime.
if [ -d "$CONF_DIR" ]; then
  n=0
  while [ ! -f "$CONF" ]; do
    if [ $((n % 20)) -eq 0 ]; then
      echo "[smb] waiting for $CONF from the web app"
    fi
    n=$((n + 1))
    sleep 3 & wait $! || true
  done
fi

STATE=$(binding_state)
write_interfaces
start
while true; do
  sleep 5 & wait $! || true
  if ! kill -0 "$PID" 2>/dev/null; then
    # smbd exited on its own: exit with its status so Docker restarts us.
    rc=0
    wait "$PID" || rc=$?
    echo "[smb] smbd exited with status $rc"
    exit "$rc"
  fi
  NOW=$(binding_state)
  if [ "$NOW" != "$STATE" ]; then
    STATE="$NOW"
    echo "[smb] boot interface changed, restarting smbd"
    stop
    write_interfaces
    start
  fi
done
