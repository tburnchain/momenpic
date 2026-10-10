# ACVM 보안 통제 — 설계서 §10 → 구현 위치

가장 가치 있는 자산은 **소셜 계정 토큰**과 **파트너스 API 키**다(§10). 유출 시 계정 탈취와 수익 리다이렉트(링크 바꿔치기)가
동시에 일어나므로, 토큰은 Vault 밖으로 나가지 않고 게시 워커만 5분짜리 자격으로 접근한다.

## 1. §10 통제 매트릭스

| 영역 | 설계 통제 | 구현 | 검증 |
|---|---|---|---|
| 자격 증명 | 토큰·파트너스 키는 Vault, DB에는 경로(token_ref)만. 게시 워커는 5분 TTL 동적 자격 | `src/acvm/infra/secrets.py`(AppRole + KV v2, 캐시 ≤ 300 s) · `deploy/vault/policies/*.hcl` · `deploy/vault/bootstrap.sh`(token_ttl=5m, 서브넷 바인딩) · `channel_account.token_ref` | `tests/unit/test_ops_configs.py::test_vault_policies_least_privilege`, `bootstrap.sh status` |
| 토큰 수명 | Meta 장기 토큰 만료 7일 전 자동 갱신, 실패 시 cooldown + 알림 | acvm-loops(publisher 정책: tokens/* create/update) · 경보 `ACVMPlatformAttributionStale` | RUNBOOK [토큰 만료](RUNBOOK.md#token-expiry) |
| 링크 무결성 | affiliate_link 생성 후 불변(append-only), 게시 직전 파트너 ID 서명 검증 | `src/acvm/db/migrations/versions/0001_initial.py` 트리거 `trg_affiliate_link_append_only`(id·product_id·sub_id·deeplink·short_url·hub_code·signature·publication_key·created_at 변경/DELETE/TRUNCATE 거부; health·checked_at만 변경 가능) · `src/acvm/security/linksign.py` · `ACVM_COUPANG__PARTNER_ID` | `test_ops_migrations.py`(오프라인 DDL), CI `migrations-postgres` 잡(실 PostgreSQL에서 UPDATE/DELETE 거부 확인) |
| 접근 제어 | RBAC owner/operator/reviewer/viewer, 게시 승인·규칙 변경은 owner·reviewer | `src/acvm/security/rbac.py` · API `ACVM_SECURITY__AUTH_MODE=cloudflare_access`(JWT 재검증) · Grafana JWT(Access) + 관리자 1명 | compose `acvm-api` 환경, Access 정책 |
| 명령 주입 방어 | 자연어 명령·외부 텍스트는 데이터 영역으로 분리, 외부 지시문 실행 안 함 | `src/acvm/security/prompt_guard.py` · LLM 에이전트에 게시/삭제/승인 도구 없음(§5.1) | 앱 단위 테스트 |
| 감사 | 모든 게시·승인·규칙 변경을 audit_log에 기록, 1년 보관 | `src/acvm/security/audit.py` · 트리거 `trg_audit_log_append_only`(UPDATE/TRUNCATE 항상 거부, DELETE는 `SET LOCAL acvm.retention='on'`인 보존 작업만) · BRIN(at) · loops 일일 보존 작업 · Vault 감사 로그 → Loki | CI `migrations-postgres` |
| 네트워크 | 콘솔은 Cloudflare Access(SSO) 뒤, 내부 서비스는 사설망, 공개는 링크 허브와 R2 서명 URL뿐 | compose 네트워크: `backend`(internal, 이그레스 없음) / `edge` / `egress` / `ops`; 공개 포트 없음(127.0.0.1 바인딩만) · cloudflared 인그레스: 허브 호스트는 `^/(hub\|r)/`, `^/media/`만, 나머지 404 · 콘솔·`/api/v1`·Grafana는 `originRequest.access.required` · k8s `networkpolicies.yaml`(default deny) | `test_compose_network_exposure`, `test_cloudflare_tunnel_only_exposes_hub_publicly` |
| 백업 | PostgreSQL 일 1회 풀 + WAL 연속, RPO 15분 / RTO 2시간 | `deploy/postgres`(WAL-G archive_command, archive_timeout 300 s) · `deploy/backup/backup.sh`(풀 + 보존 14개, ClickHouse BACKUP TO S3, Vault 저장소) · libsodium 클라이언트 측 암호화 · `restore.sh drill` 월 1회 · 경보 `PostgresWalArchiveFailing`, `BackupPostgresStale`, `RestoreDrillOverdue` | RUNBOOK [복구 훈련](RUNBOOK.md#restore-drill) |
| 공급망 | 이미지 서명, 의존성 취약점 스캔을 CI 게이트로 | `.github/workflows/release.yml`: 베이스 이미지 digest 고정 → buildx → **Trivy HIGH/CRITICAL 실패 시 push 전 중단** → GHCR push(provenance) → syft SBOM(SPDX) → cosign keyless 서명 + SBOM attest → verify · `ci.yml` Trivy fs 스캔 · `uv.lock`/`package-lock.json` 고정 설치 · Dependabot | `test_release_workflow_supply_chain`, DEPLOY 4단계 `cosign verify` |

## 2. Vault 역할·정책 매트릭스 (KV v2 mount `acvm`)

| KV 경로 (`acvm/data/...`) | 내용 | acvm-api | acvm-worker | acvm-publisher |
|---|---|---|---|---|
| `acvm` | link_signing_key, click_ip_salt | read | read | read |
| `r2` | R2 access/secret | read | read | read |
| `voyage`, `anthropic` | 임베딩/LLM 키 | read | read | — |
| `coupang` | 파트너스 access/secret (HMAC) | **deny** | read | — |
| `tokens/*` | 소셜 OAuth 토큰 (`tokens/<platform>` 키=`<handle>`) | **deny** | — (암묵적 거부) | read · create · update |

AppRole 역할 → 정책: `acvm-api`→api · `acvm-render`→worker · `acvm-publisher`→publisher · `acvm-loops`→worker+publisher
(인사이트 수집·토큰 갱신) · `acvm-worker`→worker(+publisher, 아래 큐 분리 전 임시). 모든 역할: token_ttl 5m, token_max_ttl 30m,
토큰·secret-id 모두 사설 서브넷(`ACVM_BACKEND_SUBNET`) 바인딩, secret-id는 `bootstrap.sh rotate`로 교체(분기 1회 권장).
worker 정책에 tokens 경로를 명시적 deny로 쓰지 않는 이유: deny는 정책 합집합보다 우선하므로 loops(worker+publisher) 조합을 깨뜨린다.

### 큐 분리 (임시 상태 — 앱 변경 필요)

현재 앱은 `CommandWorkflow`가 `produce_product`(렌더 포함)를 acvm-main에서 실행하고, 자식 `PublishWorkflow`도 acvm-main을
상속한다. 그래서 P0 compose는 **acvm-worker가 렌더 이미지(ffmpeg)를 쓰고 publisher 정책을 함께 가진다**
(`ACVM_WORKER_PUBLISHES=true`, 기본값). 스케줄러(acvm-loops)가 시작하는 게시 워크플로는 이미 `acvm-publish` 큐 →
acvm-publisher로 간다. 앱이 렌더 활동을 `acvm-render`, 자식 PublishWorkflow를 `acvm-publish`로 보내면:
`ACVM_WORKER_PUBLISHES=false bootstrap.sh init` → acvm-worker 이미지를 `acvm-worker`로 변경 → 토큰 접근이 publisher·loops로 한정된다.

## 3. 컨테이너·호스트 하드닝

- 이미지: 비루트 uid 10001, tini PID 1, `read_only` 루트 FS(+`/tmp` tmpfs, `/app/var` 볼륨), `cap_drop: [ALL]`,
  `no-new-privileges`, HEALTHCHECK, 이미지에 비밀 없음(`.dockerignore`가 `.env*`, `secrets/` 제외).
- 비밀 파일: `deploy/compose/.env`, `env/*.env`, `secrets/*` — `chmod 600`, git 무시. 인프라 비밀번호는 hex(URL 안전).
- Vault: 파일 스토리지 + 수동 언실(Shamir 3/5), mlock(IPC_LOCK), 루트 토큰 폐기, 리스너는 내부망 HTTP(공개 포트·터널 경로 없음).
  내부망 TLS가 필요하면 `vault.hcl`에 `tls_cert_file`/`tls_key_file`을 지정하고 `ACVM_SECURITY__VAULT_ADDR=https://vault:8200`.
- 로그: structlog 가 `token/secret/password/authorization/api_key` 키를 마스킹, OTel collector가 Authorization·쿠키·Access JWT·쿼리스트링을
  제거, 클라이언트 IP는 해시. Loki 보존 30일, audit_log 1년.
- **잔여 위험 — promtail의 docker.sock(ro)**: 읽기 전용이어도 Docker API 접근은 사실상 호스트 권한이다. promtail은 이그레스 없는
  `backend` 망에만 연결. 위험을 더 줄이려면 Grafana Alloy + `loki` Docker 로깅 드라이버로 교체.
- k8s(2단계): 네임스페이스 PodSecurity `restricted`, default-deny NetworkPolicy, 서비스어카운트 토큰 미마운트, ExternalSecrets는
  배포용 자격(AppRole id, DSN)만 동기화하고 앱 비밀은 런타임에 Vault에서 읽는다.

## 4. 운영자 보안 절차

| 주기 | 작업 |
|---|---|
| 상시 | 콘솔·Grafana 접근은 Access SSO만. 루트 토큰은 필요 시 `generate-root`로 발급 후 즉시 폐기 |
| 분기 | AppRole secret-id 교체 (`bootstrap.sh rotate acvm-<role>`), Access 정책 구성원 검토, R2 토큰 교체 |
| 릴리스마다 | `cosign verify` 후 배포, Trivy SARIF(코드 스캐닝) 확인 |
| 사고 시 | 비상 정지(RUNBOOK 상단) → 토큰 폐기(플랫폼) → Vault 토큰 갱신 → audit_log·Vault 감사 로그로 범위 확인 |
