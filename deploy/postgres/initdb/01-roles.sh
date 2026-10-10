#!/bin/bash
# First-boot initialisation (runs once, as the postgres superuser, on an empty data volume).
#   acvm          owner of database "acvm"; runs migrations (acvm-migrate) and the application
#   temporal      CREATEDB — temporalio/auto-setup creates "temporal" and "temporal_visibility" itself
#   grafana_ro    read-only role for the Grafana money/learning dashboards
#   acvm_monitor  pg_monitor for postgres-exporter (WAL archiver health → RPO alerts)
# Extensions needing superuser are created here so the migration's CREATE EXTENSION IF NOT EXISTS is a no-op.
set -eo pipefail  # no -u: the entrypoint may source (not exec) this file

: "${ACVM_DB_PASSWORD:?ACVM_DB_PASSWORD is required}"
: "${TEMPORAL_DB_PASSWORD:?TEMPORAL_DB_PASSWORD is required}"
: "${GRAFANA_DB_PASSWORD:?GRAFANA_DB_PASSWORD is required}"
: "${MONITOR_DB_PASSWORD:?MONITOR_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v acvm_pw="$ACVM_DB_PASSWORD" -v temporal_pw="$TEMPORAL_DB_PASSWORD" \
     -v grafana_pw="$GRAFANA_DB_PASSWORD" -v monitor_pw="$MONITOR_DB_PASSWORD" <<'SQL'
CREATE ROLE acvm LOGIN PASSWORD :'acvm_pw';
CREATE DATABASE acvm OWNER acvm ENCODING 'UTF8' TEMPLATE template0;
CREATE ROLE temporal LOGIN CREATEDB PASSWORD :'temporal_pw';
CREATE ROLE grafana_ro LOGIN PASSWORD :'grafana_pw';
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '30s';
CREATE ROLE acvm_monitor LOGIN PASSWORD :'monitor_pw';
GRANT pg_monitor TO acvm_monitor;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname acvm <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO grafana_ro;
-- tables are created later by acvm-migrate (role acvm); grant read access to them up front
ALTER DEFAULT PRIVILEGES FOR ROLE acvm IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
SQL
