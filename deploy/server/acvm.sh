#!/usr/bin/env bash
#
# Run an `acvm` CLI command on the server as the app user, with the app's environment. As root:
#
#   bash /srv/apps/momenpic/deploy/server/acvm.sh user create you@example.com --role owner --name 대표
#   bash /srv/apps/momenpic/deploy/server/acvm.sh user list
#
# Passwords are typed at a hidden prompt (use `ssh -t` from your PC) — never pass them as arguments.
#
set -euo pipefail
NAME="${ACVM_APP:-momenpic}"
DIR="/srv/apps/$NAME"
ENVF="/etc/apps/env/$NAME.env"
[ "$(id -u)" -eq 0 ] || { echo "root 로 실행하세요"; exit 1; }
[ -x "$DIR/.venv/bin/acvm" ] || { echo "$DIR/.venv 가 없습니다 — install.sh 를 먼저 실행하세요"; exit 1; }
cd "$DIR"
exec sudo -u apps env HOME="$DIR/.home" bash -c 'set -a; . "$0"; set +a; exec "$1/.venv/bin/acvm" "${@:2}"' \
  "$ENVF" "$DIR" "$@"
