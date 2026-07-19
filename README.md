# Beacon

[![Publish images](https://github.com/acebmxer/beacon_pxe/actions/workflows/publish.yml/badge.svg)](https://github.com/acebmxer/beacon_pxe/actions/workflows/publish.yml)
[![Latest release](https://img.shields.io/github/v/release/acebmxer/beacon_pxe)](https://github.com/acebmxer/beacon_pxe/releases/latest)
[![Container images](https://img.shields.io/badge/ghcr.io-beacon-2496ED?logo=docker&logoColor=white)](https://github.com/acebmxer?tab=packages&repo_name=beacon_pxe)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-hosted PXE/iPXE boot server with a login-protected web management
console. It netboots **both BIOS and UEFI**
clients from a **single boot menu**, manages OS images uploaded as **ISO**, and
supports **admin / user** accounts.

Runs as a small Docker Compose stack.

---

## Features

- **One menu for BIOS *and* UEFI.** Firmware requires two different initial
  binaries (`undionly.kpxe` for BIOS, `ipxe.efi` for UEFI) — that part is
  unavoidable — but after that handoff every client loads the **same**
  `http://<server>/boot.ipxe` menu.
- **Menu built from your images** — upload an ISO and it appears as an entry;
  no hand-editing boot scripts. Entries are sorted by name and can be disabled
  individually without deleting the image.
- **Web UI** (FastAPI) gated by a login screen, with light/dark themes.
- **Default admin** created on first run; password supplied via `.env` or
  **auto-generated** and printed to the logs.
- **Server settings:** enable/disable services, switch between **proxyDHCP**,
  **full DHCP**, and **external DHCP**, set the DHCP range, edit the menu,
  toggle theme.
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

> **The in-app update button replaces a local build with the published one.**
> Settings → Updates has one update path: pull the images for the configured
> channel from GHCR and recreate the stack. It has no way to rebuild from your
> source, so clicking Apply on a locally built deployment reverts it — a fix you
> just built and verified appears to regress, because the image under it was
> swapped. The Updates panel warns when the running build is local (it reports
> `dev build`); to get your build back, re-run the command above.

---

## Architecture

| Service   | Image | Role | Network |
|-----------|-------|------|---------|
| `web`     | `beacon-web` (FastAPI) | Management UI + API; renders boot configs | port 8080 |
| `nginx`   | `beacon-nginx` (nginx:alpine + config) | Serves boot files to iPXE (`/boot.ipxe`, kernels, initrds, ISOs) | port 80 |
| `dnsmasq` | `beacon-dnsmasq` (alpine + iPXE binaries) | proxyDHCP/DHCP + TFTP; BIOS/UEFI arch detection | **host** |
| `nfs`     | `beacon-nfs` (alpine) | Exports each image's live filesystem (read-only) so clients mount it instead of downloading the whole ISO into RAM | **host**, privileged |
| `smb`     | `beacon-smb` (alpine + samba) | Exports each Windows image's unpacked install media (read-only, guest) so WinPE can run `setup.exe` | **host** |
| `reload`  | `beacon-reload` (docker:cli + watch script) | Restarts dnsmasq when its config is regenerated | host |

Images are published to `ghcr.io/acebmxer/beacon-<service>`. The iPXE boot
binaries (built from source with an embedded chain script), the nginx config,
the samba config, and the reload script are baked into their images — nothing is
bind-mounted from the repo, which is why the stack runs from just
`docker-compose.yml` + `.env`.

A consequence worth knowing: **some fixes ship inside a service image, not in
the web app.** The iPXE chain script (`dnsmasq`) and the samba tuning (`smb`)
are two examples. Updating only some services, or rebuilding only `web`, can
leave those behind — which is why the supported update is a whole-stack
`docker compose pull && docker compose up -d`.

`dnsmasq` uses **host networking** because DHCP/PXE relies on layer-2 broadcasts
that don't traverse Docker's bridge network. This is the standard requirement for
any PXE server in Docker.

`nfs` uses **host networking** and runs **privileged** because it runs the
in-kernel NFS server (`nfsd`). The host must provide the `nfsd` kernel module
(standard on Linux — `modprobe nfsd` if it isn't already loaded). See "How live
images boot" below for why NFS is used.

`smb` also uses **host networking**, so WinPE reaches the share at the server's
LAN address — the same IP baked into the boot script inside `boot.wim`.

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
- **External DHCP.** Your own DHCP server drives the boot: Beacon answers no
  DHCP at all and only serves the iPXE binaries over TFTP. Use this when your
  DHCP server has network-boot fields and you would rather configure them there
  than run a proxy. Point it at:

  | Setting | Value |
  |---------|-------|
  | Next server / TFTP server | this box's IP (the **Server IP** setting) |
  | Boot file, UEFI clients | `ipxe.efi` |
  | Boot file, legacy BIOS clients | `undionly.kpxe` |

  Most DHCP servers allow only one boot filename. Because Beacon isn't answering
  DHCP in this mode it cannot detect the client architecture for you, so a
  single-filename server means picking one: set `ipxe.efi` and boot clients in
  UEFI mode, or use proxyDHCP, which serves the right binary per client.

Which one you want, in short: **proxyDHCP** if your DHCP server has no
network-boot fields (most consumer routers and mesh systems), **External DHCP**
if it does and you have filled them in, **Full DHCP** if you have no DHCP server
at all.

---

## Images: format & what actually boots

- **Upload format: `.iso` only.** On upload, the server reads the ISO without a
  privileged loop mount and extracts the Linux **kernel** + **initrd**, which are
  served over HTTP for reliable netboot. `bsdtar` handles ISO9660 (Linux, XCP-NG);
  modern Windows ISOs are UDF, which `bsdtar` can't read, so those fall back to
  `7z`. The raw ISO is also kept and served at `/images/<file>.iso`.
- **Boot arguments.** A best-effort kernel command line is filled in per distro
  family. Some live ISOs need a tweak — edit the **boot args** field on the
  Images page (note that a **Reprocess** re-derives them and overwrites your
  edit). Examples:
  - Ubuntu live: `boot=casper netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp`
  - Debian live: `boot=live netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp`
  - Fedora/RHEL netinstall: `inst.repo=${boot-url}/images/<file>.iso ip=dhcp`
  - Fedora 42+ live: `root=live:${boot-url}/os/<id>/squashfs.img rd.live.image ip=dhcp`
  - Archiso (Arch/EndeavourOS/CachyOS):
    `archiso_http_srv=${boot-url}/os/<id>/ archisobasedir=arch BOOTIF=01-${net0/mac:hexhyp} ip=dhcp`
- **XCP-NG (netinstall ISO).** XCP-NG is Xen-based, so it boots via **multiboot**
  rather than a plain kernel+initrd: the server extracts `xen.gz` (hypervisor,
  decompressed — the multiboot loader can't gunzip it itself), `vmlinuz` (dom0
  kernel) and `install.img` (installer). iPXE's own multiboot support is
  BIOS-only, and most XCP-NG hosts boot UEFI, so Beacon builds a **self-contained
  GRUB EFI binary per image** (`grub-mkstandalone`) and chainloads that; GRUB
  fetches the three files over HTTP and does the `multiboot2` itself. The server
  IP is baked into that binary, so changing **Server IP** rebuilds it
  automatically. Use the **netinstall** ISO
  (`xcp-ng-<ver>-netinstall.iso`): it pulls its package repository over the
  network, which the installer fetches by HTTP *after* the kernel is up — so it
  netboots cleanly. The full XCP-NG ISO expects its repo on local media and will
  stop at *"base installation repository was not found"*. The default boot prompts
  for the repo URL; pass an answerfile via the **boot args** field for unattended
  installs (`answerfile=http://.../answerfile.xml install`).

- **Windows ISOs boot the installer via `wimboot` + WinPE, with the install media
  served over SMB.** Windows can't PXE-boot a raw kernel/initrd, so Beacon
  extracts the WinPE boot files (`bootmgr`, `BCD`, `boot.sdi`,
  `sources/boot.wim`) and boots them through
  [`wimboot`](https://ipxe.org/wimboot). That gets WinPE running, but not the
  install media: `sources/install.wim` isn't inside `boot.wim`, and iPXE can't
  present the ISO as a virtual CD under UEFI (`sanhook` is BIOS INT 13h only).
  So the whole ISO is also unpacked to the `smbroot` volume, and the `smb`
  service exports it read-only. Beacon patches `winpeshl.ini` inside `boot.wim`
  so WinPE mounts that share on boot and runs `setup.exe` from it. The install
  runs interactively (no answer file). `wimboot` is bundled in the web image and
  served at `/wimboot`.

  This means **Windows images need ports 139 + 445 reachable** from the client
  (see Troubleshooting), and each one costs roughly **2× its size on disk** —
  the kept ISO plus the unpacked share.

If extraction can't find a kernel/initrd, the image shows `error` with a reason;
fix the boot args or check the ISO layout and hit **Retry**. If an image's
extracted files are later deleted, it shows `needs reprocess` instead — see
"Updating / tearing down".

### How live images boot (NFS or HTTP, not download-to-RAM)

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

**Fedora 42+ live** and **Archiso** (Arch, EndeavourOS, CachyOS) take a third
route: neither NFS nor download-to-RAM. Both dropped the boot methods Beacon
relied on, so rather than unpacking the whole ISO, Beacon extracts just the root
filesystem — `LiveOS/squashfs.img` or `arch/<arch>/airootfs.sfs` — into the
bootroot and lets nginx stream it over HTTP. Nothing is exported over NFS for
these, and the disk cost is the ISO plus that one file rather than a full second
copy.

One caveat specific to Archiso: its initramfs pulls the entire `airootfs.sfs`
into a RAM tmpfs before starting, so the live desktop needs **~8 GB of client
RAM** (4–6 GB thrashes or OOMs). That's fine for physical clients but worth
knowing when testing in a VM.

> **Upgrading an existing image:** an image processed before this change keeps
> its old `url=…iso` args until it is reprocessed. Hit **Reprocess** on the
> Images page — a reprocess re-derives the boot args from the ISO and overwrites
> whatever was in the field, so there is no need to clear it first. (Any manual
> edit you made is overwritten too; re-apply it afterwards.)

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

Both should pull their respective iPXE binary and land on the same
`boot.ipxe` menu. (`-netdev user` won't see the proxyDHCP server — use a bridge
to the real network.)

---

## Troubleshooting

- **Client times out fetching the boot file (`PXE-E18`, `NBP filesize is 0
  Bytes`) even though every container is healthy.** The host firewall is
  dropping the traffic. Every service Beacon needs except the web UI and NFS
  listens **below port 1025**, and default policies on Fedora/RHEL (firewalld)
  and Ubuntu (ufw) block that range — so TFTP, DHCP, HTTP, and SMB all fail
  while `docker compose ps` shows everything `Up`. Containers using host
  networking are *not* exempt: Docker only punches through for published ports.

  Ports needed: **69/udp** (TFTP), **67/udp** + **4011/udp** (DHCP/proxyDHCP),
  **80/tcp** (boot menu, kernels, `boot.wim`), **139+445/tcp** (SMB, Windows
  only), **111/tcp+udp**, **2049/tcp**, **20048/tcp+udp** (NFS + rpcbind +
  mountd, live Linux images only).

  ```bash
  # firewalld (Fedora/RHEL) — use the zone your boot interface is in
  sudo firewall-cmd --permanent --zone=FedoraWorkstation \
    --add-service=tftp --add-service=dhcp --add-service=http \
    --add-service=samba --add-service=nfs --add-service=rpc-bind \
    --add-service=mountd
  sudo firewall-cmd --reload
  ```

  Note `samba-client` is the *outbound* client service and does not open 445;
  the server service is `samba`. Confirm with `firewall-cmd --list-all`, then
  check `docker compose exec web cat /dnsmasq/dnsmasq.log` — a successful boot
  logs `sent /tftp/ipxe.efi to <client>`. If that log shows no client
  transactions at all, packets are still being dropped before dnsmasq sees them.

- **Client boots iPXE but then fetches `boot.ipxe` from the wrong IP and times
  out.** Another DHCP server on the LAN is also answering with PXE options and
  iPXE preferred its response. Beacon's log will show it served the correct URL
  (`bootfile name: http://<server-ip>/boot.ipxe`) while the client's screen
  shows a different address. Clear the `next-server` / option 66/67 settings on
  whatever hands out leases for that subnet, or move Beacon to an isolated boot
  VLAN and run it in full DHCP mode.

- **`No space left on device` / `Unable to find a live file system on the
  network`, dropping to an `(initramfs)` shell.** The image is still using the
  old download-the-whole-ISO-to-RAM boot method. Hit **Reprocess** on the Images
  page to re-derive its boot args and switch it to NFS. See "How live images
  boot" above.

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
docker compose pull && docker compose up -d   # update
docker compose down                           # stop (keeps data)
docker compose down -v                        # stop + drop the unpacked-image volumes
```

(No `--build` — `docker-compose.yml` pulls prebuilt images and defines no build
context. Building is the dev override's job; see "Building from source" above.)

`-v` destroys the `bootroot`, `nfsroot`, and `smbroot` volumes, which hold every
unpacked image — kernels, initrds, live filesystems, and Windows install media.
The database is a bind mount (`./data`) and survives, so the image list comes
back intact while the files behind it are gone. Beacon detects this at startup:
images whose boot files are missing are marked **needs reprocess**, held out of
the boot menu, and restored by hitting **Reprocess** (the uploaded ISOs live
under `IMAGE_PATH` and are not touched by `-v`). Ordinary updates never do this
— `pull && up -d` and the in-app update button leave volumes alone.

The admin UI can also update the stack in place (Settings → Updates), which
pulls new images and recreates the containers for you.

> **Upgrading from 0.2.0 or earlier to 0.2.1 must be done from the host, using
> the commands above.** The in-app update button is broken in those versions —
> it reports success without recreating anything — and the fix only takes effect
> once 0.2.1 is running. First compare `.env` against `.env.example` and add
> anything missing: `PROJECT_DIR` is required (the update cannot recreate
> containers without it), and `BEACON_TAG` selects the update channel
> (deployments without it stay on `latest`).

### Update channels

`BEACON_TAG` in `.env` picks which published images the stack tracks — both what
`docker compose pull` installs and what the web UI's update check watches:

| `BEACON_TAG` | Tracks | Updates when |
| --- | --- | --- |
| `stable` | tagged releases | a new version is released |
| `latest` | rolling `main` branch | any commit merged to `main` |

Choose `stable` if you want the boot server to sit still between releases;
`latest` if you want fixes as they land. The update check follows whichever tag
is set, so it never reports an update that `pull` would not install.

New installs get `stable` from `.env.example`. Deployments that predate update
channels have no `BEACON_TAG` set and stay on `latest`, which is what they were
already tracking — add the variable to `.env` to move onto releases.

Switching channels takes effect on the next update: the UI reports an update
available, and applying it moves the stack onto the new channel. Note that going
`latest` → `stable` installs an *older*, released build — the change is applied
through the normal update path either way.

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

---

## License

Beacon's own source code is released under the **[MIT License](LICENSE)** —
free to use, modify, and redistribute (including commercially) with attribution.

Beacon does not vendor third-party source; its Docker images install upstream
packages and build a few tools (iPXE, wimboot) from source at build time. Those
components keep their own licenses — several are GPL/LGPL (dnsmasq, samba, iPXE,
wimboot, etc.). The MIT license covers only Beacon's code, not the bundled
components. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the full
inventory and source-availability notes.

---

## Contributing

Contributions are welcome. By submitting a pull request you agree that your
contribution is licensed under the project's MIT License. Please keep changes
focused and describe what you tested (the README's QEMU section is handy for
verifying boot changes without real hardware).

Add user-facing changes to the **Unreleased** section of
[CHANGELOG.md](CHANGELOG.md) — anything an operator gains, loses, or has to do
differently. Internal refactors that change nothing observable can be skipped.
