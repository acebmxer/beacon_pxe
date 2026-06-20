# Beacon

A self-hosted PXE/iPXE boot server with a modern, colourful boot menu and a
login-protected web management console. It netboots **both BIOS and UEFI**
clients from a **single boot menu**, manages OS images uploaded as **ISO**, and
supports **admin / user** accounts.

Runs as a small Docker Compose stack.

---

## Features

- **One menu for BIOS *and* UEFI.** Firmware requires two different initial
  binaries (`undionly.kpxe` for BIOS, `ipxe.efi` for UEFI) — that part is
  unavoidable — but after that handoff every client loads the **same**
  `http://<server>/boot.ipxe` menu.
- **Modern boot menu** — colour scheme + a generated gradient background image
  (not white-on-black).
- **Web UI** (FastAPI) gated by a login screen, with light/dark themes.
- **Default admin** created on first run; password supplied via `.env` or
  **auto-generated** and printed to the logs.
- **Server settings:** enable/disable services, switch **proxyDHCP ↔ full DHCP**,
  set the DHCP server/range, edit the menu, toggle theme.
- **Images:** upload ISO → kernel/initrd auto-extracted → menu entry created;
  enable/disable, edit boot args, delete.
- **Users:** admins create/reset/delete users and set account type
  (admin/user); regular users can only change their own password.

---

## Quick start

Prebuilt images are published to GitHub Container Registry, so you don't need to
clone the repo or build anything — just grab the compose file and an `.env`:

```bash
mkdir beacon && cd beacon
curl -O https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/.env.example
# edit .env: set SERVER_IP, BOOT_INTERFACE, ADMIN_PASSWORD (or leave blank), etc.
docker compose up -d
```

The iPXE binaries, nginx config, and reload script are all baked into the
images, so the two files above are everything you need.

If you left `ADMIN_PASSWORD` blank, a strong password is generated and printed to
the logs on first start:

```bash
docker compose logs web | grep -i "admin password"
```

Then open **http://&lt;server-ip&gt;:8080**, log in, and complete the first-run
wizard (confirms server IP, interface, DHCP mode).

### Cloning the repo instead

If you'd rather clone, `setup.sh` generates `.env` for you (auto-generating the
admin password + session secret and detecting your server IP), then pulls the
images and starts the stack:

```bash
git clone https://github.com/acebmxer/beacon_pxe beacon && cd beacon
./setup.sh
```

### Building from source (development)

To build the images locally instead of pulling them, use the dev override:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

---

## Architecture

| Service   | Image | Role | Network |
|-----------|-------|------|---------|
| `web`     | `beacon-web` (FastAPI) | Management UI + API; renders boot configs | port 8080 |
| `nginx`   | `beacon-nginx` (nginx:alpine + config) | Serves boot files to iPXE (`/boot.ipxe`, kernels, initrds, ISOs) | port 80 |
| `dnsmasq` | `beacon-dnsmasq` (alpine + iPXE binaries) | proxyDHCP/DHCP + TFTP; BIOS/UEFI arch detection | **host** |
| `nfs`     | `beacon-nfs` (alpine) | Exports each image's live filesystem (read-only) so clients mount it instead of downloading the whole ISO into RAM | **host**, privileged |
| `reload`  | `beacon-reload` (docker:cli + watch script) | Restarts dnsmasq when its config is regenerated | host |

Images are published to `ghcr.io/acebmxer/beacon-<service>`. The iPXE boot
binaries (built from source with an embedded chain script), the nginx config,
and the reload script are baked into their images — nothing is bind-mounted from
the repo, which is why the stack runs from just `docker-compose.yml` + `.env`.

`dnsmasq` uses **host networking** because DHCP/PXE relies on layer-2 broadcasts
that don't traverse Docker's bridge network. This is the standard requirement for
any PXE server in Docker.

`nfs` uses **host networking** and runs **privileged** because it runs the
in-kernel NFS server (`nfsd`). The host must provide the `nfsd` kernel module
(standard on Linux — `modprobe nfsd` if it isn't already loaded). See "How live
images boot" below for why NFS is used.

---

## DHCP modes

Set during the first-run wizard and changeable any time under **Server Settings**.

- **proxyDHCP (recommended).** Runs *alongside* your existing DHCP server (e.g.
  your router). It answers only the PXE/boot portion and never assigns IP
  addresses, so it won't conflict with your network. Set **Server IP** to this
  box's LAN address.
- **Full DHCP.** This box becomes the DHCP server and assigns addresses from the
  configured range. Use only if you have **no other DHCP server** on the
  segment — two full DHCP servers will conflict.

---

## Images: format & what actually boots

- **Upload format: `.iso` only.** On upload, the server reads the ISO with
  `bsdtar` (no privileged mount) and extracts the Linux **kernel** + **initrd**,
  which are served over HTTP for reliable netboot. The raw ISO is also kept and
  served at `/images/<file>.iso`.
- **Boot arguments.** A best-effort kernel command line is filled in per distro
  family (casper/live/Fedora). Some live ISOs need a tweak — edit the **boot
  args** field on the Images page. Examples:
  - Ubuntu live: `boot=casper netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp`
  - Debian live: `boot=live netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp`
- **XCP-NG (netinstall ISO).** XCP-NG is Xen-based, so it boots via **multiboot**
  rather than a plain kernel+initrd: the server extracts `xen.gz` (hypervisor),
  `vmlinuz` (dom0 kernel) and `install.img` (installer), and the menu chains them
  with iPXE's `kernel`/`module` commands. Use the **netinstall** ISO
  (`xcp-ng-<ver>-netinstall.iso`): it pulls its package repository over the
  network, which the installer fetches by HTTP *after* the kernel is up — so it
  netboots cleanly. The full XCP-NG ISO expects its repo on local media and will
  stop at *"base installation repository was not found"*. The default boot prompts
  for the repo URL; pass an answerfile via the **boot args** field for unattended
  installs (`answerfile=http://.../answerfile.xml install`).

### How live images boot (NFS, not download-to-RAM)

For **casper (Ubuntu)** and **live (Debian)** images, the server also unpacks the
ISO's live filesystem into the `nfsroot` Docker volume (mounted at `/nfs/<id>`),
and the `nfs` service exports it read-only. The client boots the extracted
kernel+initrd over HTTP, then mounts the squashfs **over NFS on demand**
(`nfsroot=${server-ip}:/nfs/<id>`).

This replaces the older `url=…iso` / `fetch=…iso` methods, which copied the
**entire ISO into a RAM disk** before booting — that ramdisk is only ~50% of the
client's RAM, so a 6 GB desktop ISO failed with `No space left on device` /
`Unable to find a live file system on the network` on anything but a huge client
(it filled even a 12 GB laptop). NFS keeps client RAM use to a few hundred MB
regardless of image size, so 2–4 GB VMs boot fine. **It uses the same uploaded
ISO** — nothing extra is downloaded; the contents are just unpacked server-side.

Cost: each NFS-backed image uses roughly **2× its size on disk** (the kept ISO
plus the unpacked tree).

> **Upgrading an existing image:** boot args are only auto-filled when blank, so
> an image processed before this change keeps its old `url=…iso` args. Clear the
> **boot args** field and hit **Retry** (or delete and re-upload) to switch it to
> NFS.
- **Windows ISOs are stored but *not yet bootable*.** Windows can't PXE-boot a
  raw ISO; it needs `wimboot` + WinPE (and usually an SMB share for the install
  files). The UI flags Windows images as `unsupported`. This is on the roadmap.

If extraction can't find a kernel/initrd, the image shows `error` with a reason;
fix the boot args or check the ISO layout and hit **Retry**.

---

## Image storage on a local folder, NFS, or SMB

The web/nginx containers read images from a host path set by `IMAGE_PATH` in
`.env` (default `./data/images`), mounted to `/images` inside the containers.

- **Local folder:** set `IMAGE_PATH=/srv/pxe/images` (or leave the default).
- **NFS / SMB:** mount the share on the **host** to a directory, then point
  `IMAGE_PATH` at it. Example for NFS:

  ```bash
  sudo mkdir -p /mnt/pxe-images
  sudo mount -t nfs nas.local:/exports/pxe /mnt/pxe-images
  # in .env:  IMAGE_PATH=/mnt/pxe-images
  ```

  For SMB use `mount -t cifs //nas.local/pxe /mnt/pxe-images -o credentials=...`.
  Add the mount to `/etc/fstab` so it survives reboots.

---

## Testing without real hardware (QEMU)

On the same L2 network as the server, boot a VM with a PXE-capable NIC.

```bash
# UEFI client (needs OVMF firmware)
qemu-system-x86_64 -m 2048 -boot n \
  -netdev bridge,id=net0,br=br0 -device virtio-net,netdev=net0 \
  -bios /usr/share/OVMF/OVMF_CODE.fd

# Legacy BIOS client
qemu-system-x86_64 -m 2048 -boot n \
  -netdev bridge,id=net0,br=br0 -device e1000,netdev=net0
```

Both should pull their respective iPXE binary and land on the same colourful
`boot.ipxe` menu. (`-netdev user` won't see the proxyDHCP server — use a bridge
to the real network.)

---

## Troubleshooting

- **`No space left on device` / `Unable to find a live file system on the
  network`, dropping to an `(initramfs)` shell.** The image is still using the
  old download-the-whole-ISO-to-RAM boot method. Switch it to NFS: clear the
  **boot args** field on the Images page and hit **Retry** (or delete and
  re-upload). See "How live images boot" above.

- **NFS mount fails / live FS not found even with NFS args.** Check the `nfs`
  service: `docker compose logs nfs` should show `current exports: /nfs`. The
  host needs the NFS kernel module — `sudo modprobe nfsd` — and the `nfs`
  container runs privileged with host networking. Confirm the client can reach
  the server (it uses NFSv3, so ports 111 + 2049 must be open on the LAN).

- **Xen VMs: iPXE shows `Link status: Down` / `Waiting for link-up on net0 …
  Down` and never reaches the menu.** The VM's firmware downloads `ipxe.efi`
  fine, then iPXE's Xen paravirtual NIC driver (`netfront on vif/0`) can't bring
  the link up. This happens *before* any of the boot-image machinery above.

  **Root cause:** it's not really an iPXE bug — it's a regression in the Xen
  **host (dom0) kernel's** `xen-netback` driver. When a guest's netfront link
  goes to *Connected* a **second** time (the firmware connects once to download
  iPXE, then iPXE re-connects), the backend stays stuck in `InitWait`, so the
  link never comes up. Introduced by xen-netback commit `1f256578`, fixed by
  `2afeec08`, which is in stable kernels from ~April 2021 (4.9.268, 4.14.231,
  4.19.189, 5.4.114, 5.10.32 and newer). This is exactly why tools like iVentoy
  may "just work" on Xen while a fresh build here doesn't — it's the host kernel,
  not proprietary boot magic.

  Fixes, in order of reliability (none require modifying the guest OS):
  1. **Update the Xen host kernel** to one containing the fix (the proper fix;
     covers all guests at once).
  2. **Give the VM an emulated NIC** (e1000/rtl8139) instead of the PV netfront
     device — avoids netback entirely, per-VM.
  3. **Try an older iPXE** that may not re-trigger the second connect. The
     binaries are built into the `dnsmasq` image from the `IPXE_REF` build arg
     (default `master`); rebuild it pinned to an older ref:
     `docker compose -f docker-compose.yml -f docker-compose.dev.yml build --build-arg IPXE_REF=v1.21.1 dnsmasq`
     then `docker compose up -d dnsmasq`. Not guaranteed, since the bug is
     host-side.

## Updating / tearing down

```bash
docker compose pull && docker compose up -d --build   # update
docker compose down                                   # stop (keeps data)
docker compose down -v                                # stop + drop bootroot/nfsroot volumes
```

**Always update the whole stack with `docker compose`, not a per-container UI.**
The `dnsmasq` and `nfs` services run with `network_mode: host` (required:
dnsmasq serves broadcast DHCP/proxyDHCP + TFTP, and nfs runs the in-kernel NFS
server). Container-management tools such as Dockhand/Portainer recreate
containers one at a time and mishandle host-networked containers — they pull the
new image and remove the old container but never recreate it, reporting
"Container recreation failed" for exactly those two. The other three services
update fine, leaving the stack half-updated. `docker compose pull && docker
compose up -d` recreates host-networked containers correctly and is the
supported way to update. (If you've already hit the failure in such a tool, just
run that compose command to finish the update.)

User accounts, settings, and image metadata live in `./data/pxe.db`. Uploaded
ISOs live under `IMAGE_PATH`.

---

## Security notes

- The UI is HTTP on port 8080. For anything beyond a trusted LAN, put it behind a
  reverse proxy with TLS (Caddy/Traefik/nginx) — not included here.
- The `reload` sidecar mounts the Docker socket so it can restart dnsmasq. This
  keeps that privilege out of the web app. Remove the service if you'd rather
  restart dnsmasq manually after changing settings.
- Change the auto-generated admin password if you prefer your own, and create
  individual user accounts rather than sharing the admin login.
