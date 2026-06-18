#!/usr/bin/env bash
# Downloads the official iPXE network boot programs into this directory.
#   undionly.kpxe  -> handed to legacy BIOS clients
#   ipxe.efi       -> handed to UEFI clients
# These are the two firmware-specific binaries; after they load, every client
# fetches the SAME http://<server>/boot.ipxe menu.
set -euo pipefail
cd "$(dirname "$0")"

BASE="https://boot.ipxe.org"
fetch() {
  if [[ -f "$1" ]]; then
    echo "[ipxe] $1 already present — skipping"
  else
    echo "[ipxe] downloading $1 ..."
    curl -fSL "$BASE/$1" -o "$1"
  fi
}

fetch undionly.kpxe
fetch ipxe.efi
echo "[ipxe] done."
