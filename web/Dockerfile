# Pinned to bookworm (Debian 12) on purpose: it ships GRUB 2.06, whose EFI net
# stack works for the XCP-NG chainload.  Trixie's GRUB 2.12 has a broken efinet
# DHCP/route lookup here (dhcp4 start fails, "no route found"), so the
# self-configuring grubx64.efi we build must come from 2.06.
FROM python:3.12-slim-bookworm

# bsdtar (libarchive-tools) and 7z extract kernel/initrd from ISO images without
# needing a privileged loop mount.  grub-efi-amd64-bin + grub-common provide
# grub-mkstandalone, used to build the self-configuring UEFI GRUB that XCP-NG
# clients chainload (see app/services/ipxe.py:_build_grub).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libarchive-tools p7zip-full grub-efi-amd64-bin grub-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
