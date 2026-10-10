/**
 * Mock fixtures for the operations endpoints (src/acvm/api/ops.py): system status, G0 checklist,
 * write-only secret slots, workspace budget, account credentials, publications board, content
 * dossier and the alerts feed. Built from the same in-memory MockDb as the run fixtures so ids
 * line up across pages (run → content → publication → alert).
 */
import type { z } from "zod";
import type * as S from "@/lib/schemas";
import { ApiError } from "@/lib/errors";
import {
  DISCLOSURE_TEXT,
  accountFor,
  hookFor,
  moneyFixture,
  mulberry32,
  productFor,
  type CategoryKey,
  type ContentIn,
  type MockDb,
} from "./fixtures";
import { posterSvg } from "./media";
import { INTEGRATION_SLOT_LABELS, llmProvider } from "./integrations";

type PublicationItemIn = z.input<typeof S.PublicationItem>;
type SystemStatusIn = z.input<typeof S.SystemStatus>;
type ContentDossierIn = z.input<typeof S.ContentDossier>;
type AlertsIn = z.input<typeof S.AlertsFeed>;
type WorkspaceIn = z.input<typeof S.Workspace>;
type PlatformT = z.input<typeof S.Platform>;

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (ms: number) => new Date(ms).toISOString();

export const LEGAL_CHECKLIST: Record<string, string> = {
  partners_policy: "쿠팡 파트너스 운영정책 확인 (자동화 범위·링크 허브·subId)",
  ad_law: "표시광고법·추천보증 심사지침(2024-12-01 개정) 문구·위치 확인",
  ai_act: "인공지능기본법 생성형 AI 표시 의무 적용 여부 법률 검토",
  platform_terms: "각 플랫폼 약관(브랜디드 콘텐츠·제휴 링크·자동 게시) 확인",
  tax_entity: "수수료 수령 주체(개인 vs 법인)와 사업소득 신고 체계 확정",
};


function llmCheck(s: Record<string, boolean>) {
  const check = (id: string, label: string, status: "ok" | "warn" | "fail", detail: string, fix = "") => ({ id, label, status, detail, fix });
  const prov = llmProvider(s);
  const detail: Record<string, string> = {
    anthropic: "Claude claude-sonnet-5-5 작성 / claude-opus-5-5 검수",
    gemini: "Gemini gemini-3.8-flash 작성·검수 (Claude 키 미등록)",
    "anthropic+gemini": "Claude claude-sonnet-5-5 작성·검수 · 장애 시 Gemini 자동 대체",
  };
  return prov === "offline"
    ? check("llm", "AI 작성·검수 (Claude / Gemini)", "warn", "오프라인 작성기 — 근거 기반 템플릿 대본", "운영 준비 › AI 연동 › Claude 또는 Gemini 키 등록 (저장 즉시 전환)")
    : check("llm", "AI 작성·검수 (Claude / Gemini)", "ok", detail[prov] ?? prov);
}

export const SECRET_SLOTS: Array<[string, string]> = [
  ["coupang_access_key", "쿠팡 파트너스 Access Key"],
  ["coupang_secret_key", "쿠팡 파트너스 Secret Key"],
  ["link_signing_key", "제휴 링크 서명 키 (HMAC)"],
  ["click_ip_salt", "클릭 IP 해시 솔트"],
  ["voyage_api_key", "Voyage 임베딩 키"],
  ["r2_access_key", "Cloudflare R2 Access Key"],
  ["r2_secret_key", "Cloudflare R2 Secret Key"],
  ...INTEGRATION_SLOT_LABELS,
];

export interface OpsState {
  checklist: Record<string, boolean>;
  secrets: Record<string, boolean>;
  credentials: Record<string, boolean>;
  workspace: { name: string; daily_budget_krw: number };
  publications: PublicationItemIn[];
}

const LINK_PLACEMENT: Record<PlatformT, string> = {
  threads: "body",
  instagram: "hub",
  youtube: "hub",
  tiktok: "hub",
  facebook: "caption_first_line",
  kakao: "manual",
};

function categoryOf(content: ContentIn): CategoryKey {
  const cat = content.product?.category_id ?? "";
  return cat.endsWith("1018") ? "car" : cat.endsWith("1015") ? "storage" : "kitchen";
}

function captionFor(content: ContentIn, platform: PlatformT): string {
  const name = content.product?.name ?? "";
  const price = content.product?.price ? `${content.product.price.toLocaleString("ko-KR")}원` : "";
  const link = LINK_PLACEMENT[platform] === "body" ? "\n▶ https://link.coupang.com/a/acvm" : "\n👉 프로필 링크에서 확인";
  return `${DISCLOSURE_TEXT}\n\n${content.hook ?? ""}\n${name}${price ? ` — ${price}` : ""}${link}\n\n#살림템 #쿠팡추천`;
}

/** Deterministic lognormal-ish view counts so the board shows a realistic tail. */
function metricsFor(seed: number) {
  const rnd = mulberry32(seed);
  const z = Math.sqrt(-2 * Math.log(Math.max(rnd(), 1e-6))) * Math.cos(2 * Math.PI * rnd());
  const views = Math.round(650 * Math.exp(1.1 * z));
  return {
    views,
    link_clicks: Math.round(views * (0.004 + rnd() * 0.01)),
    likes: Math.round(views * (0.02 + rnd() * 0.03)),
    shares: Math.round(views * rnd() * 0.004),
  };
}

function accountIdFor(db: MockDb, platform: PlatformT, handle: string): string | null {
  const a = db.accounts.find((x) => x.platform === platform && x.handle === handle);
  return a ? String(a.id) : null;
}

export function createOps(db: MockDb, now: number): OpsState {
  const pubs: PublicationItemIn[] = [];
  let seed = 1;
  for (const [runId, contents] of Object.entries(db.contents)) {
    for (const c of contents) {
      for (const p of c.publications ?? []) {
        const platform = p.platform;
        const failed = p.state === "FAILED";
        pubs.push({
          id: String(p.id),
          platform,
          state: p.state,
          account_handle: p.account_handle,
          account_id: accountIdFor(db, platform, p.account_handle),
          content_id: String(c.id),
          kind: c.kind,
          hook: c.hook,
          product: c.product ? { id: c.product.id, name: c.product.name, price: c.product.price, image_url: c.product.image_url } : null,
          scheduled_at: p.scheduled_at,
          published_at: p.published_at,
          permalink: p.permalink,
          sub_id: p.sub_id,
          link_placement: LINK_PLACEMENT[platform],
          caption: captionFor(c, platform),
          error: failed
            ? { code: "tiktok_unaudited", message: "미감사 앱 — 공개 게시 거절 (SELF_ONLY만 허용)", run_id: runId }
            : null,
          attempts: failed ? 3 : 1,
          metrics: p.state === "PUBLISHED" ? metricsFor(seed++) : {},
        });
      }
    }
  }

  // Upcoming week: cross-posts of earlier winners (M10 amplification) spread 09–23 KST.
  const rnd = mulberry32(99);
  const kstMidnight = Math.floor((now + 9 * HOUR) / DAY) * DAY - 9 * HOUR;
  const platforms: PlatformT[] = ["threads", "instagram", "threads", "youtube", "facebook"];
  for (let d = 0; d < 7; d++) {
    const perDay = 2 + Math.floor(rnd() * 3);
    for (let k = 0; k < perDay; k++) {
      const platform = platforms[(d + k) % platforms.length];
      const category: CategoryKey = k % 3 === 2 ? "storage" : "kitchen";
      const acc = accountFor(category, platform);
      const product = productFor(category, d * 3 + k);
      const hook = hookFor(category, d + k);
      const hour = 9 + Math.floor(rnd() * 14);
      const at = kstMidnight + d * DAY + hour * HOUR + Math.floor(rnd() * 50) * MIN;
      if (at < now + 10 * MIN) continue;
      const content = { id: `cnt_amp_${d}_${k}`, hook, product, kind: platform === "threads" ? "text" : "short" } as const;
      pubs.push({
        id: `pub_amp_${d}_${k}`,
        platform,
        state: "QUEUED",
        account_handle: acc.handle,
        account_id: acc.id,
        content_id: content.id,
        kind: content.kind,
        hook,
        product: { id: product.id, name: product.name, price: product.price, image_url: product.image_url },
        scheduled_at: iso(at),
        published_at: null,
        permalink: null,
        sub_id: `acvm-amp${d}${k}-${platform.slice(0, 3)}`,
        link_placement: LINK_PLACEMENT[platform],
        caption: `${DISCLOSURE_TEXT}\n\n${hook}\n${product.name} — ${product.price.toLocaleString("ko-KR")}원\n\n#살림템`,
        error: null,
        attempts: 0,
        metrics: {},
      });
    }
  }

  return {
    checklist: { partners_policy: true, ad_law: true, ai_act: false, platform_terms: false, tax_entity: false },
    secrets: {
      coupang_access_key: true,
      coupang_secret_key: false,
      link_signing_key: true,
      click_ip_salt: true,
      anthropic_api_key: true,
      elevenlabs_api_key: true,
      gemini_api_key: false,
      openai_api_key: false,
      higgsfield_key_id: false,
      higgsfield_key_secret: false,
      voyage_api_key: false,
      r2_access_key: true,
      r2_secret_key: true,
    },
    credentials: Object.fromEntries(
      db.accounts.map((a) => [String(a.id), !(a.id === "acc_car_ig" || a.id === "acc_kit_tt")]),
    ),
    workspace: { name: "TBURN LABS", daily_budget_krw: 20_000 },
    publications: pubs.sort((a, b) => String(b.scheduled_at).localeCompare(String(a.scheduled_at))),
  };
}

/* ------------------------------------------------------------------ system status */

function todaySpent(db: MockDb, now: number): number {
  return db.runs
    .filter((r) => r.started_at && now - new Date(r.started_at).getTime() < DAY)
    .reduce((a, r) => a + (r.cost_krw ?? 0), 0);
}

export function systemStatus(db: MockDb, ops: OpsState, now: number): SystemStatusIn {
  const s = ops.secrets;
  const coupangOk = s.coupang_access_key && s.coupang_secret_key;
  const accounts = db.accounts.map((a, i) => ({
    id: a.id,
    platform: a.platform,
    handle: a.handle,
    niche: a.niche,
    status: a.status,
    credential: (a.platform === "kakao" ? "not_required" : ops.credentials[String(a.id)] ? "configured" : "missing") as
      | "configured"
      | "missing"
      | "not_required",
    used_24h: Math.min(a.daily_cap, Math.round(a.daily_cap * ((i * 37) % 100) / 100)),
    daily_cap: a.daily_cap,
  }));
  const missing = accounts.filter((a) => a.credential === "missing");
  const check = (id: string, label: string, status: "ok" | "warn" | "fail", detail: string, fix = "") => ({ id, label, status, detail, fix });
  const checks = [
    check("db", "데이터베이스", "ok", "postgresql 연결 정상"),
    check("redis", "Redis (쿼터·이벤트)", "ok", "연결 정상"),
    check("orchestrator", "오케스트레이터", "ok", "Temporal (내구 실행)"),
    check(
      "link_key",
      "제휴 링크 서명 키",
      s.link_signing_key ? "ok" : "fail",
      s.link_signing_key ? "설정됨" : "없음 — 링크 생성·게시가 차단됩니다",
      s.link_signing_key ? "" : "운영 준비 › 비밀값 › 제휴 링크 서명 키 등록 (32바이트 이상 임의값)",
    ),
    check(
      "coupang",
      "쿠팡 파트너스 API",
      coupangOk ? "ok" : "fail",
      coupangOk ? "실서비스 연결" : "키 미등록",
      coupangOk ? "" : "Access/Secret Key 등록 + ACVM_COUPANG__PARTNER_ID",
    ),
    check("platforms", "소셜 플랫폼 게시", "ok", "공식 API로 실제 게시"),
    check("tiktok", "TikTok 앱 감사", "warn", "미감사 — 비공개(SELF_ONLY) 게시만 가능", "TikTok Content Posting API 감사 통과 후 ACVM_PLATFORMS__TIKTOK_AUDITED=true"),
    llmCheck(s),
    check("ffmpeg", "렌더러 (FFmpeg)", "ok", "/usr/bin/ffmpeg"),
    check("font", "한글 폰트", "ok", "Noto Sans CJK KR"),
    check("media_url", "미디어 공개 URL", "ok", "https://media.acvm.example"),
    check("auth", "콘솔 인증", "ok", "Cloudflare Access SSO"),
    check("secrets", "비밀값 저장소", "warn", "환경변수/메모리 — 콘솔 입력값은 재시작 시 사라짐", "ACVM_SECURITY__SECRET_BACKEND=vault"),
  ];
  if (missing.length) {
    checks.push(check("tokens", "계정 토큰", "fail", `${missing.length}개 계정 토큰 없음 — 해당 계정 게시 불가`, "계정 화면 › 토큰 등록"));
  }
  const g0Items = [
    { id: "coupang_api", label: "쿠팡 파트너스 실 API 연결", done: !!coupangOk },
    ...Object.entries(LEGAL_CHECKLIST).map(([id, label]) => ({ id, label, done: !!ops.checklist[id] })),
  ];
  return {
    mode: {
      env: "staging",
      orchestrator: "temporal",
      coupang_sandbox: false,
      platform_dry_run: false,
      llm_backend: "anthropic",
      auth_mode: "cloudflare_access",
      tiktok_audited: false,
    },
    checks,
    summary: {
      ok: checks.filter((c) => c.status === "ok").length,
      warn: checks.filter((c) => c.status === "warn").length,
      fail: checks.filter((c) => c.status === "fail").length,
    },
    g0: { passed: g0Items.every((i) => i.done), items: g0Items },
    budget: { today_spent_krw: todaySpent(db, now), daily_budget_krw: ops.workspace.daily_budget_krw },
    accounts,
    breakers: [
      { name: "search", state: "closed", opened_at: null, failures: 0 },
      { name: "deeplink", state: "half_open", opened_at: iso(now - 50 * MIN), failures: 2 },
    ],
    secrets: SECRET_SLOTS.map(([name, label]) => ({ name, label, configured: !!s[name] })),
  };
}

export function workspace(db: MockDb, ops: OpsState, now: number): WorkspaceIn {
  const month = moneyFixture(30, now).daily.reduce((a, d) => a + d.cost_krw, 0);
  return {
    id: "ws_demo",
    name: ops.workspace.name,
    plan: "internal",
    daily_budget_krw: ops.workspace.daily_budget_krw,
    today_spent_krw: todaySpent(db, now),
    last30d_spent_krw: Math.round(month),
  };
}

/* ------------------------------------------------------------------ publications */

export function listPublications(
  ops: OpsState,
  q: { state?: string; platform?: string; account_id?: string; since?: string; until?: string; limit?: number },
): { items: PublicationItemIn[] } {
  const states = q.state ? q.state.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean) : null;
  const since = q.since ? new Date(q.since).getTime() : null;
  const until = q.until ? new Date(q.until).getTime() : null;
  const items = ops.publications.filter((p) => {
    if (states && !states.includes(p.state)) return false;
    if (q.platform && p.platform !== q.platform) return false;
    if (q.account_id && p.account_id !== q.account_id) return false;
    const t = p.scheduled_at ? new Date(p.scheduled_at).getTime() : 0;
    if (since !== null && t < since) return false;
    if (until !== null && t >= until) return false;
    return true;
  });
  return { items: items.slice(0, q.limit ?? 200) };
}

export function takedown(db: MockDb, ops: OpsState, id: string, reason: string) {
  const pub = ops.publications.find((p) => String(p.id) === id);
  if (!pub) throw new ApiError("http", "HTTP 404", { status: 404, detail: "publication not found" });
  if (pub.state === "TAKEN_DOWN" || pub.state === "CANCELLED") {
    throw new ApiError("http", "HTTP 409", { status: 409, detail: `이미 ${pub.state} 상태입니다` });
  }
  if (pub.state === "PUBLISHED" && (pub.platform === "instagram" || pub.platform === "tiktok")) {
    // No delete API on these platforms: the saga records the request; a human removes the post.
    pub.error = { manual_takedown_required: true, reason, message: `${pub.platform}는 게시물 삭제 API가 없습니다 — 앱에서 직접 삭제하세요` };
  } else {
    pub.state = pub.state === "PUBLISHED" ? "TAKEN_DOWN" : "CANCELLED";
    pub.error = { reason };
  }
  for (const list of Object.values(db.contents)) {
    for (const c of list) {
      const p = c.publications?.find((x) => String(x.id) === id);
      if (p) p.state = pub.state;
    }
  }
  return { publication_id: String(pub.id), state: pub.state, error: pub.error ?? null };
}

/* ------------------------------------------------------------------ content dossier */

function findContent(db: MockDb, id: string): { content: ContentIn; runId: string } | null {
  for (const [runId, list] of Object.entries(db.contents)) {
    const c = list.find((x) => String(x.id) === id);
    if (c) return { content: c, runId };
  }
  return null;
}

export function contentDossier(db: MockDb, ops: OpsState, id: string, now: number): ContentDossierIn {
  let found = findContent(db, id);
  if (!found) {
    // Upcoming amplification posts reference synthetic content ids — materialise a minimal card.
    const pub = ops.publications.find((p) => p.content_id === id);
    if (!pub || !pub.product) throw new ApiError("http", "HTTP 404", { status: 404, detail: "content not found" });
    found = {
      runId: "",
      content: {
        id,
        product: { ...pub.product, category_id: "COUPANG-1013" },
        kind: pub.kind ?? "short",
        status: "approved",
        hook: pub.hook,
        persona: "30대 여성",
        template_id: "problem-solution-v3",
        verdict: "PASS",
        findings: [],
        media_url: null,
        thumbnail_url: pub.kind === "short" ? posterSvg(pub.hook ?? "", pub.product.name, 7) : null,
        publications: [],
      },
    };
  }
  const { content: c, runId } = found;
  const hook = c.hook ?? "";
  const name = c.product?.name ?? "상품";
  const short = c.kind === "short";
  const category = categoryOf(c);
  const hooks = [hook, hookFor(category, 1), hookFor(category, 2)].filter((h, i, a) => h && a.indexOf(h) === i);
  const beats = short
    ? [
        { start_sec: 0, end_sec: 3, role: "hook", narration: hook, on_screen_text: hook, visual: "problem_scene", claim_ids: [] },
        { start_sec: 3, end_sec: 8, role: "problem", narration: "매번 손이 가는데 정리가 안 되는 그 순간, 다들 있죠?", on_screen_text: "이 불편함", visual: "problem_scene", claim_ids: [] },
        { start_sec: 8, end_sec: 16, role: "product", narration: `그래서 찾은 게 ${name}이에요.`, on_screen_text: name, visual: "product_closeup", claim_ids: ["c1"] },
        { start_sec: 16, end_sec: 24, role: "benefits", narration: "리뷰 평점이 높고 로켓배송이라 바로 받아볼 수 있어요.", on_screen_text: "평점 4.7 · 로켓배송", visual: "usage_scene", claim_ids: ["c2", "c3"] },
        { start_sec: 24, end_sec: 29, role: "cta", narration: "프로필 링크에서 확인하세요.", on_screen_text: "프로필 링크 ↗", visual: "cta_card", claim_ids: [] },
      ]
    : [];
  const pubs = ops.publications.filter((p) => p.content_id === String(c.id));
  const findings = c.findings ?? [];
  const fixed = findings.filter((f) => f.fixed);
  const open = findings.filter((f) => !f.fixed);
  const t0 = now - 3 * HOUR;
  const checks: ContentDossierIn["checks"] = [];
  if (c.verdict) {
    if (fixed.length) {
      checks.push({ id: `chk_${c.id}_1`, stage: "gate", verdict: "FIX", rule_set_version: "v1", iteration: 1, decided_at: iso(t0), findings: fixed });
    }
    checks.push({
      id: `chk_${c.id}_2`,
      stage: "gate",
      verdict: c.verdict === "FIX" ? "PASS" : c.verdict,
      rule_set_version: "v1",
      iteration: fixed.length ? 2 : 1,
      decided_at: iso(t0 + 40_000),
      findings: open,
    });
    if (pubs.some((p) => p.state === "PUBLISHED")) {
      checks.push({ id: `chk_${c.id}_3`, stage: "preflight", verdict: "PASS", rule_set_version: "v1", iteration: 1, decided_at: iso(t0 + 20 * MIN), findings: [] });
    }
  }
  const hold = db.holds.find((h) => h.content_id === String(c.id));
  return {
    id: String(c.id),
    kind: c.kind,
    status: c.status,
    persona: c.persona,
    template_id: c.template_id,
    hook_variant: "A",
    similarity_max: 0.31,
    created_at: iso(t0 - 5 * MIN),
    run_id: runId || null,
    parent_content_id: String(c.id).startsWith("cnt_amp_") ? "cnt_1b9f7c_2" : null,
    product: c.product
      ? {
          id: c.product.id,
          name: c.product.name,
          price: c.product.price,
          image_url: c.product.image_url,
          product_url: `https://www.coupang.com/vp/products/${c.product.id}`,
          category_name: category === "car" ? "차량용품" : category === "storage" ? "정리수납" : "주방",
          rating: 4.7,
          review_count: 1284,
          is_rocket: true,
        }
      : null,
    script: {
      hooks,
      chosen_hook: 0,
      hook_type: "question",
      beats,
      body: short ? "" : `${hook}\n\n1) ${name}\n2) 리뷰 평점 4.7 · 로켓배송\n3) 공간 절약형`,
      caption: `${DISCLOSURE_TEXT}\n${hook}`,
      hashtags: ["살림템", "쿠팡추천"],
      cta: "프로필 링크에서 확인하세요",
      disclosure: DISCLOSURE_TEXT,
      title: name,
      duration_sec: short ? 29 : 0,
      persona: c.persona ?? "30대 여성",
      template_id: c.template_id ?? "problem-solution-v3",
    },
    claims: [
      { id: "c1", text: `${name}`, kind: "product_identity", evidence: ["name"], grounded: true },
      { id: "c2", text: "리뷰 평점 4.7", kind: "rating", evidence: ["rating", "review_count"], grounded: true },
      { id: "c3", text: "로켓배송", kind: "shipping", evidence: ["is_rocket"], grounded: true },
      ...(open.some((f) => f.rule_id === "C-01")
        ? [{ id: "c4", text: "세균 99% 제거", kind: "efficacy", evidence: [], grounded: false }]
        : []),
    ],
    render: short
      ? {
          duration_sec: 29,
          width: 1080,
          height: 1920,
          fps: 30,
          loudness_lufs: -14.1,
          disclosure_overlay: { text: "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", start: 0, end: 3, box: "top" },
          voice_id: "ko-F-30s-brisk",
          voice_source: "elevenlabs:licensed",
          template_id: c.template_id ?? "problem-solution-v3",
          renderer: "ffmpeg-6.1",
          media_url: c.media_url ?? null,
          thumbnail_url: c.thumbnail_url ?? null,
        }
      : null,
    checks,
    review: hold
      ? { id: String(hold.id), status: "pending", reasons: hold.reasons ?? [], decided_by: null, note: null }
      : null,
    publications: pubs.map((p) => ({ id: p.id, platform: p.platform, state: p.state, scheduled_at: p.scheduled_at, caption: p.caption })),
    cost_krw: short ? 148.6 : 38.2,
  };
}

/* ------------------------------------------------------------------ alerts */

export function alertsFeed(db: MockDb, ops: OpsState, now: number): AlertsIn {
  const items: AlertsIn["items"] = [
    { at: iso(now - 9 * MIN), severity: "medium", kind: "gate.hold", label: "HOLD 대기", target: "content:cnt_a91f3e_4", actor: "system:m7", detail: { reasons: ["R-03 상표·캐릭터 노출 의심"] } },
    { at: iso(now - 47 * MIN), severity: "high", kind: "invalid_click", label: "무효 클릭 의심", target: "acvm-a91f3e-2-thr", actor: "system:m9", detail: { reason: "동일 IP 반복 클릭 (12회/3분)", score: 0.91 } },
    { at: iso(now - 47 * MIN), severity: "high", kind: "link.paused", label: "링크 일시 중지", target: "acvm-a91f3e-2-thr", actor: "system:m9", detail: { until: iso(now + 6 * HOUR) } },
    { at: iso(now - 140 * MIN), severity: "medium", kind: "budget.exceeded", label: "예산 상한 도달", target: "run:7d0be5f1-9a3c-4e2b-b1d0-1a2b3c4d5e04", actor: "system:orchestrator", detail: { cap_krw: 1000 } },
    { at: iso(now - 17 * HOUR), severity: "high", kind: "account.cooldown", label: "계정 쿨다운", target: "account:acc_car_thr", actor: "system:quota", detail: { reason: "게시 실패율 12% (24h)", hours: 48 } },
    { at: iso(now - 26 * HOUR), severity: "high", kind: "gate.block", label: "게이트 차단", target: "content:cnt_1b9f7c_5", actor: "system:m7", detail: { findings: ["권리 원장에 없는 B-roll 에셋 사용"] } },
    { at: iso(now - 29 * HOUR), severity: "medium", kind: "publication_failed", label: "게시 실패", target: "publication:pub_5a3e9d_0_tt", actor: "system:m8", detail: { message: "미감사 앱 — SELF_ONLY" } },
  ];
  for (const p of ops.publications) {
    if (p.error && (p.error as Record<string, unknown>).manual_takedown_required) {
      items.unshift({ at: iso(now), severity: "medium", kind: "manual_takedown_required", label: "수동 삭제 필요", target: `publication:${p.id}`, actor: "system:m8", detail: { platform: p.platform } });
    }
  }
  return {
    items,
    accounts_attention: db.accounts
      .filter((a) => a.status !== "active")
      .map((a) => ({ id: a.id, platform: a.platform, handle: a.handle, status: a.status, until: a.cooldown_until ?? null })),
    pending_holds: db.holds.length,
    counts: { high: items.filter((i) => i.severity === "high").length, medium: items.filter((i) => i.severity === "medium").length },
  };
}
