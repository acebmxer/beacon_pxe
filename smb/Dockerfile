# Samba server exporting each Windows image's unpacked ISO tree to WinPE.
#
# WinPE (booted via wimboot) can't read the install media over iPXE sanhook under
# UEFI, so it mounts this SMB share instead and runs setup.exe from it. The share
# is read-only with guest access (no credentials) — it serves only public install
# media on a trusted boot network.
FROM alpine:3.20
RUN apk add --no-cache samba-server
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
