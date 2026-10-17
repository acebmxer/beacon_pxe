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
2. **Release discovery.** Read each mirror's listing so new releases (for
   example Fedora 45) appear without a Beacon update. The typed version from
   stage 1 keeps working for every distro.
3. **Custom entries.** A form and a file import for entries in the same format.
   The kernel-argument template is the part admins can get wrong, so validate
   and show the final command line before saving.
4. **Track latest.** A moving entry ("Debian stable") that re-fetches boot files
   on refresh. Off by default.

## Open questions

- Which distro goes first in stage 1?
- Ubuntu's legacy netboot archive: still published for current releases? Check.
- End-of-life releases move to archive URLs. Detect a dead default mirror and
  point at the archive URL, or fail with a plain message?
- Does a netboot archive need a local-mirror option for offline LANs?

## Also on the list

- Release 0.7.2 first; bump `VERSION` to 0.7.3 once it is out, then 0.8.0 when
  this lands.

---

Delete this file once the work lands.
