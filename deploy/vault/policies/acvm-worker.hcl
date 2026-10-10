# acvm-worker — Temporal production worker (acvm-main) and render worker (acvm-render).
# Discovers products, writes scripts, renders and gates content. It does NOT publish, so it gets no access to
# social-account tokens (§10: "게시 워커만 단기 자격으로 접근").
#
#   coupang/access_key, coupang/secret_key → acvm/data/coupang  (search / deeplink, HMAC signing)
#   acvm/link_signing_key                  → acvm/data/acvm     (sign affiliate links at creation)
#   r2/*                                   → acvm/data/r2       (upload renders, signed URLs)
#   voyage/api_key                         → acvm/data/voyage   (embeddings for R-02 / S-01 similarity)

path "acvm/data/coupang" {
  capabilities = ["read"]
}

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

path "acvm/data/elevenlabs" {  # AI integration key (console › AI 연동)
  capabilities = ["read"]
}

# Social tokens (acvm/data/tokens/*) are denied implicitly — deliberately NOT an explicit "deny", because the
# acvm-loops AppRole combines this policy with acvm-publisher and an explicit deny would override that grant.

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
