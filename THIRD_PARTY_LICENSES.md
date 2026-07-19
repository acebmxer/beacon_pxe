# Third-party software & licenses

Beacon's own source code is released under the [MIT License](LICENSE).

Beacon does **not** vendor third-party source into this repository. Instead, the
published Docker images install upstream packages and build a couple of tools
from source at image-build time. Those components keep their own licenses, which
are listed here for transparency and to satisfy the redistribution-notice
obligations of the copyleft (GPL/LGPL) components bundled in the images.

The MIT license covering Beacon's own code does **not** extend to these
components — each is governed by its own license, linked below.

## Python dependencies (the `web` image)

Installed from PyPI per [`web/requirements.txt`](web/requirements.txt):

| Package          | License        |
|------------------|----------------|
| fastapi          | MIT            |
| uvicorn          | BSD-3-Clause   |
| jinja2           | BSD-3-Clause   |
| sqlalchemy       | MIT            |
| passlib          | BSD-2-Clause   |
| bcrypt           | Apache-2.0     |
| python-multipart | Apache-2.0     |
| itsdangerous     | BSD-3-Clause   |

## Base images

| Image                   | Upstream license                                  |
|-------------------------|---------------------------------------------------|
| `python:3.12-slim`      | PSF (Python) + Debian package licenses            |
| `debian:bookworm-slim`  | Mixed (mostly GPL/LGPL/BSD/MIT) per Debian        |
| `alpine:3.24`           | Mostly MIT/BSD; busybox is GPL-2.0                 |
| `nginx:alpine`          | nginx is BSD-2-Clause; on Alpine base             |
| `docker:cli`            | Apache-2.0                                         |

## System packages & built-from-source tools

| Component         | Where                    | License                          |
|-------------------|--------------------------|----------------------------------|
| iPXE              | built in `dnsmasq` image | GPL-2.0 (with UEFI link exception)|
| wimboot           | bundled in `web` image   | GPL-2.0                          |
| dnsmasq           | `dnsmasq` image          | GPL-2.0                          |
| nfs-utils (nfsd)  | `nfs` image              | GPL-2.0                          |
| samba-server      | `smb` image              | GPL-3.0                          |
| libarchive-tools (bsdtar) | `web` image      | BSD-2-Clause                     |
| p7zip             | `web` image              | LGPL-2.1 (with unRAR restriction)|
| grub-efi / grub-common | `web` image         | GPL-3.0                          |
| wimtools (wimlib) | `web` image              | GPL-3.0 / LGPL-3.0              |
| curl, ca-certificates | `web`/`dnsmasq` image | curl (MIT-like); MPL-2.0 (certs)|

## Source availability for GPL/LGPL components

All copyleft components above are unmodified upstream releases, obtained at
image-build time from their official sources:

- **iPXE** — built from <https://github.com/ipxe/ipxe> (ref pinned via the
  `IPXE_REF` build arg). License: <https://github.com/ipxe/ipxe/blob/master/COPYING>
- **wimboot** — official release binary from
  <https://github.com/ipxe/wimboot/releases>
- **dnsmasq, nfs-utils, samba, p7zip, grub, wimtools, busybox** — installed from
  the Debian and Alpine package repositories; corresponding source is available
  from those distributions:
  - Debian: <https://www.debian.org/distrib/packages>
  - Alpine: <https://pkgs.alpinelinux.org/packages>

Because these components are installed from their upstreams rather than modified
and redistributed here, their corresponding source remains available from the
links above.
