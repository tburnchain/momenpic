#!/bin/sh
# Vault bootstrap for ACVM (§10). POSIX sh — runs inside the vault container, where this directory is mounted
# read-only at /vault/acvm:
#
#   docker compose exec -e VAULT_TOKEN vault sh /vault/acvm/bootstrap.sh init
#   docker compose exec -T -e VAULT_TOKEN vault sh /vault/acvm/bootstrap.sh credentials acvm-api > env/approle-api.env
#   docker compose exec -e VAULT_TOKEN vault sh /vault/acvm/bootstrap.sh status
#
# init (idempotent):
#   * file audit device → stdout (docker logs → Loki; secrets are HMAC'd by Vault)
#   * KV v2 secrets engine at acvm/ (max 10 versions per secret)
#   * AppRole auth, policies acvm-api / acvm-worker / acvm-publisher from ./policies
#   * roles (token_ttl=5m, token_max_ttl=30m, tokens and secret-ids bound to the private docker subnet):
#       acvm-api        [acvm-api]
#       acvm-worker     [acvm-worker] (+ acvm-publisher while ACVM_WORKER_PUBLISHES=true, see below)
#       acvm-render     [acvm-worker]
#       acvm-publisher  [acvm-publisher]
#       acvm-loops      [acvm-worker, acvm-publisher]   insights pull + token refresh need tokens
#     ACVM_WORKER_PUBLISHES=false (default): publishing runs only on acvm-publish (CommandInput.publish_queue).
#     Set it to true only for a single-worker dev cluster where one process serves every queue.
#   * generates acvm/acvm link_signing_key + click_ip_salt once (never rotated implicitly: rotating the
#     link signing key invalidates every published link signature)
# credentials <role>: prints ACVM_VAULT_ROLE_ID / ACVM_VAULT_SECRET_ID (issues a NEW secret-id each call)
# rotate <role>:      issues a new secret-id and destroys all previous ones for that role
set -eu

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR
POLICY_DIR="${POLICY_DIR:-$(cd "$(dirname "$0")" && pwd)/policies}"
BOUND_CIDRS="${ACVM_VAULT_BOUND_CIDRS:-172.30.10.0/24}"
TOKEN_TTL="${ACVM_VAULT_TOKEN_TTL:-5m}"
TOKEN_MAX_TTL="${ACVM_VAULT_TOKEN_MAX_TTL:-30m}"
ROLES="acvm-api acvm-worker acvm-render acvm-publisher acvm-loops"
WORKER_PUBLISHES="${ACVM_WORKER_PUBLISHES:-false}"

log() { printf '[bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_token() {
    [ -n "${VAULT_TOKEN:-}" ] || die "VAULT_TOKEN must be set (root/admin token; revoke it afterwards)"
    vault token lookup >/dev/null 2>&1 || die "VAULT_TOKEN is invalid or Vault is sealed"
}

policies_for() {
    case "$1" in
        acvm-api) echo "acvm-api" ;;
        acvm-worker)
            if [ "$WORKER_PUBLISHES" = "true" ]; then echo "acvm-worker,acvm-publisher"; else echo "acvm-worker"; fi ;;
        acvm-render) echo "acvm-worker" ;;
        acvm-publisher) echo "acvm-publisher" ;;
        acvm-loops) echo "acvm-worker,acvm-publisher" ;;
        *) die "unknown role $1 (expected one of: $ROLES)" ;;
    esac
}

random_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

cmd_init() {
    require_token
    if ! vault audit list 2>/dev/null | grep -q '^file/'; then
        log "enabling file audit device (stdout)"
        vault audit enable file file_path=stdout log_raw=false >/dev/null
    fi
    if ! vault secrets list | grep -q '^acvm/'; then
        log "enabling kv-v2 at acvm/"
        vault secrets enable -path=acvm -version=2 -description="ACVM application secrets" kv >/dev/null
    fi
    vault write acvm/config max_versions=10 cas_required=false >/dev/null
    if ! vault auth list | grep -q '^approle/'; then
        log "enabling approle auth"
        vault auth enable approle >/dev/null
    fi
    for policy in acvm-api acvm-worker acvm-publisher; do
        [ -f "$POLICY_DIR/$policy.hcl" ] || die "missing $POLICY_DIR/$policy.hcl"
        vault policy write "$policy" "$POLICY_DIR/$policy.hcl" >/dev/null
        log "policy $policy written"
    done
    for role in $ROLES; do
        vault write "auth/approle/role/$role" \
            token_policies="$(policies_for "$role")" \
            token_ttl="$TOKEN_TTL" \
            token_max_ttl="$TOKEN_MAX_TTL" \
            token_type=service \
            token_bound_cidrs="$BOUND_CIDRS" \
            secret_id_bound_cidrs="$BOUND_CIDRS" \
            secret_id_ttl=0 \
            secret_id_num_uses=0 >/dev/null
        log "approle $role → policies [$(policies_for "$role")], ttl $TOKEN_TTL, bound $BOUND_CIDRS"
    done
    if ! vault kv get -mount=acvm acvm >/dev/null 2>&1; then
        log "generating acvm/acvm link_signing_key + click_ip_salt"
        vault kv put -mount=acvm acvm \
            link_signing_key="$(random_hex 32)" \
            click_ip_salt="$(random_hex 16)" >/dev/null
    fi
    log "done. Load the remaining secrets with: vault kv put -mount=acvm coupang access_key=- ... (docs/DEPLOY.md 4단계)"
}

cmd_credentials() {
    require_token
    role="${1:-}"; [ -n "$role" ] || die "usage: bootstrap.sh credentials <role>"
    policies_for "$role" >/dev/null
    role_id="$(vault read -field=role_id "auth/approle/role/$role/role-id")"
    secret_id="$(vault write -f -field=secret_id "auth/approle/role/$role/secret-id")"
    printf '# AppRole %s — generated %s. chmod 600; never commit.\n' "$role" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'ACVM_VAULT_ROLE_ID=%s\nACVM_VAULT_SECRET_ID=%s\n' "$role_id" "$secret_id"
}

cmd_rotate() {
    require_token
    role="${1:-}"; [ -n "$role" ] || die "usage: bootstrap.sh rotate <role>"
    policies_for "$role" >/dev/null
    old_accessors="$(vault list -format=json "auth/approle/role/$role/secret-id" 2>/dev/null \
        | tr -d '[]" ' | tr ',' '\n' | sed '/^$/d' || true)"
    cmd_credentials "$role"
    for accessor in $old_accessors; do
        vault write "auth/approle/role/$role/secret-id-accessor/destroy" secret_id_accessor="$accessor" >/dev/null
        log "destroyed old secret-id accessor $accessor"
    done
    log "restart the services using $role after replacing their env file"
}

cmd_status() {
    vault status || true
    if [ -n "${VAULT_TOKEN:-}" ]; then
        vault auth list
        vault secrets list
        for role in $ROLES; do
            printf '%s: ' "$role"
            vault read -field=token_policies "auth/approle/role/$role" 2>/dev/null || echo "(missing)"
        done
    fi
}

case "${1:-}" in
    init) cmd_init ;;
    credentials) shift; cmd_credentials "$@" ;;
    rotate) shift; cmd_rotate "$@" ;;
    status) cmd_status ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac
