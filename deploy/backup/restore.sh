#!/usr/bin/env bash
# ACVM restore (RTO target 2 h, §10). Run on the Docker host from the repo checkout.
#
#   deploy/backup/restore.sh drill [BACKUP]                    restore into a throw-away container, verify, measure
#   deploy/backup/restore.sh postgres [--time 'YYYY-MM-DD HH:MM:SS+00'] [--backup NAME] --yes
#                                                              in-place point-in-time recovery of the live database
#   deploy/backup/restore.sh clickhouse BACKUP_NAME --yes      RESTORE DATABASE acvm FROM S3(...)
#
# The drill never touches production data and never archives WAL (archive_mode=off, WALG_ENABLED=false), so it
# cannot fork the production WAL timeline. It pushes acvm_backup_last_success_timestamp_seconds{kind="restore_drill"},
# acvm_restore_drill_rto_seconds and acvm_restore_drill_rpo_seconds (alert: RestoreDrillOverdue after 35 days).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${ACVM_COMPOSE_DIR:-$HERE/../compose}"
COMPOSE=(docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/docker-compose.yml")
PROJECT="${COMPOSE_PROJECT_NAME:-acvm}"
APP_SERVICES=(acvm-api acvm-worker acvm-publisher acvm-render acvm-loops temporal temporal-ui)

log() { printf '%s [restore] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

load_env() {
    local f
    for f in "$COMPOSE_DIR/.env" "$COMPOSE_DIR/env/walg.env"; do
        [[ -f "$f" ]] || die "missing $f"
        set -a; # shellcheck disable=SC1090
        . "$f"; set +a
    done
}

pg_image() { echo "${ACVM_REGISTRY:-ghcr.io/tburn-labs}/acvm-postgres:${ACVM_POSTGRES_TAG:-16-walg}"; }

push() {  # job-suffix metric-lines
    printf '%b' "$2" | "${COMPOSE[@]}" exec -T postgres curl -fsS --data-binary @- \
        "http://pushgateway:9091/metrics/job/acvm_backup/kind/$1" || log "WARN: metrics push failed"
}

verify_sql() {
    cat <<'SQL'
\set ON_ERROR_STOP on
SELECT 'alembic_version=' || version_num FROM alembic_version;
SELECT 'tables=' || count(*) FROM information_schema.tables WHERE table_schema = 'public';
SELECT 'link_guard_triggers=' || count(*) FROM pg_trigger WHERE tgname LIKE 'trg_affiliate_link_%';
SELECT 'audit_guard_triggers=' || count(*) FROM pg_trigger WHERE tgname LIKE 'trg_audit_log_%';
SELECT 'publications=' || count(*) FROM publication;
SELECT 'affiliate_links=' || count(*) FROM affiliate_link;
SELECT 'newest_write_epoch=' || coalesce(extract(epoch FROM greatest(
    (SELECT max(at) FROM audit_log), (SELECT max(created_at) FROM outbox)))::bigint, 0);
SQL
}

cmd_drill() {
    local backup="${1:-LATEST}" started vol name out newest rto rpo
    load_env
    started="$(date +%s)"
    vol="${PROJECT}_restore_drill_$(date +%s)"
    name="${PROJECT}-restore-drill"
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume create "$vol" >/dev/null
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true; docker volume rm -f "$vol" >/dev/null 2>&1 || true' EXIT

    log "drill: fetching base backup ${backup} into ${vol}"
    docker run --rm --network "${PROJECT}_egress" --env-file "$COMPOSE_DIR/env/walg.env" \
        -e WALG_ENABLED=false -e PGDATA=/var/lib/postgresql/data/pgdata \
        -v "$vol:/var/lib/postgresql/data" --entrypoint sh "$(pg_image)" -c '
          set -e
          mkdir -p "$PGDATA" && chown -R postgres:postgres /var/lib/postgresql/data && chmod 700 "$PGDATA"
          gosu postgres wal-g backup-fetch "$PGDATA" '"$backup"'
          gosu postgres touch "$PGDATA/recovery.signal"
          printf "%s\n" "restore_command = '"'"'/usr/local/bin/acvm-wal-restore %f %p'"'"'" \
                 "recovery_target_action = '"'"'promote'"'"'" >> "$PGDATA/postgresql.auto.conf"'

    log "drill: replaying WAL"
    docker run -d --name "$name" --network "${PROJECT}_egress" --env-file "$COMPOSE_DIR/env/walg.env" \
        -e WALG_ENABLED=false -e PGDATA=/var/lib/postgresql/data/pgdata \
        -v "$vol:/var/lib/postgresql/data" "$(pg_image)" \
        postgres -c config_file=/etc/postgresql/postgresql.conf -c archive_mode=off \
                 -c shared_buffers=512MB -c effective_cache_size=1GB -c max_connections=50 >/dev/null

    for _ in $(seq 1 720); do  # ≤ 2 h (RTO budget)
        if docker exec "$name" pg_isready -q -U postgres 2>/dev/null \
           && [[ "$(docker exec "$name" psql -U postgres -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null)" == "f" ]]; then
            break
        fi
        sleep 10
    done
    [[ "$(docker exec "$name" psql -U postgres -tAc 'SELECT pg_is_in_recovery()')" == "f" ]] \
        || die "recovery did not finish within the 2 h RTO budget"

    log "drill: verifying"
    out="$(verify_sql | docker exec -i "$name" psql -U postgres -d acvm -tA)"
    printf '%s\n' "$out" >&2
    grep -q '^link_guard_triggers=2$' <<<"$out" || die "affiliate_link guards missing after restore"
    grep -q '^audit_guard_triggers=2$' <<<"$out" || die "audit_log guards missing after restore"
    newest="$(sed -n 's/^newest_write_epoch=//p' <<<"$out")"
    rto="$(( $(date +%s) - started ))"
    rpo="$(( started - ${newest:-0} ))"
    log "drill: RTO ${rto} s (target ≤ 7200), data age at restore ${rpo} s (target ≤ 900 under steady writes)"
    push restore_drill "# TYPE acvm_backup_last_success_timestamp_seconds gauge\nacvm_backup_last_success_timestamp_seconds $(date +%s)\n# TYPE acvm_restore_drill_rto_seconds gauge\nacvm_restore_drill_rto_seconds ${rto}\n# TYPE acvm_restore_drill_rpo_seconds gauge\nacvm_restore_drill_rpo_seconds ${rpo}\n"
    (( rto <= 7200 )) || die "RTO ${rto} s exceeds 2 h"
    log "drill: PASS — record it in docs/RUNBOOK.md 복구 훈련 기록"
}

cmd_postgres() {
    local target_time="" backup="LATEST" yes=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --time) target_time="$2"; shift 2 ;;
            --backup) backup="$2"; shift 2 ;;
            --yes) yes=1; shift ;;
            *) die "unknown argument $1" ;;
        esac
    done
    [[ -n "$yes" ]] || die "in-place restore replaces the live database — re-run with --yes (run 'drill' first)"
    load_env
    local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"

    log "stopping writers: ${APP_SERVICES[*]}"
    "${COMPOSE[@]}" stop "${APP_SERVICES[@]}"
    log "stopping postgres"
    "${COMPOSE[@]}" stop postgres

    log "moving current PGDATA aside (pgdata.broken-${stamp}) and fetching ${backup}"
    "${COMPOSE[@]}" run --rm --no-deps -e WALG_ENABLED=false -e TARGET_TIME="$target_time" \
        --entrypoint sh postgres -c '
          set -e
          cd /var/lib/postgresql/data
          if [ -d pgdata ]; then mv pgdata "pgdata.broken-'"$stamp"'"; fi
          mkdir -p pgdata && chown postgres:postgres pgdata && chmod 700 pgdata
          gosu postgres wal-g backup-fetch "$PGDATA" '"$backup"'
          gosu postgres touch "$PGDATA/recovery.signal"
          {
            echo "restore_command = '"'"'/usr/local/bin/acvm-wal-restore %f %p'"'"'"
            echo "recovery_target_action = '"'"'promote'"'"'"
            if [ -n "$TARGET_TIME" ]; then echo "recovery_target_time = '"'"'$TARGET_TIME'"'"'"; fi
          } >> "$PGDATA/postgresql.auto.conf"'

    log "starting postgres in recovery"
    "${COMPOSE[@]}" up -d --no-deps postgres
    for _ in $(seq 1 720); do
        if [[ "$("${COMPOSE[@]}" exec -T postgres psql -U postgres -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null)" == "f" ]]; then
            break
        fi
        sleep 10
    done
    verify_sql | "${COMPOSE[@]}" exec -T postgres psql -U postgres -d acvm -tA
    log "recovery_* settings are left in postgresql.auto.conf for the record; remove them before the next restore"
    log "taking a fresh full backup on the new timeline"
    "$HERE/backup.sh" postgres
    log "starting the application again"
    "${COMPOSE[@]}" up -d
    log "DONE. Old data kept at pgdata.broken-${stamp} inside the pgdata volume — delete after validation."
}

cmd_clickhouse() {
    local name="${1:-}"; [[ -n "$name" ]] || die "usage: restore.sh clickhouse BACKUP_NAME --yes"
    [[ "${2:-}" == "--yes" ]] || die "re-run with --yes (drops and restores database acvm)"
    load_env
    local ch=("${COMPOSE[@]}" exec -T clickhouse clickhouse-client --user acvm --password "${CLICKHOUSE_PASSWORD:?}")
    log "clickhouse: restoring into acvm_restoring first, then swapping"
    "${ch[@]}" --query "DROP DATABASE IF EXISTS acvm_restoring SYNC"
    "${ch[@]}" --query "RESTORE DATABASE acvm AS acvm_restoring FROM S3('${ACVM_CLICKHOUSE_BACKUP_URL}/${name}', '${AWS_ACCESS_KEY_ID}', '${AWS_SECRET_ACCESS_KEY}')"
    "${ch[@]}" --query "RENAME DATABASE acvm TO acvm_before_restore_$(date -u +%Y%m%d%H%M%S), acvm_restoring TO acvm"
    log "clickhouse restored from ${name}; M9 collectors re-fill the gap on their next run (perf_event is ReplacingMergeTree)"
}

case "${1:-}" in
    drill) shift; cmd_drill "$@" ;;
    postgres) shift; cmd_postgres "$@" ;;
    clickhouse) shift; cmd_clickhouse "$@" ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
esac
