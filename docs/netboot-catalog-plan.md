> Working note: plan for a netboot-archive image type. Not documentation.

# Netboot archive images (target 0.8.0)

## Goal

Add a second way to create an image, alongside ISO upload: pick a distro and
release from a catalog, and Beacon keeps only the small kernel and initrd. The
client's installer then pulls packages from a mirror. No ISO and no extracted
tree are stored, so the double disk use goes away for these images.

This is an addition. ISO upload, Reprocess and the existing netboot plans in
`web/app/services/images.py` (`_netboot_plan`) stay as they are.

## Decisions so far

- **Both kinds where the distro supports them.** Installers come from netboot
  archives. Live desktops have no separate boot files (Debian ships them only in
  the live ISO), so Beacon downloads the ISO once, keeps kernel + initrd + root
  filesystem, and deletes the ISO. Built for Debian in stage 1.
- **Mirror URL:** each catalog entry has a default mirror; the admin can override
  it per image.
- **Source:** the admin can paste a URL (Beacon downloads it) or upload a file.
- **One image per release.** Fedora 44 and Fedora 45 are separate entries, since
  the installer must match its package repo.
- **Version:** a new capability, so 0.8.0 (minor), not a patch.

## Catalog entry (shared by built-in and custom entries)

Name, archive or kernel+initrd URL template with `{version}`, default mirror,
kernel-argument template with `{mirror}` and `{version}`, and the releases
offered. Built-in entries live in code or a shipped data file; custom entries
use the same fields.

Candidate distros (arguments and URLs not yet verified against each distro):

| Distro | Boot files | Mirror argument |
| --- | --- | --- |
| Debian | `netboot.tar.gz` | preseed / mirror settings |
| Ubuntu | legacy netboot archive | to confirm |
| Alpine | netboot kernel + initramfs | `alpine_repo=` |
| Fedora/RHEL family | `images/pxeboot/` kernel + initrd | `inst.repo=` / `inst.stage2=` |
| openSUSE | `boot/x86_64/loader/` linux + initrd | `install=<url>` |

## Stages

1. **Built-in catalog, pinned releases.** Catalog format, add-image form (pick
   distro, type or pick the release, mirror pre-filled), download or upload,
   unpack and keep kernel+initrd only, boot-menu entry, one distro end to end
   first, then the rest.
2. **Release discovery.** *(Built for Debian: the list comes from the mirror's
   suites, end-of-life marking from Debian's distro-info-data LTS dates.)* Read each mirror's listing so new releases (for
   example Fedora 45) appear without a Beacon update. The typed version from
   stage 1 keeps working for every distro.
3. **Custom entries.** *(Built: installers only, from an archive or a kernel URL
   and an initrd URL; stored in the database; preview checks the files on the
   mirror.)* A form and a file import for entries in the same format.
   The kernel-argument template is the part admins can get wrong, so validate
   and show the final command line before saving.
4. **The main distros and their flavours.** *(Built: Ubuntu, Fedora, Rocky, Alma,
   CentOS Stream, openSUSE Leap and Tumbleweed, Linux Mint, Kali, Arch, Alpine,
   plus XCP-ng's installer; no Kali live entry, as Kali publishes no live ISO on cdimage.kali.org now.)*
   Built-in entries for the main
   distros beside Debian: Ubuntu, Fedora, Rocky Linux, AlmaLinux, CentOS Stream
   and openSUSE, and the other well-known ones, installer and live desktop where
   the distro has them. A distro (or flavour) is only added if it can boot this
   way: boot files Beacon can fetch without keeping an ISO for an installer, or
   a live ISO it can take the kernel, initrd and root filesystem from. One that
   cannot is still added the original way, by uploading its ISO. Each distro
   also offers its less-known flavours, picked the way Debian's Desktop list
   works: Ubuntu's Kubuntu, Xubuntu and the other official flavours, Fedora's
   desktop editions and spins (KDE, Xfce, Cinnamon and so on).
5. **Track latest.** *(Built: each release in the catalog's Release list shows
   the newest build the distro publishes, read from its mirror and refreshed
   every six hours; adding an image downloads that build.)* When Ubuntu
   releases 26.04.1, the catalog shows 26.04.1 without a Beacon update, and an
   image added from it is 26.04.1. Nothing is created or changed on its own, and
   images already built stay as they are. *(An earlier version that added new
   images by itself was built and taken out, as it was not this.)*

## Open questions

- Which distro goes first in stage 1?
- Ubuntu's legacy netboot archive: answered. The debian-installer one ended with
  20.04; 24.04 and later publish a Subiquity netboot archive whose installer
  downloads the live-server ISO (`iso-url=`). 22.04 has none.
- End-of-life releases: answered. They stay listed, marked "end of life" once
  their long-term support ends. What happens when one has left the mirror and is
  added is still to be seen.
- Does a netboot archive need a local-mirror option for offline LANs?
- Stage 5: what makes Beacon check for a newer release: answered. The catalog
  reads each mirror when the Images page is shown (kept for six hours).

## Also on the list

- Release 0.7.2 first; bump `VERSION` to 0.7.3 once it is out, then 0.8.0 when
  this lands.

---

Delete this file once the work lands.
