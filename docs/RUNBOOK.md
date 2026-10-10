# ACVM 장애 대응 런북

설계서 §9.5 + 운영 절차. 모든 경보는 `deploy/prometheus/rules/{slo,infra}.yml`에 정의되어 있고 `runbook_url`이 이 문서의
앵커를 가리킨다. **severity=page**: 즉시 대응 · **severity=ticket**: 당일 업무 시간 내.
명령은 서버의 `/opt/acvm/deploy/compose`에서 실행한다 (`dc` = `docker compose`). API 호출은 콘솔(Access 로그인) 또는
`console.example.com/api/v1/...`로 한다 (owner/reviewer 권한).

**공통 원칙** — ① 게시를 멈추는 것이 항상 안전하다(큐는 보존됨). ② 원인 게시물·계정을 격리한 뒤 복구한다. ③ 모든 조치는
audit_log에 남는 경로(콘솔/API)로 한다. DB 직접 수정은 최후 수단이며 수정 내용을 사후 보고서에 기록한다.

### 비상 정지 (Kill switch)

| 범위 | 방법 | 효과 시점 |
|---|---|---|
| 특정 계정 | `PATCH /api/v1/accounts/{id}` `{"status":"restricted"}` | 즉시 — 도래하는 예약 게시는 `cancelled_restricted`로 취소 |
| 전체 게시 | `dc stop acvm-publisher acvm-worker acvm-loops` | 즉시 — 생산·게시·스케줄러 정지, Temporal 큐 보존 |
| 전체 게시 (프로세스 유지) | Vault에서 토큰 읽기 차단: `dc exec -e VAULT_TOKEN vault sh -c 'echo "path \"acvm/data/tokens/*\" { capabilities = [\"deny\"] }" \| vault policy write acvm-publisher -'` | ≤ 5분 (토큰 TTL·캐시 TTL) — 복구: `bootstrap.sh init` |

---

<a id="platform-4xx"></a>
## 1. 플랫폼 API 4xx 급증 → 서킷 브레이커 (§9.5-1)

**경보**: `ACVMCircuitBreakerOpen{name="platform:<p>"}`(ticket), `ACVMCircuitBreakerStuckOpen`(page),
`ACVMPublishSuccessBelowSLO` / `ACVMPublishErrorBudgetFastBurn`(page).

1. Grafana **파이프라인 뷰 → 게시 시도 결과 / 플랫폼별 실패율**에서 어느 플랫폼·outcome인지 확인.
2. 브레이커가 열렸으면 해당 어댑터 게시는 자동 보류(`circuit_open` → deferred), **큐는 보존**된다. 추가 조치 없이 대기.
3. 원인 분류 (`dc logs acvm-publisher acvm-worker --since 1h | grep -i '"platform"'`):
   - 401/190 토큰 오류 → [토큰 만료](#token-expiry)
   - 4xx 정책/스팸 경고(`warning`) → 해당 계정이 자동 48시간 cooldown. [계정 제재](#account-sanction) 확인
   - 400 요청 형식 / API 버전 폐기 → `ACVM_PLATFORMS__GRAPH_VERSION` 확인, 앱 핫픽스
   - 429 → 운영 상한(§7)이 실제 한도보다 높음: `ACVM_QUOTA__DAILY_CAP` 하향
4. 브레이커가 75분 넘게 열림(page): 단건 재시도도 실패 → 해당 플랫폼 계정들을 일시 `restricted`로 두고 원인 해결 후 `active`.
5. 사후: 실패율 SLO(99 %, 정책 거절 제외) 오류 예산 소진량 기록.

<a id="coupang-403"></a>
## 2. 쿠팡 API 403 → 즉시 중단 + 캐시 운영 + 1시간 후 단건 재시도 (§9.5-2)

**경보**: `ACVMCoupangForbidden`(page), `ACVMCoupangAttributionStale`(ticket).

연속 403은 제재 사유로 보고되므로 **재시도 폭주를 막는 것이 최우선**이다.

1. Grafana 리스크 뷰 → 서킷 브레이커에서 `coupang:*`가 OPEN인지 확인 (앱이 403 시 자동 개방, `breaker_open_seconds=3600`).
   열리지 않았다면 즉시 `dc stop acvm-loops acvm-worker` (검색·딥링크·리포트 호출원 정지).
2. 운영은 6시간 캐시(`stale_cache`)로 계속된다. 신규 상품 탐색만 멈춘다.
3. 1시간 후 브레이커가 half-open → **단건** 재시도. `acvm_coupang_calls_total{outcome="forbidden"}`가 다시 오르면 3단계 반복 금지:
   키를 비활성화하고 파트너스 고객센터 확인 (HMAC 키 만료/서명 시각 오차 `date -u` 확인, IP 차단 여부).
4. 호출 예산 점검: `ACVM_COUPANG__SEARCH_CALLS_PER_HOUR`(기본 8) 이하인지, 루프가 중복 실행되지 않는지(acvm-loops 1개).
5. 리포트(D+1) 수집이 지연되면 `ACVMCoupangAttributionStale` — 403 해소 후 다음 일일 루프에서 자동 보충.

<a id="account-sanction"></a>
## 3. 계정 제재 감지 → 예약 전부 취소 · 원인 게시물 격리 · 규칙 사후 분석 (§9.5-3)

**경보**: `ACVMAccountRestricted`(page), `ACVMAccountCooldownSpike`(ticket), `ACVMAccountsMostlyCoolingDown`(page).

1. 콘솔/`GET /api/v1/accounts`에서 대상 계정 확인 → `PATCH /api/v1/accounts/{id}` `{"status":"restricted"}`
   (이미 restricted면 생략). 이 계정의 예약 게시는 도래 시점에 모두 `cancelled_restricted` 처리된다.
2. 같은 니치의 **다른 계정으로 콘텐츠를 옮겨 게시하지 않는다**(§7.1-4 계정 = 브랜드, 우회 금지).
3. 원인 게시물 격리: 리스크 뷰 → `계정 경고·실패 이벤트`에서 직전 게시물 확인 → 플랫폼에서 비공개 처리, 콘텐츠는 HOLD 큐로
   (콘솔 HOLD 화면에서 reject).
4. 규칙 사후 분석 (24시간 내): 직전 7일 해당 계정 게시물의 게이트 판정(`compliance_check`), 유사도(`content.similarity_max`),
   게시 간격/시간대, 대가성 문구 위치. 원인이 규칙 공백이면 rule_set 버전을 올리고 회귀 테스트 추가 (§8 규칙 변경 = owner/reviewer).
5. 쿨다운 급증(여러 계정): 공통 원인(동일 템플릿·동일 상품 반복·운영 상한)을 먼저 의심 → `ACVM_QUOTA__DAILY_CAP` 하향,
   해당 템플릿 사용 중지.

<a id="sold-out"></a>
## 4. 링크 품절 대량 발생 → 대체 상품 제안 · 미처리분 게시 보류 (§9.5-4)

**신호**: 리스크/머니 뷰에서 `affiliate_link.health = sold_out|discontinued` 증가, 게시 outcome `deferred_*`(link_*) 증가.

1. 링크 헬스체커(acvm-loops, 6시간 주기)가 품절 링크의 미게시분을 자동 **보류(deferred)** 한다 — 품절 링크로는 게시되지 않는다.
2. 콘솔에서 헬스체커의 **대체 상품 제안**을 검토·승인한다. affiliate_link는 append-only(§10)라 교체는 **새 링크 발급**으로만
   이루어지고, 기존 링크 행은 수정되지 않는다 (DB 트리거가 deeplink/sub_id 변경을 거부).
3. 이미 게시된 글: 링크 허브 페이지는 품절 카드를 숨기고, 본문 링크 게시물(Threads/Facebook)은 수정 플래그를 확인해 처리.
4. 특정 카테고리에서 반복되면 M1 `P_risk`(품절 이력) 가중치 상향 요청.

<a id="token-expiry"></a>
## 5. 토큰 만료 / 갱신 실패

**경보**: `ACVMPlatformAttributionStale`, 게시 outcome `token_error`/`deferred_token` 증가.

1. Meta 장기 토큰은 만료 7일 전 acvm-loops가 자동 갱신(§10). 실패하면 해당 계정이 cooldown + 알림.
2. 확인: `dc logs acvm-loops --since 24h | grep -i token`. Vault 감사 로그(`{service="vault"}` in Loki)에서 쓰기 거부가 있으면
   publisher/loops 정책 문제 → `bootstrap.sh status`.
3. 재연결: 운영자가 플랫폼에서 재인증 → 새 토큰을 Vault에 기록
   (`vault kv patch -mount=acvm tokens/<platform> <handle>="$T"`, DEPLOY 5단계와 같은 `read -s` 방식) → 계정 `active`.
4. AppRole secret-id 자체가 문제면: `bootstrap.sh rotate acvm-<role> > env/approle-<role>.env` → `dc up -d <service>`.

<a id="budget-exhaustion"></a>
## 6. 예산 소진

**경보**: `ACVMBudgetNearlyExhausted`(90 %), `ACVMBudgetExhausted`(100 %).

1. 100 %에서 실행 중인 명령의 남은 상품은 자동 일시 중지(§5.2 예산 캐핑) — 데이터 손실 없음.
2. 파이프라인 뷰 → `LLM 비용 (purpose)`로 급증 원인 확인 (재생성 루프 FIX 반복, 모델 폴백, 대량 반복 명령).
3. 정상 사용이면 워크스페이스 일 예산 상향(콘솔 / `workspace.daily_budget_krw`), 비정상이면 반복 명령(`/api/v1/schedules`) 일시 중지.
4. 사후: C_unit(§2.3) 실측치 갱신 → `ACVM_COSTS__*`.

<a id="hold-backlog"></a>
## 7. HOLD 승인 대기 적체

**경보**: `ACVMHoldQueueBacklog` (60건 초과 2시간; 설계 기준 일 ~30건·10분).

1. `GET /api/v1/holds` → 사유별 집계. 신규 계정(14일)·신규 템플릿(10건) H-01이 대부분이면 정상 — 승인 처리.
2. R-03(상표·인물·음성) HOLD가 많으면 에셋 소스 점검. 게이트 FIX 반복 후 HOLD면 규칙/프롬프트 회귀 의심.
3. 승인: `POST /api/v1/holds/{id}/decision` (reviewer/owner). 일괄 승인 금지 — 건당 20초 검토가 설계 전제(§8.4).

<a id="gate-regression"></a>
## 8. 게이트 우회 (규칙 누락) — 즉시 게시 중지

**경보**: `ACVMGateRuleRegression` (page, `acvm_gate_rule_regressions_total` 증가 = PASS 없이 배포 경로에 도달).

1. **비상 정지** — 전체 게시 정지 (위 표).
2. 대상 게시물 확인 (`audit_log` / 로그 `gate_regression`), 플랫폼에서 즉시 비공개.
3. 대가성 문구(D-01~04)·규제 카테고리(C-04)·권리 원장(R-01) 해당 여부 확인 — 수익 몰수 리스크(§12) 최상위.
4. 원인 수정 + 회귀 테스트 추가 → 배포 → 게시 재개. SLO "게이트 누락 0건" 위반으로 사후 보고서 필수.

<a id="invalid-clicks"></a>
## 9. 무효 클릭 경보

**경보**: `ACVMInvalidClickDetected`(ticket), `ACVMInvalidClickSurge`(page).

1. 앱이 해당 링크를 자동 일시 중지했는지 확인 (리스크 뷰 → 무효 클릭 경보 reason).
2. 동일 IP/짧은 간격 반복이면 Cloudflare WAF 레이트 리밋/차단 규칙 추가 (`/r/*`). 본인·지인·봇 클릭 금지(§8.2) 재공지.
3. 급증(page): 파트너스 이용 제한 사유이므로 해당 서브ID 링크 중지 유지, 파트너스에 소명 자료(로그) 준비.

<a id="slo-latency"></a>
## 10. SLO 지연 (계획 20 s · 텍스트 3분 · 쇼츠 10분 · 렌더)

**경보**: `ACVMPlanLatencySLOBreach`, `ACVMTextCommandToPublishSLOBreach`, `ACVMShortCommandToPublishSLOBreach`,
`ACVMRenderSlow`, `ACVMPublicationsStuckInFlight`, `ACVMMetricsMissing`.

1. 파이프라인 뷰 → 단계별 p95로 병목 단계 식별 (select / tts / render / gate).
2. 계획 지연: LLM 지연·폴백 (Anthropic 상태 페이지), `ACVM_LLM__TIMEOUT_SECONDS`.
3. 렌더 지연: `dc stats acvm-render acvm-worker` CPU 포화 → 동시성(`ACVM_RENDER__CONCURRENCY`) 하향 또는 렌더 워커 증설
   (`dc up -d --scale acvm-render=2`, 서버 여유 시). 쇼츠 SLO는 HOLD 제외.
4. Temporal 적체: temporal-ui(SSH 터널)에서 task queue backlog 확인.
5. `ACVMMetricsMissing`: 앱 /metrics 미수집 → [프로세스 다운](#process-down).

<a id="process-down"></a>
## 11. 프로세스 다운

**경보**: `ACVMApiDown`(page, 링크 허브 중단 = 수익 직결), `ACVMWorkerDown`, `TemporalDown`, `RedisDown`.

1. `dc ps` → 상태, `dc logs --tail 200 <service>`.
2. 재시작: `dc up -d <service>`. 앱이 Vault 오류로 죽으면 → [Vault 봉인](#vault-unseal).
3. acvm-migrate 실패로 앱이 안 뜨면: `dc logs acvm-migrate` — 마이그레이션 오류는 앱 버전 롤백(DEPLOY 8).
4. 디스크: `HostDiskAlmostFull` 동반 시 `docker system df`, pg_wal 크기(`dc exec postgres du -sh /var/lib/postgresql/data/pgdata/pg_wal`)
   — WAL 증가는 아카이브 실패 신호 → [백업 실패](#backup-failure).

<a id="vault-unseal"></a>
## 12. Vault 봉인 (재부팅 후 필수)

**경보**: `VaultSealedOrDown`(page). 서버/도커 재시작 후 Vault는 **항상 봉인 상태**로 뜬다(수동 언실 설계).

1. 언실 키 보유자 3명(또는 3개 보관처)에서 키 확보 → `dc exec vault vault operator unseal` × 3.
2. `dc ps vault` healthy 확인. 앱은 다음 비밀 조회 시 자동 재로그인(AppRole, 5분 TTL) — 재시작 불필요. 계속 실패하면 `dc restart acvm-api acvm-worker acvm-publisher acvm-render acvm-loops`.
3. 봉인 중 게시는 토큰 조회 실패로 deferred 처리되며 유실되지 않는다.

<a id="backup-failure"></a>
## 13. 백업 실패 (RPO 위험)

**경보**: `PostgresWalArchiveFailing`(page), `PostgresWalArchiveStale`, `BackupPostgresStale`(page), `BackupClickhouseStale`,
`BackupVaultStale`, `BackupPostgresNeverRecorded`.

1. WAL 푸시 실패: `dc logs postgres --since 1h | grep -i wal-g` → R2 키/버킷/엔드포인트(`env/walg.env`), 아웃바운드 연결
   (`dc exec postgres curl -sI "$AWS_ENDPOINT"`).
2. 수동 재시도: `deploy/backup/backup.sh postgres` → 성공 시 경보 자동 해제(Pushgateway 타임스탬프).
3. pg_wal 이 계속 커지면(디스크 80 %+) 원인 해결이 우선. 임시로 `WALG_ENABLED=false`로 바꾸면 **RPO가 무한대**가 되므로 금지.

> postgres·clickhouse 는 R2 업로드를 위해 `egress` 네트워크(아웃바운드 전용, 포트 공개 없음)에도 연결되어 있다.
> `backend`는 `internal: true`라 이것이 없으면 WAL-G/`BACKUP ... TO S3`가 실패한다.

<a id="restore"></a>
## 14. 복구 (PITR, RTO 2시간)

판단 기준: 데이터 손상·실수 삭제·디스크 손실. **먼저 drill 로 대상 시점을 검증**한 뒤 실제 복구한다.

```bash
deploy/backup/restore.sh drill                                   # 최신 백업으로 리허설 (운영 무영향)
deploy/backup/restore.sh postgres --time '2026-10-08 03:10:00+00' --yes   # 시점 복구 (생략 시 최신)
deploy/backup/restore.sh clickhouse 20261008T033000Z --yes       # ClickHouse (DB 교체 방식, 이전 DB 보존)
```

`restore.sh postgres`: 앱·Temporal 정지 → 현 PGDATA를 `pgdata.broken-<ts>`로 보존 → `wal-g backup-fetch` → WAL 재생 →
검증 SQL(트리거·HNSW·alembic 버전) → 새 타임라인 풀 백업 → 앱 재기동. 검증 후 `pgdata.broken-*` 삭제.
Vault 저장소 손실: R2 `ops/vault/*.tgz` 를 vaultdata 볼륨에 풀고 언실 키로 언실 (키 없이는 복구 불가).
ClickHouse 성과 이벤트는 M9 수집기가 다음 주기에 재적재(ReplacingMergeTree라 중복 무해).

<a id="restore-drill"></a>
## 15. 복구 훈련 (월 1회, 자동)

`acvm-restore-drill.timer` (매월 첫 일요일 05:00 KST) → `restore.sh drill`: 임시 컨테이너에 최신 풀 백업 + WAL 재생,
`archive_mode=off`·`WALG_ENABLED=false`(운영 WAL 타임라인 오염 방지), 검증 후 RTO(`acvm_restore_drill_rto_seconds`)·
데이터 신선도(`acvm_restore_drill_rpo_seconds`)를 Pushgateway에 기록. 경보: `RestoreDrillOverdue`(35일), `RestoreDrillRtoExceeded`.

훈련 기록 (분기별 수동 확인 시 추가):

| 일자 | 백업 | RTO (s) | 데이터 신선도 (s) | 결과 | 확인자 |
|---|---|---|---|---|---|
| | | | | | |
