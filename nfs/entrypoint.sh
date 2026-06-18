#!/bin/sh
# Start a kernel NFS server that exports /nfs (read-only) to PXE clients.
# NFSv3 is used because that is what the distro initramfs network mounter
# (klibc nfsmount) speaks, so rpcbind + rpc.mountd must be running too.
set -eu

# The nfsd/nfs modules live on the host; load them if not already present
# (harmless if they are built-in or already loaded).
modprobe nfs 2>/dev/null || true
modprobe nfsd 2>/dev/null || true

# nfsd's control filesystem.
mount -t nfsd nfsd /proc/fs/nfsd 2>/dev/null || true

# rpcbind (portmapper) is required for NFSv3.
rpcbind || true

# Publish the exports and start the daemons.
exportfs -rv
rpc.mountd --no-udp
rpc.nfsd --no-udp 8

echo "[nfs] server up; current exports:"
exportfs -v

shutdown() {
  echo "[nfs] shutting down"
  rpc.nfsd 0 || true
  exportfs -ua || true
  exit 0
}
trap shutdown TERM INT

# rpc.nfsd backgrounds its kernel threads, so hold the container open.
while true; do sleep 3600 & wait $!; done
