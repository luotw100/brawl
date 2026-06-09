# Caddy + Cloudflare DNS plugin, for ACME DNS-01 challenge.
#
# DNS-01 keeps the relay zero-trust: the cert is proven via a Cloudflare DNS
# TXT record (out of the relay's reach), so a compromised relay cannot mint a
# cert for our domain. Requires CF_API_TOKEN (Zone:DNS:Edit + Zone:Zone:Read).
#
# Pin builder and runtime to the SAME version. Bump deliberately and confirm
# caddy-dns/cloudflare still builds. GOPROXY mirror so xcaddy can fetch Go
# modules from inside mainland China.
ARG CADDY_VERSION=2.11.3

FROM caddy:${CADDY_VERSION}-builder-alpine AS builder
ENV GOPROXY=https://goproxy.cn,direct
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:${CADDY_VERSION}-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
