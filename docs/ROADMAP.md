# ACVM 로드맵 — 단계 P0–P4 · 게이트 G0–G3

각 게이트는 **측정 가능한 조건**의 체크리스트이고, 모든 항목은 명령 또는 Grafana/Prometheus 지표로 확인한다.
게이트를 통과하기 전에는 다음 단계의 스위치(설정값)를 켜지 않는다. 목표치는 설계서 §0(정량 목표), §7(운영 상한),
§9.2(SLO), §12(리스크)에서 가져왔다. (설계서 추출본에 §11 표가 빠져 있어, 코드의 G0/G1 참조 —
`ACVM_COUPANG__SANDBOX`, `ACVM_PLATFORMS__DRY_RUN` — 와 위 절을 기준으로 정리했다. 원문 §11과 다르면 원문이 우선.)

| 단계 | 범위 | 핵심 스위치 | 통과 게이트 |
|---|---|---|---|
| **P0** 기반 | 단일 서버 compose 스택, Vault·백업·관측·CI/CD, 샌드박스·드라이런 E2E | `ACVM_ENV=staging`, `SANDBOX=true`, `DRY_RUN=true` | **G0** 실 API 연결 |
| **P1** 텍스트 자판기 (V1) | Threads 실게시 (계정 3개, 니치 3개), 쿠팡 실 API, subId 귀속 | `ACVM_ENV=prod`, `SANDBOX=false`, Threads만 실게시 | **G1** 쇼츠·다중 배포 |
| **P2** 쇼츠·다중 배포 (V2/V3) | 렌더(M6), Instagram·Facebook·YouTube, 카카오 원클릭 큐; TikTok은 감사 통과 후 | `DRY_RUN=false`(전 플랫폼), `TIKTOK_AUDITED` | **G2** 학습 자동화 |
| **P3** 학습·증폭 (M10) | 밴딧 탐색 20 %, 증폭(상위 2 % → 변형 5개), 퇴출, 플레이북 KB | 자동 승인 확대, 반복 명령(예약) | **G3** 스케일아웃 |
| **P4** 스케일 / SaaS | k3s 이전(`deploy/k8s`), 큐 분리 완료, 다중 워크스페이스·과금 | `kubectl apply -k deploy/k8s/overlays/prod` | — |

---

## P0 — 기반 구축

- [ ] `docs/DEPLOY.md` 1–7단계 완료 (서버, Cloudflare Tunnel/Access, Vault init/unseal/bootstrap, 백업 타이머)
- [ ] CI 녹색: `ci.yml`(ruff · mypy[비차단] · pytest+coverage · console lint/build · compose/promtool/amtool/kubeconform · Trivy fs)
- [ ] 첫 릴리스 `v1.0.0` 태그 → `release.yml`(Trivy 게이트 → GHCR → SBOM → cosign) → 서버에서 `cosign verify`
- [ ] 드라이런 E2E: `docker compose run --rm acvm-migrate acvm demo` 또는 콘솔에서 "주방 5개 쇼츠 만들어서 스레드랑 인스타에 올려줘"
      → 계획 → 승인 → 생산 → 게이트 → (드라이런) 게시 → 대시보드 반영

## G0 — 실 API 연결 승인 (P0 → P1)

| # | 조건 | 확인 방법 |
|---|---|---|
| 1 | 테스트·마이그레이션 | `make lint test test-ops` 녹색; CI `migrations-postgres`(append-only 트리거·HNSW 3개) 녹색 |
| 2 | 게이트 회귀 100 % | 게이트 규칙 테스트 전부 통과; `acvm:gate_rule_regressions:increase24h == 0` |
| 3 | 관측·경보 | 5개 대시보드 데이터 유입; `amtool alert add` 테스트로 page/ticket 채널 수신; `ACVMMetricsMissing` 미발생 |
| 4 | 비밀 관리 | 루트 토큰 폐기, `bootstrap.sh status` 역할 5종, 앱 env에 `ACVM_SECRET__*` 없음, 언실 키 3/5 오프라인 분산 |
| 5 | 네트워크 | 허브 호스트 `/api/v1/me` → 404; 콘솔·Grafana Access 없이 접근 불가; `docker compose ps`에 0.0.0.0 공개 포트 없음 |
| 6 | 백업 | `backup.sh all` 성공; `restore.sh drill` PASS(`acvm_restore_drill_rto_seconds ≤ 7200`) |
| 7 | 법·정책 (§8.3) | 파트너스 운영정책(자동화 범위·링크 허브·subId), 추천보증 심사지침(문구·위치), AI 생성물 표시 의무, 플랫폼 약관, 소득 신고 주체 — 서면 확인 |
| 8 | 결정 항목 (부록 B) | 수익 수령 주체, 1차 니치 3개·계정 브랜드명, B-roll 촬영 여부 확정 |

통과 시: `ACVM_ENV=prod`, `ACVM_COUPANG__SANDBOX=false`, 실 `ACVM_COUPANG__PARTNER_ID` → `docker compose up -d`.
Threads만 실게시: Threads 계정만 `active`, 나머지 플랫폼 계정은 `restricted`로 두고 `ACVM_PLATFORMS__DRY_RUN=false`.

## P1 — 텍스트 자판기 (Threads)

- 운영 상한 Threads 20건/계정/일(§7), 쿠팡 검색 8회/시간, 신규 계정 14일 HOLD
- 매일: HOLD 큐 처리(≤ 15분), 리스크 뷰 확인 / 매주: 규칙·금칙어 업데이트(§8.4)

## G1 — 쇼츠·다중 배포 승인 (P1 → P2), P1 운영 14일 이상

| # | 조건 (14일 연속) | 지표 |
|---|---|---|
| 1 | 정책 위반 게시 0건 | `acvm:gate_rule_regressions:increase24h == 0`, 계정 제재 0 (`acvm:accounts:by_status{status="restricted"}`) |
| 2 | 게시 성공률 ≥ 99 % (정책 거절 제외) | `acvm:publish_success:ratio_rate1h`, 오류 예산 소진 경보 0 |
| 3 | 텍스트 명령→게시 p95 ≤ 3분 | `acvm:command_to_publish_seconds:p95_1h{kind="text"}` |
| 4 | 계획 p95 ≤ 20 s | `acvm:command_plan_seconds:p95_1h` |
| 5 | 수익 귀속 동작 | `acvm:attribution_lag_seconds:max{source="platform"} ≤ 3600`, `{source="coupang"} ≤ 129600`; 머니 뷰 콘텐츠 ROI에 subId 1:1 행 |
| 6 | 사람 개입 ≤ 15분/일 | HOLD 대기 `acvm_queue_depth{queue="hold_review"}` 일 30건 이하, `ACVMHoldQueueBacklog` 0 |
| 7 | 무효 클릭 0 | `acvm_invalid_click_alerts_total` 증가 없음 |
| 8 | 단위 경제성 측정 | 머니 뷰 "플랫폼별 중앙값 조회수 ÷ I*" 산출, C_unit 실측 → `ACVM_COSTS__*`/`ACVM_ECONOMICS__*` 갱신 |

## P2 — 쇼츠 자판기 + 다중 배포

- 렌더 워커(ffmpeg, 1080×1920·30fps·−14 LUFS), 대가성 오버레이 첫 3초 고정(D-03), 권리 원장(R-01)
- Instagram 5 · YouTube 3 · Facebook 3 건/계정/일; TikTok은 **감사 통과 전 비공개만**(`ACVM_PLATFORMS__TIKTOK_AUDITED=false`)
- 앱 큐 분리(렌더 → `acvm-render`, PublishWorkflow → `acvm-publish`) 완료 후 `ACVM_WORKER_PUBLISHES=false` (SECURITY.md)

## G2 — 학습 자동화 승인 (P2 → P3), P2 운영 30일 이상

| # | 조건 | 지표 |
|---|---|---|
| 1 | 쇼츠 명령→게시 p95 ≤ 10분 (HOLD 제외) | `acvm:command_to_publish_seconds:p95_1h{kind="short"}` |
| 2 | 렌더 p95 ≤ 60 s (30초 영상) | `acvm:render_seconds:p95_1h` |
| 3 | 쿨다운 전환 안정 | `ACVMAccountCooldownSpike` 30일 내 ≤ 1회, `ACVMAccountsMostlyCoolingDown` 0 |
| 4 | 처리량 | 일 콘텐츠 200건 생성 / 게시 ≤ 1,000건 처리 시에도 SLO 유지 (§0) |
| 5 | arm 데이터 충분 | 학습 뷰: arm별 trials ≥ 30인 arm 다수, 관측(arm_observation) 일별 누적 |
| 6 | 예산 준수 | `ACVMBudgetExhausted` 0 (월 비용 ≈ 70–80만 원, §9.4) |

## P3 — 학습·최적화

- Thompson Sampling 탐색 20 %(`ACVM_OPTIMIZER__EXPLORATION_BUDGET`), 48시간 상위 2 % 증폭 5개, 30회 후 하위 10 % 퇴출
- 플레이북 KB 자동 갱신 → M4 프롬프트 주입; 반복 명령(예약)으로 "24시간 자판기"

## G3 — 스케일아웃 (P3 → P4)

| # | 조건 | 지표·명령 |
|---|---|---|
| 1 | 트리거: 일 게시 ≥ 1,000건 **또는** 다중 워크스페이스 판매 결정 (§9) | `sum(increase(acvm_publish_attempts_total{outcome="published"}[1d]))` |
| 2 | 단일 서버 한계 | 렌더/워커 CPU 포화로 `ACVMRenderSlow`·`ACVMShortCommandToPublishSLOBreach` 반복, 또는 디스크/메모리 경보 |
| 3 | 큐 분리 완료 | acvm-main 워커에 토큰 접근 없음 (`ACVM_WORKER_PUBLISHES=false`) |
| 4 | 이전 리허설 | 스테이징 k3s에 `kubectl apply -k deploy/k8s/overlays/prod`(이미지 태그·호스트명 교체), migrate Job 성공, KEDA 스케일 확인 |
| 5 | 복구 | 최근 30일 `restore.sh drill` PASS, 데이터 계층 이전 계획(CloudNativePG/관리형) 확정 |

## P4 — k3s · SaaS

- `deploy/k8s`: api HPA, worker/render/publisher KEDA(Temporal 백로그), loops 1개(Recreate), migrate Job, default-deny NetworkPolicy,
  PodSecurity restricted, ExternalSecrets(AppRole·DSN)
- 멀티테넌시·과금 모듈(부록 B "외부 판매 여부" 결정 시)
