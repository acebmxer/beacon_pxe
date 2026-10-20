# Changelog

All notable changes to Beacon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe user-facing changes: what a Beacon operator gains, loses, or has
to do differently. Internal refactors that change nothing observable are omitted.

## [Unreleased]

### Security

- **NFS and SMB only answer the boot network.** The NFS server exported the
  unpacked images to every address and Samba listened on every interface. NFS
  now exports only to the subnet of the **Boot interface** in Server Settings,
  and Samba listens only on that interface; both follow a change to it within
  seconds. This needs the current `docker-compose.yml`, which shares the
  dnsmasq config with `nfs` and `smb`; with an older one both work as before
  and say so in their logs.

- **One fewer container holds the Docker socket.** The `reload` container had
  full Docker access only to restart dnsmasq after a settings change; dnsmasq
  now restarts itself, and `reload` is gone from `docker-compose.yml`. A
  compose file that still lists it keeps working. `web` keeps the socket for
  self-update and restore, and the compose file now says why a socket proxy
  wouldn't make that safer.

- **The Windows boot helper is checked before it goes into the image.** Every
  Windows PXE client runs wimboot, which the web image downloads when it is
  built, and nothing checked that the file was the real release. Its SHA-256 is
  now pinned, so a different file stops the build. The web image also no longer
  carries the test suite.

- **The two-factor code can no longer be guessed by trying them all.** After
  the password, the 6-digit code could be entered wrong any number of times, so
  anyone with a password could keep trying until a code was accepted. Five wrong
  codes in a row now lock that account's two-factor step for 15 minutes. Someone
  who already has the password can use this to keep the account's owner out for
  those 15 minutes.

- **A few open Images pages can no longer freeze the console.** Each live status
  stream on the Images page kept a database connection for as long as it was
  open, so about 14 of them, from any signed-in user, stopped every page from
  loading until they were closed. A stream now holds no connection, and each
  user can have six open at once; past that the page falls back to checking
  every few seconds.

- **Links inside an unpacked ISO can't point the boot server elsewhere.** An
  installer ISO unpacked for HTTP could carry a link such as `x -> /etc/passwd`,
  and nginx served the file it pointed to. Links that lead outside the unpacked
  ISO are now removed; links inside it (Debian and Ubuntu ISOs have one) stay.

- **An uploaded ISO can no longer run commands on the Beacon host.** A file
  inside the ISO whose name began with `-` was read by bsdtar as an option, and
  one option runs a program. With the web container's Docker access that meant
  control of the host. File names from an ISO are now always treated as names.

- **Pages on other ports of the same machine can no longer act as you.** The
  session cookie stops other websites, but not another service on the same host
  (nginx on port 80, or anything else on the box). A form there could create an
  admin account in Beacon while you were signed in. Beacon now refuses form
  posts that don't come from its own page; scripts using an API token are
  unaffected. Behind a reverse proxy, the proxy must pass on the original Host
  header (or send X-Forwarded-Host from an address in `TRUSTED_PROXIES`), or
  saving a form returns 403; see `.env.example`.

- **Anonymous uploads are refused before they are read.** An upload sent without
  signing in was saved to disk before the sign-in check ran, so anyone who could
  reach the console could fill its disk.

- **A doctored backup can no longer slip settings past Beacon's checks.**
  Restoring a backup took its settings and image records as they were: a line
  break could add directives to dnsmasq's config or commands to the boot menu,
  an image's file name could point Delete at any file, and database triggers in
  the file stayed in the live database. Values written to dnsmasq.conf and
  boot.ipxe are cleaned every time, image files are only ever looked for in the
  images folder, and a backup containing triggers or views is refused.

- **Image names and boot arguments can't add boot menu commands.** iPXE treats
  the words `;`, `&&` and `||` as the start of a new command, so a name such as
  `Foo ; chain http://…` (typed, or from an imported catalog entry) ran when
  the menu loaded. Those words are left out of the menu now, `${…}` in a name
  or the menu title is shown as text, and a word starting with `#` loses the `#`.

### Added

- **The netboot catalog shows each release's newest build.** The **Release** list
  of **Add from a netboot catalog** names the build each distro publishes now,
  read from its mirror, so a point release shows up without a Beacon update:
  "Ubuntu 26.04 LTS — 26.04.1", "Debian 13 (trixie) — 13.7", "Rocky Linux 10 —
  10.2", Alpine's and AlmaLinux's point releases, XCP-ng's, Arch's monthly
  release, and the current Tumbleweed snapshot, CentOS Stream compose and Kali
  build. Adding the image downloads that build. The list is read again every
  six hours; if a mirror is out of reach the release is listed without it.

- **Several images can be reprocessed or deleted at once.** The Images table has
  a tick box on each row, and one in the header for every image; **Reprocess
  selected** and **Delete selected** act on the ticked images after asking. They
  are reprocessed one after another, and an image still processing is left alone.

- **The netboot catalog has the main distros beside Debian.** **Add from a netboot
  catalog** now lists Ubuntu, Fedora, Rocky Linux, AlmaLinux, CentOS Stream,
  openSUSE Leap, openSUSE Tumbleweed, Linux Mint, Kali Linux, Arch Linux, Alpine
  Linux and XCP-ng, each with its installer and live desktops where the distro publishes
  them. **Desktop** lists each distro's flavours: Ubuntu, Kubuntu, Xubuntu,
  Lubuntu, Ubuntu MATE, Budgie, Cinnamon, Unity, Studio, Edubuntu and Kylin;
  Fedora Workstation, KDE Plasma and the spins; the Rocky, Alma and CentOS Stream
  live images; Tumbleweed's GNOME, KDE Plasma and Xfce; Linux Mint's Cinnamon, MATE
  and Xfce; and, under Arch Linux, EndeavourOS and CachyOS. A desktop published only for some releases is listed for those.
  Installers keep only their kernel and initrd where the distro's installer loads
  the rest from the mirror (Fedora, Tumbleweed, Leap 15.6, Ubuntu Server, which
  downloads its live-server ISO into the client's RAM, and Kali), and also keep
  the installer's own system where the mirror's copy can change under it (Rocky,
  Alma and CentOS Stream's runtime image, Leap 16's Agama system, Arch's root
  filesystem, Alpine's modloop). XCP-ng's installer is Xen, its dom0 kernel and
  `install.img` from XCP-ng's netinstall folder, booted through the same UEFI GRUB
  chainloader as an uploaded XCP-ng ISO. Fedora, Rocky, Alma, CentOS Stream, Tumbleweed,
  EndeavourOS and CachyOS live desktops stream their root filesystem over HTTP like
  Debian's;
  Ubuntu's and Linux Mint's boot with casper, which mounts its root over NFS only,
  so their ISO is unpacked into the NFS share and then deleted. Release lists are
  read from the mirrors for Ubuntu (default: the newest LTS), Fedora, Rocky, Alma,
  CentOS Stream, Linux Mint, Alpine and XCP-ng, with end-of-life dates from Ubuntu's
  distro-info-data, Fedora's Bodhi and Alpine's release list.

  Pre-releases are listed for every distro that publishes them, marked
  "pre-release" at the top of the **Release** list, not only Debian's testing
  release: Ubuntu's beta before its release day, Fedora's Beta, Rocky Linux's and
  AlmaLinux's betas (from Rocky's staging mirror and Alma's vault, filled in when
  picked), openSUSE Leap's release candidate, Linux Mint's beta, Kali's
  `kali-dev` and Alpine's `edge`. A beta the distro has since released is left
  out. **Offer pre-releases** under **Settings → Netboot catalog** (on by
  default) hides them and refuses adding one when turned off; images already
  added are kept. A custom entry's release can be marked `"prerelease": true`.

  A catalog download that stalls or drops part-way is resumed where it stopped,
  up to four tries, instead of failing with "Download timed out" and starting
  the whole multi-GB ISO again on Retry. A mirror that answers the resume from
  the wrong place in the file is asked for the whole file again, instead of the
  two pieces being joined into a broken ISO.

- **Custom netboot catalog entries add installers for other distros.** **Custom
  catalog entries** on the Images page takes a name, a default mirror, the boot
  files (a netboot archive and the kernel and initrd paths inside it, or a kernel
  URL and an initrd URL such as Fedora's `images/pxeboot/`), kernel arguments and
  a list of releases, using `{mirror}` and `{version}` placeholders. **Preview**
  shows every release's download URLs, checked on the mirror, and the kernel
  command line the boot menu will carry, and Save is offered only after it; an
  unknown placeholder or a URL that is not http(s) is refused. **Import from a
  file** takes entries in the built-in catalog's JSON format, previews them all,
  and imports nothing if one is wrong. Entries can be edited and deleted, appear
  in **Add from a netboot catalog** marked "(custom)", and are kept in the
  database, so a backup carries them. A downloaded kernel that is not a Linux
  kernel (an error page, say) is refused.

  An installer from a custom entry whose kernel arguments name `inst.repo=` or
  `inst.stage2=` (Fedora's) was marked "needs reprocess" at every start, because
  the startup check looked for an unpacked ISO tree that a netboot image never
  has; it now stays ready.

- **Debian installers and live desktops can be added without keeping an ISO.** **Add from a netboot
  catalog** on the Images page takes a release (`14`, `trixie`, `bookworm`, or any
  other name the mirror carries) and a mirror (default `deb.debian.org`), then
  downloads Debian's netboot archive and keeps only its kernel and initrd. The
  client's installer fetches its packages from the mirror, so nothing is stored
  per image beyond the roughly 55 MB archive, which stays so **Reprocess** can
  rebuild the boot files. The archive can also be fetched from another URL or
  uploaded, for a Beacon host with no internet.

  **Live desktop** does the same for Debian's live desktops (KDE Plasma, GNOME,
  Xfce, Cinnamon, MATE, LXQt, LXDE, Standard): Beacon downloads the live ISO once,
  keeps the kernel, initrd and root filesystem, and deletes the ISO. Reprocess
  downloads it again. **Release** is a list on both Installer and Live desktop,
  read from Debian's mirror (its stable, oldstable, oldoldstable and testing
  suites), so a new Debian release appears without a Beacon update; today that is
  Debian 14 (forky, testing, a pre-release), 13 (trixie), 12 (bookworm) and 11
  (bullseye). The list is refreshed every 6 hours, and the catalog's own list is
  shown when the mirror is out of reach. A release is marked "end of life" once
  Debian's long-term support for it has ended, using the dates Debian publishes
  (distro-info-data); it stays selectable, and a release the mirror has dropped
  stays listed if the catalog names it. **Offer end-of-life releases** under
  **Settings → Netboot catalog** (on by default) hides them from every distro's
  Release list and refuses adding one when turned off; images already added are
  kept. An installer image's default name carries the
  version number as well as the codename ("Debian 11 bullseye (netboot)").

  A live desktop whose Mirror folder holds a different release than the one
  picked (for example Debian 13's folder with Debian 14 chosen) is refused with a
  message, instead of being added under the wrong name. An ISO uploaded as an installer's
  archive file is refused with a message saying so, instead of an unpacking error. An
  installer archive whose own version file names a different release than the
  one picked (bookworm's archive added as trixie) is refused, since its kernel
  would not match the packages the installer fetches. Testing and unstable
  archives are exempt, as Debian's own version file in them names the previous
  release. **Other** takes a number (`12`, `15`) or a codename
  (`bookworm`, `duke`) on both. A release that is not in the list is found on
  Debian's own mirror (its stable, oldstable and testing files give each
  version and codename), so a new release works before it is added to the list;
  Live desktop then fetches its ISO from Debian's current, archive or weekly
  testing folder. Installer also takes any other release name the mirror carries.
  A live image is named after its release, e.g. "Debian 12 Live KDE Plasma".

### Changed

- **Passwords are checked with bcrypt directly.** passlib, which did this
  before, is no longer maintained and held back bcrypt and Python updates.
  Existing passwords keep working; nothing to do.

### Fixed

- **Fedora and RHEL installer ISOs no longer need a lot of client RAM.** Over
  HTTP the installer copied `images/install.img` into memory, which needs about
  twice its size: Fedora Kinoite's 3.8 GB one powered off a 6 GB client part-way.
  These ISOs are now unpacked into the NFS share and the installer reads the file
  from it, as from a disc. Clients need the NFS ports (111, 2049, 20048) open, as
  for the live desktops. Installer images added before this keep booting over
  HTTP; **Reprocess** moves them to NFS.

  A restart no longer takes Fedora 44 installer images out of the boot menu
  as "Boot files are missing". The startup check looked for a `.treeinfo`
  file, which the Fedora 44 netinstall and Kinoite ISOs don't have; it now
  checks for `images/install.img`, the file the installer loads.

- **Deleting an image that is still processing no longer leaves files
  behind.** The run carried on after the Delete, writing boot files (and
  downloading the live ISO again) that no image owned. The image now shows
  "deleting" until the run stops at its next step, then it goes with all its
  files. Pressing Delete again removes an image that stays on "deleting".

- **A double click on a button that asks first no longer asks twice.** Delete,
  Delete selected, Reprocess selected and the other buttons with an "Are you
  sure?" question asked again on the second click, and sent the request
  twice.

- **Importing a badly nested catalog file shows an error instead of crashing.**
  A JSON file nested thousands of levels deep made **Import from a file** fail
  with a server error; it now says the file is not valid JSON. The import's
  Save step also keeps the 256 KB limit the file upload has.

- **Click-to-select works on the Profile page.** Clicking the two-factor secret
  key or the API token was meant to select it for copying, but the browser
  blocked it, so nothing happened.

- **Images no longer get stuck on "processing" until a restart.** An
  unexpected failure while unpacking, such as a truncated download, a full disk
  or a missing tool, left the image on "processing", where Reprocess skips it.
  The image is now marked failed with the reason, so it can be retried.

- **Reprocess on an image that is still processing leaves it alone.** It started
  a second run that wiped the files the first one was writing; the image's own
  Reprocess now follows the same rule as Reprocess selected and says so.

  The same goes for an image waiting for its run to start, so pressing
  Reprocess twice in a row queues one run, not two. An image left waiting by a
  restart is marked "needs reprocess" at startup, as one left processing is.

- **Uploading an ISO with a name already in use keeps both.** The second upload
  replaced the first image's ISO, and deleting either image removed the other's.
  The new one is now saved as `name-2.iso`, `name-3.iso`, and so on.

- **`setup.sh` finishes a fresh install again.** It stopped straight after
  creating `.env`, leaving the admin password and session secret blank and the
  stack not started; running it again kept the blank values. It now fills both
  in and starts the stack, and `.env` is readable only by its owner, since it
  holds those secrets.

- **Top deployed images keeps the newest images when counts tie.** The list
  shows five, and images booted the same number of times were cut in no fixed
  order, so a just-booted image could be missing while an older one stayed.

- **The Server Settings icon in the sidebar is the same size as the others.** It
  was drawn as a small text symbol instead of an emoji.

## [0.7.2] - 2026-10-17

**Beta updates can be switched on from Settings, and betas are now numbered.**
`docker-compose.yml` needs no change and no action is needed on upgrade.

### Added

- **Beta updates can be turned on from Settings → Updates.** **Include beta
  versions** moves the update to the `beta` channel without editing `.env`;
  turning it off returns to the `BEACON_TAG` channel at the next update (an older
  build). Betas are published by hand (Actions → Publish images → Run workflow
  on `dev`) as numbered pre-releases, `v0.7.2-beta.1`, `-beta.2`, and so on, and
  the running version shows that number; pushing to `dev` no longer publishes
  one. A plain `docker compose up -d` on the host still uses `BEACON_TAG` from
  `.env`.

  The **Include beta versions** checkbox sits next to its label, in the same
  layout as the other checkboxes on the page, instead of being pushed to the
  opposite side of the card.

## [0.7.1] - 2026-10-16

**Fedora and RHEL netinstall ISOs boot again, and the dashboard copes with very
large client counts.** `docker-compose.yml` needs no change. **Reprocess** any
existing Fedora/RHEL image after upgrading.

### Fixed

- **Fedora and RHEL netinstall ISOs now boot.** The installer was pointed at the
  ISO file, so it failed with 404s for `.treeinfo` and `install.img`. The ISO is
  now unpacked into the boot root and served over HTTP. A netinstall ISO has no
  packages, so it is used as the installer only and the client installs from the
  distro's online mirrors (it needs internet access); a full DVD is also used as
  the package repo. **Reprocess** existing Fedora/RHEL images to apply this.

- **Clients keep appearing on the dashboard on a very busy or abused network.**
  Once 10,000 different clients had loaded the boot menu, new ones were ignored
  until **Reset statistics**; now the least-recently-seen client is dropped to
  make room.

## [0.7.0] - 2026-10-12

**A `beta` update channel, and clients show on the dashboard as soon as they load the
boot menu.** `docker-compose.yml` needs no change and no action is needed on upgrade
— the new table is created automatically on start.

### Added

- **A `beta` update channel that tracks the `dev` branch.** Set `BEACON_TAG=beta`
  in `.env` to run the newest, not-yet-released changes (pre-release; may be
  unfinished). `stable` (releases) and `latest` (`main`) work as before.
- **Clients appear on the dashboard as soon as they load the boot menu.** The
  menu now reports in once when it loads, so a client behind an external DHCP
  server shows under **Recent clients** without having to pick an image first.
  Menu loads are not counted as deployments, and **Reset statistics** clears
  them along with the rest.

## [0.6.0] - 2026-08-25

**Accounts gain a second factor and API tokens, the boot menu gains ordering and
an explicit default, and the database can be backed up and restored.**
`docker-compose.yml` is unchanged and no action is needed on upgrade — existing
databases pick up the new user and image columns automatically on start.

### Added

- **Two-factor authentication (TOTP).** Each user can enable an authenticator
  app from **My Profile**: Beacon shows a QR code to scan (plus the secret in
  text for manual entry), and activation requires a valid code, so a mistyped
  secret can't lock you out. Once enabled, login asks for the 6-digit code on a
  second page — the password alone grants no access to any page until the code
  is confirmed. Codes from the adjacent 30-second window are accepted, so one
  read just as the code rolls over still works. Disabling 2FA requires the
  account password.
- **API tokens for scripted access.** **My Profile** issues an opaque Bearer
  token that authenticates requests without a browser session
  (`Authorization: Bearer <token>`), and revokes or regenerates it on demand.
  One token per account, and it authenticates as that account with its role.
- **A boot menu you control the shape of.** Images can be dragged into an
  explicit order on the **Images** page, and one image can be marked the
  default. The marked image is highlighted, flagged with a ★ in the menu, and
  is the only thing that starts the countdown — with nothing marked, the menu
  highlights the first entry but **waits for a human indefinitely rather than
  booting anything on its own**. The countdown length is configurable under
  **Server Settings → Appearance & boot menu** (`0` disables it).
- **Database backup and restore.** **Server Settings → Backup & restore**
  downloads the whole SQLite database as `beacon-backup.db`, copied to a temp
  file first so the download is consistent even if writes land mid-transfer —
  and takes one back. Uploading a backup shows a **preview of every change**
  (settings, accounts, and how each host setting was decided) before anything is
  touched; confirming swaps the database in, saves the replaced one as
  `data/pxe.db.pre-restore`, and restarts Beacon. A file that isn't a SQLite
  database, fails an integrity check, isn't a Beacon backup, or holds no admin
  account is refused rather than applied.
- **A restore keeps this host's identity.** `server_ip` and `boot_interface`
  describe the machine rather than your preferences, so they are checked against
  the host: an interface must exist, and an IP must be one the host answers to. A
  backup from other hardware — a replacement server on a different base OS, where
  the NIC is called something else — keeps the current values instead of
  restoring settings that would silently break PXE, and the preview says which
  and why. Same IP on the replacement means the backup's value is used as-is.
  Update bookkeeping (`update_*`) is dropped rather than restored, so a backup
  taken mid-update can't leave the UI stuck on "Update in progress".
- **Deployment history and stats.** The dashboard reports total deploys,
  distinct clients ever served, and the top five images, folding per-boot
  records into the log-derived client table so a client's IP and last-seen time
  are right in proxyDHCP mode too. An admin can reset the all-time counters.
  `GET /api/events` exposes the same boot records for external tooling, with
  `image_id`, `since`, and `limit` filters.
- **`GET /healthz`** — an unauthenticated liveness probe that also checks the
  database is reachable, for monitoring and container health checks.
- **The web UI works on a phone.** A hamburger menu collapses the sidebar on
  small screens, and long-running image work now raises a toast instead of
  requiring you to watch the page.
- **A test suite** (`web/tests`, pytest) covering auth, the login throttle,
  iPXE menu rendering, backup validation and restore decisions, and route
  access control.

### Changed

- **iPXE is now built from a pinned commit** rather than whatever `master`
  happened to be on the day you rebuilt. The pin is the revision verified to
  boot both a UEFI VM and a physical laptop. Move it deliberately with
  `--build-arg IPXE_REF=<ref>` once you've tested a client boot.
- **NFS `rpc.mountd` now binds a fixed port** (`20048`, override with
  `MOUNTD_PORT`) instead of a different ephemeral port on every restart, which
  makes it possible to put the NFS service behind a host firewall. With one,
  open `111/tcp+udp`, `2049/tcp`, and `20048/tcp` on the boot interface.
- **Logs are emitted as one JSON object per line**, so they can be shipped and
  queried rather than only read by eye.

### Security

- **The failed-login lockout now survives a restart.** It was in-memory, so
  restarting the container cleared the throttle and handed an attacker a fresh
  budget of attempts; it is now persisted per client IP in the database.

## [0.5.0] - 2026-08-24

**Security hardening.** No action is needed for a normal trusted-LAN install and
`docker-compose.yml` is unchanged. If you serve Beacon over **HTTPS behind a
reverse proxy**, add two optional settings to your `.env` (both shown in
`.env.example`): `SESSION_SECURE=true` marks the session cookie Secure, and
`TRUSTED_PROXIES=<proxy ip/cidr>` lets the login throttle see the real client
through the proxy. Existing databases are migrated automatically on start.

### Added

- **Laptop touchpads now work in Windows Setup.** Modern trackpads are
  HID-over-I2C: WinPE carries the touchpad driver but no driver for the I2C/GPIO
  controllers underneath it, so until now only an external USB mouse worked
  during an interactive install (the keyboard is unaffected — it's PS/2). Two
  new one-click packs on the **Windows Drivers** page supply the missing bus
  drivers, sha256-pinned from the Windows Update CDN like the rest of the
  catalog: **Intel Serial IO** (one CAB per generation, 6th-gen Core through
  Meteor/Arrow/Lunar Lake) and **AMD I2C/GPIO** (all Ryzen laptop generations).
  Setup PnP-loads only the driver matching the machine, the touchpad comes
  alive at the first Setup screen, and like all `share` drivers it applies to
  every Windows image on the next boot — no re-processing.

### Fixed

- **Windows 11 24H2-and-later installs on VMD/RAID machines no longer bugcheck
  `INACCESSIBLE_BOOT_DEVICE` on first boot.** Starting with 24H2 (25H2 media
  behaves identically; the fix was validated on 25H2), the root `setup.exe`
  became a stub for a new Setup flow that loads a staged storage
  driver into WinPE — the disk list fills in — but never copies it into the
  OS it installs (`DriverCopy: No drivers.` in its own logs), so the finished
  install can't see its own disk. WinPE now launches the classic
  `sources\setup.exe`, which still reflects `DriverPaths` drivers into the
  installed OS; pre-24H2 media chained the stub to it anyway, so those
  installs are unchanged. Two knock-on fixes ride along: the answer file
  carries an explicitly empty `<ProductKey>` (the classic Setup requires the
  element on multi-edition media and refuses to start without it), and
  `/copylogs` is only passed to the new-flow stub (the classic Setup rejects
  the option). Visible change: Windows Setup shows the classic (pre-24H2)
  UI on all media.
- **The `latest`-channel web container crash-looped on startup** after a
  dependency bump to bcrypt 5.0, which is incompatible with passlib and made
  password hashing fail before the app could bind. bcrypt is pinned back to
  4.0.x and the automated bumper now knows to leave it there. Tagged releases
  (v0.4.0 and earlier) were never affected — if you follow `latest`, pull
  again to pick up the repaired image.
- **Updating manually on the host no longer confuses the Updates panel.** The
  update check used to compare GHCR against a digest it remembered from its
  own last apply, so a `docker compose pull && up -d` run by hand (or a
  channel switch) left it insisting an update was available when none was;
  pressing **Apply update** then found nothing to recreate, waited five
  minutes for a restart that could never come, and reported `recreate_failed`.
  The check now reads what is actually deployed from the running container,
  the status page retracts a stale "Update available" badge on its own, and
  Apply recognizes the already-current case and reports success immediately.
  As a bonus, Apply now properly finishes a half-done manual update (images
  pulled but containers never recreated), and a failed recreation reports the
  updater's actual error output instead of a generic timeout.

### Security

- **The session cookie can be marked `Secure`** (`SESSION_SECURE=true`) so the
  admin session is never transmitted over plain HTTP when Beacon is fronted by
  an HTTPS reverse proxy.
- **A reverse proxy can no longer collapse the login lockout onto one address.**
  The failed-login throttle now identifies the real client via `TRUSTED_PROXIES`
  instead of keying every request to the proxy — previously a few failures from
  anyone could lock out the admin.
- **Changing a password now signs out that account's other sessions.** A reset
  no longer leaves an old or stolen session valid until it expires.
- **Content-Security-Policy now uses a per-request script nonce** instead of
  permitting all inline scripts, so injected markup cannot execute script.
- **Sign-out is now a POST**, so a cross-site page can't silently log an admin
  out.
- **The boot-tracking endpoint (`/track`) is rate-limited** and records only
  real, enabled images, so it can't be flooded to grow the database.
- **The optional diagnostics capture share writes as an unprivileged user**
  rather than root.
- **Published images now build from commit-pinned GitHub Actions**, closing a
  supply-chain vector in how the images are produced.

## [0.4.0] - 2026-07-25

**This release changes `docker-compose.yml`** (new `./data/drivers` bind mount
for Windows drivers, and an optional diagnostics-capture mount). Re-fetch it:

```bash
curl -O https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/docker-compose.yml
mkdir -p data/drivers
docker compose up -d
```

### Added

- **Windows storage drivers, fixing installs that stop at an empty disk list.**
  Stock Windows 11 WinPE has no driver for Intel VMD / RST or AMD RAID, so on
  hardware that enables those in firmware (most laptops from Intel's 11th gen
  on) Setup shows no disk to install to. Stage the vendor's F6 package on the
  new admin **Windows Drivers** page — upload a `.zip` or loose `.inf`/`.sys`/
  `.cat` files, or drop it into `./data/drivers` — and Setup PnP-injects the
  matching driver so the disk appears *and* the installed OS boots (without it,
  a VMD/RAID machine bugchecks `INACCESSIBLE_BOOT_DEVICE` / 0x7B on first boot).
  Mixing vendors or versions is safe; it applies to every Windows image on the
  next boot with no reprocessing. Firmware `AHCI` mode is the per-machine
  alternative — see [docs/guide.md](docs/guide.md).

- **One-click driver fetch.** The Windows Drivers page can pull known-good WHQL
  packs — Intel RST VMD, AMD RAID, and an Intel NIC pack — straight from
  Microsoft's Windows Update CDN, sha256-verified against a pin in the release.
  Nothing is bundled or downloaded until an admin clicks Fetch; a file that no
  longer matches its hash is refused. Needs internet from the Beacon host;
  offline sites upload instead.

- **Network drivers baked into `boot.wim`, for NICs stock WinPE can't see.**
  Symptom is WinPE mounting the install share failing with "System error 1231"
  (Intel I225/I226 2.5GbE is the usual culprit). A NIC driver can't come over
  the network it's needed to reach, so packs staged with kind **Network** (or
  the catalog's Intel NIC pack) are baked into every Windows image and loaded
  before networking starts. Changes re-bake all ready images automatically.

- **Optional Windows Setup log capture** for diagnosing a failed install: set
  `ENABLE_DIAG_CAPTURE=true` and WinPE drops Setup's logs into `./data/capture`
  on the host. Off by default (it is a guest-writable SMB share).

### Fixed

- **The dashboard's "Recent clients" updates on every boot instead of
  freezing.** "Last seen" and the boot IP came only from parsing dnsmasq's DHCP
  log, but in proxyDHCP mode a re-boot often shows up only as TFTP with no
  MAC-bearing line, so a row could sit at "1h ago" with a blank IP. It now also
  folds in each `BootEvent` (MAC + IP + timestamp), so every boot refreshes the
  row.

### Documentation

- The README is trimmed to the essentials; the full reference now lives in
  [docs/guide.md](docs/guide.md) and [docs/troubleshooting.md](docs/troubleshooting.md).

## [0.3.0] - 2026-07-19

**This release changes `docker-compose.yml`.** The in-app update button and
`docker compose pull` only update container images — they do not touch your local
compose file — so the container hardening below arrives only if you re-fetch it:

```bash
curl -O https://raw.githubusercontent.com/acebmxer/beacon_pxe/main/docker-compose.yml
docker compose up -d
```

Everything else ships inside the `web` image and lands with a normal update.

### Changed

- **The web app's framework and dependencies were upgraded to clear known
  security advisories.** FastAPI 0.115 → 0.139 (which pulls in Starlette 1.3),
  along with Jinja2, python-multipart and others, resolving a batch of published
  CVEs — including unauthenticated denial-of-service vectors in the form and
  file-upload parsers that front the login page. No configuration changes are
  required: the new versions ship inside the `web` image and arrive with a normal
  update.

### Fixed

- **The dashboard's network graph now shows the whole machine's traffic**
  instead of only the web container's own. Deploying an image lit up the disk
  graph but left the network graph nearly flat, showing just the few KB/s of
  dashboard polling — the image data pushed by the `nginx`, `nfs`, and `smb`
  services never appeared. The web container reads the host's `/proc`, but
  `/proc/net` is per-network-namespace, so those counters still came from the
  container's own veth; disk was unaffected because `/proc/diskstats` is not
  namespaced. Beacon now reads the host init process's interface counters, so
  the network graph tracks a deployment the way the disk graph already did.

### Security

- **The login page is now rate-limited and no longer reveals which usernames
  exist.** Five failed attempts from one client IP trigger a 15-minute lockout,
  during which even the correct password is refused, so an exposed instance can't
  be brute-forced at speed. A failed login now also takes the same time whether
  or not the account exists — previously a real username was measurably slower
  (it ran the password hash) while a missing one returned almost instantly, which
  let an attacker enumerate valid accounts. The lockout is in-memory per `web`
  process, so restarting the container clears it.

- **Passwords must now be at least 12 characters** (and at most 72 bytes, past
  which bcrypt silently ignores the rest). This applies when creating a user,
  resetting a user's password, or changing your own; existing passwords keep
  working until they are next changed.

- **The UI sends security headers and escapes image names correctly.** Every
  response now carries a Content-Security-Policy plus `X-Frame-Options`,
  `X-Content-Type-Options`, and `Referrer-Policy` — the framing headers mean the
  console can no longer be embedded in an `<iframe>` on another site. Separately,
  an image name containing a single quote could previously break out of an
  attribute in the dashboard's "top images" list and inject markup; names are now
  fully escaped.

- **Settings and boot arguments can no longer inject extra configuration.**
  Values that get written into the generated `dnsmasq.conf` and `boot.ipxe` (the
  DHCP DNS field, per-image boot args, and so on) are stripped of newlines and
  other control characters, so a crafted value cannot append its own directives
  or iPXE commands to those files.

- **The containers are hardened against privilege escalation.** Every service now
  runs with `no-new-privileges`, and the `reload` sidecar additionally drops all
  Linux capabilities. Because these settings live in `docker-compose.yml`, the
  in-app update button does not apply them — it only pulls new images. To pick
  them up, re-fetch `docker-compose.yml` and run `docker compose up -d`.

## [0.2.3] - 2026-07-19

### Added

- **The running version and update channel now show in the topbar** on every
  page, left of the light/dark toggle — e.g. `Running v0.2.2 · latest channel`.
  Previously this only appeared in Settings → Updates, which is admin-only; the
  topbar shows it to every user. Locally built images read `Running dev build`
  rather than a version number.

### Changed

- **The `dnsmasq`, `nfs`, and `smb` images now build on Alpine 3.24** instead of
  Alpine 3.20, which reached end of life in April 2026 and had stopped receiving
  security updates. The bundled daemons move with it: Samba 4.19.9 → 4.23.8
  (4.19 was also past upstream end of life), dnsmasq 2.90 → 2.92, and nfs-utils
  to a newer 2.6.4 revision. Nothing in Beacon's configuration changes, but
  operators serving Windows install media over SMB should re-test a WinPE boot
  after updating, since the Samba jump spans four minor releases.

## [0.2.2] - 2026-07-19

**Two of the fixes below ship inside images other than `web`, so the in-app
update button is not enough on its own** — it pulls every image, but a deployment
that updates by any other means (or that rebuilt only the web image) will not
pick them up. The proxyDHCP chain fix is embedded in the iPXE binaries at build
time (`dnsmasq` image) and the SMB keepalive fix is baked into `smb.conf` (`smb`
image). Update the whole stack:

```bash
docker compose pull && docker compose up -d
```

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
- **Images whose boot files have been deleted no longer appear bootable.**
  `status` recorded how the last extraction went and was never rechecked against
  the disk, so an image stayed `ready` after the files that extraction produced
  were gone. `docker compose down -v` is what does this: it destroys the
  `bootroot`, `nfsroot`, and `smbroot` volumes holding every unpacked image,
  while the database — a bind mount — survives with every row still marked
  ready. Those images were listed in the boot menu and failed at the client with
  nothing pointing at the real cause. Beacon now verifies each ready image's
  files at startup and marks anything missing **needs reprocess**, which holds it
  out of the boot menu and offers a Reprocess button; the uploaded ISO is
  untouched by `-v`, so reprocessing rebuilds everything. Ordinary updates were
  never affected — `pull && up -d` and the update button leave volumes alone.
- **An image whose extraction was interrupted no longer sits at "processing"
  forever.** Extraction runs as a background task, so restarting the web
  container while one is in flight left the row marked processing with nothing
  running, and the images page polled that state indefinitely. Interrupted
  extractions are now marked needs reprocess at startup.

### Documentation

- The Updates panel warns when the running images were built locally
  (`docker-compose.dev.yml`), and the Apply confirmation says so explicitly.
  Applying an update has only one path — pull the published images for the
  configured channel and recreate the stack — so on a locally built deployment
  it silently reverts to the published build, which reads as a verified fix
  spontaneously regressing. Also documented in the README's build-from-source
  section.
- **Corrected README and in-app text that described how Windows images boot
  before 0.2.0.** Both claimed iPXE mounted the ISO as a virtual CD (`sanhook`)
  and that **no SMB share was required** — the reverse of what Beacon has done
  since 0.2.0, and directly contradicted by the same README's troubleshooting
  entry telling operators to open 139/445. Anyone sizing disk or writing
  firewall rules from that paragraph got it wrong. The XCP-NG description was
  similarly stale (it claimed iPXE chains the Xen files directly, when Beacon
  builds a standalone GRUB EFI per image because iPXE can't multiboot under
  UEFI), and the advice to clear the boot-args field before reprocessing no
  longer applies — a reprocess re-derives those args unconditionally.
- README additions for things shipped in 0.2.0 but never documented there: the
  `smb` service in the architecture table, the HTTP single-file boot path used
  by Fedora 42+ live and Archiso (including Archiso's ~8 GB client-RAM
  requirement), the third DHCP mode in the feature list, and the `7z` fallback
  for UDF ISOs. Also dropped a stray `--build` from the documented update
  command, which does nothing against the prebuilt compose file.

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

[Unreleased]: https://github.com/acebmxer/beacon_pxe/compare/v0.7.2...HEAD
[0.7.2]: https://github.com/acebmxer/beacon_pxe/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/acebmxer/beacon_pxe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/acebmxer/beacon_pxe/releases/tag/v0.1.0
