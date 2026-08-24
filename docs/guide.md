# Beacon guide

Reference detail that doesn't belong on the front page. See the
[README](../README.md) for setup and [troubleshooting](troubleshooting.md) for
failure cases.

## Architecture

| Service   | Role | Network |
|-----------|------|---------|
| `web`     | Management UI + API (FastAPI); renders boot configs | port 8080 |
| `nginx`   | Serves boot files to iPXE (`/boot.ipxe`, kernels, initrds, ISOs) | port 80 |
| `dnsmasq` | proxyDHCP/DHCP + TFTP; BIOS/UEFI arch detection | **host** |
| `nfs`     | Exports each Linux image's live filesystem so clients mount it instead of copying the whole ISO to RAM | **host**, privileged |
| `smb`     | Exports each Windows image's unpacked install media so WinPE can run `setup.exe` | **host** |
| `reload`  | Restarts dnsmasq when its config is regenerated | host |

Images publish to `ghcr.io/acebmxer/beacon-<service>`. iPXE binaries, the nginx
and samba configs, and the reload script are baked into the images — nothing is
bind-mounted — which is why the stack runs from just `docker-compose.yml` +
`.env`. A consequence: **some fixes ship inside a service image, not `web`** (the
iPXE chain script in `dnsmasq`, the samba tuning in `smb`), so the supported
update is a whole-stack `docker compose pull && docker compose up -d`.

`dnsmasq` uses **host networking** because DHCP/PXE relies on layer-2 broadcasts
that don't cross Docker's bridge. `nfs` is **host + privileged** because it runs
the in-kernel NFS server (`nfsd` — `modprobe nfsd` if the host hasn't loaded it).
`smb` is **host** so WinPE reaches the share at the server's LAN address.

## DHCP modes

- **proxyDHCP** — runs alongside your existing DHCP server, answering only the
  PXE portion. Set **Server IP** to this box's LAN address. Best for consumer
  routers/mesh systems with no network-boot fields.
- **Full DHCP** — Beacon assigns addresses from the configured range. Only when
  there's no other DHCP server on the segment.
- **External DHCP** — your DHCP server drives boot; Beacon answers no DHCP and
  serves only TFTP. Point it at:

  | Setting | Value |
  |---------|-------|
  | Next server / TFTP server | Beacon's IP (the **Server IP** setting) |
  | Boot file, UEFI clients | `ipxe.efi` |
  | Boot file, legacy BIOS clients | `undionly.kpxe` |

  Most DHCP servers allow only one boot filename and Beacon can't detect the
  client arch in this mode, so a single-filename server means picking one (use
  `ipxe.efi` and boot clients UEFI, or use proxyDHCP).

## Update channels

`BEACON_TAG` in `.env` selects which published images the stack tracks — both
what `docker compose pull` installs and what the UI's update check watches:

| `BEACON_TAG` | Tracks | Updates when |
| --- | --- | --- |
| `stable` | tagged releases | a new version is released |
| `latest` | rolling `main` | any commit merged to `main` |

New installs default to `stable`. Switching channels takes effect on the next
update.

> **Always update the whole stack with `docker compose`, not a per-container UI.**
> `dnsmasq` and `nfs` run with host networking, which tools like Portainer
> mishandle — they pull the new image but never recreate those two, leaving the
> stack half-updated. `docker compose pull && docker compose up -d` recreates
> them correctly.

## How images boot

Upload format is `.iso` only. On upload the kernel + initrd are extracted (via
`bsdtar`; `7z` for the UDF ISOs modern Windows uses) and served over HTTP; the
raw ISO is kept at `/images/<file>.iso`. A best-effort kernel command line is
filled in per distro — edit **boot args** on the Images page if a live ISO needs
a tweak (a **Reprocess** re-derives them and overwrites your edit).

| Distro | Boot args |
|--------|-----------|
| Ubuntu live | `boot=casper netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp` |
| Debian live | `boot=live netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp` |
| Fedora/RHEL netinstall | `inst.repo=${boot-url}/images/<file>.iso ip=dhcp` |
| Fedora 42+ live | `root=live:${boot-url}/os/<id>/squashfs.img rd.live.image ip=dhcp` |
| Archiso (Arch/EndeavourOS/CachyOS) | `archiso_http_srv=${boot-url}/os/<id>/ archisobasedir=arch BOOTIF=01-${net0/mac:hexhyp} ip=dhcp` |

**Live filesystems** (Ubuntu casper, Debian live) are unpacked into the `nfsroot`
volume and mounted over NFS on demand, so client RAM use stays low regardless of
image size (the old `url=`/`fetch=` methods copied the whole ISO into a RAM disk
and failed on large images — hit **Reprocess** to switch an old image to NFS).
Fedora 42+ live and Archiso instead stream just their root filesystem over HTTP;
Archiso pulls the whole `airootfs.sfs` into RAM, so its live desktop needs
**~8 GB** of client RAM.

Each NFS-backed or Windows image costs roughly **2× its size on disk** (kept ISO
plus the unpacked tree).

## Windows images

Windows can't PXE-boot a raw kernel/initrd, so Beacon boots the WinPE files
(`bootmgr`, `BCD`, `boot.sdi`, `sources/boot.wim`) through
[`wimboot`](https://ipxe.org/wimboot), unpacks the whole ISO to the SMB share,
and patches `winpeshl.ini` in `boot.wim` so WinPE mounts the share and runs
`setup.exe`. The install runs interactively.

### Drivers

Two kinds, staged on the **Windows Drivers** page (or by dropping folders into
the host paths directly):

- **Storage** (`./data/drivers`) — served over SMB and handed to Setup via an
  answer file (`setup.exe /unattend`, PnP-matched `DriverPaths`), so the disk
  appears in Setup's list *and* the driver installs into the finished OS
  (without it, VMD/RAID machines bugcheck `INACCESSIBLE_BOOT_DEVICE` / 0x7B on
  first boot). Shared by every Windows image; applies on the next boot, no
  reprocessing. Beacon launches the classic Setup (`sources\setup.exe`), so
  installs show the pre-24H2 UI: the new flow on 24H2-and-later media loads a
  staged storage driver for its own use but never installs it into the
  finished OS, which is precisely the 0x7B above. Mixing vendors/versions is
  safe — Setup installs only the driver the hardware needs. The alternative is
  per-machine firmware: switch `VMD Controller` / `Intel RST` / `SATA
  Operation` to **AHCI** (don't do this to a disk that already has Windows on
  it).
- **Network** (`./data/nicdrivers`) — a NIC driver can't come over the network
  it's needed to reach, so these are baked into every image's `boot.wim` and
  loaded before networking starts (symptom of a missing one: WinPE mounts fail
  with "System error 1231"). Adding/removing one re-bakes all ready Windows
  images automatically.

The catalog fetches known-good WHQL packs (Intel RST VMD, AMD RAID, Intel
I225/I226 + X-series NIC) straight from Microsoft's Windows Update CDN,
sha256-verified. Nothing is bundled or downloaded until you click Fetch, and the
fetch needs internet from the Beacon host — offline networks upload instead.

## XCP-NG

Use the **netinstall** ISO (`xcp-ng-<ver>-netinstall.iso`); the full ISO stops at
*"base installation repository was not found"*. XCP-NG is Xen-based and boots via
multiboot, which iPXE can't do under UEFI, so Beacon builds a self-contained GRUB
EFI binary per image (`grub-mkstandalone`) and chainloads it; GRUB fetches
`xen.gz`, `vmlinuz`, and `install.img` over HTTP. The server IP is baked into that
binary, so changing **Server IP** rebuilds it. Pass an answerfile via boot args
for unattended installs (`answerfile=http://.../answerfile.xml install`).

## Image storage on NFS/SMB

`web`/`nginx` read images from `IMAGE_PATH` in `.env` (default `./data/images`).
To use a NAS, mount the share on the **host** and point `IMAGE_PATH` at it:

```bash
sudo mount -t nfs nas.local:/exports/pxe /mnt/pxe-images
# in .env:  IMAGE_PATH=/mnt/pxe-images
```

Add it to `/etc/fstab` so it survives reboots. Database and settings live in
`./data/pxe.db`; uploaded ISOs live under `IMAGE_PATH`.

## Building from source (development)

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

> The in-app update button pulls the **published** images, so clicking Apply on a
> locally built deployment reverts it to the published build. The Updates panel
> reports `dev build` when the running image was built locally; re-run the
> command above to get your build back.

## Testing without hardware (QEMU)

On the same L2 network as the server, boot a VM with a PXE-capable NIC (use a
bridge — `-netdev user` won't see proxyDHCP):

```bash
# UEFI (needs OVMF)
qemu-system-x86_64 -m 2048 -boot n -netdev bridge,id=net0,br=br0 \
  -device virtio-net,netdev=net0 -bios /usr/share/OVMF/OVMF_CODE.fd

# Legacy BIOS
qemu-system-x86_64 -m 2048 -boot n -netdev bridge,id=net0,br=br0 \
  -device e1000,netdev=net0
```
