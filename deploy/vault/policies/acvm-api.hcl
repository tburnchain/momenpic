# acvm-api — public link hub (/hub, /r), media proxy and the admin API behind Cloudflare Access.
# Least privilege (§10): NO access to social-account tokens or Coupang keys. The API never publishes;
# account-connection OAuth callbacks must hand the token to a publisher activity instead of writing it.
#
# KV v2 mount "acvm" (bootstrap.sh). Secret refs used by acvm.config → KV paths:
#   acvm/link_signing_key, acvm/click_ip_salt  → acvm/data/acvm   (verify signed links, hash click IPs)
#   r2/access_key, r2/secret_key               → acvm/data/r2     (signed media URLs)
#   voyage/api_key                             → acvm/data/voyage (similarity search in the console)

path "acvm/data/acvm" {
  capabilities = ["read"]
}

path "acvm/data/r2" {
  capabilities = ["read"]
}

path "acvm/data/voyage" {
  capabilities = ["read"]
}

path "acvm/data/anthropic" {
  capabilities = ["read"]
}

path "acvm/data/gemini" {  # AI integration key (console › AI 연동)
  capabilities = ["read"]
}

path "acvm/data/openai" {  # AI integration key (console › AI 연동)
  capabilities = ["read"]
}

path "acvm/data/higgsfield" {  # AI integration key (console › AI 연동)
  capabilities = ["read"]
}

path "acvm/data/elevenlabs" {  # key (read) + automatic voice_map written on key registration
  capabilities = ["read", "create", "update"]
}

# explicit denies win over any future broad grant
path "acvm/data/tokens/*" {
  capabilities = ["deny"]
}

path "acvm/metadata/tokens/*" {
  capabilities = ["deny"]
}

path "acvm/data/coupang" {
  capabilities = ["deny"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
