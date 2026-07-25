#!/bin/sh
# Run Samba (smbd) in the foreground, serving /srv/install read-only to WinPE.
# nmbd (NetBIOS name service) isn't needed: WinPE connects by IP, not name.
set -eu

# smbd needs these runtime dirs; the alpine package doesn't always create them.
mkdir -p /var/lib/samba/private /var/run/samba /var/log/samba

# Writable capture share (Setup logs). Ensure it exists and is writable even if
# the host bind-mount came in root-owned and empty.
mkdir -p /srv/capture
chmod 0777 /srv/capture 2>/dev/null || true

echo "[smb] starting smbd; 'install' -> /srv/install (ro), 'capture' -> /srv/capture (rw), guest"
exec smbd --foreground --no-process-group --debug-stdout
