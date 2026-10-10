#!/bin/sh
# restore_command hook used only during point-in-time recovery (deploy/backup/restore.sh writes
# restore_command = '/usr/local/bin/acvm-wal-restore %f %p' into postgresql.auto.conf).
set -eu
exec /usr/local/bin/wal-g wal-fetch "$1" "$2"
