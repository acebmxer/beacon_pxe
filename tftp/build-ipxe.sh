#!/usr/bin/env bash
# Builds the iPXE network boot programs from source inside a throwaway Docker
# container and drops them into this directory:
#   undionly.kpxe  -> legacy BIOS clients
#   ipxe.efi       -> UEFI clients
#
# Building (rather than downloading) avoids depending on the availability of any
# particular mirror and always produces matching BIOS + UEFI binaries.
set -euo pipefail
cd "$(dirname "$0")"

# Which iPXE revision to build (branch, tag, or commit). Override to pin a
# specific version for reproducible builds, or to test a different one:
#   IPXE_REF=v1.21.1 ./build-ipxe.sh
# Default stays on master because that's the revision known to build cleanly with
# this toolchain; older tags may fail against newer GCC.
#
# Xen note: a "Link status: Down / Waiting for link-up on net0" failure on Xen
# VMs is NOT really an iPXE bug — it's a dom0 xen-netback kernel regression
# triggered when iPXE brings the netfront link up a second time (after the
# firmware already used it). The real fix is updating the Xen HOST kernel (see
# README Troubleshooting). Trying an older IPXE_REF (e.g. v1.21.1) sometimes
# sidesteps it but is not guaranteed.
IPXE_REF="${IPXE_REF:-master}"

if [[ -f undionly.kpxe && -f ipxe.efi ]]; then
  echo "[ipxe] undionly.kpxe and ipxe.efi already present — skipping build"
  echo "       (delete them, or run fetch-ipxe.sh, to rebuild from a different ref)"
  exit 0
fi

echo "[ipxe] building from source at ref '$IPXE_REF' (this takes a minute or two) ..."
img="pxe-ipxe-builder:tmp"

docker build -t "$img" --build-arg IPXE_REF="$IPXE_REF" - <<'DOCKERFILE'
FROM debian:bookworm-slim
ARG IPXE_REF
RUN apt-get update && apt-get install -y --no-install-recommends \
        git make gcc gcc-multilib binutils perl liblzma-dev mtools ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Shallow-clone just the requested ref (works for branches, tags, and commits).
RUN git clone https://github.com/ipxe/ipxe.git /ipxe \
    && git -C /ipxe checkout "$IPXE_REF"
WORKDIR /ipxe/src
# Embedded script: the instant iPXE starts it pulls the HTTP menu from whatever
# server pointed it here (${next-server} = the DHCP next-server / your boot box).
# This makes the binaries self-chaining, so an existing DHCP server only needs
# next-server + filename (BIOS->undionly.kpxe, UEFI->ipxe.efi) — no proxyDHCP,
# works across VLANs, works for any client (XCP-ng, other hypervisors, physical).
RUN printf '#!ipxe\ndhcp || dhcp\nchain http://${next-server}/boot.ipxe || shell\n' > chain.ipxe
RUN make -j"$(nproc)" EMBED=chain.ipxe bin/undionly.kpxe bin-x86_64-efi/ipxe.efi
DOCKERFILE

cid=$(docker create "$img")
docker cp "$cid:/ipxe/src/bin/undionly.kpxe" ./undionly.kpxe
docker cp "$cid:/ipxe/src/bin-x86_64-efi/ipxe.efi" ./ipxe.efi
docker rm "$cid" >/dev/null
docker rmi "$img" >/dev/null 2>&1 || true

echo "[ipxe] done:"
ls -l undionly.kpxe ipxe.efi
