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
   force user = root
   create mask = 0664
   directory mask = 0775
EOF
    echo "[smb] diagnostics capture share ENABLED (guest-writable /srv/capture)"
else
    : > /etc/samba/capture.conf
    echo "[smb] diagnostics capture share disabled (set ENABLE_DIAG_CAPTURE=true to enable)"
fi

echo "[smb] starting smbd; 'install' -> /srv/install (read-only, guest)"
exec smbd --foreground --no-process-group --debug-stdout
