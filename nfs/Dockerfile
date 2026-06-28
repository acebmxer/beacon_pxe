FROM alpine:3.20
# Kernel NFS server (nfsd) used to serve each image's live filesystem to clients
# without copying the whole ISO into client RAM. Runs privileged with host
# networking (see docker-compose.yml) because nfsd needs /proc/fs/nfsd and the
# host's nfsd kernel module.
RUN apk add --no-cache nfs-utils
COPY exports /etc/exports
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r//' /entrypoint.sh && chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
