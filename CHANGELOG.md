# Changelog

All notable changes to Beacon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe user-facing changes: what a Beacon operator gains, loses, or has
to do differently. Internal refactors that change nothing observable are omitted.

## [Unreleased]

### Added

- Fedora 42+ live and Archiso HTTP netboot support — these distros dropped the
  boot methods Beacon relied on, so they now netboot from a single-file HTTP
  root instead of NFS.
- Self-update from the web UI: an admin can pull new images and recreate the
  stack via `docker compose` without shell access to the host.
- SMB service (`smb`) serving each Windows image's unpacked install media, so
  WinPE can reach `sources/install.wim` and run Setup. iPXE cannot present an
  ISO as a CD under UEFI, so SMB is how Setup reaches the media.
- `PROJECT_DIR` in `.env`, giving the web container the host project path so
  self-update resolves relative volume paths (`./data`, etc.) correctly.
- Selectable update channel via `BEACON_TAG` in `.env`: `latest` tracks the
  rolling `main` branch (the previous, only behaviour), `stable` tracks tagged
  releases so the stack sits still between versions. Compose and the web app's
  update check read the same variable, so the check can never report an update
  that `docker compose pull` would not install. A new `:stable` image tag is
  published on release tags only — it first appears with this release, so
  `stable` is not selectable before then.

### Fixed

- Windows Setup failed with `Access is denied` when launched from the SMB share.
  7z unpacks the ISO with mode 644 (no execute bit), and smbd answers an
  open-for-execute with `ACCESS_DENIED` when the POSIX execute bit is missing.
  Reads succeeded, so WinPE mounted the share and could read `install.wim`, but
  `setup.exe` would not start. The share now sets `acl allow execute always`.

### Documentation

- Troubleshooting entry for host firewalls blocking the boot chain. Every
  service except the web UI and NFS listens below port 1025, so a default
  firewalld or ufw policy drops TFTP, DHCP, HTTP, and SMB while every container
  still reports healthy.

## [0.1.2] - 2026-06-21

### Added

- Windows PXE boot via wimboot + SMB, and live dashboard stats.
- XCP-NG / XenServer netinstall support via Xen multiboot and a self-configuring
  UEFI GRUB binary built with `grub-mkstandalone`.
- Per-image processing status with a live upload/extraction UI, and a
  **Reprocess** button available for images in any state.
- Native USB drivers in the iPXE build, so USB keyboards work at the boot menu.
- License badge, and License and Contributing sections in the README.

### Changed

- Images list sorts alphabetically by name, case-insensitively.
- Dropped iPXE boot-menu colour theming in favour of a plain-text console after
  the colour handling proved unreliable across firmware.
- Pinned the web image to `python:3.12-slim-bookworm`.

### Fixed

- dnsmasq config is only rewritten when its contents actually change, so the
  reload sidecar stops restarting dnsmasq on every settings save.
- `init` and a stop grace period for the `dnsmasq` and `nfs` services.
- XCP-NG installer console, VGA, and dom0 framebuffer arguments, including a
  hang on AMD APU hosts.
- ISO9660 version-suffix handling when matching the `grubx64.efi` path.

## [0.1.1] - 2026-06-18

### Fixed

- CI: restrict the SHA image tag to the default branch only.

## [0.1.0] - 2026-06-18

### Added

- Initial release. PXE/iPXE boot server as a Docker Compose stack: dnsmasq
  (proxyDHCP or full DHCP, plus TFTP with iPXE binaries baked in), nginx serving
  the HTTP boot root, a FastAPI management UI with auth and image handling, and
  an NFS service exporting each image's live filesystem so clients mount it on
  demand instead of downloading whole ISOs into RAM.
- Distribution as prebuilt GHCR images, so a deployment needs only the compose
  file and a `.env` — no repo checkout or local build.

[Unreleased]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/acebmxer/beacon_pxe/releases/tag/v0.1.0
