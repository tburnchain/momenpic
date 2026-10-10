/**
 * Realistic fixture data for mock mode (NEXT_PUBLIC_ACVM_MOCK=1).
 *
 * Everything here is typed as the *wire* shape (`z.input<…>`) of the contract schemas, so `tsc`
 * rejects fixtures that drift from the contract, and every response is additionally parsed by
 * the same zod schemas at runtime (api.ts) and by `npm run validate:mocks`.
 *
 * Numbers follow the design spec: C_unit ≈ ₩150, CTR 0.8 %, CVR 3 %, AOV ₩30,000, r 3 %
 * → I* ≈ 694 views (§2.5); per-account daily caps from §7; rule set v1 from §8.1.
 */
import type { z } from "zod";
import type * as S from "@/lib/schemas";
import { posterSvg, productSvg } from "./media";

export type RunIn = z.input<typeof S.RunSummary>;
export type PlanIn = z.input<typeof S.Plan>;
export type ContentIn = z.input<typeof S.Content>;
export type PublicationIn = z.input<typeof S.Publication>;
export type HoldIn = z.input<typeof S.HoldItem>;
export type AccountIn = z.input<typeof S.Account>;
export type ScheduleIn = z.input<typeof S.Schedule>;
export type ManualIn = z.input<typeof S.ManualItem>;
export type AuditIn = z.input<typeof S.AuditEntry>;
export type FindingIn = z.input<typeof S.Finding>;
export type TargetIn = z.input<typeof S.PlanTarget>;
export type StepIn = z.input<typeof S.PlanStep>;
type PlatformT = z.input<typeof S.Platform>;
type RunStatusT = z.input<typeof S.RunStatus>;

export const DISCLOSURE_TEXT = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.";
export const DISCLOSURE_VIDEO = "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다";

/* ------------------------------------------------------------------ utilities */

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gauss(rnd: () => number): number {
  const u = Math.max(rnd(), 1e-9);
  const v = rnd();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

const iso = (ms: number) => new Date(ms).toISOString();

function hex(rnd: () => number, len: number): string {
  let s = "";
  for (let i = 0; i < len; i++) s += Math.floor(rnd() * 16).toString(16);
  return s;
}

export function uuid(rnd: () => number): string {
  return `${hex(rnd, 8)}-${hex(rnd, 4)}-4${hex(rnd, 3)}-a${hex(rnd, 3)}-${hex(rnd, 12)}`;
}

/* ------------------------------------------------------------------ catalog */

export type CategoryKey = "kitchen" | "car" | "storage";

export const CATEGORIES: Record<CategoryKey, { label: string; id: string; niche: string }> = {
  kitchen: { label: "주방", id: "COUPANG-1013", niche: "주방" },
  car: { label: "차량용품", id: "COUPANG-1018", niche: "차량" },
  storage: { label: "정리수납", id: "COUPANG-1015", niche: "정리수납" },
};

export interface ProductSeed {
  id: number;
  name: string;
  price: number;
}

export const PRODUCTS: Record<CategoryKey, ProductSeed[]> = {
  kitchen: [
    { id: 7712001, name: "실리콘 과자 밀봉 클립 12P", price: 8900 },
    { id: 7712002, name: "스텐 304 냄비 받침 3종 세트", price: 12800 },
    { id: 7712003, name: "싱크대 배수구 거름망 100매", price: 9900 },
    { id: 7712004, name: "자석 키친타월 홀더", price: 15900 },
    { id: 7712005, name: "원터치 양념통 6P 세트", price: 19800 },
    { id: 7712006, name: "접이식 실리콘 깔때기 2P", price: 6900 },
    { id: 7712007, name: "실리콘 주방 집게 2P", price: 11900 },
    { id: 7712008, name: "냉장고 계란 트레이 30구", price: 13500 },
    { id: 7712009, name: "도마 거치대 스탠드", price: 17900 },
    { id: 7712010, name: "전자레인지 덮개 실리콘", price: 7900 },
  ],
  car: [
    { id: 7718001, name: "차량용 틈새 수납함 2P", price: 13900 },
    { id: 7718002, name: "무선 핸디 차량 청소기", price: 39000 },
    { id: 7718003, name: "차량용 햇빛가리개 우산형", price: 17900 },
    { id: 7718004, name: "헤드레스트 후크 4P", price: 7900 },
    { id: 7718005, name: "차량용 컵홀더 확장 트레이", price: 15800 },
    { id: 7718006, name: "트렁크 정리함 접이식", price: 24900 },
  ],
  storage: [
    { id: 7715001, name: "투명 냉장고 정리 트레이 6P", price: 24900 },
    { id: 7715002, name: "옷장 압축 행거 5단", price: 16800 },
    { id: 7715003, name: "서랍 칸막이 정리함 8P", price: 14500 },
    { id: 7715004, name: "벽걸이 멀티 수납 선반", price: 21900 },
    { id: 7715005, name: "케이블 정리 박스", price: 12900 },
  ],
};

export const HOOKS: Record<CategoryKey, string[]> = {
  kitchen: [
    "과자 봉지, 아직도 빨래집게로 집어요?",
    "8,900원으로 주방 스트레스가 사라졌다",
    "싱크대 냄새, 원인은 따로 있었어요",
    "이거 모르고 키친타월 쓰셨죠?",
    "양념통 정리 3초 컷",
    "다들 깔때기 이렇게 쓰세요?",
  ],
  car: [
    "운전석 틈새로 떨어진 폰, 이제 끝",
    "세차장 안 가도 되는 이유",
    "여름 차 안 온도 10도 낮추는 법",
    "뒷좌석 가방 걸 데 없을 때",
  ],
  storage: [
    "냉장고 열 때마다 한숨 쉬는 분?",
    "옷장 공간 2배 만드는 법",
    "서랍이 매일 엉망이라면",
  ],
};

export function productFor(category: CategoryKey, i: number) {
  const list = PRODUCTS[category];
  const p = list[i % list.length];
  return {
    id: p.id,
    name: p.name,
    price: p.price,
    image_url: productSvg(p.name, p.id),
    category_id: CATEGORIES[category].id,
  };
}

export function hookFor(category: CategoryKey, i: number): string {
  const list = HOOKS[category];
  return list[i % list.length];
}

/* ------------------------------------------------------------------ accounts */

interface AccountSeed {
  id: string;
  platform: PlatformT;
  handle: string;
  niche: string;
  category: CategoryKey;
  status: "active" | "cooldown" | "restricted";
  daily_cap: number;
  ageDays: number;
  cooldownHours?: number;
}

const ACCOUNT_SEEDS: AccountSeed[] = [
  { id: "acc_kit_thr", platform: "threads", handle: "@kitchen_pick", niche: "주방", category: "kitchen", status: "active", daily_cap: 20, ageDays: 126 },
  { id: "acc_kit_ig", platform: "instagram", handle: "@kitchen_pick", niche: "주방", category: "kitchen", status: "active", daily_cap: 5, ageDays: 126 },
  { id: "acc_kit_yt", platform: "youtube", handle: "@kitchen_pick_shorts", niche: "주방", category: "kitchen", status: "active", daily_cap: 3, ageDays: 98 },
  { id: "acc_kit_tt", platform: "tiktok", handle: "@kitchen.pick", niche: "주방", category: "kitchen", status: "restricted", daily_cap: 3, ageDays: 41 },
  { id: "acc_kit_fb", platform: "facebook", handle: "@kitchenpick.page", niche: "주방", category: "kitchen", status: "active", daily_cap: 3, ageDays: 77 },
  { id: "acc_kit_kakao", platform: "kakao", handle: "주방픽 채널", niche: "주방", category: "kitchen", status: "active", daily_cap: 5, ageDays: 60 },
  { id: "acc_car_thr", platform: "threads", handle: "@car_pick", niche: "차량", category: "car", status: "cooldown", daily_cap: 20, ageDays: 6, cooldownHours: 31 },
  { id: "acc_car_ig", platform: "instagram", handle: "@car_pick", niche: "차량", category: "car", status: "active", daily_cap: 5, ageDays: 6 },
  { id: "acc_tidy_thr", platform: "threads", handle: "@tidy_pick", niche: "정리수납", category: "storage", status: "active", daily_cap: 20, ageDays: 52 },
  { id: "acc_tidy_ig", platform: "instagram", handle: "@tidy_pick", niche: "정리수납", category: "storage", status: "active", daily_cap: 5, ageDays: 52 },
];

export function accountsFixture(now: number): AccountIn[] {
  return ACCOUNT_SEEDS.map((a) => ({
    id: a.id,
    platform: a.platform,
    handle: a.handle,
    niche: a.niche,
    status: a.status,
    daily_cap: a.daily_cap,
    cooldown_until: a.cooldownHours ? iso(now + a.cooldownHours * HOUR) : null,
    created_at: iso(now - a.ageDays * DAY),
  }));
}

export function accountFor(category: CategoryKey, platform: PlatformT) {
  return (
    ACCOUNT_SEEDS.find((a) => a.category === category && a.platform === platform) ??
    ACCOUNT_SEEDS.find((a) => a.category === "kitchen" && a.platform === platform) ??
    ACCOUNT_SEEDS[0]
  );
}

export function accountAgeDays(id: string): number {
  return ACCOUNT_SEEDS.find((a) => a.id === id)?.ageDays ?? 999;
}

/* ------------------------------------------------------------------ plans */

const PLACEMENT: Record<PlatformT, string> = {
  threads: "body",
  instagram: "hub",
  youtube: "hub",
  tiktok: "hub",
  facebook: "caption",
  kakao: "manual",
};

function formatFor(platform: PlatformT, kind: "text" | "short"): string {
  if (platform === "threads") return kind === "short" ? "text+video" : "text";
  if (platform === "instagram") return "reels";
  if (platform === "youtube") return "shorts";
  if (platform === "tiktok") return "video";
  if (platform === "facebook") return kind === "short" ? "reels" : "post";
  return "manual";
}

export interface PlanSpec {
  runId: string;
  category: CategoryKey;
  count: number;
  kind: "text" | "short";
  persona: string;
  platforms: PlatformT[];
  schedule: string;
  window: [number, number];
  cron: string | null;
  budget: number;
  intent: "create_and_publish" | "create_only" | "analyze";
  extraReasons?: string[];
}

export function buildPlan(spec: PlanSpec): PlanIn {
  const cat = CATEGORIES[spec.category];
  const targets: TargetIn[] =
    spec.intent === "create_and_publish"
      ? spec.platforms.map((p) => {
          const acc = accountFor(spec.category, p);
          return {
            platform: p,
            account_id: acc.id,
            account_handle: acc.handle,
            link_placement: PLACEMENT[p],
            format: formatFor(p, spec.kind),
          };
        })
      : [];

  const unit = spec.intent === "analyze" ? 0 : spec.kind === "short" ? 150 : 40;
  const est = spec.intent === "analyze" ? 40 : spec.count * unit;

  const reasons: string[] = [];
  if (spec.category !== "kitchen" && spec.category !== "storage") reasons.push(`신규 카테고리: ${cat.label} (성과 이력 없음)`);
  for (const t of targets) {
    const age = accountAgeDays(String(t.account_id));
    if (age < 14) reasons.push(`계정 ${t.account_handle} 생성 14일 미경과 (${age}일차)`);
    if (t.platform === "tiktok") reasons.push("TikTok 미감사 앱 — 비공개(SELF_ONLY) 게시만 가능");
  }
  if (est > spec.budget) reasons.push(`추정 비용 ₩${est.toLocaleString("ko-KR")}이 예산 상한 ₩${spec.budget.toLocaleString("ko-KR")} 초과`);
  if (spec.persona !== "30대 여성") reasons.push(`템플릿-페르소나 조합 검증 전 (${spec.persona})`);
  reasons.push(...(spec.extraReasons ?? []));
  const uniqueReasons = Array.from(new Set(reasons));
  const approval = uniqueReasons.length
    ? { mode: "console" as const, reasons: uniqueReasons }
    : { mode: "auto" as const, reasons: ["계정 14일 경과", "템플릿 검증 완료", "예산 상한 이내", "기존 카테고리"] };

  const template = spec.kind === "short" ? "problem-solution-v3" : "thread-listicle-v2";
  const voice = spec.kind === "short" ? (spec.persona.includes("남") ? "ko-M-40s-punchy" : "ko-F-30s-brisk") : null;

  const steps: StepIn[] = [];
  if (spec.intent === "analyze") {
    steps.push(
      { id: "query", module: "M9", action: "report", params: { category: cat.id, days: 7 }, depends_on: [] },
      { id: "posterior", module: "M10", action: "summarize_arms", params: { category: cat.id }, depends_on: ["query"] },
      { id: "playbook", module: "M10", action: "propose_rules", params: { max_rules: 5 }, depends_on: ["posterior"] },
    );
  } else {
    steps.push({ id: "select", module: "M1", action: "select", params: { category: cat.id, n: spec.count, min_pos: 0.7 }, depends_on: [] });
    for (let i = 1; i <= spec.count; i++) {
      steps.push({ id: `link-${i}`, module: "M3", action: "deeplink", params: { product: i, sub_id: "per-publication" }, depends_on: ["select"] });
      steps.push({
        id: `script-${i}`,
        module: "M4",
        action: "generate",
        params: { product: i, kind: spec.kind, persona: spec.persona, hooks: 3, len: spec.kind === "short" ? 29 : null },
        depends_on: ["select"],
      });
      let prev = `script-${i}`;
      if (spec.kind === "short") {
        steps.push({ id: `render-${i}`, module: "M6", action: "render", params: { product: i, template, voice }, depends_on: [prev] });
        prev = `render-${i}`;
      }
      steps.push({ id: `gate-${i}`, module: "M7", action: "check", params: { product: i, rule_set: "v1" }, depends_on: [prev, `link-${i}`] });
      for (const t of targets) {
        steps.push({
          id: `publish-${t.platform}-${i}`,
          module: "M8",
          action: "publish",
          params: { product: i, platform: t.platform, account: t.account_handle, link: t.link_placement },
          depends_on: [`gate-${i}`],
        });
      }
    }
  }

  const platformsText = targets
    .map((t) => `${PLATFORM_KO[t.platform]}(${PLACEMENT_KO[t.link_placement] ?? t.link_placement})`)
    .join("·");
  const explanation =
    spec.intent === "analyze"
      ? `${cat.label} 카테고리의 최근 7일 성과(M9)를 집계하고 arm 사후분포(M10)를 요약해 플레이북 규칙 후보를 제안합니다. 게시는 하지 않습니다.`
      : `${cat.label} 카테고리에서 POS 0.7 이상 상품 ${spec.count}개를 골라 ${spec.persona} 페르소나로 ${
          spec.kind === "short" ? "22초 쇼츠(빠른 보이스)" : "스레드형 글"
        }를 만들고, 모든 결과물은 M7 게이트(rule_set v1)를 통과해야 합니다.${
          targets.length
            ? ` ${platformsText}에 ${String(spec.window[0]).padStart(2, "0")}–${String(spec.window[1]).padStart(2, "0")}시 사이 ±7분 지터로 분산 게시합니다.`
            : " 게시 없이 생산까지만 진행합니다."
        }${spec.cron ? ` 반복 예약(cron ${spec.cron})으로 저장됩니다.` : ""}`;

  return {
    run_id: spec.runId,
    intent: spec.intent,
    slots: {
      intent: spec.intent,
      category: cat.label,
      count: spec.count,
      kind: spec.kind,
      persona: spec.persona,
      platforms: spec.platforms,
      schedule: spec.schedule,
      budget_krw: spec.budget,
    },
    category_id: cat.id,
    niche: cat.niche,
    count: spec.count,
    min_pos: 0.7,
    kind: spec.kind,
    persona: spec.persona,
    hooks: 3,
    length_sec: spec.kind === "short" ? 29 : null,
    template_id: template,
    voice_id: voice,
    rule_set: "v1",
    targets,
    schedule_window: spec.window,
    jitter_minutes: 7,
    recurring_cron: spec.cron,
    est_cost_krw: est,
    budget_cap_krw: spec.budget,
    approval,
    steps,
    explanation,
  };
}

const PLATFORM_KO: Record<PlatformT, string> = {
  threads: "스레드",
  instagram: "인스타그램",
  youtube: "유튜브",
  tiktok: "틱톡",
  facebook: "페이스북",
  kakao: "카카오",
};
const PLACEMENT_KO: Record<string, string> = { body: "본문 링크", hub: "링크 허브", caption: "캡션 링크", manual: "수동" };

/* ------------------------------------------------------------------ command parsing (mock Commander) */

const KO_NUM: Record<string, number> = { 한: 1, 하나: 1, 두: 2, 세: 3, 네: 4, 다섯: 5, 여섯: 6, 일곱: 7, 여덟: 8, 아홉: 9, 열: 10 };

export function parseCommand(text: string): Omit<PlanSpec, "runId"> {
  const t = text.replace(/\s+/g, " ");
  const category: CategoryKey = /차량|자동차/.test(t) ? "car" : /정리|수납/.test(t) ? "storage" : "kitchen";

  let count = 5;
  const num = /(\d+)\s*개/.exec(t);
  const word = /(하나|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*개/.exec(t);
  if (num) count = Number(num[1]);
  else if (word) count = KO_NUM[word[1]] ?? 5;
  count = Math.max(1, Math.min(30, count));

  const intent: PlanSpec["intent"] = /분석/.test(t)
    ? "analyze"
    : /만들기만|만들어만|생성만/.test(t)
      ? "create_only"
      : "create_and_publish";

  const platforms: PlatformT[] = [];
  if (/스레드|threads/i.test(t)) platforms.push("threads");
  if (/인스타|instagram|릴스/i.test(t)) platforms.push("instagram");
  if (/유튜브|youtube|숏츠/i.test(t)) platforms.push("youtube");
  if (/틱톡|tiktok/i.test(t)) platforms.push("tiktok");
  if (/페이스북|페북|facebook/i.test(t)) platforms.push("facebook");
  if (/카카오|카톡/.test(t)) platforms.push("kakao");
  if (!platforms.length) platforms.push("threads", "instagram");

  let kind: "text" | "short" = /쇼츠|숏폼|영상|릴스/.test(t) ? "short" : /글|텍스트/.test(t) ? "text" : "short";
  if (kind === "text" && platforms.some((p) => p === "instagram" || p === "youtube" || p === "tiktok")) kind = "short";

  const pm = /(\d)0\s*대\s*(여자|여성|남자|남성)/.exec(t);
  const persona = pm ? `${pm[1]}0대 ${pm[2].startsWith("여") ? "여성" : "남성"}` : "30대 여성";

  let schedule = "시간대 분산 (09–23시)";
  let window: [number, number] = [9, 23];
  let cron: string | null = null;
  const daily = /매일\s*(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시/.exec(t);
  if (daily) {
    let h = Number(daily[2]);
    if ((daily[1] === "오후" || daily[1] === "저녁" || daily[1] === "밤") && h < 12) h += 12;
    cron = `0 ${h} * * *`;
    schedule = `매일 ${String(h).padStart(2, "0")}:00`;
    window = [h, Math.min(24, h + 3)];
  } else if (/오늘\s*밤/.test(t)) {
    schedule = "오늘 밤";
    window = [21, 24];
  } else if (/지금|바로/.test(t)) {
    schedule = "지금";
  }

  let budget = 2000;
  const thousand = /(\d+)\s*천\s*원/.exec(t);
  const won = /([\d,]{3,})\s*원/.exec(t);
  if (thousand) budget = Number(thousand[1]) * 1000;
  else if (won) budget = Number(won[1].replace(/,/g, ""));

  return { category, count, kind, persona, platforms, schedule, window, cron, budget, intent };
}

/* ------------------------------------------------------------------ contents */

const PERMALINK: Record<PlatformT, (handle: string, key: string) => string | null> = {
  threads: (h, k) => `https://www.threads.net/${h}/post/${k}`,
  instagram: (_h, k) => `https://www.instagram.com/reel/${k}/`,
  youtube: (_h, k) => `https://youtube.com/shorts/${k}`,
  tiktok: (h, k) => `https://www.tiktok.com/${h}/video/${k}`,
  facebook: (_h, k) => `https://www.facebook.com/reel/${k}`,
  kakao: () => null,
};

const ABBR: Record<PlatformT, string> = { threads: "thr", instagram: "ig", youtube: "yt", tiktok: "tt", facebook: "fb", kakao: "kk" };

export type ContentOutcome = "published" | "fixed" | "hold" | "block" | "rendering" | "queued" | "paused" | "failed";

export function makeContents(
  plan: PlanIn,
  category: CategoryKey,
  outcomes: ContentOutcome[],
  startedAt: number,
  seed: number,
): ContentIn[] {
  const rnd = mulberry32(seed);
  const runKey = String(plan.run_id).slice(0, 6);
  return outcomes.map((outcome, idx) => {
    const product = productFor(category, idx + (seed % 3));
    const hook = hookFor(category, idx + seed);
    const findings: FindingIn[] = [];
    let verdict: ContentIn["verdict"] = null;
    let status = "draft";
    if (outcome === "published") {
      verdict = "PASS";
      status = "approved";
    } else if (outcome === "fixed") {
      verdict = "FIX";
      status = "approved";
      findings.push(
        idx % 2 === 0
          ? { rule_id: "C-02", verdict: "FIX", message: "최상급 표현 '무조건' 삭제 후 재검사 통과", fixed: true }
          : { rule_id: "D-01", verdict: "FIX", message: "대가성 문구를 캡션 첫 줄로 이동", fixed: true },
      );
    } else if (outcome === "hold") {
      verdict = "HOLD";
      status = "gated";
      findings.push(
        { rule_id: "D-01", verdict: "FIX", message: "대가성 문구 첫 줄 자동 삽입", fixed: true },
        { rule_id: "R-03", verdict: "HOLD", message: "상표 로고 노출 의심 (4.2초 프레임, 신뢰도 0.71)", fixed: false },
      );
    } else if (outcome === "block") {
      verdict = "BLOCK";
      status = "blocked";
      findings.push({ rule_id: "C-03", verdict: "FIX", message: "가격 문구 제거 (실시간 가격 불일치)", fixed: true });
      findings.push({ rule_id: "R-01", verdict: "BLOCK", message: "권리 원장에 없는 B-roll 에셋 사용 (asset 9f2c…)", fixed: false });
    } else if (outcome === "rendering") {
      status = plan.kind === "short" ? "rendered" : "draft";
    } else if (outcome === "paused" || outcome === "failed") {
      status = outcome === "failed" ? "blocked" : "draft";
    }

    const publications: PublicationIn[] = [];
    const shipped = outcome === "published" || outcome === "fixed" || outcome === "queued" || outcome === "failed";
    if (shipped) {
      plan.targets?.forEach((t, ti) => {
        const platform = t.platform;
        const key = `${runKey}${idx}${ti}`.replace(/[^a-z0-9]/gi, "").slice(0, 11) + Math.floor(rnd() * 1e4);
        const scheduled = startedAt + (20 + idx * 37 + ti * 11) * MIN + Math.floor(rnd() * 14 - 7) * MIN;
        let state = "PUBLISHED";
        if (outcome === "queued") state = idx % 3 === 0 ? "PROCESSING" : "QUEUED";
        if (outcome === "failed") state = "FAILED";
        if (platform === "kakao") state = outcome === "queued" ? "MANUAL_PENDING" : state === "PUBLISHED" ? "PUBLISHED" : "MANUAL_PENDING";
        const published = state === "PUBLISHED";
        publications.push({
          id: `pub_${runKey}_${idx}_${ABBR[platform]}`,
          platform,
          account_handle: t.account_handle,
          state,
          scheduled_at: iso(scheduled),
          published_at: published ? iso(scheduled + Math.floor(rnd() * 4 + 1) * MIN) : null,
          permalink: published ? PERMALINK[platform](t.account_handle, key) : null,
          sub_id: `acvm-${runKey}-${idx + 1}-${ABBR[platform]}`,
        });
      });
    }

    return {
      id: `cnt_${runKey}_${idx + 1}`,
      product,
      kind: plan.kind,
      status,
      hook,
      persona: plan.persona,
      template_id: plan.template_id,
      verdict,
      findings,
      media_url: null,
      thumbnail_url: plan.kind === "short" ? posterSvg(hook, product.name, product.id + idx) : null,
      publications,
    };
  });
}

/* ------------------------------------------------------------------ runs */

interface RunSeed {
  id: string;
  text: string;
  status: RunStatusT;
  agoMin: number;
  durationMin: number | null;
  costRatio: number;
  summary: string | null;
  outcomes: ContentOutcome[];
  overrides?: Partial<PlanSpec>;
}

const RUN_SEEDS: RunSeed[] = [
  {
    id: "6f1d2c9a-3b7e-4c1a-9e55-1a2b3c4d5e01",
    text: "정리수납 5개 쇼츠, 30대 여자 페르소나로 인스타에 올려줘",
    status: "approved",
    agoMin: 1,
    durationMin: null,
    costRatio: 0,
    summary: "승인됨 — 워커 대기열 진입",
    outcomes: ["rendering", "rendering", "rendering", "rendering", "rendering"],
  },
  {
    id: "c2a7e0b4-55d1-4f0e-8b0c-1a2b3c4d5e02",
    text: "매일 오전 9시에 차량용품 10개 글로 만들어서 스레드에 올려줘",
    status: "awaiting_approval",
    agoMin: 8,
    durationMin: null,
    costRatio: 0,
    summary: "콘솔 승인 필요 — 신규 카테고리, 신규 계정",
    outcomes: [],
  },
  {
    id: "a91f3e22-0c4b-4d7a-a6f1-1a2b3c4d5e03",
    text: "주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘",
    status: "running",
    agoMin: 26,
    durationMin: null,
    costRatio: 0.69,
    summary: "5개 중 3개 게시 · HOLD 1 · 렌더 1",
    outcomes: ["published", "fixed", "published", "hold", "rendering"],
  },
  {
    id: "7d0be5f1-9a3c-4e2b-b1d0-1a2b3c4d5e04",
    text: "주방 8개 쇼츠, 40대 남자 페르소나로 인스타 틱톡에 1천 원 이내",
    status: "paused_budget",
    agoMin: 140,
    durationMin: 46,
    costRatio: 1,
    summary: "예산 상한 도달 — 6/8 생산 후 일시정지",
    outcomes: ["published", "fixed", "published", "published", "fixed", "published", "paused", "paused"],
    overrides: { budget: 1000 },
  },
  {
    id: "e4c81a07-6d2f-4b39-9c7e-1a2b3c4d5e05",
    text: "정리수납 3개 글로 스레드에 올려줘",
    status: "completed",
    agoMin: 190,
    durationMin: 4,
    costRatio: 0.9,
    summary: "3/3 게시 · PASS 2 · FIX 1",
    outcomes: ["published", "fixed", "published"],
  },
  {
    id: "1b9f7c3e-2a8d-4f61-8d2a-1a2b3c4d5e06",
    text: "주방 10개 쇼츠로 만들어서 유튜브, 페이스북에 올려줘",
    status: "completed",
    agoMin: 26 * 60,
    durationMin: 38,
    costRatio: 0.94,
    summary: "10개 생성 · 18건 게시 · BLOCK 1",
    outcomes: ["published", "published", "fixed", "published", "block", "published", "fixed", "published", "published", "published"],
  },
  {
    id: "5a3e9d11-7f4c-4a0b-a3e9-1a2b3c4d5e07",
    text: "차량용품 5개 쇼츠 만들어서 틱톡에 올려줘",
    status: "failed",
    agoMin: 29 * 60,
    durationMin: 12,
    costRatio: 0.8,
    summary: "M8 TikTok 어댑터: 미감사 앱 — 공개 게시 거절 (SELF_ONLY)",
    outcomes: ["failed", "failed", "failed", "failed", "failed"],
  },
  {
    id: "0c6d4b88-3e1f-4c7d-9f3b-1a2b3c4d5e08",
    text: "주방 카테고리 지난주 성과 분석해줘",
    status: "completed",
    agoMin: 47 * 60,
    durationMin: 2,
    costRatio: 0.88,
    summary: "플레이북 규칙 후보 3건 제안",
    outcomes: [],
  },
  {
    id: "9e2f6a5c-1d3b-4e8f-b7a6-1a2b3c4d5e09",
    text: "아무 카테고리나 30개 지금 다 올려줘",
    status: "rejected",
    agoMin: 50 * 60,
    durationMin: 0,
    costRatio: 0,
    summary: "반려: 계정 일일 상한(스레드 20건) 초과 계획",
    outcomes: [],
  },
];

export interface MockDb {
  runs: RunIn[];
  plans: Record<string, PlanIn | null>;
  contents: Record<string, ContentIn[]>;
  costs: Record<string, { spent_krw: number; cap_krw: number | null }>;
  holds: HoldIn[];
  accounts: AccountIn[];
  schedules: ScheduleIn[];
  manual: ManualIn[];
  audit: AuditIn[];
}

function seedFromId(id: string): number {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h) % 100_000;
}

/* ------------------------------------------------------------------ HOLD queue */

function shortScript(hook: string, product: string, persona: string): string {
  return [
    `[0–3초] ${hook}`,
    `[3–6.5초] ${persona} 시점 — 매번 손이 가는데 정리가 안 되는 그 순간`,
    `[6.5–11초] 그래서 찾은 게 '${product}'`,
    "[11–18초] 장점: 공간 절약 · 세척 간편 · 리뷰 평점 기반 (근거: 리뷰 수·평점 필드)",
    "[18–22초] 고정 댓글/프로필 링크에서 확인하세요",
  ].join("\n");
}

function caption(body: string, tags: string): string {
  return `${DISCLOSURE_TEXT}\n\n${body}\n\n${tags}`;
}

export function holdsFixture(now: number): HoldIn[] {
  const items: Array<{
    category: CategoryKey;
    idx: number;
    kind: "text" | "short";
    reasons: string[];
    findings: FindingIn[];
    agoMin: number;
    run: string;
    content: string;
  }> = [
    {
      category: "kitchen",
      idx: 3,
      kind: "short",
      reasons: ["R-03 상표·캐릭터 노출 의심 (비전 분류기 0.71)"],
      findings: [
        { rule_id: "D-01", verdict: "FIX", message: "대가성 문구 첫 줄 자동 삽입", fixed: true, details: { position: "line 1" } },
        {
          rule_id: "R-03",
          verdict: "HOLD",
          message: "상표 로고 노출 의심 (4.2초 프레임)",
          fixed: false,
          details: { frame_sec: 4.2, label: "brand_logo", score: 0.71 },
        },
      ],
      agoMin: 9,
      run: "a91f3e22-0c4b-4d7a-a6f1-1a2b3c4d5e03",
      content: "cnt_a91f3e_4",
    },
    {
      category: "car",
      idx: 0,
      kind: "text",
      reasons: ["H-01 신규 계정 첫 14일 (@car_pick · 6일차)"],
      findings: [
        { rule_id: "C-02", verdict: "FIX", message: "최상급 표현 '최고' → '자주 쓰는' 치환", fixed: true, details: { before: "최고의 수납함", after: "자주 쓰는 수납함" } },
        { rule_id: "H-01", verdict: "HOLD", message: "신규 계정 첫 14일 — 사람 승인 필요", fixed: false },
      ],
      agoMin: 22,
      run: "5a3e9d11-7f4c-4a0b-a3e9-1a2b3c4d5e07",
      content: "cnt_5a3e9d_1",
    },
    {
      category: "storage",
      idx: 1,
      kind: "short",
      reasons: ["H-01 신규 템플릿 첫 10건 (before-after-v1 · 4/10)"],
      findings: [{ rule_id: "H-01", verdict: "HOLD", message: "신규 템플릿 before-after-v1 검증 4/10", fixed: false }],
      agoMin: 35,
      run: "e4c81a07-6d2f-4b39-9c7e-1a2b3c4d5e05",
      content: "cnt_e4c81a_2",
    },
    {
      category: "kitchen",
      idx: 4,
      kind: "short",
      reasons: ["R-03 보이스 출처 확인 필요 (ko-M-40s-punchy 신규 등록)"],
      findings: [
        { rule_id: "C-01", verdict: "FIX", message: "근거 없는 효능 문장 1개 삭제 ('세균 99% 제거')", fixed: true, details: { removed: "세균 99% 제거" } },
        { rule_id: "R-03", verdict: "HOLD", message: "보이스 출처 원장 미확인", fixed: false, details: { voice_id: "ko-M-40s-punchy" } },
      ],
      agoMin: 48,
      run: "7d0be5f1-9a3c-4e2b-b1d0-1a2b3c4d5e04",
      content: "cnt_7d0be5_5",
    },
    {
      category: "car",
      idx: 2,
      kind: "short",
      reasons: ["H-01 신규 계정 첫 14일 (@car_pick · 6일차)", "H-01 신규 템플릿 첫 10건 (problem-solution-v4 · 2/10)"],
      findings: [
        { rule_id: "S-01", verdict: "FIX", message: "최근 30일 문장과 MinHash 0.64 — 2문장 재생성", fixed: true, details: { minhash: 0.64 } },
        { rule_id: "H-01", verdict: "HOLD", message: "신규 계정 + 신규 템플릿", fixed: false },
      ],
      agoMin: 63,
      run: "5a3e9d11-7f4c-4a0b-a3e9-1a2b3c4d5e07",
      content: "cnt_5a3e9d_3",
    },
    {
      category: "kitchen",
      idx: 5,
      kind: "text",
      reasons: ["R-03 캐릭터 이미지 포함 의심 (상품 패키지)"],
      findings: [{ rule_id: "R-03", verdict: "HOLD", message: "패키지 캐릭터 노출 — 라이선스 확인", fixed: false, details: { label: "character", score: 0.58 } }],
      agoMin: 95,
      run: "1b9f7c3e-2a8d-4f61-8d2a-1a2b3c4d5e06",
      content: "cnt_1b9f7c_6",
    },
  ];

  return items.map((it, n) => {
    const product = productFor(it.category, it.idx);
    const hook = hookFor(it.category, it.idx);
    const persona = it.category === "car" ? "30대 남성" : "30대 여성";
    const body =
      it.kind === "short"
        ? `${hook}\n${product.name}, 써보니 이 부분이 제일 편했어요.\n👉 프로필 링크에서 확인`
        : `${hook}\n\n1) ${product.name} — ${product.price.toLocaleString("ko-KR")}원대\n2) 리뷰 평점 4.7 · 로켓배송\n3) 공간 절약형\n\n링크는 본문 아래에 있어요.`;
    const tags = it.category === "car" ? "#차량용품 #차박 #운전꿀템" : it.category === "storage" ? "#정리수납 #살림템" : "#주방꿀템 #살림템 #쿠팡추천";
    return {
      id: `hold_${String(n + 1).padStart(3, "0")}`,
      content_id: it.content,
      run_id: it.run,
      created_at: iso(now - it.agoMin * MIN),
      reasons: it.reasons,
      product: { name: product.name, price: product.price, image_url: product.image_url },
      kind: it.kind,
      hook,
      script_text: it.kind === "short" ? shortScript(hook, product.name, persona) : body,
      caption_preview: caption(body, tags),
      media_url: null,
      thumbnail_url: it.kind === "short" ? posterSvg(hook, product.name, product.id + n) : null,
      findings: it.findings,
    };
  });
}

/* ------------------------------------------------------------------ db */

export function createDb(now: number): MockDb {
  const db: MockDb = {
    runs: [],
    plans: {},
    contents: {},
    costs: {},
    holds: holdsFixture(now),
    accounts: accountsFixture(now),
    schedules: [
      { id: "sch_01", raw_command: "매일 오전 9시에 주방 10개 쇼츠 만들어서 스레드랑 인스타에 올려줘", cron: "0 9 * * *", active: true, created_by: "owner@acvm.local" },
      { id: "sch_02", raw_command: "매일 오후 9시에 정리수납 5개 글로 스레드에 올려줘", cron: "0 21 * * *", active: true, created_by: "operator@acvm.local" },
      { id: "sch_03", raw_command: "매주 월요일 오전 8시에 주방 카테고리 지난주 성과 분석해줘", cron: "0 8 * * 1", active: false, created_by: "owner@acvm.local" },
    ],
    manual: [
      {
        publication_id: "pub_kakao_0001",
        platform: "kakao",
        account_handle: "주방픽 채널",
        caption: caption("과자 봉지, 아직도 빨래집게로 집어요?\n실리콘 과자 밀봉 클립 12P — 8,900원\n▶ 링크: https://link.acvm.example/h/kitchen", "#주방꿀템 #살림템"),
        media_url: posterSvg("과자 봉지, 아직도 빨래집게로 집어요?", "실리콘 과자 밀봉 클립 12P", 11),
        scheduled_at: iso(now + 25 * MIN),
      },
      {
        publication_id: "pub_kakao_0002",
        platform: "kakao",
        account_handle: "주방픽 채널",
        caption: caption("양념통 정리 3초 컷\n원터치 양념통 6P 세트 — 19,800원\n▶ 링크: https://link.acvm.example/h/kitchen", "#주방정리"),
        media_url: posterSvg("양념통 정리 3초 컷", "원터치 양념통 6P 세트", 23),
        scheduled_at: iso(now + 95 * MIN),
      },
      {
        publication_id: "pub_kakao_0003",
        platform: "kakao",
        account_handle: "주방픽 채널",
        caption: caption("싱크대 냄새, 원인은 따로 있었어요\n싱크대 배수구 거름망 100매 — 9,900원\n▶ 링크: https://link.acvm.example/h/kitchen", "#싱크대 #살림템"),
        media_url: null,
        scheduled_at: iso(now - 40 * MIN),
      },
    ],
    audit: [],
  };

  for (const seed of RUN_SEEDS) {
    const parsed = parseCommand(seed.text);
    const spec: PlanSpec = { ...parsed, ...seed.overrides, runId: seed.id };
    const plan = buildPlan(spec);
    if (seed.id.startsWith("c2a7e0b4")) {
      plan.approval = {
        mode: "console",
        reasons: ["신규 카테고리: 차량용품 (성과 이력 없음)", "계정 @car_pick 생성 14일 미경과 (6일차)", "계정 @car_pick 쿨다운 중 — 첫 게시가 쿨다운 해제 후로 재스케줄됨"],
      };
    }
    const startedAt = now - seed.agoMin * MIN;
    const est = plan.est_cost_krw;
    const spent = Math.round(est * seed.costRatio);
    db.runs.push({
      id: seed.id,
      raw_command: seed.text,
      status: seed.status,
      est_cost_krw: est,
      cost_krw: seed.status === "awaiting_approval" || seed.status === "rejected" ? null : spent,
      started_at: seed.status === "awaiting_approval" || seed.status === "rejected" ? null : iso(startedAt),
      ended_at: seed.durationMin !== null ? iso(startedAt + seed.durationMin * MIN) : null,
      summary: seed.summary,
    });
    db.plans[seed.id] = plan;
    db.contents[seed.id] = makeContents(plan, spec.category, seed.outcomes, startedAt, seedFromId(seed.id));
    db.costs[seed.id] = { spent_krw: spent, cap_krw: plan.budget_cap_krw ?? null };
  }

  // Keep HOLD items consistent with their content cards (pending review ⇒ no publications yet).
  for (const h of db.holds) {
    for (const list of Object.values(db.contents)) {
      const c = list.find((x) => x.id === h.content_id);
      if (c) {
        c.verdict = "HOLD";
        c.status = "gated";
        c.findings = h.findings ?? [];
        c.publications = [];
      }
    }
  }

  db.audit = auditFixture(now);
  return db;
}

/* ------------------------------------------------------------------ audit */

function auditFixture(now: number): AuditIn[] {
  const rnd = mulberry32(7);
  const out: AuditIn[] = [];
  const actors = ["owner@acvm.local", "operator@acvm.local", "reviewer@acvm.local"];
  const templates: Array<(i: number) => Omit<AuditIn, "at">> = [
    (i) => ({ actor: actors[1], action: "command.submit", target: `run:${RUN_SEEDS[i % RUN_SEEDS.length].id}`, payload: { text: RUN_SEEDS[i % RUN_SEEDS.length].text } }),
    (i) => ({ actor: "system:orchestrator", action: "plan.auto_approve", target: `run:${RUN_SEEDS[i % RUN_SEEDS.length].id}`, payload: { reasons: ["계정 14일 경과", "템플릿 검증 완료", "예산 상한 이내"] } }),
    (i) => ({ actor: actors[2], action: "hold.approve", target: `hold:hold_${String(100 + i).padStart(3, "0")}`, payload: { note: null, seconds_spent: 14 + Math.floor(rnd() * 20) } }),
    (i) => ({ actor: actors[2], action: "hold.reject", target: `hold:hold_${String(200 + i).padStart(3, "0")}`, payload: { note: "상표 로고가 2초 이상 노출" } }),
    () => ({ actor: "system:m7", action: "gate.block", target: "content:cnt_1b9f7c_5", payload: { rule_id: "R-01", verdict: "BLOCK" } }),
    (i) => ({ actor: "system:m8", action: "publication.published", target: `publication:pub_${hex(rnd, 6)}`, payload: { platform: ["threads", "instagram", "youtube"][i % 3], state: "PUBLISHED" } }),
    () => ({ actor: "system:quota", action: "account.cooldown", target: "account:acc_car_thr", payload: { reason: "게시 실패율 12% (24h)", hours: 48 } }),
    () => ({ actor: "system:m9", action: "link.pause", target: "sub_id:acvm-a91f3e-2-thr", payload: { reason: "동일 IP 반복 클릭", score: 0.91 } }),
    () => ({ actor: actors[0], action: "schedule.create", target: "schedule:sch_01", payload: { cron: "0 9 * * *" } }),
    () => ({ actor: actors[1], action: "manual.done", target: "publication:pub_kakao_0000", payload: { permalink: "https://pf.kakao.com/_xabc/101" } }),
    () => ({ actor: actors[0], action: "account.patch", target: "account:acc_kit_tt", payload: { status: "restricted" } }),
    () => ({ actor: "system:m7", action: "rule_set.load", target: "rule_set:v1", payload: { rules: 14 } }),
  ];
  let t = now - 3 * MIN;
  for (let i = 0; i < 64; i++) {
    const tpl = templates[Math.floor(rnd() * templates.length)];
    out.push({ at: iso(t), ...tpl(i) });
    t -= Math.floor(rnd() * 90 + 8) * MIN;
  }
  return out;
}

/* ------------------------------------------------------------------ dashboards */

function kstDateString(ms: number): string {
  return new Date(ms + 9 * HOUR).toISOString().slice(0, 10);
}

export function moneyFixture(days: number, now: number): z.input<typeof S.MoneyDashboard> {
  const span = 90;
  const rnd = mulberry32(42);
  const all = [];
  for (let i = 0; i < span; i++) {
    const ramp = Math.min(1, 0.3 + i / 55);
    let gmv = 2_300_000 * ramp * Math.exp(0.32 * gauss(rnd));
    if (rnd() < 0.07) gmv *= 2.4 + rnd() * 1.8; // tail content day
    const commission = gmv * (0.027 + rnd() * 0.007);
    const cancelled = commission * (0.04 + rnd() * 0.06);
    const contents = Math.round((120 + rnd() * 80) * (0.6 + 0.4 * ramp));
    const cost = contents * (135 + rnd() * 30);
    const r = (x: number) => Math.round(x);
    all.push({
      date: kstDateString(now - (span - 1 - i) * DAY),
      gmv_krw: r(gmv),
      commission_krw: r(commission),
      cancelled_krw: r(cancelled),
      cost_krw: r(cost),
      net_krw: r(commission) - r(cancelled) - r(cost),
    });
  }
  const daily = all.slice(-Math.max(1, Math.min(span, days)));
  const sum = (k: "gmv_krw" | "commission_krw" | "cancelled_krw" | "cost_krw" | "net_krw") =>
    daily.reduce((a, d) => a + d[k], 0);
  const totals = {
    gmv_krw: sum("gmv_krw"),
    commission_krw: sum("commission_krw"),
    cancelled_krw: sum("cancelled_krw"),
    cost_krw: sum("cost_krw"),
    net_krw: sum("net_krw"),
    effective_rate: sum("commission_krw") / Math.max(1, sum("gmv_krw")),
  };
  const ch = (
    platform: PlatformT,
    account: string,
    n: number,
    median: number,
    ci: [number, number],
    be: number,
  ) => ({
    platform,
    account,
    n,
    median_views: median,
    median_ci: ci,
    breakeven_views: be,
    ratio: Math.round((median / be) * 100) / 100,
    admitted: median >= be && n >= 30,
  });
  return {
    daily,
    totals,
    channels: [
      ch("threads", "@kitchen_pick", 142, 862, [781, 948], 694),
      ch("threads", "@tidy_pick", 64, 731, [640, 820], 694),
      ch("facebook", "@kitchenpick.page", 37, 1120, [930, 1290], 1041),
      ch("instagram", "@kitchen_pick", 88, 1450, [1310, 1620], 2604),
      ch("youtube", "@kitchen_pick_shorts", 41, 2080, [1720, 2460], 3472),
      ch("instagram", "@tidy_pick", 33, 980, [810, 1170], 2604),
      ch("threads", "@car_pick", 18, 410, [300, 540], 694),
      ch("tiktok", "@kitchen.pick", 12, 160, [90, 240], 3472),
    ],
  };
}

export function pipelineFixture(): z.input<typeof S.PipelineDashboardShape> {
  return {
    publication_states: {
      QUEUED: 23,
      UPLOADING: 2,
      PROCESSING: 4,
      PUBLISHED: 812,
      FAILED: 6,
      EXPIRED: 1,
      MANUAL_PENDING: 3,
      CANCELLED: 5,
      TAKEN_DOWN: 1,
    },
    runs_by_status: {
      planned: 0,
      awaiting_approval: 1,
      approved: 1,
      running: 1,
      paused_budget: 1,
      completed: 37,
      failed: 2,
      rejected: 3,
    },
    gate_verdicts: { PASS: 612, FIX: 287, HOLD: 31, BLOCK: 9 },
    render: { count: 196, p50_s: 41.2, p95_s: 58.7 },
    queues: {
      "m1.scout": 0,
      "m4.script": 3,
      "m6.render": 5,
      "m7.gate": 1,
      "m8.publish.threads": 12,
      "m8.publish.instagram": 4,
      "m8.publish.youtube": 2,
      "m9.ingest": 0,
    },
  };
}

export function riskFixture(now: number, accounts: AccountIn[]): z.input<typeof S.RiskDashboard> {
  return {
    accounts: accounts.map((a) => ({
      id: a.id,
      platform: a.platform,
      handle: a.handle,
      status: a.status,
      cooldown_until: a.cooldown_until ?? null,
    })),
    gate_verdicts: { PASS: 612, FIX: 287, HOLD: 31, BLOCK: 9 },
    invalid_click_alerts: [
      { at: iso(now - 47 * MIN), sub_id: "acvm-a91f3e-2-thr", reason: "동일 IP 반복 클릭 (12회/3분) — 링크 일시 중지", score: 0.91 },
      { at: iso(now - 5 * HOUR), sub_id: "acvm-1b9f7c-4-fb", reason: "CTR 급등 (7일 중앙값 대비 ×6.2)", score: 0.74 },
      { at: iso(now - 27 * HOUR), sub_id: "acvm-e4c81a-1-thr", reason: "짧은 간격 연속 클릭 (0.8초 간격)", score: 0.62 },
    ],
    warnings: [
      { at: iso(now - 17 * HOUR), account: "@car_pick", detail: "게시 실패율 12% (24h) → 48시간 쿨다운 자동 전환 (§7.1)" },
      { at: iso(now - 30 * HOUR), account: "@kitchen.pick", detail: "TikTok 미감사 앱 — 공개 게시 거절, SELF_ONLY만 가능" },
      { at: iso(now - 52 * HOUR), account: "@kitchen_pick", detail: "Meta 장기 토큰 만료 6일 전 — 자동 갱신 예약됨" },
    ],
  };
}

export function learningFixture(): z.input<typeof S.LearningDashboardShape> {
  const arm = (
    category: string,
    hook_type: string,
    template: string,
    persona: string,
    platform: string,
    slot: string,
    trials: number,
    ctr: number,
    halfWidth: number,
    epc: number,
    retired = false,
  ) => ({
    key: [category, hook_type, template, persona, platform, slot].join("|"),
    category,
    hook_type,
    template,
    persona,
    platform,
    slot,
    trials,
    ctr_mean: ctr,
    ctr_ci: [Math.max(0, ctr - halfWidth), ctr + halfWidth] as [number, number],
    epc_mean: epc,
    rpi_mean: Math.round(ctr * epc * 10000) / 10000,
    retired,
  });
  return {
    arms: [
      arm("주방", "price_twist", "problem-solution-v3", "30대 여성", "threads", "21-23", 64, 0.0131, 0.0021, 31.4),
      arm("주방", "before_after", "before-after-v1", "30대 여성", "threads", "12-14", 41, 0.0112, 0.0026, 28.9),
      arm("주방", "question", "problem-solution-v3", "30대 여성", "instagram", "21-23", 58, 0.0049, 0.0012, 26.1),
      arm("정리수납", "empathy", "thread-listicle-v2", "30대 여성", "threads", "09-11", 37, 0.0094, 0.0024, 33.8),
      arm("주방", "shock", "problem-solution-v3", "40대 남성", "youtube", "18-20", 29, 0.0038, 0.0015, 24.3),
      arm("주방", "price_twist", "problem-solution-v3", "30대 여성", "facebook", "21-23", 22, 0.0087, 0.0031, 29.5),
      arm("차량", "question", "thread-listicle-v2", "30대 남성", "threads", "07-09", 9, 0.0071, 0.0048, 35.2),
      arm("주방", "question", "thread-listicle-v2", "20대 여성", "threads", "15-17", 33, 0.0041, 0.0014, 12.7, true),
      arm("정리수납", "shock", "problem-solution-v3", "40대 여성", "instagram", "09-11", 31, 0.0029, 0.0011, 14.2, true),
    ],
    explore_exploit: { explore: 0.2, exploit: 0.8 },
    tail_contents: [
      { content_id: "cnt_1b9f7c_2", platform: "threads", views: 48210, percentile: 0.998, hook: "8,900원으로 주방 스트레스가 사라졌다" },
      { content_id: "cnt_e4c81a_1", platform: "threads", views: 21870, percentile: 0.994, hook: "냉장고 열 때마다 한숨 쉬는 분?" },
      { content_id: "cnt_1b9f7c_4", platform: "youtube", views: 19340, percentile: 0.991, hook: "이거 모르고 키친타월 쓰셨죠?" },
      { content_id: "cnt_7d0be5_1", platform: "instagram", views: 15420, percentile: 0.985, hook: "과자 봉지, 아직도 빨래집게로 집어요?" },
      { content_id: "cnt_a91f3e_1", platform: "threads", views: 9980, percentile: 0.979, hook: "싱크대 냄새, 원인은 따로 있었어요" },
    ],
    admission: {
      threads: { admitted: true, median_views: 862, breakeven_views: 694, ratio: 1.24, reason: "중앙값 ≥ I*, 표본 142건" },
      facebook: { admitted: true, median_views: 1120, breakeven_views: 1041, ratio: 1.08, reason: "캡션 링크 클릭 가능 — 중앙값 ≥ I*" },
      instagram: { admitted: false, median_views: 1450, breakeven_views: 2604, ratio: 0.56, reason: "링크 허브 경유로 CTR 하락 — 꼬리 콘텐츠만 교차 배포" },
      youtube: { admitted: false, median_views: 2080, breakeven_views: 3472, ratio: 0.6, reason: "설명란 링크 제한 — 증폭 대상만 배포" },
      tiktok: { admitted: false, median_views: 160, breakeven_views: 3472, ratio: 0.05, reason: "감사 미통과 (SELF_ONLY) — 출시 게이트" },
    },
    playbook: [
      { text: "주방 · 가격반전 훅은 스레드 21–23시 게시에서 CTR이 가장 높다", confidence: 0.91, lift: 0.31 },
      { text: "1만~2만 원대 상품은 첫 3초에 가격을 노출하면 클릭당 수익이 오른다", confidence: 0.82, lift: 0.18 },
      { text: "인스타그램은 링크 허브 경유라 신규 콘텐츠 대신 꼬리 콘텐츠 재배포에만 사용", confidence: 0.77, lift: 0.12 },
      { text: "20대 여성 · 질문형 훅 조합은 30회 시도 후 EPC 하위 10% — 퇴출", confidence: 0.88, lift: -0.42 },
    ],
  };
}

export function rulesFixture(): z.input<typeof S.RuleSet> {
  return {
    version: "v1",
    rules: [
      { id: "D-01", check: "대가성 문구가 캡션·본문 첫 줄에 있는가", method: "정규식 + 위치 검사", on_fail: "FIX (자동 삽입)" },
      { id: "D-02", check: "문구가 '수수료를 제공받는다'는 경제적 대가를 명시하는가", method: "허용 문구 화이트리스트", on_fail: "FIX" },
      { id: "D-03", check: "영상 첫 3초 화면에 '광고' 표시 오버레이가 있는가", method: "렌더 메타데이터 + 프레임 OCR", on_fail: "BLOCK" },
      { id: "D-04", check: "링크 허브 페이지 최상단 고정 문구", method: "페이지 빌드 검사", on_fail: "BLOCK" },
      { id: "C-01", check: "근거 없는 사실·효능 주장", method: "주장-근거 매핑 + LLM 판정", on_fail: "FIX (문장 삭제)" },
      { id: "C-02", check: "최상급·절대 표현 (\"최고\", \"100%\", \"무조건\")", method: "금칙어 사전", on_fail: "FIX" },
      { id: "C-03", check: "가격·할인 표기가 실시간 가격과 일치", method: "게시 직전 가격 재조회", on_fail: "FIX (또는 가격 문구 제거)" },
      { id: "C-04", check: "규제 카테고리 상품", method: "카테고리 화이트리스트", on_fail: "BLOCK" },
      { id: "R-01", check: "권리 원장에 없는 에셋 사용", method: "에셋 ID 대조", on_fail: "BLOCK" },
      { id: "R-02", check: "레퍼런스와의 유사도", method: "코사인 0.80, 5-gram 15%", on_fail: "FIX (재생성)" },
      { id: "R-03", check: "상표·캐릭터·유명인 얼굴/음성", method: "비전 분류기 + 보이스 출처 확인", on_fail: "HOLD" },
      { id: "S-01", check: "계정 최근 30일 콘텐츠와 중복", method: "MinHash 0.6", on_fail: "FIX" },
      { id: "S-02", check: "계정 쿼터·쿨다운 상태", method: "쿼터 관리자", on_fail: "HOLD (보류 후 재스케줄)" },
      { id: "A-01", check: "AI 생성물 표시", method: "플랫폼 라벨 필드 설정 (YouTube 합성 콘텐츠 공개, TikTok AI 생성 라벨)", on_fail: "FIX" },
      { id: "H-01", check: "신규 계정 첫 14일, 신규 템플릿 첫 10건", method: "정책", on_fail: "HOLD (사람 승인)" },
    ],
    disclosure: { text: DISCLOSURE_TEXT, video_text: DISCLOSURE_VIDEO },
  };
}
