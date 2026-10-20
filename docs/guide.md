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
| `beta` | `dev` builds published by hand | a beta is published — pre-release, may be broken |

New installs default to `stable`. Switching channels takes effect on the next
update.

Rather than editing `.env`, an admin can turn on **Include beta versions** under
**Settings → Updates**. Updates then move to `beta` whatever `BEACON_TAG` says;
turning it off returns to the `.env` channel at the next update (an older build).
It applies to updates started from the UI: a plain `docker compose up -d` on the
host still uses `BEACON_TAG` from `.env`.

> **Always update the whole stack with `docker compose`, not a per-container UI.**
> `dnsmasq` and `nfs` run with host networking, which tools like Portainer
> mishandle — they pull the new image but never recreate those two, leaving the
> stack half-updated. `docker compose pull && docker compose up -d` recreates
> them correctly.

## How images boot

Upload format is `.iso` only. On upload the kernel + initrd are extracted (via
`bsdtar`; `7z` for the UDF ISOs modern Windows uses) and served over HTTP; the
raw ISO is kept at `/images/<file>.iso`; uploading a second ISO with a name
already in use saves it as `<file>-2.iso`, `<file>-3.iso`, and so on. A
best-effort kernel command line is
filled in per distro — edit **boot args** on the Images page if a live ISO needs
a tweak (a **Reprocess** re-derives them and overwrites your edit).

To reprocess or delete several images at once, tick them in the Images table (the
box in the header ticks every image) and press **Reprocess selected** or **Delete
selected**; both ask first. The images are reprocessed one after another, and an
image still processing is left alone, by a row's own **Reprocess** too.

### Netboot installers (no ISO)

**Add from a netboot catalog** on the Images page creates an image without an
ISO. Pick a distro (the built-in ones are listed in the table after this
paragraph; others are added as custom entries, below), a release (pick from the list, which is read from the mirror and marks
releases past their long-term support "end of life"; type `12` or `bookworm`; or enter any
other release name the mirror carries, or a newer version number, which is read
from the mirror) and a mirror (pre-filled, editable).
Beacon downloads the distro's netboot archive (about 55 MB for Debian), keeps the
kernel and initrd for the boot menu, and sets the kernel arguments so the
client's installer fetches its packages from that mirror. The archive itself
stays in `/images` so **Reprocess** can rebuild the boot files; the client needs
internet (or a reachable local mirror) during the install. The archive can also
come from another URL, or be uploaded when the Beacon host is offline. 
Choose **Live desktop** instead to boot Debian's live desktops (KDE Plasma, GNOME,
Xfce, Cinnamon, MATE, LXQt, LXDE or Standard). Debian publishes live boot files
only inside its live ISO, so Beacon downloads that ISO once (about 3–4 GB), keeps
the kernel, initrd and `filesystem.squashfs`, and deletes the ISO. Clients fetch
the squashfs over HTTP into RAM, so they need about 6 GB. **Reprocess** downloads
the ISO again. Pick the **Release**: the list is read from Debian's
mirror, so it shows the releases Debian carries now (today 14 testing, a
pre-release built weekly, then 13, 12 and 11) and a new release appears on its
own. A release whose long-term support has ended, by Debian's published dates,
stays listed, marked "end of life", unless **Offer end-of-life releases** under
**Server Settings → Netboot catalog** is turned off: then such releases are not
listed for any distro and cannot be added, while images already added stay as
they are. The live ISO's folder is the **Mirror**
field, and the file name is read from it, so new point releases are picked up.
For Debian 12 the newest point release is found in Debian's archive. **Other**
accepts a version number or codename that is not in the list (`15` or its
codename): Beacon finds it on Debian's mirror, then looks for the live ISO in
Debian's current, archive or weekly testing folder.

The other built-in distros work the same way, each in the way the distro
publishes its boot files:

| Distro | Installer | Live desktops |
|--------|-----------|---------------|
| Debian | netboot archive (kernel and initrd) | KDE Plasma, GNOME, Xfce, Cinnamon, MATE, LXQt, LXDE, Standard |
| Ubuntu | Ubuntu Server: netboot archive; the client downloads the live-server ISO from the mirror into RAM | Ubuntu, Kubuntu, Xubuntu, Lubuntu, Ubuntu MATE, Ubuntu Budgie, Ubuntu Cinnamon, Ubuntu Unity, Ubuntu Studio, Edubuntu, Ubuntu Kylin (unpacked ISO over NFS) |
| Fedora | kernel and initrd; the installer is loaded from the mirror | Workstation, KDE Plasma and the spins (Xfce, Cinnamon, LXQt, MATE, Budgie, COSMIC, Sway, i3, LXDE, MiracleWM, KDE Plasma Mobile, Sugar on a Stick) |
| Rocky Linux, AlmaLinux, CentOS Stream | kernel, initrd and the installer's own system (about 1 GB, kept so a point release on the mirror cannot leave them out of step) | Rocky: Workstation, Workstation Lite, KDE Plasma, Xfce, MATE, Cinnamon. Alma: GNOME, GNOME Mini, KDE Plasma, Xfce, MATE. CentOS Stream (Alternative Images SIG): GNOME, KDE Plasma, Cinnamon, MATE, Xfce, MAX, MIN. Some only for some releases |
| openSUSE Leap | 16.0: kernel, initrd and the Agama installer system (about 420 MB). 15.6: kernel and initrd, YaST loaded from the mirror | none |
| openSUSE Tumbleweed | kernel and initrd, YaST loaded from the mirror | GNOME, KDE Plasma, Xfce |
| Linux Mint | none (it installs from the live desktop) | Cinnamon, MATE, Xfce (unpacked ISO over NFS) |
| Kali Linux | netboot archive, as Debian | none |
| Arch Linux | kernel, initrd and the live root filesystem (about 1 GB) | EndeavourOS, CachyOS (Arch-based; kernel, initrd and `airootfs.sfs` taken from their ISO, booted like Arch's own) |
| Alpine Linux | kernel, initramfs and modloop; packages from the mirror | none |
| XCP-ng | Xen, dom0 kernel and `install.img` from XCP-ng's netinstall folder, booted like an uploaded XCP-ng ISO (UEFI GRUB chainloader); packages from the mirror | none |

Ubuntu, Fedora, Rocky, Alma, CentOS Stream, Linux Mint, Alpine and XCP-ng read
their release list from the mirror like Debian, so a new release appears on its own
(Ubuntu's default is the newest LTS); end-of-life dates come from Ubuntu's
distro-info-data, Fedora's Bodhi and Alpine's `releases.json`, or the catalog's
own dates for the others. Each release in the list also names the newest build
the distro publishes now, read from its mirror and refreshed every six hours, so
a point release shows up on its own ("Ubuntu 26.04 LTS — 26.04.1", "Debian 13
(trixie) — 13.7", Rocky's and Alma's 10.2, Alpine's 3.24.2, XCP-ng's 8.3.0,
Arch's monthly release, and the current Tumbleweed snapshot, CentOS Stream
compose and Kali build); adding the image downloads that build. Live desktops whose root is a single squashfs (Fedora,
Rocky, Alma, CentOS Stream, Tumbleweed, EndeavourOS, CachyOS) are kept and streamed
over HTTP like Debian's, so clients need RAM for it. Ubuntu's and Linux Mint's desktops boot
with casper, which mounts its root over NFS only, so Beacon unpacks their ISO
into the NFS share (about the ISO's size) and deletes the ISO; clients need
ports 111, 2049 and 20048 to Beacon, as for an uploaded Ubuntu ISO. A distro or
desktop not in the list is still added by uploading its ISO. A download that
stalls or drops part-way is resumed where it stopped, up to four tries, before
the image is marked as failed.

Pre-releases are listed at the top of the **Release** list, marked
"pre-release", where the distro publishes them, read from its mirror like its
releases: Debian's testing release, Ubuntu's beta before its release day (its
beta archive and ISOs are used until the final ones are out), Fedora's Beta from
`releases/test/`, Rocky Linux's betas from its staging mirror and AlmaLinux's
from its vault (installers only; Rocky's live desktops too), openSUSE Leap's
beta or release candidate, Linux Mint's beta ISOs from `testing/`, Kali's
`kali-dev` and Alpine's `edge`. Picking one fills in the mirror it is on. A
beta the distro has since released (Mint's 22.3 beta once 22.3 is out) is not
listed. CentOS Stream, Tumbleweed, Arch and XCP-ng publish none. **Offer
pre-releases** under **Server Settings → Netboot catalog** (on by default) hides
them all and refuses adding one when turned off, typed under **Other** too;
images already added stay as they are.

**Custom catalog entries** add an installer for a distro the catalog does not
have. Under **Custom catalog entries** on the Images page, give a name, a default
mirror, the boot files and the kernel arguments, and list the releases one per
line (the first is the default; `43 | Fedora 43` sets a label). The boot files are
either a netboot archive with the paths of the kernel and initrd inside it
(Debian's `netboot.tar.gz`), or a kernel URL and an initrd URL, downloaded as they
are (Fedora's `images/pxeboot/`, Alpine's `netboot/`); those are fetched again on
**Reprocess**, and only **Download from the mirror** applies to them. In the URLs,
`{mirror}` is the image's mirror and `{version}` the release; the kernel arguments
also take `{mirror_proto}`, `{mirror_host}` and `{mirror_path}`. A literal brace
is written twice, so the iPXE variable `${boot-url}` is `${{boot-url}}`. For
example, Fedora's installer (also built in) as a custom entry:

| Field | Value |
|-------|-------|
| Default mirror | `https://dl.fedoraproject.org/pub/fedora/linux` |
| Kernel URL | `{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/vmlinuz` |
| Initrd URL | `{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/initrd.img` |
| Kernel arguments | `inst.repo={mirror}/releases/{version}/Everything/x86_64/os ip=dhcp` |
| Releases | `44` |

**Preview** fills the templates in for every release, checks each file on the
mirror (found with its size, not found, or not checked when the Beacon host is
offline) and shows the kernel command line the boot menu will carry; **Save**
appears after a preview, and editing a field after it asks for a new one. An
unknown placeholder, a URL that does not come out as http(s), or several releases
with no `{version}` in a URL is refused with a message. The entry then appears in
the **Distro** list of **Add from a netboot catalog**, marked "(custom)". A
release not in the list can be typed under **Other**.

**Import from a file** takes JSON in the built-in catalog's own format
(`{"distros": [ ... ]}`), a list of entries, or one entry, with the fields `name`,
`default_mirror`, `archive_url` + `kernel_member` + `initrd_member` or
`kernel_url` + `initrd_url`, `boot_args`, `releases` (each a name, or an object
with `id` and optionally `label`, `number`, `default`, `eol`, and `prerelease`:
`true` marks a beta, hidden with the built-in pre-releases), and an optional
`id`. Every entry is previewed, and nothing is imported if any entry is wrong; an
entry with the ID of an existing custom entry replaces it. Custom entries are kept
in the database, so a backup carries them. Deleting one leaves its images
booting, but they cannot be reprocessed until an entry with the same ID is back.

| Distro | Boot args |
|--------|-----------|
| Ubuntu live | `boot=casper netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp` |
| Debian live | `boot=live netboot=nfs nfsroot=${server-ip}:/nfs/<id> ip=dhcp` |
| Fedora/RHEL netinstall | `inst.stage2=${boot-url}/os/<id>/tree ip=dhcp` (packages come from the online mirrors; the client needs internet) |
| Fedora/RHEL full DVD | `inst.repo=${boot-url}/os/<id>/tree ip=dhcp` |
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

## Accounts, 2FA, and the API

**Two-factor authentication** is per-user, enabled from **My Profile**. Beacon
generates a TOTP secret (RFC 6238, 30-second steps), shows it as a QR code and as
text for manual entry, and only activates it once you enter a valid code — a
mistyped secret can't lock you out. A code from the previous or next window is
accepted, so reading one just as it rolls over still works. Five wrong codes
in a row lock that account's 2FA step for 15 minutes, so the code can't be
guessed by trying them all. Losing the
authenticator means losing the account: another admin can delete and recreate the
user, or clear `totp_enabled` in `data/pxe.db` directly.

**API tokens** are also per-user (one at a time), issued and revoked from **My
Profile**, and sent as `Authorization: Bearer <token>`. A token authenticates as
its user with that user's role, and bypasses the 2FA challenge by design — it is
a credential for machines, so treat it like a password. Any authenticated
endpoint accepts one:

| Endpoint | Access | Returns |
| --- | --- | --- |
| `GET /api/events` | user | Boot records — `image_id`, `since` (ISO 8601 UTC), `limit` (default 100, max 1000) |
| `GET /api/stats` | user | Live host metrics, recent clients, deploy counters |
| `GET /api/backup` | admin | The SQLite database as `beacon-backup.db` |
| `POST /api/restore/preview` | admin | Stages an uploaded backup, returns what restoring it would change |
| `POST /api/restore/apply` | admin | Applies the staged backup (needs the preview's `token`) and restarts |
| `GET /healthz` | none | Liveness plus a database connectivity check |

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://<server-ip>:8080/api/events?since=2026-01-01T00:00:00&limit=50"
```

Note that a missing or invalid token gets the browser behaviour — `303` to
`/login` — rather than a `401`, so check the status code, not just the body.

## Backup and restore

**Server Settings → Backup & restore** downloads `beacon-backup.db`: the SQLite
database itself, holding users (password hashes, 2FA secrets, API tokens),
every server setting, image metadata, custom netboot catalog entries and boot
history.

It is a **configuration** snapshot, not a media backup. ISOs (`./data/images`),
drop-in drivers (`./data/drivers`), your `.env`, and the extracted boot files on
the `bootroot`/`nfsroot`/`smbroot` volumes are all outside it. Back the ISOs up
separately if re-downloading them would be painful.

### Restoring

Upload the file on the same page. Beacon validates it, then shows a preview of
exactly what would change — settings, accounts, and how each host setting was
decided — before anything is touched. Confirming swaps the database in and
restarts the web container; the page reconnects on its own.

A backup is refused outright if it isn't a SQLite file, fails an integrity check
(the usual sign of a truncated download), lacks Beacon's tables, or contains no
admin account — restoring that last one would lock you out permanently.

On the next start Beacon migrates the restored schema if the backup came from an
older build, regenerates `dnsmasq.conf` and the boot menu from the restored rows,
and marks any image whose extracted files are missing as *needs reprocess* so a
broken entry can't sit in the boot menu. Reprocess those from **Images** to
rebuild them from the ISO.

### What is not restored

**Host identity.** `server_ip` and `boot_interface` describe the machine, not
your preferences, and a backup from other hardware carries values that are wrong
here — dnsmasq would bind an interface that doesn't exist, or hand clients a
next-server that isn't this box. Both are checked against the host: an interface
must exist and an IP must be one the host answers to. A value that doesn't match
is left alone, the current one is kept, and the preview says so. Restoring onto a
replacement server with the same IP therefore just works; restoring onto a
different base OS keeps this host's NIC name instead of the old one's.

The rest of the DHCP block (range, gateway, DNS) can't be checked that way, so it
*is* restored — but when a host setting was rejected the preview flags the whole
network section for review, because a range from the old server's subnet is
equally wrong here.

**Update bookkeeping.** The `update_*` settings describe the deployment doing the
restoring, not the one that was backed up, so they're dropped rather than
restored — otherwise a backup taken mid-update would leave the UI stuck on
"Update in progress" forever.

### Undoing a restore

The database being replaced is saved as `data/pxe.db.pre-restore` first. If the
restored database has no password you know:

```bash
cd /path/to/beacon
docker compose stop web
cp data/pxe.db.pre-restore data/pxe.db
docker compose start web
```

### Rebuilding a server from a backup

```bash
# 1. compose file + .env in an empty dir; set SERVER_IP / BOOT_INTERFACE
docker compose up -d
# 2. log in with the admin password from `docker compose logs web`
# 3. Server Settings → Backup & restore → upload beacon-backup.db → confirm
```

Copy the ISOs back into `./data/images` under the filenames the backup recorded
and reprocess them if you want the images too; delete the rows if you don't.

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
