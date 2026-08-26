# Beacon

[![Publish images](https://github.com/acebmxer/beacon_pxe/actions/workflows/publish.yml/badge.svg)](https://github.com/acebmxer/beacon_pxe/actions/workflows/publish.yml)
[![Latest release](https://img.shields.io/github/v/release/acebmxer/beacon_pxe)](https://github.com/acebmxer/beacon_pxe/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-hosted PXE/iPXE boot server with a login-protected web console. It
netboots **BIOS and UEFI** clients from a single menu, turns uploaded **ISOs**
into boot entries (Linux, Windows, XCP-NG), and runs as a small Docker Compose
stack.

## Quick start

Prebuilt images are on GHCR, so you only need the compose file and an `.env`:

```bash
mkdir beacon && cd beacon
curl -O https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/.env.example
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env  # else everyone is signed out on each restart
# edit .env: SERVER_IP, BOOT_INTERFACE, ADMIN_PASSWORD (leave blank to auto-generate)
mkdir -p data/drivers
docker compose up -d
```

If you left `ADMIN_PASSWORD` blank, read the generated one from the logs:

```bash
docker compose logs web | grep -i "admin password"
```

Open **http://&lt;server-ip&gt;:8080**, log in, and finish the first-run wizard
(server IP, boot interface, DHCP mode). Cloning the repo instead? `./setup.sh`
does all of the above.

## DHCP modes

Chosen in the wizard, changeable under **Server Settings**:

- **proxyDHCP** (recommended) — runs alongside your existing DHCP server (e.g. a
  router); answers only the PXE/boot part, never hands out IPs.
- **Full DHCP** — Beacon *is* the DHCP server. Use only if nothing else serves
  DHCP on the segment.
- **External DHCP** — your DHCP server points clients at Beacon; Beacon serves
  only the iPXE binaries over TFTP.

## Images

Upload an `.iso` in the web UI and it becomes a boot-menu entry — the kernel and
initrd are extracted automatically. Linux, **Windows** (installer via WinPE),
and **XCP-NG** are supported. Entries can be enabled/disabled and have their boot
args edited.

**Windows** needs SMB ports **139 + 445** reachable, and on Intel VMD / AMD RAID
machines needs a storage driver or Setup shows an empty disk list. Stage storage
*and* NIC drivers on the **Windows Drivers** page — the built-in catalog can
fetch known-good Intel/AMD packs in one click.

Drag entries to set the boot-menu order, and mark one as the **default** — it's
highlighted, flagged with a ★, and is the only thing that starts the countdown
(timeout set under **Server Settings**, `0` disables it). With nothing marked,
the menu waits for a human instead of booting anything on its own.

Details — per-distro boot args, how Windows/XCP-NG boot, driver staging, the
service architecture — are in **[docs/guide.md](docs/guide.md)**.

## Updating

```bash
docker compose pull && docker compose up -d   # update
docker compose down                           # stop (data kept)
docker compose down -v                        # stop + drop unpacked-image volumes
```

The admin UI can self-update too (**Settings → Updates**). `BEACON_TAG` in `.env`
selects the channel: `stable` (releases) or `latest` (rolling `main`).

**Server Settings → Backup & restore** downloads the database (users, settings,
image metadata) as `beacon-backup.db`, and restores one. It is a configuration
snapshot: uploaded ISOs live in `./data` and aren't part of it, so images come
back listed but marked *needs reprocess* until you reprocess them from the ISO.
Restoring shows a preview of every change first, then swaps the database in and
restarts Beacon — see the [guide](docs/guide.md#backup-and-restore) for what it
keeps from the current host and how to undo one. `GET /healthz` is an
unauthenticated liveness probe for monitoring.

> **Updates never touch `docker-compose.yml` or `.env`.** When a release changes
> either, the [CHANGELOG](CHANGELOG.md) says so — re-fetch the file and re-run
> `docker compose up -d`.

## Troubleshooting

- **Client times out fetching the boot file while every container shows `Up`** —
  the host firewall is dropping it. Beacon's services listen below port 1025,
  which firewalld/ufw block by default. Open **69/udp** (TFTP), **67 + 4011/udp**
  (DHCP), **80/tcp** (HTTP), **139 + 445/tcp** (SMB, Windows), and
  **111 / 2049 / 20048** (NFS, live Linux).
- **Windows Setup fails partway (`0x80070035`) on a disk with old partitions** —
  delete them at Setup's disk screen. Standard dirty-disk behaviour, not a Beacon
  bug.

More cases (DHCP conflicts, Xen link-up, NFS, SMB) are in
**[docs/troubleshooting.md](docs/troubleshooting.md)**.

## Accounts and API access

Each user can turn on **two-factor authentication** from **My Profile**: scan the
QR code with any authenticator app (or type the secret in), confirm a code, and
logins then require the 6-digit code as well as the password. Disabling it asks
for the account password.

**My Profile** also issues a Bearer token for scripts, so automation doesn't need
a browser session or a stored password:

```bash
curl -H "Authorization: Bearer $TOKEN" http://<server-ip>:8080/api/events
```

The token carries its account's role and can be revoked or regenerated at any
time. See **[docs/guide.md](docs/guide.md)** for the API endpoints.

## Security

The UI is HTTP on port 8080 — put it behind a TLS reverse proxy for anything past
a trusted LAN. The `web` and `reload` containers mount the Docker socket (for
self-update and the dnsmasq reload), which is **root-equivalent on the host** —
an accepted trade-off; remove the mounts to opt out. Passwords must be ≥ 12
characters, login is rate-limited per client IP (the lockout is persisted, so
restarting the stack doesn't clear it), and accounts can require a TOTP second
factor.

## License

Beacon's code is [MIT](LICENSE). Bundled components (dnsmasq, samba, iPXE,
wimboot, …) keep their own licenses — see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). Contributions welcome; add
user-facing changes to the **Unreleased** section of [CHANGELOG.md](CHANGELOG.md).
