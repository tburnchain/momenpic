# ACVM 배포 가이드 — P0 단일 서버 (Contabo + Docker Compose)

설계서 §9: 1단계는 Contabo 서버 1대에 Docker Compose로 전 모듈을 올리고, 일 1,000건 이상 또는 다중 워크스페이스 시점에
k3s(`deploy/k8s`)로 이전한다. 이 문서는 **빈 서버 → G0 통과**까지의 순서다. 장애 대응은 [RUNBOOK](RUNBOOK.md),
통제 항목은 [SECURITY](SECURITY.md), 단계/게이트는 [ROADMAP](ROADMAP.md).

## 0. 구성 한눈에 보기

| 계층 | 서비스 (compose) | 노출 |
|---|---|---|
| 엣지 | `cloudflared` (Tunnel) | 아웃바운드만. 공개: `/hub/*`, `/r/*` (+로컬 미디어 모드 `/media/*`) · Access(SSO): 콘솔, `/api/v1/*`, Grafana |
| 앱 | `acvm-api`, `acvm-worker`(acvm-main), `acvm-publisher`(acvm-publish), `acvm-render`(acvm-render), `acvm-loops`, `console`, `acvm-migrate`(1회성) | 사설망 |
| 데이터 | `postgres`(pgvector+WAL-G), `redis`, `clickhouse`, `temporal`, `vault` | `backend` 내부망(이그레스 없음) |
| 관측 | `otel-collector`, `prometheus`, `alertmanager`, `pushgateway`, `grafana`, `loki`, `promtail`, exporters | 내부망 (Grafana만 Access 경유) |
| 관리 UI | `temporal-ui` | `127.0.0.1:8088` → SSH 터널 |

권장 사양: 12 vCPU / 48 GB RAM / NVMe 400 GB 이상, Ubuntu 24.04 LTS. compose 메모리 상한 합계 ≈ 45 GB
(렌더 8 GB, PostgreSQL 10 GB, ClickHouse 8 GB). 사양이 작으면 `docker-compose.yml`의 `deploy.resources.limits`와
`deploy/postgres/postgresql.conf`(shared_buffers = 한도의 25 %)를 같이 줄인다.

## 1. 서버 준비

```bash
# 사용자·SSH
adduser ops && usermod -aG sudo ops            # 이후 ops로 작업, root SSH 로그인/비밀번호 로그인 비활성화
sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw enable   # 80/443 불필요 (터널은 아웃바운드)
sudo apt-get install -y unattended-upgrades jq && sudo dpkg-reconfigure -plow unattended-upgrades
# Docker Engine + compose plugin ≥ 2.24 (env_file required/!override 문법)
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker ops
docker compose version
# cosign (이미지 서명 검증)
sudo apt-get install -y cosign || echo "https://github.com/sigstore/cosign/releases 참고"
# 코드
sudo mkdir -p /opt/acvm && sudo chown ops: /opt/acvm && git clone <repo> /opt/acvm && cd /opt/acvm/deploy/compose
```

## 2. Cloudflare (DNS · R2 · Tunnel · Access)

1. **R2 버킷** 2개: `acvm-media`(커스텀 도메인 연결, 예: `media.example.com` — Instagram/TikTok이 서명 URL로 가져감),
   `acvm-backups`(비공개). API 토큰도 **버킷별로 분리**(media RW / backups RW). backups 버킷 수명주기 규칙:
   `clickhouse/` 30일, `ops/vault/` 90일 후 삭제.
2. **Tunnel** (로컬 관리형 — 인그레스 규칙이 git에 버전 관리됨):
   ```bash
   cloudflared tunnel login && cloudflared tunnel create acvm          # UUID + ~/.cloudflared/<UUID>.json
   mkdir -p secrets && sudo install -m 640 -o "$USER" -g 65532 ~/.cloudflared/<UUID>.json secrets/cloudflared-credentials.json
   # 컨테이너 사용자(cloudflared uid/gid 65532)만 그룹으로 읽을 수 있게
   cloudflared tunnel route dns acvm go.example.com        # 링크 허브 (공개)
   cloudflared tunnel route dns acvm console.example.com   # 콘솔 + /api/v1 (Access)
   cloudflared tunnel route dns acvm grafana.example.com   # Grafana (Access)
   ```
3. **Access 애플리케이션** (Zero Trust → Access → Applications → Self-hosted) 2개: `console.example.com`,
   `grafana.example.com`. 정책 = 운영자 이메일/IdP 그룹만 Allow. 각 앱의 **AUD 태그**를 `.env`의
   `CF_ACCESS_CONSOLE_AUD`, `CF_ACCESS_GRAFANA_AUD`에 넣는다. cloudflared가 JWT를 한 번 더 검증한다
   (`originRequest.access.required`), API는 `ACVM_SECURITY__AUTH_MODE=cloudflare_access`로 다시 검증한다.
4. (권장) WAF 레이트 리밋: `go.example.com/r/*` IP당 분당 30회 — 무효 클릭(§4 M9) 1차 방어.

## 3. 설정·비밀 파일 (`chmod 600`/컨테이너 그룹 전용 `640`, git 무시됨: `deploy/compose/.gitignore`)

```bash
cd /opt/acvm/deploy/compose
cp .env.example .env                 # 호스트명, Access, 인프라 비밀번호, 이미지 버전
for v in POSTGRES_SUPERUSER ACVM_DB TEMPORAL_DB GRAFANA_DB MONITOR_DB REDIS CLICKHOUSE CLICKHOUSE_GRAFANA GRAFANA_ADMIN; do
  sed -i "s/^${v}_PASSWORD=$/${v}_PASSWORD=$(openssl rand -hex 24)/" .env; done
mkdir -p env secrets/alertmanager
cp ../../.env.example env/acvm.env   # ACVM_* 앱 설정 (아래 값 수정)
cp ../backup/walg.env.example env/walg.env   # R2 backups 키 + WALG_LIBSODIUM_KEY=$(openssl rand -hex 32)
printf 'ANTHROPIC_API_KEY=%s\n' '<key>' > env/llm.env
printf '%s' 'https://hooks.slack.com/services/...' > secrets/alertmanager/page_webhook_url
printf '%s' 'https://hooks.slack.com/services/...' > secrets/alertmanager/ticket_webhook_url
chmod 600 .env env/*                               # compose CLI(호스트 사용자)만 읽음
sudo chown "$USER":65534 secrets/alertmanager/* && chmod 640 secrets/alertmanager/*   # alertmanager = nobody(65534)
```

`env/acvm.env` P0 값 (G0 전까지 **staging + 드라이런 + 샌드박스**, `assert_production_safe`가 prod에서 이를 거부하므로):

```dotenv
ACVM_ENV=staging                        # .env 의 ACVM_ENV 도 staging (compose 가 주입)
ACVM_SECURITY__SECRET_BACKEND=vault
ACVM_SECURITY__AUTH_MODE=cloudflare_access
ACVM_PLATFORMS__DRY_RUN=true
ACVM_COUPANG__SANDBOX=true
ACVM_COUPANG__PARTNER_ID=AF0000000
ACVM_LLM__BACKEND=anthropic
ACVM_EMBEDDINGS__BACKEND=voyage
ACVM_OBJECTS__BACKEND=s3
ACVM_OBJECTS__ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
ACVM_OBJECTS__PUBLIC_BASE_URL=https://media.example.com
# ACVM_SECRET__* 줄은 지운다 (prod/staging 은 Vault 만 사용)
```

DB/Redis/ClickHouse/Temporal/OTel/Vault 주소는 `docker-compose.yml`이 주입하므로 `acvm.env`에 쓰지 않는다.

## 4. 이미지

```bash
export ACVM_VERSION=1.0.0      # .env 의 ACVM_VERSION 과 동일하게
for i in api worker render console; do
  cosign verify ghcr.io/tburn-labs/acvm-$i:$ACVM_VERSION \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github.com/tburn-labs/acvm/.github/workflows/release.yml@' >/dev/null \
    && echo "acvm-$i signature OK"; done
docker compose pull
docker compose build postgres         # pgvector + WAL-G (WALG_SHA256 를 .env 에 넣어 체크섬 검증)
```

## 5. 데이터 계층 기동 + Vault 초기화/언실

```bash
docker compose up -d postgres redis clickhouse vault
docker compose exec vault vault operator init -key-shares=5 -key-threshold=3
#  → Unseal Key 5개 + Initial Root Token: 오프라인 2곳(비밀번호 관리자 + 금고 보관 종이)에 분산 보관. 서버에 저장 금지.
docker compose exec vault vault operator unseal      # 서로 다른 키로 3회
docker compose ps vault                               # healthy = 초기화 + 언실 완료
export VAULT_TOKEN=<root token>                       # 이 셸에서만
docker compose exec -e VAULT_TOKEN vault sh /vault/acvm/bootstrap.sh init
for r in api worker render publisher loops; do
  docker compose exec -T -e VAULT_TOKEN vault sh /vault/acvm/bootstrap.sh credentials acvm-$r > env/approle-$r.env
done; chmod 600 env/approle-*.env
```

`bootstrap.sh init`: KV v2 `acvm/`, AppRole, 정책 3종, 역할 5종(token_ttl 5분, 사설 서브넷 바인딩), 감사 로그(stdout→Loki),
`acvm/acvm`(link_signing_key·click_ip_salt) 1회 생성. **역할 매핑**: api→acvm-api, worker→acvm-worker(+publisher, 큐 분리 전
임시), render→acvm-worker, publisher→acvm-publisher, loops→acvm-worker+acvm-publisher.

비밀 적재 (셸 히스토리에 남지 않게 컨테이너 셸에서 `read -s`):

```bash
docker compose exec -e VAULT_TOKEN vault sh
/ # read -rs AK; read -rs SK; vault kv put -mount=acvm coupang access_key="$AK" secret_key="$SK"; unset AK SK
/ # read -rs AK; read -rs SK; vault kv put -mount=acvm r2 access_key="$AK" secret_key="$SK"; unset AK SK
/ # read -rs K; vault kv put -mount=acvm voyage api_key="$K"; unset K
# AI 연동 키 (선택): 넣은 뒤 콘솔 › 운영 준비 › AI 연동에서 '연결 확인'을 누르면 즉시 적용 (재시작 불필요)
/ # read -rs K; vault kv put -mount=acvm anthropic api_key="$K"; unset K
/ # read -rs K; vault kv put -mount=acvm gemini api_key="$K"; unset K
/ # read -rs K; vault kv put -mount=acvm openai api_key="$K"; unset K
/ # read -rs I; read -rs S; vault kv put -mount=acvm higgsfield key_id="$I" key_secret="$S"; unset I S
/ # read -rs K; vault kv put -mount=acvm elevenlabs api_key="$K"; unset K   # voice_map은 앱이 자동 기록
# 소셜 토큰: channel_account.token_ref = tokens/<platform>/<handle> → 경로 tokens/<platform>, 키 <handle>
/ # read -rs T; vault kv patch -mount=acvm tokens/threads kitchen_pick="$T" || vault kv put -mount=acvm tokens/threads kitchen_pick="$T"; unset T
/ # exit
docker compose exec -e VAULT_TOKEN vault vault token revoke "$VAULT_TOKEN"; unset VAULT_TOKEN   # 루트 토큰 폐기
```

루트 토큰이 다시 필요하면 `vault operator generate-root`(언실 키 3개)로 재발급한다.

## 6. 전체 기동

```bash
docker compose up -d                       # acvm-migrate(acvm db upgrade) 성공 후 api/worker/loops 시작
docker compose ps                          # 모두 running/healthy, acvm-migrate = exited (0)
docker compose logs acvm-migrate | tail    # alembic "Running upgrade  -> 0001"
docker compose exec acvm-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read())"
docker compose run --rm acvm-migrate acvm seed     # 워크스페이스·계정 시드 (최초 1회)
```

확인 순서: ① `https://go.example.com/hub/<handle>` 공개 응답 ② `https://go.example.com/api/v1/me` → **404** (허브 호스트는 API 비노출)
③ `https://console.example.com` → Access 로그인 ④ Grafana `ACVM` 폴더 대시보드 5종 ⑤ 알림 경로:
`docker compose exec alertmanager amtool alert add acvm_test severity=ticket --alertmanager.url=http://127.0.0.1:9093`.
Temporal UI: `ssh -L 8088:127.0.0.1:8088 ops@server` → http://localhost:8088.

## 7. 백업 설치 (§10: RPO 15분 / RTO 2시간)

```bash
sudo cp /opt/acvm/deploy/backup/systemd/acvm-* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now acvm-backup.timer acvm-restore-drill.timer
/opt/acvm/deploy/backup/backup.sh all       # 첫 풀 백업 (WAL 은 archive_command 로 상시 전송)
/opt/acvm/deploy/backup/backup.sh list
/opt/acvm/deploy/backup/restore.sh drill    # 복구 훈련: RTO/RPO 측정 → Grafana/Pushgateway 기록
```

## 8. 업그레이드 · 롤백

```bash
# .env 의 ACVM_VERSION 변경 → 서명 검증(4단계) → 
docker compose pull && docker compose up -d     # acvm-migrate 가 먼저 돌고, 실패하면 앱은 재시작되지 않음
```

롤백은 이전 `ACVM_VERSION`으로 `up -d`. 스키마는 **전진만**(down 마이그레이션을 운영에 쓰지 않음) — 스키마 손상 시
RUNBOOK [복구](RUNBOOK.md#restore)로 PITR. 0001은 G0 이전까지 ORM 메타데이터에서 생성되며, G0 이후 모든 스키마 변경은
`uv run alembic revision --autogenerate -m "..."`로 새 리비전을 만든다.

## 9. 로컬 개발 스택

`make up` = `docker-compose.dev.yml` 오버라이드: 소스에서 이미지 빌드, 포트 127.0.0.1 공개, Vault dev 모드(토큰 `dev-root-token`),
`ACVM_PLATFORMS__DRY_RUN=true`, `ACVM_COUPANG__SANDBOX=true`, 비밀은 `.env`의 `ACVM_SECRET__*`. 포트: api 8000, console 3000,
grafana 3001, prometheus 9090, temporal-ui 8088, postgres 5432. 트레이싱이 필요하면
`-f docker-compose.tracing.yml`(Tempo) 추가.

## 10. G0 체크리스트 (실 API 연결 승인 — 상세는 ROADMAP)

- [ ] `make lint test test-ops` 녹색, CI `migrations-postgres` 잡(트리거·HNSW 검증) 녹색, release 이미지 서명 검증 완료
- [ ] `docker compose ps` 전부 healthy, Grafana `ACVM · SLO 개요`에 데이터 유입, 테스트 알림 page/ticket 수신 확인
- [ ] Vault: 루트 토큰 폐기, 언실 키 3/5 오프라인 분산, `bootstrap.sh status` 역할 5종, 앱에 `ACVM_SECRET__*` 없음
- [ ] 허브 호스트에서 `/api/v1/*` 404, 콘솔·Grafana는 Access 로그인 없이는 403
- [ ] `backup.sh all` 성공 + `restore.sh drill` PASS (RTO ≤ 7200 s), `BackupPostgresStale` 경보 해제 상태
- [ ] §8.3 법·정책 체크리스트(파트너스 운영정책·추천보증 심사지침·AI 표시 의무·플랫폼 약관·소득 신고 주체) 서면 확인
- [ ] 게이트 회귀 테스트 100 % (`uv run --no-sync pytest -k gate`), `acvm:gate_rule_regressions:increase24h = 0`
- [ ] 위 항목 확인 후: `env/acvm.env` → `ACVM_ENV=prod`, `ACVM_COUPANG__SANDBOX=false`, 실 `ACVM_COUPANG__PARTNER_ID`;
      `.env` → `ACVM_ENV=prod`; `docker compose up -d`. 실게시(`ACVM_PLATFORMS__DRY_RUN=false`)는 G1 이후.

## 11. 단일 서버 배포 (appctl·Caddy 공용 서버 — momenpic.tburn.biz)

Docker 스택을 띄우기 전, 기존 공용 서버(`167.86.116.198`, Caddy + `appctl`)에 ACVM 한 벌을 바로 올리는 방법입니다.
도메인 `momenpic.tburn.biz` 는 Cloudflare(프록시)를 거쳐 Caddy 로 들어오고, 인증서는 Caddy 가 받습니다.

| 항목 | 값 |
|---|---|
| 등록 이름 / 포트 | `momenpic` / appctl 이 배정(예: 4046) — `appctl list` |
| 코드 | `/srv/apps/momenpic` (`.venv` Python, `console/.next/standalone` 콘솔) |
| 데이터 | `/srv/apps/momenpic/data` — `acvm.db`(SQLite WAL), `objects/`(영상·이미지), `secrets.json`(콘솔에서 등록한 키, 600) |
| 설정 | `/etc/apps/env/momenpic.env` (root:apps 640) — 콘솔 접속 토큰 `ACVM_SECURITY__DEV_API_TOKEN` 포함 |
| 프로세스 | `app@momenpic` → `deploy/server/launcher.mjs` 가 API(uvicorn)·콘솔(Next.js)·루프를 띄우고 한 포트로 묶음 |
| 경로 라우터 | `http://167.86.116.198/momenpic` → `https://momenpic.tburn.biz/` (308) |
| 로그 | `/var/log/apps/momenpic.log` (`appctl logs momenpic`) |

배포·업데이트 (내 PC → 서버):

```bash
# 1) 올리기 (.git·.venv·var·node_modules·.next 제외 — 로컬 DB·비밀값은 올라가지 않음)
tar -czf momenpic.tar.gz --exclude=./.git --exclude=./.venv --exclude=./var --exclude=./.claude --exclude=node_modules \
  --exclude=.next --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache -C . .
scp momenpic.tar.gz root@167.86.116.198:/tmp/ && ssh root@167.86.116.198 \
  "tar -xzf /tmp/momenpic.tar.gz -C /srv/apps/momenpic && rm /tmp/momenpic.tar.gz"
# 2) 설치·재시작 (멱등 — 데이터·키·토큰 유지)
ssh root@167.86.116.198 "bash /srv/apps/momenpic/deploy/server/install.sh momenpic momenpic.tburn.biz"
```

콘솔 접속은 **회원 로그인**(`ACVM_SECURITY__AUTH_MODE=session`)입니다. `https://momenpic.tburn.biz/login` 에서 이메일과
비밀번호로 들어가며, 가입은 `/signup` → 소유자가 `회원 관리`에서 승인합니다.

최초 소유자는 서버에서 한 번 만듭니다. 비밀번호는 숨김 입력이라 화면·셸 기록·로그에 남지 않습니다:

```bash
ssh -t root@167.86.116.198 "bash /srv/apps/momenpic/deploy/server/acvm.sh user create <이메일> --role owner --name <이름>"
```

같은 명령으로 비밀번호를 다시 정할 수 있고(그 회원의 모든 로그인 해제), `acvm.sh user list`·`user disable <이메일>` 도 씁니다.
스크립트용 서비스 토큰(`ACVM_SECURITY__SERVICE_TOKEN`)은 env 파일에만 있으며 비우면 꺼집니다.

이 모드의 한계 (운영 전환 시 §1~§8 의 Docker·Postgres·Vault·Cloudflare Access 구성으로 옮길 것):
- SQLite 단일 파일·단일 서버. 백업은 `data/` 폴더를 통째로 복사(앱 정지 후 또는 `sqlite3 .backup`).
- 회원 DB(`app_user`·`auth_session`)는 SQLite 안에 있습니다. 비밀번호는 scrypt 해시, 세션은 HttpOnly 쿠키(토큰 해시만 저장).
  사내 SSO 가 필요해지면 Cloudflare Access(§2, `AUTH_MODE=cloudflare_access`)로 전환할 수 있습니다.
- Cloudflare 프록시의 응답 제한 100초: 'AI 모션'처럼 오래 걸리는 동기 요청은 100초를 넘으면 524 가 날 수 있습니다
  (렌더는 백그라운드라 영향 없음).
