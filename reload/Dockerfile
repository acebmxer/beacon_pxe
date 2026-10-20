# Sidecar that restarts the dnsmasq container when the web app regenerates its
# config. Built on docker:cli (has `docker` + `stat`); the watch script is baked
# in so end users need only the compose file.
#
# No longer part of docker-compose.yml: the dnsmasq container now restarts
# dnsmasq itself (dnsmasq/entrypoint.sh), so nothing but this needed the socket.
# Still built and published so older compose files that list it keep working.
FROM docker:cli
COPY reload.sh /reload.sh
RUN sed -i 's/\r//' /reload.sh
ENTRYPOINT ["/bin/sh", "/reload.sh"]
