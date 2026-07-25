# Troubleshooting

Common cases live in the [README](../README.md#troubleshooting); this is the full
list. The CHANGELOG has the deep "why" behind most of these.

## Client times out fetching the boot file (`PXE-E18`, `NBP filesize is 0 Bytes`)

The host firewall is dropping traffic — every Beacon service except the web UI
and NFS listens **below port 1025**, which firewalld (Fedora/RHEL) and ufw
(Ubuntu) block by default. Host-networked containers are *not* exempt; Docker
only opens published ports.

Ports: **69/udp** (TFTP), **67 + 4011/udp** (DHCP/proxyDHCP), **80/tcp** (boot
menu, kernels, `boot.wim`), **139 + 445/tcp** (SMB, Windows only), and
**111 tcp+udp / 2049 tcp / 20048 tcp+udp** (NFS + rpcbind + mountd, live Linux
only).

```bash
# firewalld — use the zone your boot interface is in
sudo firewall-cmd --permanent --zone=FedoraWorkstation \
  --add-service=tftp --add-service=dhcp --add-service=http \
  --add-service=samba --add-service=nfs --add-service=rpc-bind --add-service=mountd
sudo firewall-cmd --reload
```

`samba-client` is the outbound service and does *not* open 445 — the server
service is `samba`. A successful boot logs `sent /tftp/ipxe.efi to <client>` in
`docker compose exec web cat /dnsmasq/dnsmasq.log`; no client transactions there
means packets are still being dropped upstream.

## Client boots iPXE but fetches `boot.ipxe` from the wrong IP

Another DHCP server on the LAN is also answering with PXE options and iPXE
preferred it. Beacon's log shows it served the correct URL while the client's
screen shows a different address. Clear the `next-server` / option 66/67 settings
on whatever else hands out leases, or move Beacon to an isolated boot VLAN in full
DHCP mode.

## Windows Setup fails partway (`0x80070035`) on a disk with old partitions

Delete the existing partitions at Setup's "Where do you want to install Windows?"
screen (down to unallocated space) and continue. A cluttered disk can knock loose
the drive letter Setup uses for the SMB install media mid-apply. Standard
dirty-disk behaviour — the same ISO on a USB stick behaves identically; a fresh
disk installs without the step. To see exactly where Setup stopped, set
`ENABLE_DIAG_CAPTURE=true` in `.env` and re-run the install — WinPE drops Setup's
logs into `./data/capture` on the host.

## Windows "System error 1231" / network timeout in WinPE

The client's NIC isn't in stock WinPE (Intel I225/I226 2.5GbE is the usual
culprit). Fetch the Intel NIC pack on the Windows Drivers page, or upload a pack
with kind **Network** — it bakes into `boot.wim` and loads before networking.

## Windows "System error 53" on the boot after a successful install

A stale SMB socket from the previous session. Fixed in 0.2.2 (per-socket
keepalive reaps it in ~25s and WinPE retries for ~2 minutes); if you're on an
older build, update the whole stack. As a one-off, bounce the `smb` container
between attempts.

## `No space left on device` / "Unable to find a live file system", dropping to an `(initramfs)` shell

The image is still using the old copy-the-whole-ISO-to-RAM boot method. Hit
**Reprocess** on the Images page to re-derive its args and switch it to NFS.

## NFS mount fails / live filesystem not found

`docker compose logs nfs` should show `current exports: /nfs`. The host needs the
NFS kernel module (`sudo modprobe nfsd`), the `nfs` container runs privileged with
host networking, and the client uses NFSv3 (ports 111 + 2049 must be open on the
LAN).

## Xen VMs: iPXE shows `Link status: Down` and never reaches the menu

Not an iPXE bug — a regression in the Xen **host (dom0) kernel's** `xen-netback`
driver (introduced by commit `1f256578`, fixed by `2afeec08`, in stable kernels
from ~April 2021). When the guest's netfront link connects a second time (firmware
connects once for iPXE, iPXE reconnects) the backend stays stuck in `InitWait`.
Fixes, most reliable first:

1. **Update the Xen host kernel** to one with the fix (covers all guests).
2. **Give the VM an emulated NIC** (e1000/rtl8139) instead of PV netfront.
3. **Try an older iPXE**: rebuild `dnsmasq` pinned to an older ref —
   `docker compose -f docker-compose.yml -f docker-compose.dev.yml build --build-arg IPXE_REF=v1.21.1 dnsmasq` then `docker compose up -d dnsmasq`. Not guaranteed; the bug is host-side.

## Portainer/Dockhand reports "Container recreation failed" for dnsmasq/nfs

Those tools mishandle host-networked containers — they remove the old container
but never recreate it. The other three services update fine, leaving the stack
half-updated. Run `docker compose pull && docker compose up -d` to finish.

## `docker compose down -v` left images marked "needs reprocess"

`-v` drops the `bootroot`/`nfsroot`/`smbroot` volumes (every unpacked image) while
the database (a bind mount) survives. Beacon detects the missing files at startup,
holds those images out of the menu, and restores them when you hit **Reprocess**
(uploaded ISOs under `IMAGE_PATH` are untouched by `-v`). Ordinary `pull && up -d`
never does this.
