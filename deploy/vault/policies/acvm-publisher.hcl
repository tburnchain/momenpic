# acvm-publisher — the only identity that can read social-account OAuth tokens (§10).
# Used by the publish worker (task queue acvm-publish) and, combined with acvm-worker, by acvm-loops
# (hourly insights pull + Meta long-lived token refresh 7 days before expiry).
# AppRole tokens carrying this policy live 5 minutes (bootstrap.sh token_ttl=5m) and are bound to the
# private docker subnet.
#
# channel_account.token_ref = "tokens/<platform>/<handle>"  → KV v2 path acvm/data/tokens/<platform>, key <handle>
#   acvm/link_signing_key → acvm/data/acvm  (verify the deeplink partner-ID signature right before publishing)
#   r2/*                  → acvm/data/r2    (2-hour signed media URLs that Instagram/TikTok fetch)

path "acvm/data/tokens/*" {
  capabilities = ["read", "create", "update"]   # create/update = token refresh writes a new KV version
}

path "acvm/metadata/tokens/*" {
  capabilities = ["read"]                        # expiry bookkeeping (custom_metadata), no delete/destroy
}

path "acvm/data/acvm" {
  capabilities = ["read"]
}

path "acvm/data/r2" {
  capabilities = ["read"]
}

# never: destroying token history, or touching other KV trees
path "acvm/destroy/*" {
  capabilities = ["deny"]
}

path "acvm/delete/*" {
  capabilities = ["deny"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
