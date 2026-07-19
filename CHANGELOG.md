# Changelog

All notable changes to Beacon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe user-facing changes: what a Beacon operator gains, loses, or has
to do differently. Internal refactors that change nothing observable are omitted.

## [Unreleased]

### Added

- **External DHCP mode.** A third DHCP mode for networks whose own DHCP server
  already supplies next-server/filename. Beacon answers no DHCP whatsoever and
  serves only the iPXE binaries over TFTP, so it cannot race the server that is
  already doing the job. The Settings page shows the exact values to enter on
  your DHCP server. Existing deployments are unaffected — the default is still
  proxyDHCP.

### Fixed

- **Windows installs no longer fail with "System error 53" on the boot after a
  successful one.** A client reset mid-install never closes its SMB session, so
  the server kept that TCP socket ESTABLISHED for up to two hours. WinPE reuses
  the same ephemeral port on every cold boot, so the next attempt's SYN hit the
  stale socket and got an RFC 5961 challenge ACK instead of a SYN-ACK; WinPE
  never sends the RST that would clear it, so the mount failed until the socket
  aged out. Because a *successful* install is what creates the stale socket, this
  presented as Windows booting fine one time and failing the next, and looked
  convincingly like corrupt install media — reprocessing the ISO never helped,
  because nothing was wrong with it. The SMB service now reaps dead connections
  in ~25s via per-socket keepalive timers, and WinPE retries the mount for ~2
  minutes instead of 30s, so the socket is always gone before the client gives up.
- **proxyDHCP boots now complete when your DHCP server has network boot turned
  off.** iPXE loaded correctly but then chained to `http://<your-router>/boot.ipxe`
  and failed with a permission error, because the embedded chain script trusted
  `${next-server}` — a field the client's own DHCP server owns, and which routers
  commonly stamp with their own address. The script now prefers the address the
  proxyDHCP server actually answered from (`${proxydhcp/next-server}`), falling
  back to `${next-server}` for setups where an external DHCP server points it at
  Beacon deliberately. **Requires a rebuilt/repulled `dnsmasq` image**, since the
  script is embedded in the iPXE binaries at build time.
- **Turning off DHCP no longer silently turns off TFTP.** `enable-tftp` was
  emitted only when the DHCP service was enabled, so disabling "DHCP / proxyDHCP"
  in Server Settings left clients with no TFTP server — breaking exactly the
  setup where an external DHCP server supplies next-server/filename itself. TFTP
  now follows its own service toggle.

## [0.2.1] - 2026-07-19

**Upgrading to this release requires two manual steps.** The self-update fixes
below cannot apply themselves: clicking Apply in a 0.2.0 (or earlier) deployment
runs the *old*, broken updater, which reports success without recreating
anything. Before updating, check `.env` against `.env.example` and add anything
missing — `PROJECT_DIR` (required; the update cannot recreate containers without
it) and `BEACON_TAG` (optional; picks the update channel, defaults to `latest`
when absent). Then update from the host:

```bash
docker compose pull && docker compose up -d
```

Once 0.2.1 is running, the in-app update button works as intended.

### Added

- The Updates panel in Settings now reports what is deployed and which channel
  it came from: the running version (`v0.2.1` on a release, `main (<sha>)` on a
  rolling build, `dev build` when built locally) alongside the active channel.
  Previously an admin could see only whether an update existed, not which build
  they were on or where it would come from.

### Fixed

- Self-update now actually recreates the containers. `docker compose up -d` was
  run from inside the web container, but that command performs the recreation
  in the foreground — so stopping the web container killed the update midway,
  usually before the web service itself was replaced. The recreation is now
  handed to a short-lived container outside the stack, which nothing in the
  stack can take down.
- Self-update no longer reports success before it has done anything. The result
  and the new image digest were written to the database *before* recreation was
  attempted, and that command's output was discarded, so a failed update still
  showed "Update applied successfully. Services are restarting." and left the
  UI reporting "Up to date" while the old images kept running. Success is now
  recorded by the replacement container once it starts, which is the only
  trustworthy confirmation; an update that never replaces it is reported as
  failed after five minutes.
- Self-update fails with an explanation when `PROJECT_DIR` is unset in `.env`
  instead of silently doing nothing — without it the recreation cannot resolve
  the stack's project directory. Deployments that predate `PROJECT_DIR` need to
  add it (see `.env.example`).
- "Update applied successfully. Services are restarting." no longer persists
  indefinitely. The message was written to the database and never cleared, so
  it reappeared on every visit to Settings long after the restart finished —
  and after a later failed update it sat alongside the failure. Successes now
  expire 30 minutes after the update; failures persist (they describe a
  condition that still needs attention) but can be dismissed.

## [0.2.0] - 2026-07-19

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

[Unreleased]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/acebmxer/beacon_pxe/releases/tag/v0.1.0
