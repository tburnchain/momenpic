# ACVM — AI 제휴 콘텐츠 자판기 (Affiliate Content Vending Machine)

자연어 명령 한 줄로 **상품 발굴 → 선별 → 링크 → 제작 → 검수 → 배포 → 학습** 7단계를 실행하는 엔터프라이즈 시스템입니다.
원본 "자동 현금 자판기"의 기능을 그대로 재현하면서, 계정 정지와 수익 몰수를 부르는 요소는 구조적으로 제거했습니다.
설계서: *AI 제휴 콘텐츠 자판기(ACVM) 엔터프라이즈 구축 설계서* (2026-10-08).

```
"주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘"
  → 계획: M1.select(1013, n=5, min_pos=0.7) → M4 대본(3훅, 22초) → M6 렌더(광고 오버레이 고정) → M7 게이트
          → threads(본문 링크) · instagram(링크 허브) · 09–23시 분산 ±7분 · 추정 750원 / 상한 2,000원
```

## 빠른 시작 (오프라인 · 개발)

```bash
uv sync
```

```bash
uv run acvm demo --days 3
```

`demo`는 샌드박스 쿠팡 API와 샌드박스 플랫폼 위에서 실제 FFmpeg 렌더, 게이트, 스케줄 게시, 성과 수집, 베이지안 학습, 증폭까지 3일치를 수 분 안에 시뮬레이션합니다. 결과 영상과 DB는 `var/demo/`에 남습니다.

서버와 콘솔을 띄우려면 다음 순서로 실행합니다.

```bash
uv run acvm seed --mature
```

```bash
uv run acvm serve
```

```bash
cd console && npm install && npm run dev
```

API 문서는 http://localhost:8000/api/docs 에서, 콘솔은 http://localhost:3000 에서 열립니다(개발 토큰: `dev-owner-token`, 콘솔 ⚙ 설정에 입력).

콘솔의 **쇼츠 스튜디오**에서는 상품을 고르면 ① 영상소스 ② 대본 ③ 워터마크 ④ 보이스 ⑤ 자막을 AI가 모두 채웁니다.
원하는 단계만 고친 뒤 `영상 생성하기` → `검수 후 게시`를 누르거나, `전부 자동 생성` 한 번으로 끝낼 수 있습니다.

**AI 연동** (`운영 준비 › AI 연동`): 키를 넣으면 서버가 바로 확인하고 관련 단계를 켭니다. 재시작은 필요 없습니다.
- **Claude**: 대본 작성·검수
- **Gemini**: Claude가 없을 때 대신하고, 장애 시 자동 대체합니다. 대본 추출(STT)도 맡습니다
- **GPT Image**: 문제 장면 배경을 만듭니다. 상품은 생성하지 않습니다
- **Higgsfield**: AI 장면을 모션 영상으로 바꿉니다
- **ElevenLabs**: 실제 음성으로 바꾸고 빠른 보이스를 자동 매핑합니다

자세한 내용은 [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) 0-1에 있습니다.

쇼츠는 **20~25초 상품 설명**이고 **빠르고 간결한 보이스만** 씁니다.
- 보이스 카탈로그에는 속도 ×1.2 이상인 9종만 있습니다. 느린 보이스를 다시 넣으면 앱이 시작되지 않습니다. 예전 보이스 id는 빠른 보이스로 연결됩니다.
- 대본은 빠른 속도(8음절/초)로 길이를 추정합니다. 25초를 넘으면 장점 문장을 줄이고, 그래도 넘으면 생성을 막습니다.
- 렌더할 때 실제 말하기 속도를 재서 7.5음절/초보다 느리면 8음절/초까지 올립니다(최대 ×1.35). 그래도 느린 음성은 거부합니다.
매일 운영 방법은 [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md)에 있습니다.

## 구조

| 계층 | 위치 | 역할 |
|---|---|---|
| 제어 평면 | `acvm/agents/commander.py`, `acvm/orchestration/planner.py` | 명령 → 슬롯 → 실행 계획(DAG)·비용·승인. LLM은 해석만 하고 실행은 결정적 코드가 맡음 |
| 오케스트레이션 | `acvm/orchestration/` | `pipeline.py` 활동 · `local.py` 인프로세스 · `temporal/` 내구 워크플로 · `loops.py` 주기 작업 |
| 실행 평면 | `acvm/modules/m1…m10` | 모듈 간 직접 호출 금지(아키텍처 테스트가 강제). 이벤트(§6.3)와 공유 데이터 계층으로만 연결 |
| 단일 게이트 | `acvm/modules/m7_gate/` | 생산 → 배포 사이의 유일한 경로. 규칙 17종, 회귀 테스트 100% 커버 |
| 수학 | `acvm/quant/` | 손익분기·꼬리분포·POS·켤레 베이지안·Thompson·MinHash/LSH·쿼터·스케줄·이상 탐지 → [docs/MATH.md](docs/MATH.md) |
| 정책 | `acvm/policy/` | 금칙어·대가성 문구·주장-근거 검증. M4가 *회피*하고 M7이 *강제* |
| 데이터 | `acvm/db/` (PostgreSQL 16 + pgvector), `acvm/infra/perf_*` (ClickHouse) | `content_id` 조인, subId ↔ publication 1:1 |
| 인프라 어댑터 | `acvm/infra/` | 쿠팡 파트너스(HMAC), Threads/Instagram/Facebook/YouTube/TikTok 공식 API, 카카오 수동 큐, Vault, R2, Redis, Claude |
| API·콘솔 | `acvm/api/`, `console/` | FastAPI `/api/v1`, Next.js 운영 콘솔(HOLD 큐, 대시보드 4종, 수익 시뮬레이터) |
| 배포·운영 | `deploy/`, `.github/workflows/` | Docker Compose(P0) → k3s(2단계), Prometheus SLO 경보, Grafana, WAL-G 백업 |

### 모듈

| 모듈 | 핵심 |
|---|---|
| M1 상품 인텔리전스 | 쿠팡 Open API(HMAC) · 검색 8회/시간 예산 · 403 서킷(1시간 후 단건 탐침) · 6시간 캐시 · POS · 규제 품목 하드 배제 |
| M2 Content DNA | 자기 채널 꼬리 성과물의 구조만 학습. 타인 미디어는 저장하지 않고 메타데이터만 사용 |
| M3 링크 | 게시물마다 subId 1개 · HMAC 서명 · 파트너 ID 검증 · 링크 허브(대가성 문구 최상단) · `/r` 클릭 로그(IP 해시) · 6시간 헬스 체크 |
| M4 스크립트 | Writer(중형)·Critic(대형, 다른 모델) · 주장-근거 태깅 · 금칙어 · 30일 MinHash 다양성 · 한국어 조사 처리 |
| M5 에셋 | 권리 원장. 원장에 없는 에셋은 렌더 불가. 상품 실물은 AI로 생성·변형하지 않음 |
| M6 렌더 | YAP-1000 브리지(ElevenLabs TTS·정렬) + 네이티브 FFmpeg · 1080×1920/30fps/H.264/−14 LUFS · 광고 오버레이를 템플릿 레벨에서 잠금 |
| M7 게이트 | PASS / FIX / HOLD / BLOCK · 게시 직전 preflight → [docs/COMPLIANCE.md](docs/COMPLIANCE.md) |
| M8 배포 | 공식 API만 사용 · 쿼터 이중 강제(API 한도 30% 이하) · 24시간 이동창 · 쿨다운 · 상태 머신 · 사가 보상 |
| M9 분석 | 인사이트(시간별) + 쿠팡 리포트(D+1) subId 조인 · 무효 클릭 감시 · 머니/파이프라인/리스크/학습 대시보드 |
| M10 학습 | Thompson Sampling(Beta-Bernoulli × Gamma-Poisson) · 탐색 20% · 증폭(48시간·상위 2%·변형 5개) · 퇴출 · 플레이북 KB |

## 테스트

```bash
uv run pytest
```

```bash
uv run pytest -m render
```

설계서의 수치(I* = 694.4회, 7.39배, 63%/95%, 750원/2,000원 등)는 테스트가 매 빌드마다 다시 계산합니다.

## 문서

- [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) — 하루 15분 운영 가이드 (스튜디오·HOLD·운영 준비)
- [docs/MATH.md](docs/MATH.md) — 수식·유도·검증값
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md) — 게이트 규칙과 운영
- [docs/DEPLOY.md](docs/DEPLOY.md) · [docs/RUNBOOK.md](docs/RUNBOOK.md) · [docs/SECURITY.md](docs/SECURITY.md) · [docs/ROADMAP.md](docs/ROADMAP.md)

## 실서비스 전환 전 결정 사항 (설계서 부록 B)

1. 수수료 수령 주체(개인 vs TBURN LABS 법인)와 파트너스 계정 명의
2. 1차 니치 3개와 계정 브랜드명 — 시드 기본값은 `kitchen_pick`·`car_pick`·`tidy_pick` (주방·차량·정리수납)
3. B-roll 직접 촬영 여부(샘플 구매 예산). 촬영본은 `own_footage`로 원장에 등록하면 최우선으로 쓰입니다
4. 외부 판매(SaaS화) 여부. 데이터 모델은 이미 `workspace` 단위 멀티테넌시를 지원합니다

실키 연결 방법은 `ACVM_COUPANG__SANDBOX=false`, `ACVM_PLATFORMS__DRY_RUN=false`, `ACVM_LLM__BACKEND=anthropic`, Vault 토큰 등록입니다. 절차는 [docs/DEPLOY.md](docs/DEPLOY.md)의 G0 체크리스트에 있습니다.
