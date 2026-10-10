#!/usr/bin/env bash
#
# ACVM (momenpic) — install or update on the single-host appctl/Caddy server. Run as root on the server after the
# code was uploaded to /srv/apps/<name> (see docs/DEPLOY.md "단일 서버 배포"):
#
#   bash /srv/apps/momenpic/deploy/server/install.sh [name] [domain]      # defaults: momenpic momenpic.tburn.biz
#
# Idempotent: the first run registers the app (appctl), later runs update it. Data (SQLite, media, secrets.json)
# and the console access token are kept across runs.
#
#   1. appctl registration + Caddy site (https://<domain>) + path router http://<server-ip>/<name> → https://<domain>/
#   2. /etc/apps/env/<name>.env  (ACVM_* settings; access token generated once, never printed)
#   3. Python venv via uv (uv.lock, no dev deps) · 4. console build (Next.js standalone, same-origin /api/v1)
#   5. DB seed (idempotent) · 6. restart app@<name> and verify locally and through the domain
#
set -euo pipefail

NAME="${1:-momenpic}"
DOMAIN="${2:-momenpic.tburn.biz}"
SERVER_IP=167.86.116.198
DIR="/srv/apps/$NAME"
ENVF="/etc/apps/env/$NAME.env"
ENTRY="deploy/server/launcher.mjs"
REG=/etc/apps/projects.tsv
SITES=/etc/caddy/sites
PY=/usr/bin/python3.12

G='\033[32m'; R='\033[31m'; Y='\033[33m'; B='\033[36m'; D='\033[90m'; O='\033[0m'
step() { echo -e "\n${B}▶ $*${O}"; }
ok()   { echo -e "  ${G}✓${O} $*"; }
warn() { echo -e "  ${Y}!${O} $*"; }
note() { echo -e "  ${D}$*${O}"; }
die()  { echo -e "  ${R}✗${O} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "root 로 실행하세요"
command -v appctl >/dev/null || die "appctl 이 없습니다"
[ -f "$DIR/pyproject.toml" ] && [ -f "$DIR/$ENTRY" ] || die "$DIR 에 코드가 없습니다 — 먼저 업로드하세요"
[ -x "$PY" ] || die "python3.12 가 없습니다"
cd "$DIR"  # apps 사용자로 도는 명령이 root 의 작업 폴더를 물려받지 않게

as_apps() { sudo -u apps env HOME="$DIR/.home" PATH="/usr/local/bin:/usr/bin:/bin" "$@"; }

# ── 1. 등록 · Caddy ──────────────────────────────────────────────────────
step "라우터 등록 ($NAME → $DOMAIN)"
EXIST=$(awk -F'\t' -v n="$NAME" '$1==n {print $2}' "$REG" 2>/dev/null || true)
if [ -n "$EXIST" ] && [ "$EXIST" != "$DOMAIN" ]; then
  warn "예전 등록($EXIST)을 지우고 다시 붙입니다"
  appctl rm "$NAME" >/dev/null
  EXIST=""
fi
if [ -z "$EXIST" ]; then
  appctl add "$NAME" "$DOMAIN" --entry "$ENTRY" >/dev/null
  ok "appctl add $NAME $DOMAIN"
else
  ok "이미 등록됨 ($EXIST)"
fi
PORT=$(awk -F'\t' -v n="$NAME" '$1==n {print $3}' "$REG")
[[ "$PORT" =~ ^[0-9]+$ ]] || die "포트를 찾지 못했습니다"
ok "포트 $PORT (API $((PORT + 1100)) · 콘솔 $((PORT + 1200)) · 루프 메트릭 $((PORT + 1300)), 모두 127.0.0.1)"

cat > "$SITES/$NAME.caddy" <<CADDY
# $NAME — ACVM 콘솔 + API (deploy/server/launcher.mjs 가 한 포트로 묶는다)
$DOMAIN {
	import common
	reverse_proxy 127.0.0.1:$PORT {
		# 배포·재시작으로 앱이 잠깐 내려가도 오류 대신 잠시 기다렸다 붙는다
		lb_try_duration 20s
		lb_try_interval 250ms
	}
}
CADDY
ok "Caddy $SITES/$NAME.caddy"

# 경로 라우터: http://<서버IP>/<이름> 은 도메인으로 보낸다 (yap1000 과 같은 방식, appctl rm 이 함께 지운다)
SHARED="$SITES/00-shared-$(echo "$SERVER_IP" | tr '.:' '--').caddy"
if [ -f "$SHARED" ] && ! grep -q "# >>> $NAME\$" "$SHARED"; then
  awk -v n="$NAME" -v d="$DOMAIN" '
    /^}$/ && !ins {
      printf "\t# >>> %s\n", n
      printf "\thandle /%s {\n\t\tredir https://%s/ 308\n\t}\n", n, d
      printf "\thandle_path /%s/* {\n\t\tredir https://%s{uri} 308\n\t}\n", n, d
      printf "\t# <<< %s\n", n
      ins = 1
    }
    { print }
  ' "$SHARED" > "$SHARED.tmp" && mv "$SHARED.tmp" "$SHARED"
  ok "경로 라우터 http://$SERVER_IP/$NAME → https://$DOMAIN/"
fi
if caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  chown -R caddy:caddy /var/log/caddy 2>/dev/null || true
  systemctl reload caddy
  ok "Caddy 반영"
else
  chown -R caddy:caddy /var/log/caddy 2>/dev/null || true
  caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -5
  die "Caddy 설정 오류 — 반영하지 않았습니다"
fi

# ── 2. 환경변수 ──────────────────────────────────────────────────────────
step "환경변수 $ENVF"
set_env()  { sed -i "/^$1=/d" "$ENVF"; printf '%s=%s\n' "$1" "$2" >> "$ENVF"; }
keep_env() { grep -q "^$1=" "$ENVF" || printf '%s=%s\n' "$1" "$2" >> "$ENVF"; }
set_env ENTRY "$DIR/$ENTRY"
set_env HOME "$DIR/.home"
set_env NEXT_TELEMETRY_DISABLED 1
set_env PYTHONUNBUFFERED 1
keep_env ACVM_ENV staging
set_env ACVM_DATA_DIR "$DIR/data"
set_env ACVM_DATABASE_URL "sqlite+aiosqlite:///$DIR/data/acvm.db"
set_env ACVM_OBJECTS__BACKEND local
set_env ACVM_OBJECTS__LOCAL_ROOT "$DIR/data/objects"
set_env ACVM_OBJECTS__PUBLIC_BASE_URL "https://$DOMAIN/media"
set_env ACVM_LINK_HUB_BASE_URL "https://$DOMAIN"
set_env ACVM_CONSOLE_ORIGINS "'[\"https://$DOMAIN\"]'"
set_env ACVM_SECURITY__SECRET_BACKEND file
set_env ACVM_SECURITY__AUTH_MODE session  # 회원 로그인 (이메일 + 비밀번호, HttpOnly 세션 쿠키)
keep_env ACVM_SECURITY__SIGNUP_MODE approval  # 가입 신청 → 소유자 승인
keep_env ACVM_SECURITY__DEV_API_TOKEN "$(openssl rand -hex 24)"
# 스크립트·점검용 서비스 토큰 (사람은 로그인으로만 들어온다). 예전 개발 토큰이 있으면 그 값을 이어받는다.
if ! grep -q '^ACVM_SECURITY__SERVICE_TOKEN=' "$ENVF"; then
  SVC=$(grep -m1 '^ACVM_SECURITY__DEV_API_TOKEN=' "$ENVF" | cut -d= -f2-)
  printf 'ACVM_SECURITY__SERVICE_TOKEN=%s\n' "${SVC:-$(openssl rand -hex 24)}" >> "$ENVF"
  unset SVC
fi
set_env ACVM_RENDER__FONT_FAMILY '"Noto Sans CJK KR"'
keep_env ACVM_RUN_LOOPS 1
chmod 640 "$ENVF"; chown root:apps "$ENVF"
ok "설정 완료 (회원 로그인 모드 · 서비스 토큰은 이 파일에만 — 화면에 출력하지 않음)"

mkdir -p "$DIR/data/objects" "$DIR/.home" "$DIR/.cache"
chown -R apps:apps "$DIR"
chmod 700 "$DIR/data"
[ -f "$DIR/data/secrets.json" ] && chmod 600 "$DIR/data/secrets.json"

# ── 3. Python ────────────────────────────────────────────────────────────
step "Python 의존성 (uv.lock)"
[ -x "$DIR/.uvenv/bin/uv" ] || { as_apps "$PY" -m venv "$DIR/.uvenv" && as_apps "$DIR/.uvenv/bin/pip" install -q --upgrade uv; }
as_apps env UV_PROJECT_ENVIRONMENT="$DIR/.venv" UV_CACHE_DIR="$DIR/.cache/uv" UV_PYTHON_DOWNLOADS=never \
  "$DIR/.uvenv/bin/uv" sync --frozen --no-dev --python "$PY" --project "$DIR" --quiet
as_apps "$DIR/.venv/bin/python" -c "import acvm, uvicorn; print('  acvm', acvm.__version__)"
ok ".venv 준비"

# ── 4. 콘솔 ──────────────────────────────────────────────────────────────
step "콘솔 빌드 (Next.js standalone, API = 같은 주소 /api/v1)"
as_apps env npm_config_cache="$DIR/.cache/npm" NEXT_TELEMETRY_DISABLED=1 bash -c \
  "cd '$DIR/console' && npm ci --no-audit --no-fund --loglevel=error >/dev/null"
as_apps env npm_config_cache="$DIR/.cache/npm" NEXT_TELEMETRY_DISABLED=1 NEXT_PUBLIC_ACVM_API=/api/v1 \
  NEXT_PUBLIC_ACVM_MOCK=0 bash -c "cd '$DIR/console' && npx next build >/tmp/acvm-next-build.log 2>&1" \
  || { tail -40 /tmp/acvm-next-build.log; die "콘솔 빌드 실패"; }
as_apps bash -c "cd '$DIR/console' && rm -rf .next/standalone/.next/static .next/standalone/public \
  && cp -r .next/static .next/standalone/.next/static && cp -r public .next/standalone/public"
ok "console/.next/standalone"

# ── 5. DB ────────────────────────────────────────────────────────────────
step "데이터베이스"
as_apps bash -c "set -a; . '$ENVF'; set +a; cd '$DIR' && .venv/bin/acvm seed --mature" | tail -3
ok "$DIR/data/acvm.db (워크스페이스·계정 시드, 기존 데이터 유지)"

# ── 6. 시작 · 확인 ───────────────────────────────────────────────────────
step "시작"
systemctl enable "app@$NAME" >/dev/null 2>&1 || true
systemctl reset-failed "app@$NAME" 2>/dev/null || true
systemctl restart "app@$NAME"
CODE=000
for _ in $(seq 1 60); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/healthz" || echo 000)
  [ "$CODE" = "200" ] && break
  sleep 1
done
if [ "$CODE" != "200" ]; then
  tail -40 "/var/log/apps/$NAME.log" 2>/dev/null || true
  die "앱이 응답하지 않습니다 (healthz $CODE)"
fi
ok "app@$NAME 동작 중 — healthz 200"
WEBC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://127.0.0.1:$PORT/" || echo 000)
ok "콘솔 / → $WEBC"
AUTH=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/api/v1/me" || echo 000)
[ "$AUTH" = "401" ] && ok "로그인 없는 API 요청 거부 (401)" || warn "로그인 없는 /api/v1/me → $AUTH"
OWNERS=$(bash "$DIR/deploy/server/acvm.sh" user list 2>/dev/null | awk '$2=="owner" && $3=="active" && $5=="있음"' | wc -l)
if [ "$OWNERS" -gt 0 ]; then
  ok "로그인 가능한 소유자 계정 ${OWNERS}개"
else
  warn "아직 소유자 계정이 없습니다 — 아래 '소유자 만들기' 명령을 한 번 실행하세요"
fi

step "도메인 https://$DOMAIN/"
note "Caddy 가 인증서를 받는 데 보통 10~60초"
OUT=000
for _ in $(seq 1 24); do
  OUT=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" || echo 000)
  [ "$OUT" = "200" ] && break
  sleep 5
done
[ "$OUT" = "200" ] && ok "https://$DOMAIN/healthz → 200 (서버 인증서 정상)" \
  || warn "아직 https 가 열리지 않았습니다 ($OUT) — journalctl -u caddy -n 30 --no-pager"

echo
echo -e "${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${O}"
echo -e "${G}  $NAME 올라갔습니다 — https://$DOMAIN/${O}"
echo -e "${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${O}"
echo "  로그인     https://$DOMAIN/login   (회원가입: https://$DOMAIN/signup → 소유자 승인)"
echo "  소유자 만들기 (비밀번호는 숨김 입력):"
echo "             ssh -t root@$SERVER_IP \"bash $DIR/deploy/server/acvm.sh user create <이메일> --role owner --name <이름>\""
echo "  로그       appctl logs $NAME"
echo "  상태       appctl list"
echo "  업데이트   코드 업로드 후 bash $DIR/deploy/server/install.sh $NAME $DOMAIN"
echo
