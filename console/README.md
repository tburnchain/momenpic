# ACVM 운영 콘솔

AI 제휴 콘텐츠 자판기(ACVM)의 운영 콘솔입니다. 한 줄 명령 → 계획(DAG) → 승인 → 생산 → M7 게이트 → 배포 → 분석 → 학습 루프를
한 화면에서 다루고, 운영자의 하루 15분(대부분 HOLD 큐)을 기준으로 설계했습니다.

- Next.js 16 (App Router, TypeScript strict) · Tailwind CSS 4 · TanStack Query 5 · zod 4
- 차트는 의존성 없는 SVG (단일 y축, 표로 보기, 키보드 탐색)
- 다크/라이트 테마, 모바일 대응(HOLD 검토·스튜디오 포함), 한국어 UI, 금액 ₩·천 단위, 시간 KST

## 실행

```bash
npm ci

# 1) 실제 API (기본 http://localhost:8000/api/v1)
cp .env.example .env.local     # 필요하면 NEXT_PUBLIC_ACVM_API 수정
npm run dev                    # http://localhost:3000

# 2) 모의 모드 — 백엔드 없이 src/mocks 의 fixture 로 동작 (상태는 탭 메모리)
npm run dev:mock               # = NEXT_PUBLIC_ACVM_MOCK=1 next dev

# 3) 프로덕션 빌드 (output: standalone)
npm run build                  # 모의 모드 빌드는 npm run build:mock
npm start                      # .next/static·public 을 standalone 옆에 복사하고 server.js 실행 (Docker와 동일)

# 4) Docker (멀티 스테이지, non-root, 헬스체크)
docker build -t acvm-console --build-arg NEXT_PUBLIC_ACVM_API=https://acvm.example.com/api/v1 .
docker run --rm -p 3000:3000 acvm-console
```

검증 스크립트

```bash
npm run lint && npm run typecheck && npm run build
npm run validate:mocks                         # 모든 모의 응답을 zod 스키마로 검증 (93개)
ACVM_TOKEN=dev-owner-token npm run validate:api   # 실행 중인 백엔드의 모든 GET 응답을 스키마로 검증
```

### 백엔드와 함께 (로컬)

```bash
# 저장소 루트에서
uv run acvm seed --mature
uv run acvm serve --port 8000
# 콘솔: http://localhost:3000 → 우측 상단 ⚙(연결·표시 설정) → 개발용 Bearer 토큰 입력
```

API의 CORS 허용 목록(`ACVM_CONSOLE_ORIGINS`, 기본 `http://localhost:3000`)에 콘솔 origin이 있어야 합니다.

## 설정 · 인증

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `NEXT_PUBLIC_ACVM_API` | `http://localhost:8000/api/v1` | API base URL (빌드 시 인라인) |
| `NEXT_PUBLIC_ACVM_MOCK` | `0` | `1`이면 모의 모드 |

- **개발**: 설정 대화상자에 Bearer 토큰 입력 → `localStorage`에 저장 → `Authorization: Bearer <token>`.
  개발용 역할 전환(`X-Dev-Role`, API가 env=dev/test일 때만 적용)으로 RBAC 화면을 확인할 수 있습니다.
- **운영**: 콘솔은 Cloudflare Access 뒤에 있고 모든 요청은 `credentials: "include"` 로 나갑니다.
  API가 `Cf-Access-Jwt-Assertion`/`CF_Authorization` 을 검증합니다(토큰 입력 불필요).
- 현재 사용자는 `GET /me`로 표시하고, 역할이 할 수 없는 동작은 숨깁니다(API도 동일 매트릭스로 강제).

| 동작 | owner | operator | reviewer | viewer |
| --- | :-: | :-: | :-: | :-: |
| 명령·예약·스튜디오(초안·생성·게시)·수동 게시 완료 | ✓ | ✓ | | |
| 계획 승인/반려 · HOLD 승인/반려 · 게시 중단 | ✓ | | ✓ | |
| 소스 업로드·컷편집·이미지→영상·대본 추출 | ✓ | ✓ | | |
| 계정·토큰 · 비밀값 · G0 체크리스트 · 워크스페이스 예산 | ✓ | | | |
| 규칙 | 읽기 전용 | 읽기 전용 | 읽기 전용 | 읽기 전용 |

## 페이지

| 경로 | 화면 |
| --- | --- |
| `/studio` | **쇼츠 스튜디오** — 상품 선택 → AI 초안(모든 단계 자동 채움) → ①영상소스 ②대본(실시간 검수·주장 근거) ③워터마크 ④보이스 ⑤자막 → 9:16 미리보기 → 영상 생성 → 검수 후 게시, ‘전부 자동 생성’, 툴(컷편집·대본 추출·이미지→영상) |
| `/` | 명령 — 자연어 명령, 예시 칩, 계획 미리보기(슬롯·대상·스케줄·비용·승인 사유·DAG), 승인/반려, 최근 실행. 실패한 준비 항목이 있으면 소유자는 세션당 1회 `/setup` 으로 이동(해제 가능) |
| `/holds` | HOLD 큐 — 한 건씩, `A` 승인 · `R` 반려 · `J/K` 이동, 첫 줄 대가성 문구 확인, “오늘 남은 검토 n건 · 예상 n분” |
| `/publications` | 게시 현황 — 상태·플랫폼 필터, 주간 캘린더(KST), 상세 서랍(캡션·지표·permalink·subId·게시 중단) |
| `/dashboards/{money,pipeline,risk,learning}` | §9.3 대시보드 4종 (GMV·확정 수수료·순이익 분리, KPI#1 = 채널 중앙값 ÷ I*) |
| `/runs`, `/runs/[id]` | 실행 목록·상세(콘텐츠 카드, 게이트 판정, 게시 표, 비용 바) |
| `/contents/[id]` | 콘텐츠 상세 — 영상, 비트별 대본, 주장·근거, 게이트 이력, 사람 검토, 게시, 비용 |
| `/accounts` | 계정 — 상태·상한·쿨다운, 토큰 상태·24h 사용량, 토큰 등록(쓰기 전용) |
| `/schedules` | 예약 — cron(KST) 반복 명령 추가·중지 |
| `/manual` | 수동 게시(카카오) — 캡션·링크 복사, 미디어 열기/다운로드, permalink 로 완료 처리 |
| `/simulator` | 수익 시뮬레이터 — I*, P(I ≥ I*), 기대 이익, 꼬리 점유율, 1−(1−q)ⁿ 곡선 |
| `/setup` | 운영 준비 — 준비 점검, G0 게이트, 비밀값(쓰기 전용), 예산, 서킷 브레이커 |
| `/settings` | 설정 — 워크스페이스 이름·일 예산, 오늘/30일 지출 |
| `/rules`, `/audit` | 규칙(읽기 전용), 감사 로그 |

헤더의 🔔 알림은 `/alerts`(높음 건수 배지, HOLD 대기, 주의 계정)를 보여 줍니다.

## 구조

```
src/
  app/                 라우트 (모두 클라이언트 데이터 패칭, 동적 라우트는 /runs/[id], /contents/[id])
  components/
    shell/             AppShell(사이드바·상단바·모바일 드로어), 알림 벨, 설정 대화상자, 테마
    ui/                Button, Card, Pill, Dialog(native <dialog>, side drawer), Field, Toast, Feedback…
    charts/            LineChart, BarList, Donut, RangeBar, RatioBar, Legend, ChartTable (SVG)
    command/ runs/ holds/ dashboards/ admin/ ops/ simulator/ studio/
  lib/
    schemas.ts         zod 스키마 (API 계약 미러링)
    api.ts             타입드 클라이언트 (실제/모의 공통 검증, 업로드·바이너리 미리보기)
    queries.ts         TanStack Query 훅 · 키
    format.ts          ₩/KST/퍼센트/한국식 만·억 단위
    labels.ts rbac.ts quant.ts cron.ts hooks.ts session.ts
  mocks/               fixtures.ts · ops.ts · studio.ts · media.ts(SVG) · handler.ts(라우팅)
scripts/               with-mock.mjs · validate-mocks.ts · validate-api.ts
```

## API 계약 메모

원래 콘솔 계약과 실제 백엔드(`src/acvm/api`) 응답이 다른 부분은 `schemas.ts`의 작은 전처리(`aliases()`)로
양쪽 모두 받도록 했습니다 — 계약 필드명이 정본입니다.

- `runs[].summary`: 계약은 문자열, 백엔드는 dict(`selected`, `verdicts`, `products`…) → 한 줄 요약으로 렌더
- 대시보드: `publications_by_state`→`publication_states`, `render_video_seconds`(영상 길이 p50/p90)→`render`(metric 표시),
  risk `account_id`→`id` · warnings `handle`→`account`, learning `arm_key`→`key` · `ctr_90`→`ctr_ci`(90% 표시) ·
  `allocation`→`explore_exploit`, money channels `handle`→`account`. CI 경계·비율은 `null` 허용.
- `POST /commands`는 `plan`/`approval`이 `null`일 수 있음(계획 실패) → 실패 카드 표시.
- `DELETE /schedules/{id}`는 삭제가 아니라 비활성화 → UI 문구 “중지”.
- `daily_cap` 1–50(백엔드 검증과 동일), `effective_rate`는 비율(0.03 = 3%), 백분위는 0–1/0–100 모두 허용.
- 게시 상태는 열린 문자열(QUEUED…/MANUAL_PENDING/CANCELLED/TAKEN_DOWN 라벨링, 미지 값은 그대로 표시).
- 인증이 필요한 바이너리(보이스 미리듣기, 자막 스타일 타일)는 `fetch`+Blob URL로 불러옵니다(`<img>`/`<audio>`는 Bearer 헤더를 못 보냄).
- 쿠팡 **샌드박스** 상품 이미지(`/image/sandbox/…`)는 존재하지 않는 경로라 요청하지 않고 대체 아이콘을 씁니다.

## 디자인 원칙

- 머니 뷰는 GMV / 확정 수수료 / 취소 차감 / 비용 / 순이익을 **항상 분리**해 표시(“매출” 단일 숫자 없음, §2.4).
  GMV는 수수료와 규모가 달라 별도 차트(이중 축 금지).
- 상태 색(통과·자동수정·보류·차단)은 아이콘·라벨과 함께만 쓰고, 플랫폼 색은 고정 슬롯(엔티티별)입니다.
- 쓰기 전용 값(비밀값·계정 토큰)은 전송 후 입력란을 비우고 다시 표시하지 않습니다. `persistent=false`면
  “Vault 미연결: 재시작 시 사라집니다”를 표시합니다.
