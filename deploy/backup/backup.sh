#!/usr/bin/env bash
# ACVM backups → Cloudflare R2 (run on the Docker host; systemd timer acvm-backup.timer runs `all` daily).
#
#   deploy/backup/backup.sh postgres     WAL-G full base backup + retention (WAL is archived continuously)
#   deploy/backup/backup.sh clickhouse   BACKUP DATABASE acvm TO S3(...) (native ClickHouse backup)
#   deploy/backup/backup.sh vault        tar of the (barrier-encrypted) Vault file storage
#   deploy/backup/backup.sh all          all of the above
#   deploy/backup/backup.sh list         list Postgres base backups
#
# Every success pushes acvm_backup_last_success_timestamp_seconds{kind=...} to the Pushgateway; the
# BackupStale alerts in deploy/prometheus/rules/infra.yml page when a daily backup is > 26 h old.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${ACVM_COMPOSE_DIR:-$HERE/../compose}"
COMPOSE=(docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/docker-compose.yml")

load_env() {
    local f
    for f in "$COMPOSE_DIR/.env" "$COMPOSE_DIR/env/walg.env"; do
        [[ -f "$f" ]] || { echo "missing $f" >&2; exit 1; }
        set -a; # shellcheck disable=SC1090
        . "$f"; set +a
    done
}

log() { printf '%s [backup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

push_metric() {  # kind duration_seconds
    local kind="$1" duration="$2" now
    now="$(date +%s)"
    printf '# TYPE acvm_backup_last_success_timestamp_seconds gauge\nacvm_backup_last_success_timestamp_seconds %s\n# TYPE acvm_backup_duration_seconds gauge\nacvm_backup_duration_seconds %s\n' \
        "$now" "$duration" \
      | "${COMPOSE[@]}" exec -T postgres curl -fsS --data-binary @- \
          "http://pushgateway:9091/metrics/job/acvm_backup/kind/${kind}" \
      || log "WARN: could not push metrics for ${kind}"
}

timed() {  # kind command...
    local kind="$1"; shift
    local start end
    start="$(date +%s)"
    "$@"
    end="$(date +%s)"
    push_metric "$kind" "$((end - start))"
    log "${kind} ok in $((end - start)) s"
}

pg_full() {
    log "postgres: wal-g backup-push"
    "${COMPOSE[@]}" exec -T -u postgres postgres sh -c 'wal-g backup-push "$PGDATA"'
    log "postgres: retention (keep ${ACVM_BACKUP_RETAIN_FULL:-14} full backups + their WAL)"
    "${COMPOSE[@]}" exec -T -u postgres postgres \
        wal-g delete retain FULL "${ACVM_BACKUP_RETAIN_FULL:-14}" --confirm
}

clickhouse_backup() {
    : "${ACVM_CLICKHOUSE_BACKUP_URL:?set in env/walg.env}"
    local name; name="$(date -u +%Y%m%dT%H%M%SZ)"
    log "clickhouse: BACKUP DATABASE acvm → ${ACVM_CLICKHOUSE_BACKUP_URL}/${name}"
    # ClickHouse masks S3 credentials in system.query_log / server logs.
    "${COMPOSE[@]}" exec -T clickhouse clickhouse-client \
        --user acvm --password "${CLICKHOUSE_PASSWORD:?}" \
        --query "BACKUP DATABASE acvm TO S3('${ACVM_CLICKHOUSE_BACKUP_URL}/${name}', '${AWS_ACCESS_KEY_ID}', '${AWS_SECRET_ACCESS_KEY}') SETTINGS compression_method = 'zstd'"
}

vault_backup() {
    local name; name="ops/vault/vault-file-$(date -u +%Y%m%dT%H%M%SZ).tgz"
    log "vault: file storage → ${name} (barrier-encrypted; restore also needs the unseal keys)"
    "${COMPOSE[@]}" exec -T vault tar -czf - -C /vault file \
      | "${COMPOSE[@]}" exec -T -u postgres postgres sh -c \
          "cat > /tmp/vault.tgz && wal-g st put /tmp/vault.tgz '${name}'; rc=\$?; rm -f /tmp/vault.tgz; exit \$rc"
}

main() {
    load_env
    case "${1:-}" in
        postgres) timed postgres_full pg_full ;;
        clickhouse) timed clickhouse clickhouse_backup ;;
        vault) timed vault vault_backup ;;
        all)
            local rc=0
            timed postgres_full pg_full || rc=1
            timed clickhouse clickhouse_backup || rc=1
            timed vault vault_backup || rc=1
            exit "$rc"
            ;;
        list) "${COMPOSE[@]}" exec -T -u postgres postgres wal-g backup-list --detail ;;
        *) sed -n '2,12p' "$0"; exit 2 ;;
    esac
}

main "$@"
