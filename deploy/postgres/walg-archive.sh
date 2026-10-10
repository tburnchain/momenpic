#!/bin/sh
# archive_command hook (postgresql.conf: archive_command = '/usr/local/bin/acvm-wal-archive %p').
#
# WALG_ENABLED=true  → push the segment to R2 with WAL-G; a non-zero exit makes PostgreSQL keep and retry it
#                      (alert: PostgresWalArchiveFailing — pg_wal grows until the push succeeds).
# otherwise (dev)    → acknowledge without archiving so pg_wal does not grow on laptops.
set -eu
if [ "${WALG_ENABLED:-false}" != "true" ]; then
    exit 0
fi
exec /usr/local/bin/wal-g wal-push "$1"
