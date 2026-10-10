# Vault server — production profile for the single-server P0/P1 deployment (§10 자격 증명).
#
# * file storage on the "vaultdata" volume; backed up together with the R2 backups (see docs/RUNBOOK.md)
# * MANUAL UNSEAL after every start: `docker compose exec vault vault operator unseal` × 3 (Shamir 3-of-5)
# * listener is plain HTTP on the internal "backend" docker network only — no published port, no tunnel route.
#   AppRole logins are additionally bound to that subnet (bootstrap.sh: token_bound_cidrs / secret_id_bound_cidrs).
#   For TLS, mount a cert/key and set tls_cert_file/tls_key_file + tls_disable = false (docs/SECURITY.md).

ui            = false
disable_mlock = false          # compose grants IPC_LOCK so secrets never hit swap
api_addr      = "http://vault:8200"
log_level     = "info"
log_format    = "json"

default_lease_ttl = "5m"       # §10: publish workers use 5-minute credentials
max_lease_ttl     = "1h"

storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true

  telemetry {
    unauthenticated_metrics_access = true   # Prometheus job "vault" (sealed ⇒ scrape fails ⇒ VaultSealedOrDown)
  }
}

telemetry {
  prometheus_retention_time = "60s"
  disable_hostname          = true
}
