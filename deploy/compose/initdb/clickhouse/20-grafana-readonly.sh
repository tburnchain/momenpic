#!/bin/bash
# Runs once on an empty ClickHouse volume, after deploy/clickhouse/schema.sql (10-schema.sql).
# Creates the read-only user the Grafana money/learning dashboards use (datasource "ACVM ClickHouse").
set -eo pipefail
: "${CLICKHOUSE_GRAFANA_PASSWORD:?CLICKHOUSE_GRAFANA_PASSWORD is required}"
pw_sql="$(printf '%s' "$CLICKHOUSE_GRAFANA_PASSWORD" | sed "s/'/''/g")"

clickhouse client --host 127.0.0.1 --user "${CLICKHOUSE_USER:-acvm}" --password "${CLICKHOUSE_PASSWORD}" \
    --multiquery <<SQL
CREATE USER IF NOT EXISTS grafana_ro IDENTIFIED WITH sha256_password BY '${pw_sql}'
    SETTINGS readonly = 1, max_execution_time = 30, max_memory_usage = 2000000000;
GRANT SELECT ON acvm.* TO grafana_ro;
SQL
