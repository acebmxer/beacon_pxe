#!/bin/sh
# Run Samba (smbd) in the foreground, serving /srv/install read-only to WinPE.
# nmbd (NetBIOS name service) isn't needed: WinPE connects by IP, not name.
set -eu

# smbd needs these runtime dirs; the alpine package doesn't always create them.
mkdir -p /var/lib/samba/private /var/run/samba /var/log/samba

echo "[smb] starting smbd; share 'install' -> /srv/install (read-only, guest)"
exec smbd --foreground --no-process-group --debug-stdout
