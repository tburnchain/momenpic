/**
 * Mock Shorts Studio (src/acvm/api/studio.py): options, product search, drafts with live lint,
 * background render, gate + publish, "전부 자동", and the asset library tools.
 * Binary endpoints (voice preview WAV, caption style tile) are synthesised in the browser.
 */
import type { z } from "zod";
import type * as S from "@/lib/schemas";
import { ApiError } from "@/lib/errors";
import {
  CATEGORIES,
  DISCLOSURE_TEXT,
  DISCLOSURE_VIDEO,
  HOOKS,
  PRODUCTS,
  mulberry32,
  uuid,
  type CategoryKey,
  type MockDb,
} from "./fixtures";
import { posterSvg, productSvg } from "./media";

type DraftIn = z.input<typeof S.DraftView>;
type OptionsIn = z.input<typeof S.StudioOptions>;
type ProductIn = z.input<typeof S.StudioProduct>;
type LintIn = z.input<typeof S.LintResult>;
type AssetIn = z.input<typeof S.StudioAsset>;
type PublishIn = z.input<typeof S.StudioPublishResult>;
type BeatIn = z.input<typeof S.DraftBeat>;

const rnd = mulberry32(4242);
const MIN = 60_000;
const iso = (ms: number) => new Date(ms).toISOString();

function http(status: number, detail: string): never {
  throw new ApiError("http", `HTTP ${status}`, { status, detail });
}

/* ------------------------------------------------------------------ catalog */

const STUDIO_CATEGORY: Record<CategoryKey, { id: string; name: string }> = {
  kitchen: { id: "1013", name: "주방용품" },
  car: { id: "1018", name: "자동차용품" },
  storage: { id: "1014", name: "생활용품" },
};

// Fast voices only (src/acvm/media/voices.py): shorts are 20–25 s product pitches, never narration pace.
const VOICES = [
  { id: "ko-F-30s-brisk", name: "서윤", gender: "F", age_band: "30s", tone: "경쾌한 대화체", description: "30대 여성의 빠르고 경쾌한 대화체. 살림·주방 쇼츠의 기본 보이스.", persona_tags: ["30대", "여성", "직장인", "살림", "주방"], rate: 1.22, f0: 210 },
  { id: "ko-F-30s-rapid", name: "지현", gender: "F", age_band: "30s", tone: "속사포 정보형", description: "핵심만 빠르게 짚는 정보형 톤. 비교·스펙 정리 쇼츠에 강해요.", persona_tags: ["정보", "비교", "스펙", "리뷰"], rate: 1.28, f0: 214 },
  { id: "ko-F-20s-bright", name: "하은", gender: "F", age_band: "20s", tone: "발랄한", description: "20대 여성의 밝고 빠른 톤. 자취템·뷰티·간식 쇼츠에 잘 어울려요.", persona_tags: ["20대", "여성", "대학생", "자취"], rate: 1.25, f0: 228 },
  { id: "ko-F-20s-energetic", name: "다인", gender: "F", age_band: "20s", tone: "하이텐션", description: "텐션 높은 속도감 있는 톤. 가격 반전·특가 훅에 가장 강해요.", persona_tags: ["20대", "특가", "가성비"], rate: 1.3, f0: 236 },
  { id: "ko-F-40s-lively", name: "미소", gender: "F", age_band: "40s", tone: "생기있는", description: "40대 여성의 생기 있고 빠른 살림 톤. 육아·대용량 생활용품에 어울려요.", persona_tags: ["40대", "50대", "여성", "주부"], rate: 1.2, f0: 196 },
  { id: "ko-M-30s-brisk", name: "준호", gender: "M", age_band: "30s", tone: "빠른 대화체", description: "30대 남성의 빠르고 친근한 대화체. 차량·캠핑·공구 쇼츠의 기본 보이스.", persona_tags: ["30대", "남성", "차량", "캠핑", "직장인"], rate: 1.22, f0: 118 },
  { id: "ko-M-30s-rapid", name: "태오", gender: "M", age_band: "30s", tone: "속사포 리뷰", description: "속도감 있는 남성 리뷰 톤. 전자기기·가전 핵심 정리에 적합.", persona_tags: ["30대", "남성", "가전", "리뷰"], rate: 1.3, f0: 122 },
  { id: "ko-M-20s-energetic", name: "민재", gender: "M", age_band: "20s", tone: "활발한", description: "20대 남성의 활기차고 빠른 톤. 게임·자취·남성 타깃 쇼츠에 적합.", persona_tags: ["20대", "남성", "게임", "자취"], rate: 1.28, f0: 128 },
  { id: "ko-M-40s-punchy", name: "상훈", gender: "M", age_band: "40s", tone: "힘있는 단문", description: "40대 남성의 힘 있고 짧게 끊는 톤. 차량·아빠템 쇼츠에 어울려요.", persona_tags: ["40대", "50대", "남성", "아빠"], rate: 1.2, f0: 106 },
];

const CAPTION_STYLES = [
  { id: "classic", label: "기본", description: "흰 글자 + 검정 테두리" },
  { id: "box", label: "박스", description: "반투명 검정 박스 위 흰 글자" },
  { id: "highlight", label: "형광펜", description: "노란 형광 배경 강조" },
  { id: "bold", label: "굵은 외곽선", description: "두꺼운 외곽선 + 그림자" },
  { id: "minimal", label: "미니멀", description: "작고 얇은 흰 글자" },
  { id: "pop", label: "팝", description: "키워드 컬러 강조" },
];

export function studioOptions(db: MockDb): OptionsIn {
  return {
    voices: VOICES.map((v) => ({
      id: v.id,
      name: v.name,
      gender: v.gender,
      age_band: v.age_band,
      tone: v.tone,
      description: v.description,
      persona_tags: v.persona_tags,
      rate: v.rate,
      sample_text: `${v.name}이에요! 이거 하나면 끝, 바로 보여드릴게요.`,
    })),
    caption_styles: CAPTION_STYLES,
    caption_positions: ["bottom", "middle", "upper"],
    tts: {
      provider: "sapi",
      real_voice: false,
      note: "모의 모드: ElevenLabs 키가 없어 기본 한국어 음성 1종을 속도·높이만 바꿔 사용합니다. 미리듣기는 합성 톤입니다.",
    },
    personas: [
      { name: "30대 여성 직장인", voice_hint: "ko-F-30s-brisk", tone: "공감형" },
      { name: "20대 자취생", voice_hint: "ko-F-20s-bright", tone: "발랄" },
      { name: "40대 주부", voice_hint: "ko-F-40s-lively", tone: "생기" },
      { name: "30대 남성 직장인", voice_hint: "ko-M-30s-brisk", tone: "실용" },
    ],
    templates: [
      { id: "problem-solution-v3", kind: "short", description: "문제 → 불편 증폭 → 상품 → 장점 → CTA", duration: 22 },
      { id: "before-after-v1", kind: "short", description: "전후 비교", duration: 22 },
      { id: "text-v1", kind: "text", description: "스레드형 리스트 글", duration: 0 },
    ],
    hook_types: ["question", "shock", "before_after", "price_twist", "empathy"],
    accounts: db.accounts.map((a) => ({ id: a.id, platform: a.platform, handle: a.handle, niche: a.niche, status: a.status })),
    disclosure: {
      locked: true,
      text: DISCLOSURE_VIDEO,
      seconds: 3,
      note: "광고 표기는 법적 의무라 끌 수 없습니다 (표시광고법·추천보증 심사지침, 설계서 §8 D-03)",
    },
    limits: {
      sources: 5,
      script_chars: 500,
      watermark_chars: 24,
      min_seconds: SHORT_MIN_SECONDS,
      max_seconds: SHORT_MAX_SECONDS,
      syllables_per_second: FAST_SPS,
      min_speech_sps: 7.5,
      note: "쇼츠는 20~25초 상품 설명 — 빠르고 간결한 보이스만 사용합니다",
    },
  };
}

function allProducts(): Array<ProductIn & { key: CategoryKey }> {
  const out: Array<ProductIn & { key: CategoryKey }> = [];
  (Object.keys(PRODUCTS) as CategoryKey[]).forEach((key) => {
    PRODUCTS[key].forEach((p, i) => {
      const r = mulberry32(p.id)();
      out.push({
        key,
        id: p.id,
        name: p.name,
        price: p.price,
        image_url: productSvg(p.name, p.id),
        category_id: STUDIO_CATEGORY[key].id,
        category_name: STUDIO_CATEGORY[key].name,
        is_rocket: i % 3 !== 2,
        rating: Math.round((4.2 + r * 0.7) * 10) / 10,
        review_count: Math.round(300 + r * 4000),
        pos: Math.round((0.62 + r * 0.33) * 1000) / 1000,
        price_checked_at: iso(Date.now() - Math.round(r * 50) * MIN),
      });
    });
  });
  return out.sort((a, b) => (b.pos ?? 0) - (a.pos ?? 0));
}

export function studioProducts(q: { category?: string; q?: string; limit?: number }): { items: ProductIn[] } {
  const term = (q.q ?? "").trim();
  const items = allProducts()
    .filter((p) => !q.category || p.category_id === q.category)
    .filter((p) => !term || p.name.includes(term))
    .slice(0, q.limit ?? 30)
    .map((p) => {
      const { key, ...rest } = p;
      void key;
      return rest;
    });
  return { items };
}

function productById(id: number) {
  const p = allProducts().find((x) => x.id === id);
  if (!p) http(404, `product ${id} not found`);
  return p;
}

/* ------------------------------------------------------------------ lint */

const BANNED: Array<{ re: RegExp; term: string; suggestion: string; kind: string; message: string }> = [
  { re: /최고(의)?/, term: "최고", suggestion: "자주 쓰는", kind: "superlative", message: "최상급 표현 (C-02)" },
  { re: /무조건/, term: "무조건", suggestion: "대부분", kind: "absolute", message: "절대 표현 (C-02)" },
  { re: /100\s*%/, term: "100%", suggestion: "문장 삭제", kind: "absolute", message: "근거 없는 100% 표현 (C-02)" },
  { re: /완벽(한|하게)?/, term: "완벽", suggestion: "꼼꼼", kind: "superlative", message: "최상급 표현 (C-02)" },
  { re: /(세균|바이러스).{0,6}(제거|박멸|살균)/, term: "", suggestion: "문장 삭제", kind: "efficacy", message: "근거 없는 효능 주장 (C-01)" },
];

function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?。！？])\s+|\n+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

const SHORT_MIN_SECONDS = 20;
const SHORT_MAX_SECONDS = 25;
const FAST_SPS = 8;

/** Mirrors api/studio.py _with_duration: fast-rate estimate + 1.2 s CTA hold; > 25 s blocks generation. */
function withDuration(lint: LintIn, narrations: string[]): LintIn {
  const syllables = narrations.reduce((n, t) => n + Array.from(t.replace(/[^가-힣0-9A-Za-z]/g, "")).length, 0);
  const est = Math.round((syllables / FAST_SPS + 1.2) * 10) / 10;
  const issues = [...(lint.issues ?? [])];
  if (est > SHORT_MAX_SECONDS) {
    issues.push({
      sentence: "",
      kind: "too_long",
      severity: "error",
      term: "",
      suggestion: "",
      message: `예상 ${est}초 — ${SHORT_MAX_SECONDS}초 이내로 줄이세요 (빠른 속도 ${FAST_SPS}음절/초 기준)`,
    });
  } else if (est < SHORT_MIN_SECONDS) {
    issues.push({
      sentence: "",
      kind: "too_short",
      severity: "warning",
      term: "",
      suggestion: "",
      message: `예상 ${est}초 — ${SHORT_MIN_SECONDS}초 미만은 CTA 화면으로 채워집니다`,
    });
  }
  return {
    ...lint,
    issues,
    ok: !issues.some((i) => i.severity === "error"),
    duration: { estimated_seconds: est, syllables, min_seconds: SHORT_MIN_SECONDS, max_seconds: SHORT_MAX_SECONDS, syllables_per_second: FAST_SPS },
  };
}

export function lintText(productId: number, text: string, narrations?: string[] | null): LintIn {
  const out = lintBase(productId, text);
  return narrations ? withDuration(out, narrations) : out;
}

function draftLint(d: DraftIn): LintIn {
  return lintText(d.product?.id ?? 0, draftText(d), d.kind === "short" ? (d.beats ?? []).map((b) => b.narration ?? "") : null);
}

function lintBase(productId: number, text: string): LintIn {
  const product = productById(productId);
  const issues: LintIn["issues"] = [];
  const claims: NonNullable<LintIn["claims"]> = [];
  for (const s of sentences(text)) {
    for (const b of BANNED) {
      const m = b.re.exec(s);
      if (m) issues?.push({ sentence: s, kind: b.kind, message: b.message, term: b.term || m[0], suggestion: b.suggestion, severity: "error" });
    }
    const price = /([\d,]{4,})\s*원/.exec(s);
    if (price) {
      const n = Number(price[1].replace(/,/g, ""));
      if (product.price && n !== product.price) {
        issues?.push({
          sentence: s,
          kind: "price",
          message: `가격 불일치 — 현재 ${product.price.toLocaleString("ko-KR")}원 (C-03)`,
          term: price[0],
          suggestion: `${product.price.toLocaleString("ko-KR")}원`,
          severity: "error",
        });
      } else {
        claims.push({ id: `a${claims.length + 1}`, text: s, kind: "price", evidence: ["price", "price_checked_at"], grounded: true });
      }
    }
    if (/리뷰/.test(s)) claims.push({ id: `a${claims.length + 1}`, text: s, kind: "review", evidence: ["review_count"], grounded: true });
    if (/평점|별점/.test(s)) claims.push({ id: `a${claims.length + 1}`, text: s, kind: "review", evidence: ["rating"], grounded: true });
    if (/로켓/.test(s)) claims.push({ id: `a${claims.length + 1}`, text: s, kind: "shipping", evidence: ["is_rocket"], grounded: true });
    if (/\d+\s*(cm|mm|ml|L|개입|P)\b/i.test(s)) claims.push({ id: `a${claims.length + 1}`, text: s, kind: "spec", evidence: ["spec.size"], grounded: true });
    if (/(인생템|강추)/.test(s)) {
      issues?.push({ sentence: s, kind: "testimonial", message: "실사용 확인 전 체험담 표현은 피하세요 (§8.2)", term: "인생템", suggestion: "추천템", severity: "warning" });
    }
  }
  return { ok: !(issues ?? []).some((i) => i.severity === "error"), issues, claims };
}

/* ------------------------------------------------------------------ drafts */

const drafts = new Map<string, DraftIn & { _renderAt?: number; _productKey: CategoryKey }>();

function draftText(d: DraftIn): string {
  return [...(d.beats ?? []).map((b) => b.narration ?? ""), ...(d.beats ?? []).map((b) => b.on_screen_text ?? ""), d.body ?? "", d.caption ?? ""].join("\n");
}

function view(d: DraftIn & { _renderAt?: number; _productKey?: CategoryKey }): DraftIn {
  if (d.render?.state === "rendering" && d._renderAt && Date.now() - d._renderAt > 7000) {
    const hook = d.hooks?.[d.chosen_hook ?? 0] ?? "";
    d.render = {
      state: "done",
      media_url: null,
      thumbnail_url: posterSvg(hook, d.product?.name ?? "", d.product?.id ?? 1),
      duration: d.duration_target ?? 22,
      voice_id: (d.render_options?.voice_id as string | undefined) ?? "ko-F-30s-brisk",
      loudness_lufs: -14.2,
    };
  }
  const copy = JSON.parse(JSON.stringify(d)) as DraftIn & { _renderAt?: number; _productKey?: string };
  delete copy._renderAt;
  delete copy._productKey;
  return copy;
}

function getDraft(id: string) {
  const d = drafts.get(id);
  if (!d) http(404, "draft not found");
  return d;
}

export function createDraft(
  db: MockDb,
  body: { product_id: number; kind?: "short" | "text"; persona?: string; account_id?: string; platform?: string },
): DraftIn {
  const p = productById(body.product_id);
  const kind = body.kind ?? "short";
  const persona = body.persona || (p.key === "car" ? "30대 남성 직장인" : "30대 여성 직장인");
  const acc =
    db.accounts.find((a) => a.id === body.account_id) ??
    db.accounts.find((a) => (kind === "text" ? a.platform === "threads" : a.platform === "instagram") && a.niche === CATEGORIES[p.key].niche) ??
    db.accounts.find((a) => a.platform === "threads");
  const hooks = [HOOKS[p.key][0], HOOKS[p.key][1] ?? "이거 모르고 사셨죠?", `${(p.price ?? 0).toLocaleString("ko-KR")}원으로 바뀐 일상`];
  const price = (p.price ?? 0).toLocaleString("ko-KR");
  const beats: BeatIn[] =
    kind === "short"
      ? [
          { index: 0, role: "hook", start: 0, end: 3, narration: hooks[0], on_screen_text: hooks[0], visual: "problem_scene", claim_ids: [] },
          { index: 1, role: "problem", start: 3, end: 6.5, narration: "매번 손이 가는데 정리가 안 되는 그 순간, 다들 있죠?", on_screen_text: "이 불편함, 저만 그래요?", visual: "problem_scene", claim_ids: [] },
          { index: 2, role: "product", start: 6.5, end: 11, narration: `그래서 찾은 게 ${p.name}이에요.`, on_screen_text: p.name, visual: "product_closeup", claim_ids: [] },
          { index: 3, role: "benefits", start: 11, end: 18, narration: `리뷰가 ${(p.review_count ?? 0).toLocaleString("ko-KR")}개 넘게 달렸고 평점은 ${p.rating}점이에요. 로켓배송이라 내일 받아요.`, on_screen_text: `평점 ${p.rating} · 리뷰 ${(p.review_count ?? 0).toLocaleString("ko-KR")}`, visual: "usage_scene", claim_ids: ["c1", "c2"] },
          { index: 4, role: "cta", start: 18, end: 22, narration: "가격은 프로필 링크에서 확인하세요.", on_screen_text: "프로필 링크 ↗", visual: "cta_card", claim_ids: [] },
        ]
      : [];
  const id = `drf_${uuid(rnd).slice(0, 8)}`;
  const draft: DraftIn & { _productKey: CategoryKey } = {
    _productKey: p.key,
    id,
    status: "draft",
    kind,
    persona,
    template_id: kind === "short" ? "problem-solution-v3" : "text-v1",
    hook_type: "question",
    run_id: null,
    product: {
      id: p.id,
      name: p.name,
      price: p.price,
      image_url: p.image_url,
      category_name: p.category_name,
      rating: p.rating,
      review_count: p.review_count,
      is_rocket: p.is_rocket,
      price_checked_at: p.price_checked_at,
    },
    account: acc ? { id: acc.id, platform: acc.platform, handle: acc.handle } : null,
    hooks,
    chosen_hook: 0,
    beats,
    caption: `${DISCLOSURE_TEXT}\n${hooks[0]}`,
    body:
      kind === "text"
        ? `${DISCLOSURE_TEXT}\n\n${hooks[0]}\n\n✔ 리뷰가 ${(p.review_count ?? 0).toLocaleString("ko-KR")}개 넘게 달린 상품이에요\n✔ 로켓배송이라 받기도 편해요\n✔ 평점은 ${p.rating}점이에요\n\n가격은 ${price}원이에요. 링크는 본문 아래에 남겨둘게요.`
        : "",
    hashtags: ["살림템", "쿠팡추천"],
    title: p.name,
    disclosure: DISCLOSURE_TEXT,
    duration_target: kind === "short" ? 22 : 0,
    claims: [
      { id: "c1", text: `리뷰 ${(p.review_count ?? 0).toLocaleString("ko-KR")}개`, kind: "review", evidence: ["review_count"], grounded: true },
      { id: "c2", text: `평점 ${p.rating}점`, kind: "review", evidence: ["rating"], grounded: true },
      { id: "c3", text: "로켓배송", kind: "shipping", evidence: ["is_rocket"], grounded: true },
    ],
    render_options: {
      voice_id: persona.includes("남성") ? "ko-M-30s-brisk" : "ko-F-30s-brisk",
      caption_style: "classic",
      caption_position: "bottom",
      captions_enabled: true,
      watermark: acc ? `@${acc.handle.replace(/^@/, "")}` : "",
      beat_assets: {},
    },
    render: { state: "idle" },
    lint: { ok: true, issues: [], claims: [] },
    gate: null,
    publications: [],
  };
  draft.lint = draftLint(draft);
  drafts.set(id, draft);
  return view(draft);
}

export function hasDraft(id: string): boolean {
  return drafts.has(id);
}

/** Minimal /contents/{id} dossier for a studio draft (mock mode). */
export function draftDossier(id: string): z.input<typeof S.ContentDossier> {
  const d = view(getDraft(id));
  const p = d.product;
  return {
    id: String(d.id),
    kind: d.kind,
    status: d.status,
    persona: d.persona,
    template_id: d.template_id,
    hook_variant: "A",
    similarity_max: 0.22,
    created_at: new Date().toISOString(),
    run_id: null,
    parent_content_id: null,
    product: p ? { id: p.id, name: p.name, price: p.price, image_url: p.image_url, category_name: p.category_name, rating: p.rating, review_count: p.review_count, is_rocket: p.is_rocket } : null,
    script: {
      hooks: d.hooks ?? [],
      chosen_hook: d.chosen_hook ?? 0,
      hook_type: d.hook_type,
      beats: (d.beats ?? []).map((b) => ({ start_sec: b.start, end_sec: b.end, role: b.role, narration: b.narration ?? "", on_screen_text: b.on_screen_text ?? "", visual: b.visual ?? "", claim_ids: b.claim_ids ?? [] })),
      body: d.body ?? "",
      caption: d.caption ?? "",
      hashtags: d.hashtags ?? [],
      cta: "",
      disclosure: d.disclosure ?? "",
      title: d.title ?? "",
      duration_sec: d.duration_target ?? 0,
      persona: d.persona,
      template_id: d.template_id,
    },
    claims: d.claims ?? [],
    render: d.render?.state === "done" ? { duration_sec: d.render.duration ?? null, width: 1080, height: 1920, fps: 30, loudness_lufs: d.render.loudness_lufs ?? null, voice_id: d.render.voice_id ?? null, media_url: d.render.media_url ?? null, thumbnail_url: d.render.thumbnail_url ?? null } : null,
    checks: d.gate ? [{ id: `chk_${d.id}`, stage: d.gate.stage ?? "gate", verdict: d.gate.verdict, rule_set_version: "v1", iteration: 1, decided_at: d.gate.decided_at ?? null, findings: d.gate.findings ?? [] }] : [],
    review: null,
    publications: (d.publications ?? []).map((x) => ({ id: x.id, platform: x.platform, state: x.state, scheduled_at: x.scheduled_at ?? null, caption: d.caption ?? null })),
    cost_krw: d.kind === "short" ? 151.3 : 36.4,
  };
}

export function getDraftView(id: string): DraftIn {
  return view(getDraft(id));
}

export function updateDraft(id: string, edits: Record<string, unknown>, assets: AssetIn[]): DraftIn {
  const d = getDraft(id);
  if (d.status === "approved" || d.status === "blocked") http(422, "승인·차단된 콘텐츠는 수정할 수 없습니다 — 새 초안을 만드세요");
  const before = JSON.stringify([d.hooks, d.chosen_hook, d.beats, d.caption, d.body, d.render_options]);
  if (Array.isArray(edits.hooks)) d.hooks = (edits.hooks as string[]).map((h) => String(h).slice(0, 120)).slice(0, 3);
  if (typeof edits.chosen_hook === "number") {
    if (edits.chosen_hook < 0 || edits.chosen_hook >= (d.hooks?.length ?? 0)) http(422, "chosen_hook out of range");
    d.chosen_hook = edits.chosen_hook;
  }
  if (Array.isArray(edits.beats)) {
    (edits.beats as Array<Record<string, string> | null>).forEach((b, i) => {
      const beat = d.beats?.[i];
      if (!b || !beat) return;
      if (typeof b.narration === "string") beat.narration = b.narration.slice(0, 400);
      if (typeof b.on_screen_text === "string") beat.on_screen_text = b.on_screen_text.slice(0, 120);
    });
  }
  if ((Array.isArray(edits.hooks) || typeof edits.chosen_hook === "number") && d.beats?.[0]?.role === "hook") {
    d.beats[0].narration = d.hooks?.[d.chosen_hook ?? 0] ?? d.beats[0].narration;
  }
  for (const k of ["caption", "body", "title"] as const) if (typeof edits[k] === "string") d[k] = edits[k] as string;
  if (Array.isArray(edits.hashtags)) d.hashtags = (edits.hashtags as string[]).map((h) => String(h).replace(/^#/, "")).slice(0, 10);
  if (edits.render_options && typeof edits.render_options === "object") {
    const ro = { ...(d.render_options ?? {}) } as Record<string, unknown>;
    for (const [k, v] of Object.entries(edits.render_options as Record<string, unknown>)) {
      if (!["voice_id", "caption_style", "caption_position", "captions_enabled", "watermark", "beat_assets"].includes(k)) http(422, `unknown render option ${k}`);
      ro[k] = k === "watermark" && typeof v === "string" ? v.slice(0, 24) : v;
    }
    d.render_options = ro;
  }
  // Resolve beat asset overrides for the view.
  const map = (d.render_options?.beat_assets ?? {}) as Record<string, string>;
  d.beats?.forEach((b, i) => {
    const a = assets.find((x) => x.id === map[String(i)]);
    b.asset = a ? { id: a.id, kind: a.kind, url: a.url ?? null, rights_source: a.rights_source ?? null } : null;
  });
  const changed = JSON.stringify([d.hooks, d.chosen_hook, d.beats, d.caption, d.body, d.render_options]) !== before;
  if (changed) {
    d.status = "draft";
    if (d.render?.state !== "rendering") d.render = { state: "idle" };
    d.gate = null;
  }
  d.lint = draftLint(d);
  return view(d);
}

export function renderDraft(id: string) {
  const d = getDraft(id);
  if (d.kind !== "short") http(422, "글 콘텐츠는 렌더가 필요 없습니다");
  const est = draftLint(d).duration?.estimated_seconds ?? 0;
  if (est > SHORT_MAX_SECONDS) {
    http(422, `대본이 너무 깁니다: 예상 ${est.toFixed(1)}초 (쇼츠는 ${SHORT_MAX_SECONDS}초 이내, 빠른 속도 기준) — 문장을 줄여 주세요`);
  }
  if (d.render?.state !== "rendering") {
    d.render = { state: "rendering" };
    d._renderAt = Date.now();
  }
  return { state: "rendering" };
}

export function publishDraft(db: MockDb, id: string, targets: string[] | undefined): PublishIn {
  const d = getDraft(id);
  if (d.kind === "short" && d.render?.state !== "done") {
    d.render = { state: "rendering" };
    d._renderAt = Date.now() - 7001; // "renders first if needed"
    view(d);
  }
  const lint = draftLint(d);
  const targetAccounts = (targets?.length ? targets : d.account ? [String(d.account.id)] : [])
    .map((t) => db.accounts.find((a) => a.id === t))
    .filter((a): a is NonNullable<typeof a> => !!a);
  let result: PublishIn;
  if (!lint.ok) {
    result = {
      verdict: "FIX",
      actions: ["regenerate"],
      findings: (lint.issues ?? []).filter((i) => i.severity === "error").map((i) => ({ rule_id: i.kind === "price" ? "C-03" : i.kind === "efficacy" ? "C-01" : "C-02", verdict: "FIX", message: `${i.message}: “${i.sentence}”`, fixed: false })),
      publications: [],
    };
  } else if (targetAccounts.some((a) => a.id === "acc_car_thr" || a.id === "acc_car_ig")) {
    result = {
      verdict: "HOLD",
      actions: ["human_review"],
      findings: [
        { rule_id: "D-01", verdict: "FIX", message: "대가성 문구 첫 줄 확인", fixed: true },
        { rule_id: "H-01", verdict: "HOLD", message: "신규 계정 첫 14일 — 사람 승인 필요", fixed: false },
      ],
      publications: [],
    };
    d.status = "gated";
  } else {
    const now = Date.now();
    result = {
      verdict: "PASS",
      actions: [],
      findings: [{ rule_id: "D-01", verdict: "FIX", message: "대가성 문구 첫 줄 확인", fixed: true }],
      publications: targetAccounts.map((a, i) => ({
        id: `pub_std_${uuid(rnd).slice(0, 6)}`,
        platform: a.platform,
        state: a.platform === "kakao" ? "MANUAL_PENDING" : "QUEUED",
        scheduled_at: iso(now + (18 + i * 23) * MIN),
      })),
    };
    d.status = "approved";
    d.publications = result.publications;
  }
  d.gate = { verdict: result.verdict, stage: "gate", decided_at: new Date().toISOString(), findings: result.findings };
  return result;
}

export function autoStudio(db: MockDb, body: { product_id: number; kind?: "short" | "text"; persona?: string; targets?: Array<{ account_id: string }> }): PublishIn {
  const targets = body.targets?.map((t) => t.account_id);
  const d = createDraft(db, { product_id: body.product_id, kind: body.kind, persona: body.persona, account_id: targets?.[0] });
  const res = publishDraft(db, String(d.id), targets);
  return { ...res, content_id: d.id };
}

/* ------------------------------------------------------------------ assets */

const assets: AssetIn[] = [];

function seedAssets() {
  if (assets.length) return;
  const now = Date.now();
  const sample = allProducts().slice(0, 6);
  sample.forEach((p, i) => {
    assets.push({
      id: `ast_img_${p.id}`,
      kind: "image",
      rights_source: "coupang_product_image",
      license: "쿠팡 파트너스 상품 이미지 (사용 범위 내)",
      product_id: p.id,
      tags: ["product"],
      url: p.image_url,
      created_at: iso(now - (i + 2) * 3_600_000),
      expires_at: null,
    });
  });
  assets.push(
    { id: "ast_vid_kitchen_broll", kind: "video", rights_source: "own_footage", license: "직접 촬영", product_id: null, tags: ["studio", "b-roll"], url: null, created_at: iso(now - 26 * 3_600_000), expires_at: null },
    { id: "ast_vid_stock_sink", kind: "video", rights_source: "licensed_stock", license: "상업 라이선스 스톡 (2027-06 만료)", product_id: null, tags: ["stock"], url: null, created_at: iso(now - 50 * 3_600_000), expires_at: "2027-06-30" },
  );
}

export function listAssets(productId: number | null): { items: AssetIn[] } {
  seedAssets();
  return { items: assets.filter((a) => productId === null || a.product_id === null || a.product_id === undefined || a.product_id === productId) };
}

export function allAssets(): AssetIn[] {
  seedAssets();
  return assets;
}

export function addUploaded(file: { name: string; type: string; url: string | null }, productId: number | null): AssetIn {
  seedAssets();
  const a: AssetIn = {
    id: `ast_up_${uuid(rnd).slice(0, 8)}`,
    kind: file.type.startsWith("video/") ? "video" : "image",
    rights_source: "own_footage",
    license: "직접 촬영 (업로드 시 권리 보유 확인)",
    product_id: productId,
    tags: ["studio", file.name],
    url: file.url,
    created_at: new Date().toISOString(),
    expires_at: null,
  };
  assets.unshift(a);
  return a;
}

export function assetRights(id: string): string | null {
  seedAssets();
  return assets.find((a) => a.id === id)?.rights_source ?? null;
}

export function deriveAsset(id: string, op: "cut" | string, extra: Record<string, unknown>): AssetIn {
  seedAssets();
  const src = assets.find((a) => a.id === id);
  if (!src) http(404, "asset not found");
  if (op === "cut" && src.kind !== "video") http(422, "영상만 자를 수 있습니다");
  if (op.startsWith("i2v") && src.kind !== "image") http(422, "이미지만 영상으로 변환할 수 있습니다");
  const a: AssetIn = {
    ...src,
    id: `ast_${op}_${uuid(rnd).slice(0, 6)}`,
    kind: "video",
    tags: [...(src.tags ?? []), op, ...Object.entries(extra).map(([k, v]) => `${k}=${String(v)}`)],
    created_at: new Date().toISOString(),
  };
  assets.unshift(a);
  return a;
}

export function transcribeAsset(id: string) {
  seedAssets();
  const a = assets.find((x) => x.id === id);
  if (!a) http(404, "asset not found");
  if (a.rights_source !== "own_footage" || a.kind !== "video") {
    http(403, "대본 추출은 권리 원장에 '직접 촬영'으로 등록된 영상에만 사용할 수 있습니다");
  }
  const text = "안녕하세요. 오늘은 주방에서 매일 쓰는 밀봉 클립을 써봤어요. 과자 봉지가 눅눅해지지 않아서 좋았어요.";
  return { asset_id: a.id, text, language: "ko", engine: "mock-stt", words: text.split(" ").map((w, i) => [w, i * 0.4, i * 0.4 + 0.35]) };
}

/* ------------------------------------------------------------------ binary previews */

/** 16-bit PCM WAV with a short two-tone "voice" so ▶ preview plays something in mock mode. */
export function voiceWav(voiceId: string): Blob {
  const v = VOICES.find((x) => x.id === voiceId);
  if (!v) http(404, "unknown voice");
  const rate = 16000;
  const seconds = 1.2;
  const n = Math.floor(rate * seconds);
  const buf = new ArrayBuffer(44 + n * 2);
  const dv = new DataView(buf);
  const w = (o: number, s: string) => [...s].forEach((c, i) => dv.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF");
  dv.setUint32(4, 36 + n * 2, true);
  w(8, "WAVE");
  w(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true);
  dv.setUint32(24, rate, true);
  dv.setUint32(28, rate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  w(36, "data");
  dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    const env = Math.min(1, t * 12) * Math.min(1, (seconds - t) * 8);
    const syll = 0.55 + 0.45 * Math.sin(2 * Math.PI * 6.5 * v.rate * t); // fast syllable cadence
    const s = env * syll * (0.6 * Math.sin(2 * Math.PI * v.f0 * t) + 0.25 * Math.sin(2 * Math.PI * v.f0 * 2 * t));
    dv.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s * 0.5)) * 32767, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

const STYLE_SVG: Record<string, (t: string) => string> = {
  classic: (t) => `<text x="80" y="62" text-anchor="middle" font-size="20" font-weight="800" fill="#fff" stroke="#000" stroke-width="4" paint-order="stroke">${t}</text>`,
  box: (t) => `<rect x="18" y="40" width="124" height="32" rx="6" fill="#000" fill-opacity="0.65"/><text x="80" y="62" text-anchor="middle" font-size="18" font-weight="700" fill="#fff">${t}</text>`,
  highlight: (t) => `<rect x="22" y="42" width="116" height="28" fill="#ffe14d"/><text x="80" y="62" text-anchor="middle" font-size="18" font-weight="800" fill="#111">${t}</text>`,
  bold: (t) => `<text x="80" y="64" text-anchor="middle" font-size="22" font-weight="900" fill="#fff" stroke="#111" stroke-width="7" paint-order="stroke">${t}</text>`,
  minimal: (t) => `<text x="80" y="62" text-anchor="middle" font-size="15" font-weight="500" fill="#fff">${t}</text>`,
  pop: (t) => `<text x="80" y="62" text-anchor="middle" font-size="20" font-weight="900" fill="#fff" stroke="#000" stroke-width="4" paint-order="stroke">${t.slice(0, 2)}<tspan fill="#ffd400">${t.slice(2)}</tspan></text>`,
};

export function styleTile(id: string): Blob {
  const f = STYLE_SVG[id];
  if (!f) http(404, "unknown caption style");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 100" width="320" height="200" font-family="Pretendard, 'Malgun Gothic', sans-serif"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3d4a5c"/><stop offset="1" stop-color="#8a6f5a"/></linearGradient></defs><rect width="160" height="100" fill="url(#g)"/>${f("자막 예시")}</svg>`;
  return new Blob([svg], { type: "image/svg+xml" });
}

export { STUDIO_CATEGORY };
