# syntax=docker/dockerfile:1.7
# PostgreSQL 16 + pgvector + WAL-G (continuous WAL archiving to Cloudflare R2, §10 백업: RPO 15m / RTO 2h).
#
# Asset names/checksums: https://github.com/wal-g/wal-g/releases — always pass WALG_SHA256 in production builds.
ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.1-pg16-bookworm
ARG PGVECTOR_DIGEST=
FROM ${PGVECTOR_IMAGE}${PGVECTOR_DIGEST:+@}${PGVECTOR_DIGEST}

ARG WALG_VERSION=v3.0.7
ARG WALG_ASSET=wal-g-pg-22.04-amd64
ARG WALG_SHA256=

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/wal-g.tar.gz \
      "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${WALG_ASSET}.tar.gz"; \
    if [ -n "${WALG_SHA256}" ]; then echo "${WALG_SHA256}  /tmp/wal-g.tar.gz" | sha256sum -c -; \
    else echo "WARNING: WALG_SHA256 not set — wal-g download not verified (dev build only)" >&2; fi; \
    mkdir -p /tmp/walg; tar -xzf /tmp/wal-g.tar.gz -C /tmp/walg; \
    install -m 0755 "$(find /tmp/walg -type f -name 'wal-g*' | head -n1)" /usr/local/bin/wal-g; \
    rm -rf /tmp/walg /tmp/wal-g.tar.gz; \
    wal-g --version

COPY --chmod=0755 walg-archive.sh /usr/local/bin/acvm-wal-archive
COPY --chmod=0755 walg-restore.sh /usr/local/bin/acvm-wal-restore
COPY --chmod=0644 postgresql.conf /etc/postgresql/postgresql.conf
