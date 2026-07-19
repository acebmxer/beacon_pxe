# Bring in the Docker CLI + compose plugin so the web container can run
# `docker compose pull/up` for self-updates (requires docker socket mount).
FROM docker:cli AS docker-cli

FROM python:3.12-slim-bookworm
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose \
                       /usr/local/libexec/docker/cli-plugins/docker-compose

# bsdtar (libarchive-tools) and 7z extract kernel/initrd from ISO images without
# needing a privileged loop mount. grub-efi-amd64-bin + grub-common provide
# grub-mkstandalone, used to build a self-contained UEFI GRUB binary that
# multiboots XCP-NG (iPXE itself can't multiboot under UEFI). curl fetches the
# wimboot binary below.
# wimtools (wimlib-imagex) injects a startnet.cmd into the Windows boot.wim so
# WinPE auto-mounts the SMB install share and launches setup.exe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libarchive-tools p7zip-full grub-efi-amd64-bin grub-common wimtools \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# wimboot: iPXE's helper kernel that boots Windows PE (boot.wim + bootmgr/BCD)
# from an in-memory ramdisk. Windows ISOs can't be netbooted as a plain
# kernel+initrd; wimboot is the standard chain. Copied into the bootroot at
# startup (services.bootstrap) so nginx serves it at /wimboot.
ARG WIMBOOT_VERSION=2.8.0
RUN curl -fsSL -o /usr/local/share/wimboot \
        "https://github.com/ipxe/wimboot/releases/download/v${WIMBOOT_VERSION}/wimboot"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Build provenance, surfaced in the UI's Updates panel so an admin can tell what
# is actually deployed. Set by the publish workflow from the docker metadata
# action; left at the defaults for local `docker-compose.dev.yml` builds.
#   BEACON_VERSION - "1.2.3" on a release tag, "main" on a main-branch build
#   BEACON_COMMIT  - full git sha of the build
# Declared after COPY app so a version-only change doesn't rebuild the layers above.
ARG BEACON_VERSION=dev
ARG BEACON_COMMIT=""
ENV BEACON_VERSION=${BEACON_VERSION} \
    BEACON_COMMIT=${BEACON_COMMIT}

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
