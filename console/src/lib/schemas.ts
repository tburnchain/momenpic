/**
 * zod schemas mirroring the ACVM HTTP API contract (/api/v1).
 *
 * Conventions
 * - Times are ISO-8601 UTC strings; the UI converts to KST for display (see format.ts).
 * - Money is KRW. Integers are expected, but floats are tolerated (DB column is `numeric`)
 *   and rounded at display time.
 * - Closed vocabularies that the backend owns (run status, verdict, role, platform, kind,
 *   account status) are strict enums. Publication state is an open string because the
 *   backend may add saga/manual states (e.g. MANUAL_PENDING, TAKEN_DOWN); the UI labels the
 *   known ones and shows unknown ones verbatim.
 * - Optional/nullable fields use `.nullish()` so a `null` or a missing key both validate.
 * - Unknown extra keys are stripped (zod default), so additive backend changes never break the UI.
 * - Where the shipped backend (src/acvm/api) names a field differently from the original console
 *   contract, a small `aliases()` pre-processor maps it onto the contract name, so both shapes
 *   validate (e.g. `arm_key`→`key`, `ctr_90`→`ctr_ci`, `publications_by_state`→`publication_states`).
 *   `*Shape` exports are the canonical object schemas (used to type mock fixtures).
 */
import { z } from "zod";

/* ------------------------------------------------------------------ helpers */

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);

/** Copies `from` → `to` when the canonical key is absent. */
function aliases(map: Record<string, string>, extra?: (o: Obj) => void) {
  return (v: unknown) => {
    if (!isObj(v)) return v;
    const o: Obj = { ...v };
    for (const [from, to] of Object.entries(map)) {
      if (o[to] === undefined && o[from] !== undefined) o[to] = o[from];
    }
    extra?.(o);
    return o;
  };
}

/* ------------------------------------------------------------------ primitives */

export const IsoTime = z.string().min(1);
export const Krw = z.number();
const Id = z.union([z.string(), z.number()]).transform((v) => String(v));
const NumOrNull = z.number().nullable();

export const Role = z.enum(["owner", "operator", "reviewer", "viewer"]);
export type Role = z.infer<typeof Role>;

export const PLATFORMS = ["threads", "instagram", "youtube", "tiktok", "facebook", "kakao"] as const;
export const Platform = z.enum(PLATFORMS);
export type Platform = z.infer<typeof Platform>;

export const VERDICTS = ["PASS", "FIX", "HOLD", "BLOCK"] as const;
export const Verdict = z.enum(VERDICTS);
export type Verdict = z.infer<typeof Verdict>;

export const RUN_STATUSES = [
  "planned",
  "awaiting_approval",
  "approved",
  "running",
  "paused_budget",
  "completed",
  "failed",
  "rejected",
] as const;
export const RunStatus = z.enum(RUN_STATUSES);
export type RunStatus = z.infer<typeof RunStatus>;

export const ContentKind = z.enum(["text", "short"]);
export type ContentKind = z.infer<typeof ContentKind>;

export const ACCOUNT_STATUSES = ["active", "cooldown", "restricted"] as const;
export const AccountStatus = z.enum(ACCOUNT_STATUSES);
export type AccountStatus = z.infer<typeof AccountStatus>;

/** Known publication states (M8 state machine + manual/saga states). Open-ended on the wire. */
export const PUBLICATION_STATES = [
  "QUEUED",
  "UPLOADING",
  "PROCESSING",
  "PUBLISHED",
  "FAILED",
  "EXPIRED",
  "MANUAL_PENDING",
  "CANCELLED",
  "TAKEN_DOWN",
] as const;
export const PublicationState = z.string().min(1);

const Range = z.tuple([z.number(), z.number()]);
/** CI bounds may be null when the backend cannot estimate them (e.g. too few samples). */
const LooseRange = z.tuple([NumOrNull, NumOrNull]);

/* ------------------------------------------------------------------ principal */

export const Me = z.object({
  subject: z.string(),
  email: z.string().nullish(),
  name: z.string().nullish(),
  role: Role,
  workspace_id: z.string().nullish(),
  user_id: z.string().nullish(),
  /** dev | session | service | cloudflare_access */
  auth: z.string().nullish(),
});
export type Me = z.infer<typeof Me>;

/* ------------------------------------------------------------------ members & login (account.py) */

export const AuthConfig = z.object({
  mode: z.string(),
  signup: z.enum(["approval", "open", "closed"]).catch("approval"),
  password_min: z.number().default(8),
  password_rule: z.string().default(""),
});
export type AuthConfig = z.infer<typeof AuthConfig>;

export const SignupResult = z.object({ status: z.string(), message: z.string(), email: z.string() });
export type SignupResult = z.infer<typeof SignupResult>;

export const MEMBER_STATUSES = ["pending", "active", "disabled"] as const;
export const Member = z.object({
  id: Id,
  email: z.string(),
  name: z.string().default(""),
  role: Role,
  status: z.enum(MEMBER_STATUSES).catch("active"),
  workspace_id: z.string().nullish(),
  created_at: z.string(),
  last_login_at: z.string().nullish(),
  has_password: z.boolean().default(true),
});
export type Member = z.infer<typeof Member>;
export const MemberList = z.object({ items: z.array(Member).default([]), pending: z.number().default(0) });
export type MemberList = z.infer<typeof MemberList>;
export const OkResponse = z.object({ ok: z.boolean() });

/* ------------------------------------------------------------------ plan / commands */

export const ApprovalMode = z.enum(["auto", "console"]);
export const Approval = z.object({
  mode: ApprovalMode,
  reasons: z.array(z.string()).default([]),
});
export type Approval = z.infer<typeof Approval>;

export const Slots = z.object({
  intent: z.string().nullish(),
  category: z.string().nullish(),
  count: z.number().nullish(),
  kind: z.string().nullish(),
  persona: z.string().nullish(),
  platforms: z.array(z.string()).default([]),
  schedule: z.string().nullish(),
  budget_krw: Krw.nullish(),
});
export type Slots = z.infer<typeof Slots>;

export const PlanTarget = z.object({
  platform: Platform,
  account_id: Id,
  account_handle: z.string(),
  link_placement: z.string(),
  format: z.string(),
});
export type PlanTarget = z.infer<typeof PlanTarget>;

export const PlanStep = z.object({
  id: z.string(),
  module: z.string(),
  action: z.string(),
  params: z.record(z.string(), z.unknown()).default({}),
  depends_on: z.array(z.string()).default([]),
});
export type PlanStep = z.infer<typeof PlanStep>;

export const Plan = z.object({
  run_id: Id,
  intent: z.string(),
  slots: Slots,
  category_id: z.string().nullish(),
  niche: z.string().nullish(),
  count: z.number(),
  min_pos: z.number().nullish(),
  kind: ContentKind,
  persona: z.string().nullish(),
  /** Appendix A uses `hooks=3` (number of hook variants); a list of hook types is tolerated. */
  hooks: z.union([z.number(), z.array(z.string())]).nullish(),
  length_sec: z.number().nullish(),
  template_id: z.string().nullish(),
  voice_id: z.string().nullish(),
  rule_set: z.string().nullish(),
  targets: z.array(PlanTarget).default([]),
  schedule_window: Range.nullish(),
  jitter_minutes: z.number().nullish(),
  recurring_cron: z.string().nullish(),
  est_cost_krw: Krw,
  budget_cap_krw: Krw.nullish(),
  approval: Approval,
  steps: z.array(PlanStep).default([]),
  explanation: z.string().nullish(),
});
export type Plan = z.infer<typeof Plan>;

export const CommandResponse = z.object({
  run_id: Id,
  status: RunStatus,
  /** null when planning failed before a plan existed (the run is then `failed`). */
  plan: Plan.nullish(),
  approval: Approval.nullish(),
  explanation: z.string().nullish(),
});
export type CommandResponse = z.infer<typeof CommandResponse>;

/* ------------------------------------------------------------------ runs */

export const RunSummary = z.object({
  id: Id,
  raw_command: z.string(),
  status: RunStatus,
  est_cost_krw: Krw.nullish(),
  cost_krw: Krw.nullish(),
  started_at: IsoTime.nullish(),
  ended_at: IsoTime.nullish(),
  /** Contract: a sentence. Shipped backend: a structured dict ({selected, verdicts, …}). Both render. */
  summary: z.union([z.string(), z.record(z.string(), z.unknown())]).nullish(),
});
export type RunSummary = z.infer<typeof RunSummary>;

export const RunList = z.object({ items: z.array(RunSummary) });
export type RunList = z.infer<typeof RunList>;

export const Finding = z.object({
  rule_id: z.string(),
  verdict: Verdict,
  message: z.string(),
  fixed: z.boolean().default(false),
  details: z.unknown().optional(),
});
export type Finding = z.infer<typeof Finding>;

export const Publication = z.object({
  id: Id,
  platform: Platform,
  account_handle: z.string(),
  state: PublicationState,
  scheduled_at: IsoTime.nullish(),
  published_at: IsoTime.nullish(),
  permalink: z.string().nullish(),
  sub_id: z.string().nullish(),
});
export type Publication = z.infer<typeof Publication>;

export const ContentProduct = z.object({
  id: Id,
  name: z.string(),
  price: z.number().nullish(),
  image_url: z.string().nullish(),
  category_id: z.string().nullish(),
});

export const Content = z.object({
  id: Id,
  product: ContentProduct.nullish(),
  kind: ContentKind,
  status: z.string(),
  hook: z.string().nullish(),
  persona: z.string().nullish(),
  template_id: z.string().nullish(),
  verdict: Verdict.nullish(),
  findings: z.array(Finding).default([]),
  media_url: z.string().nullish(),
  thumbnail_url: z.string().nullish(),
  publications: z.array(Publication).default([]),
});
export type Content = z.infer<typeof Content>;

export const RunDetail = z.object({
  run: RunSummary.extend({ plan: Plan.nullish() }),
  contents: z.array(Content).default([]),
  cost: z.object({ spent_krw: Krw, cap_krw: Krw.nullish() }),
});
export type RunDetail = z.infer<typeof RunDetail>;

/* ------------------------------------------------------------------ HOLD queue */

export const HoldItem = z.object({
  id: Id,
  content_id: Id,
  run_id: Id.nullish(),
  created_at: IsoTime,
  reasons: z.array(z.string()).default([]),
  product: z
    .object({
      name: z.string(),
      price: z.number().nullish(),
      image_url: z.string().nullish(),
    })
    .nullish(),
  kind: ContentKind,
  hook: z.string().nullish(),
  script_text: z.string().nullish(),
  caption_preview: z.string().nullish(),
  media_url: z.string().nullish(),
  thumbnail_url: z.string().nullish(),
  findings: z.array(Finding).default([]),
});
export type HoldItem = z.infer<typeof HoldItem>;

export const HoldList = z.object({ items: z.array(HoldItem) });
export type HoldList = z.infer<typeof HoldList>;

export const DecisionResponse = z.object({ status: z.string() });

/* ------------------------------------------------------------------ dashboards */

export const GateVerdicts = z.object({
  PASS: z.number().default(0),
  FIX: z.number().default(0),
  HOLD: z.number().default(0),
  BLOCK: z.number().default(0),
});
export type GateVerdicts = z.infer<typeof GateVerdicts>;

export const MoneyDay = z.object({
  date: z.string(),
  gmv_krw: Krw,
  commission_krw: Krw,
  cancelled_krw: Krw,
  cost_krw: Krw,
  net_krw: Krw,
});
export type MoneyDay = z.infer<typeof MoneyDay>;

export const ChannelStatShape = z.object({
  platform: Platform,
  account: z.string(),
  n: z.number(),
  median_views: z.number().nullish(),
  median_ci: LooseRange.nullish(),
  breakeven_views: z.number().nullish(),
  ratio: z.number().nullish(),
  admitted: z.boolean(),
  reason: z.string().nullish(),
});
export const ChannelStat = z.preprocess(aliases({ handle: "account" }), ChannelStatShape);
export type ChannelStat = z.infer<typeof ChannelStatShape>;

export const MoneyDashboardShape = z.object({
  daily: z.array(MoneyDay),
  totals: z.object({
    gmv_krw: Krw,
    commission_krw: Krw,
    cancelled_krw: Krw,
    cost_krw: Krw,
    net_krw: Krw,
    /** Confirmed commission ÷ GMV as a fraction (0.03 = 3 %). */
    effective_rate: z.number().nullish(),
  }),
  channels: z.array(ChannelStat).default([]),
});
export const MoneyDashboard = MoneyDashboardShape;
export type MoneyDashboard = z.infer<typeof MoneyDashboardShape>;

const Counts = z.record(z.string(), z.number());

export const PipelineDashboardShape = z.object({
  publication_states: Counts,
  runs_by_status: Counts,
  gate_verdicts: GateVerdicts,
  render: z.object({
    count: z.number(),
    p50_s: z.number().nullish(),
    p95_s: z.number().nullish(),
    p90_s: z.number().nullish(),
    /** render_time = wall-clock render duration; video_length = rendered video duration. */
    metric: z.enum(["render_time", "video_length"]).default("render_time"),
  }),
  queues: Counts,
  contents_by_status: Counts.optional(),
  publish_failure_rate: z.number().nullish(),
});
export const PipelineDashboard = z.preprocess(
  aliases({ publications_by_state: "publication_states" }, (o) => {
    const r = o.render_video_seconds;
    if (o.render === undefined && isObj(r)) {
      o.render = { count: r.count ?? 0, p50_s: r.p50 ?? null, p90_s: r.p90 ?? null, p95_s: null, metric: "video_length" };
    }
  }),
  PipelineDashboardShape,
);
export type PipelineDashboard = z.infer<typeof PipelineDashboardShape>;

export const RiskDashboardShape = z.object({
  accounts: z.array(
    z.preprocess(
      aliases({ account_id: "id" }),
      z.object({
        id: Id,
        platform: Platform,
        handle: z.string(),
        status: AccountStatus,
        cooldown_until: IsoTime.nullish(),
        niche: z.string().nullish(),
      }),
    ),
  ),
  gate_verdicts: GateVerdicts,
  invalid_click_alerts: z
    .array(
      z.object({
        at: IsoTime.nullish(),
        sub_id: z.string(),
        reason: z.string().nullish(),
        score: z.number().nullish(),
      }),
    )
    .default([]),
  warnings: z
    .array(
      z.preprocess(
        aliases({ handle: "account" }),
        z.object({ at: IsoTime.nullish(), account: z.string(), detail: z.string(), platform: z.string().nullish() }),
      ),
    )
    .default([]),
});
export const RiskDashboard = RiskDashboardShape;
export type RiskDashboard = z.infer<typeof RiskDashboardShape>;

export const ArmShape = z.object({
  key: z.string(),
  category: z.string().nullish(),
  hook_type: z.string().nullish(),
  template: z.string().nullish(),
  persona: z.string().nullish(),
  platform: z.string().nullish(),
  slot: z.union([z.string(), z.number()]).nullish(),
  trials: z.number(),
  ctr_mean: z.number().nullish(),
  ctr_ci: LooseRange.nullish(),
  /** Credible-interval level of ctr_ci (backend ships a 90 % interval as `ctr_90`). */
  ci_level: z.number().default(0.95),
  epc_mean: z.number().nullish(),
  rpi_mean: z.number().nullish(),
  retired: z.boolean().default(false),
});
export const Arm = z.preprocess(
  aliases({ arm_key: "key" }, (o) => {
    if (o.ctr_ci === undefined && o.ctr_90 !== undefined) {
      o.ctr_ci = o.ctr_90;
      o.ci_level = 0.9;
    }
  }),
  ArmShape,
);
export type Arm = z.infer<typeof ArmShape>;

export const Admission = z.object({
  admitted: z.boolean(),
  n: z.number().nullish(),
  median_views: z.number().nullish(),
  breakeven_views: z.number().nullish(),
  ratio: z.number().nullish(),
  reason: z.string().nullish(),
});
export type Admission = z.infer<typeof Admission>;

export const LearningDashboardShape = z.object({
  arms: z.array(Arm),
  explore_exploit: z.object({ explore: z.number(), exploit: z.number() }),
  tail_contents: z
    .array(
      z.object({
        content_id: Id,
        platform: z.string(),
        views: z.number().nullish(),
        /** Percentile rank; 0–1 or 0–100 are both accepted (normalised in the UI). */
        percentile: z.number().nullish(),
        hook: z.string().nullish(),
      }),
    )
    .default([]),
  admission: z.record(z.string(), Admission).default({}),
  playbook: z
    .array(z.object({ text: z.string(), confidence: z.number().nullish(), lift: z.number().nullish() }))
    .default([]),
});
export const LearningDashboard = z.preprocess(
  aliases({ allocation: "explore_exploit" }),
  LearningDashboardShape,
);
export type LearningDashboard = z.infer<typeof LearningDashboardShape>;

/* ------------------------------------------------------------------ accounts / schedules / manual */

export const Account = z.object({
  id: Id,
  platform: Platform,
  handle: z.string(),
  niche: z.string().nullish(),
  status: AccountStatus,
  daily_cap: z.number(),
  cooldown_until: IsoTime.nullish(),
  created_at: IsoTime.nullish(),
});
export type Account = z.infer<typeof Account>;
export const AccountList = z.object({ items: z.array(Account) });

/** Backend limit (AccountIn / AccountPatch): 1 ≤ daily_cap ≤ 50 per account per day. */
export const DAILY_CAP_MAX = 50;

export const AccountCreate = z.object({
  platform: Platform,
  handle: z
    .string()
    .trim()
    .min(2, "핸들을 입력하세요")
    .max(64)
    .regex(/^@?[A-Za-z0-9._\-가-힣 ]+$/, "영문·숫자·한글·._- 만 사용할 수 있습니다"),
  niche: z.string().trim().min(1, "니치를 입력하세요").max(40),
  token_ref: z
    .string()
    .trim()
    .min(3, "Vault 경로를 입력하세요")
    .regex(/^[A-Za-z0-9/_\-.:]+$/, "토큰 원문이 아닌 Vault 경로(예: kv/acvm/threads/kitchen_pick)만 입력하세요"),
  daily_cap: z.number().int().min(1, "1 이상").max(DAILY_CAP_MAX, `${DAILY_CAP_MAX} 이하`),
  persona_default: z.string().trim().max(40).optional(),
});
export type AccountCreate = z.infer<typeof AccountCreate>;

export const AccountPatch = z.object({
  status: AccountStatus.optional(),
  daily_cap: z.number().int().min(1).max(DAILY_CAP_MAX).optional(),
});
export type AccountPatch = z.infer<typeof AccountPatch>;

export const Schedule = z.object({
  id: Id,
  raw_command: z.string(),
  cron: z.string(),
  active: z.boolean(),
  created_by: z.string().nullish(),
});
export type Schedule = z.infer<typeof Schedule>;
export const ScheduleList = z.object({ items: z.array(Schedule) });

export const ManualItem = z.object({
  publication_id: Id,
  platform: Platform,
  account_handle: z.string(),
  caption: z.string(),
  media_url: z.string().nullish(),
  link_url: z.string().nullish(),
  content_id: Id.nullish(),
  scheduled_at: IsoTime.nullish(),
});
export type ManualItem = z.infer<typeof ManualItem>;
export const ManualQueue = z.object({ items: z.array(ManualItem) });

/* ------------------------------------------------------------------ rules / audit */

export const Rule = z.object({
  id: z.string(),
  check: z.string(),
  method: z.string(),
  on_fail: z.string(),
});
export type Rule = z.infer<typeof Rule>;

export const RuleSet = z.object({
  version: z.string(),
  rules: z.array(Rule),
  disclosure: z.object({ text: z.string(), video_text: z.string() }),
});
export type RuleSet = z.infer<typeof RuleSet>;

export const AuditEntry = z.object({
  at: IsoTime,
  actor: z.string(),
  action: z.string(),
  target: z.string().nullish(),
  payload: z.unknown().optional(),
});
export type AuditEntry = z.infer<typeof AuditEntry>;
export const AuditList = z.object({ items: z.array(AuditEntry) });

/* ------------------------------------------------------------------ quant */

export const Breakeven = z.object({
  /** null when revenue per impression is 0 (I* = ∞ cannot be encoded in JSON). */
  breakeven_views: z.number().nullable(),
  revenue_per_impression: z.number(),
  epc: z.number(),
  commission_per_order: z.number(),
});
export type Breakeven = z.infer<typeof Breakeven>;

export const Simulation = z.object({
  mean_views: z.number(),
  p_breakeven: z.number(),
  expected_profit_per_content: z.number(),
  tail_share_top1: z.number(),
  tail_share_top5: z.number(),
  prob_tail_hit: z.object({ q: z.number(), n: z.number(), p: z.number() }),
  trials_for_95: z.number().nullable(),
});
export type Simulation = z.infer<typeof Simulation>;

/* ------------------------------------------------------------------ operations (ops.py) */

export const CheckStatus = z.enum(["ok", "warn", "fail"]);
export type CheckStatus = z.infer<typeof CheckStatus>;

export const CREDENTIAL_STATES = ["configured", "missing", "not_required"] as const;

export const SystemStatus = z.object({
  mode: z.object({
    env: z.string().nullish(),
    orchestrator: z.string().nullish(),
    coupang_sandbox: z.boolean().nullish(),
    platform_dry_run: z.boolean().nullish(),
    llm_backend: z.string().nullish(),
    auth_mode: z.string().nullish(),
    tiktok_audited: z.boolean().nullish(),
  }),
  checks: z.array(
    z.object({
      id: z.string(),
      label: z.string(),
      status: CheckStatus,
      detail: z.string().default(""),
      fix: z.string().default(""),
    }),
  ),
  summary: z.object({ ok: z.number().default(0), warn: z.number().default(0), fail: z.number().default(0) }),
  g0: z.object({
    passed: z.boolean(),
    items: z.array(z.object({ id: z.string(), label: z.string(), done: z.boolean() })),
  }),
  budget: z.object({ today_spent_krw: Krw, daily_budget_krw: Krw }),
  accounts: z
    .array(
      z.object({
        id: Id,
        platform: Platform,
        handle: z.string(),
        niche: z.string().nullish(),
        status: AccountStatus,
        credential: z.enum(CREDENTIAL_STATES),
        used_24h: z.number().default(0),
        daily_cap: z.number(),
      }),
    )
    .default([]),
  breakers: z
    .array(
      z.object({
        name: z.string(),
        state: z.string().nullish(),
        opened_at: z.union([z.string(), z.number()]).nullish(),
        failures: z.number().nullish(),
      }),
    )
    .default([]),
  secrets: z.array(z.object({ name: z.string(), label: z.string(), configured: z.boolean() })).default([]),
});
export type SystemStatus = z.infer<typeof SystemStatus>;

export const LEGAL_CHECKLIST_ITEMS = ["partners_policy", "ad_law", "ai_act", "platform_terms", "tax_entity"] as const;

export const ChecklistResponse = z.object({ item: z.string(), done: z.boolean() });
/* ------------------------------------------------------------------ AI integrations (integrations.py) */

export const INTEGRATION_STATES = ["missing", "unverified", "active", "degraded", "error", "paused"] as const;
export type IntegrationState = (typeof INTEGRATION_STATES)[number];

export const Integration = z.object({
  name: z.string(),
  label: z.string(),
  state: z.enum(INTEGRATION_STATES).catch("missing"),
  detail: z.string().default(""),
  enabled: z.boolean().default(true),
  configured: z.boolean().default(false),
  active: z.boolean().default(false),
  checked_at: z.string().nullish(),
  slots: z.array(z.object({ name: z.string(), label: z.string(), configured: z.boolean() })).default([]),
  automates: z.array(z.string()).default([]),
  guardrails: z.array(z.string()).default([]),
  model: z.string().default(""),
  unit_cost: z.string().default(""),
  key_url: z.string().nullish(),
  voices: z.record(z.string(), z.object({ voice_id: z.string(), name: z.string().default("") })).nullish(),
  actions: z.array(z.string()).nullish(),
});
export type Integration = z.infer<typeof Integration>;

export const IntegrationList = z.object({
  items: z.array(Integration).default([]),
  llm: z.object({ provider: z.string() }).nullish(),
});
export type IntegrationList = z.infer<typeof IntegrationList>;

/** Secret slots owned by the AI integrations card (the generic secrets card hides them). */
export const INTEGRATION_SLOTS = new Set([
  "anthropic_api_key",
  "gemini_api_key",
  "openai_api_key",
  "higgsfield_key_id",
  "higgsfield_key_secret",
  "elevenlabs_api_key",
]);

export const SecretPutResponse = z.object({
  name: z.string(),
  configured: z.boolean(),
  persistent: z.boolean(),
  integration: Integration.nullish(),
});
export type SecretPutResponse = z.infer<typeof SecretPutResponse>;
export const SecretDeleteResponse = z.object({
  name: z.string(),
  configured: z.boolean(),
  removed: z.boolean(),
  integration: Integration.nullish(),
});
export const VoiceRemapResponse = z.object({
  mapped: z.number(),
  voices: z.record(z.string(), z.object({ voice_id: z.string(), name: z.string().default("") })).default({}),
  integration: Integration,
});
export const CredentialsPutResponse = z.object({
  account_id: Id,
  configured: z.boolean(),
  persistent: z.boolean(),
});
export type CredentialsPutResponse = z.infer<typeof CredentialsPutResponse>;

export const Workspace = z.object({
  id: Id,
  name: z.string(),
  plan: z.string().nullish(),
  daily_budget_krw: Krw,
  today_spent_krw: Krw,
  last30d_spent_krw: Krw,
});
export type Workspace = z.infer<typeof Workspace>;

export const PublicationItem = z.object({
  id: Id,
  platform: Platform,
  state: PublicationState,
  account_handle: z.string(),
  account_id: Id.nullish(),
  content_id: Id,
  kind: ContentKind.nullish(),
  hook: z.string().nullish(),
  product: z
    .object({ id: Id, name: z.string(), price: z.number().nullish(), image_url: z.string().nullish() })
    .nullish(),
  scheduled_at: IsoTime.nullish(),
  published_at: IsoTime.nullish(),
  permalink: z.string().nullish(),
  sub_id: z.string().nullish(),
  link_placement: z.string().nullish(),
  caption: z.string().nullish(),
  error: z.record(z.string(), z.unknown()).nullish(),
  attempts: z.number().default(0),
  metrics: z
    .object({
      views: z.number().nullish(),
      link_clicks: z.number().nullish(),
      likes: z.number().nullish(),
      shares: z.number().nullish(),
    })
    .default({}),
});
export type PublicationItem = z.infer<typeof PublicationItem>;
export const PublicationList = z.object({ items: z.array(PublicationItem) });
export type PublicationList = z.infer<typeof PublicationList>;

export const TakedownResponse = z.object({
  publication_id: Id,
  state: PublicationState,
  error: z.record(z.string(), z.unknown()).nullish(),
});
export type TakedownResponse = z.infer<typeof TakedownResponse>;

export const ScriptBeat = z.object({
  start_sec: z.number(),
  end_sec: z.number(),
  role: z.string(),
  narration: z.string(),
  on_screen_text: z.string().default(""),
  visual: z.string().default(""),
  claim_ids: z.array(z.string()).default([]),
});
export type ScriptBeat = z.infer<typeof ScriptBeat>;

export const ContentDossier = z.object({
  id: Id,
  kind: ContentKind,
  status: z.string(),
  persona: z.string().nullish(),
  template_id: z.string().nullish(),
  hook_variant: z.string().nullish(),
  similarity_max: z.number().nullish(),
  created_at: IsoTime.nullish(),
  run_id: Id.nullish(),
  parent_content_id: Id.nullish(),
  product: z
    .object({
      id: Id,
      name: z.string(),
      price: z.number().nullish(),
      image_url: z.string().nullish(),
      product_url: z.string().nullish(),
      category_name: z.string().nullish(),
      rating: z.number().nullish(),
      review_count: z.number().nullish(),
      is_rocket: z.boolean().nullish(),
    })
    .nullish(),
  script: z.object({
    hooks: z.array(z.string()).default([]),
    chosen_hook: z.number().default(0),
    hook_type: z.string().nullish(),
    beats: z.array(ScriptBeat).default([]),
    body: z.string().default(""),
    caption: z.string().default(""),
    hashtags: z.array(z.string()).default([]),
    cta: z.string().default(""),
    disclosure: z.string().default(""),
    title: z.string().default(""),
    duration_sec: z.number().nullish(),
    persona: z.string().nullish(),
    template_id: z.string().nullish(),
  }),
  claims: z
    .array(
      z.object({
        id: z.string(),
        text: z.string(),
        kind: z.string().nullish(),
        evidence: z.array(z.string()).default([]),
        grounded: z.boolean().default(false),
      }),
    )
    .default([]),
  render: z
    .object({
      duration_sec: z.number().nullish(),
      width: z.number().nullish(),
      height: z.number().nullish(),
      fps: z.number().nullish(),
      loudness_lufs: z.number().nullish(),
      disclosure_overlay: z.record(z.string(), z.unknown()).nullish(),
      voice_id: z.string().nullish(),
      voice_source: z.string().nullish(),
      template_id: z.string().nullish(),
      renderer: z.string().nullish(),
      media_url: z.string().nullish(),
      thumbnail_url: z.string().nullish(),
    })
    .nullish(),
  checks: z
    .array(
      z.object({
        id: Id,
        stage: z.string().nullish(),
        verdict: Verdict,
        rule_set_version: z.string().nullish(),
        iteration: z.number().nullish(),
        decided_at: IsoTime.nullish(),
        findings: z.array(Finding).default([]),
      }),
    )
    .default([]),
  review: z
    .object({
      id: Id,
      status: z.string(),
      reasons: z.array(z.string()).default([]),
      decided_by: z.string().nullish(),
      note: z.string().nullish(),
    })
    .nullish(),
  publications: z
    .array(
      z.object({
        id: Id,
        platform: Platform,
        state: PublicationState,
        scheduled_at: IsoTime.nullish(),
        caption: z.string().nullish(),
      }),
    )
    .default([]),
  cost_krw: z.number().nullish(),
});
export type ContentDossier = z.infer<typeof ContentDossier>;

export const AlertsFeed = z.object({
  items: z
    .array(
      z.object({
        at: IsoTime,
        severity: z.enum(["high", "medium"]),
        kind: z.string(),
        label: z.string(),
        target: z.string().nullish(),
        actor: z.string().nullish(),
        detail: z.unknown().optional(),
      }),
    )
    .default([]),
  accounts_attention: z
    .array(
      z.object({
        id: Id,
        platform: Platform,
        handle: z.string(),
        status: z.string(),
        until: IsoTime.nullish(),
      }),
    )
    .default([]),
  pending_holds: z.number().default(0),
  counts: z.object({ high: z.number().default(0), medium: z.number().default(0) }),
});
export type AlertsFeed = z.infer<typeof AlertsFeed>;

/** Responses whose body the console does not rely on (approve/reject/create/patch/delete). */
export const Ack = z.unknown();

/* ------------------------------------------------------------------ shorts studio (studio.py) */

export const CAPTION_POSITIONS = ["upper", "middle", "bottom"] as const;

export const StudioVoice = z.object({
  id: z.string(),
  name: z.string(),
  gender: z.string().nullish(),
  age_band: z.string().nullish(),
  tone: z.string().nullish(),
  description: z.string().nullish(),
  persona_tags: z.array(z.string()).default([]),
  sample_text: z.string().nullish(),
  /** Speed vs natural pace; every catalog voice is fast (≥ 1.2) — shorts are 20–25 s pitches. */
  rate: z.number().nullish(),
});
export type StudioVoice = z.infer<typeof StudioVoice>;

export const StudioOptions = z.object({
  voices: z.array(StudioVoice).default([]),
  caption_styles: z
    .array(z.object({ id: z.string(), label: z.string(), description: z.string().nullish() }))
    .default([]),
  caption_positions: z.array(z.string()).default(["bottom", "middle", "upper"]),
  tts: z
    .object({ provider: z.string().nullish(), real_voice: z.boolean().nullish(), note: z.string().nullish() })
    .default({}),
  /** Which AI integrations are active right now (keys registered in 운영 준비 › AI 연동). */
  integrations: z
    .object({
      anthropic: z.boolean(),
      gemini: z.boolean(),
      openai_image: z.boolean(),
      higgsfield: z.boolean(),
      elevenlabs: z.boolean(),
      llm: z.string(),
      ai_scenes: z.boolean(),
      ai_motion: z.boolean(),
      stt: z.boolean(),
      note: z.string(),
    })
    .partial()
    .nullish(),
  personas: z
    .array(z.object({ name: z.string(), voice_hint: z.string().nullish(), tone: z.string().nullish() }))
    .default([]),
  templates: z
    .array(z.object({ id: z.string(), kind: z.string(), description: z.string().nullish(), duration: z.number().nullish() }))
    .default([]),
  hook_types: z.array(z.string()).default([]),
  accounts: z
    .array(z.object({ id: Id, platform: Platform, handle: z.string(), niche: z.string().nullish(), status: z.string() }))
    .default([]),
  disclosure: z.object({
    locked: z.boolean().default(true),
    text: z.string(),
    seconds: z.number().nullish(),
    note: z.string().nullish(),
  }),
  limits: z.object({
    sources: z.number().default(5),
    script_chars: z.number().default(500),
    watermark_chars: z.number().default(24),
    min_seconds: z.number().default(20),
    max_seconds: z.number().default(25),
    /** Fast-delivery estimate rate (Korean syllables/s) used for the length check. */
    syllables_per_second: z.number().default(8),
    min_speech_sps: z.number().default(7.5),
    note: z.string().nullish(),
  }),
});
export type StudioOptions = z.infer<typeof StudioOptions>;

export const StudioProduct = z.object({
  id: z.number(),
  name: z.string(),
  price: z.number().nullish(),
  image_url: z.string().nullish(),
  category_id: z.string().nullish(),
  category_name: z.string().nullish(),
  is_rocket: z.boolean().nullish(),
  rating: z.number().nullish(),
  review_count: z.number().nullish(),
  pos: z.number().nullish(),
  price_checked_at: IsoTime.nullish(),
});
export type StudioProduct = z.infer<typeof StudioProduct>;
export const StudioProductList = z.object({ items: z.array(StudioProduct) });

export const LintIssue = z.object({
  sentence: z.string(),
  kind: z.string(),
  message: z.string(),
  term: z.string().default(""),
  suggestion: z.string().default(""),
  severity: z.enum(["error", "warning"]).default("error"),
});
export type LintIssue = z.infer<typeof LintIssue>;

export const ClaimShape = z.object({
  id: z.string(),
  text: z.string(),
  kind: z.string().nullish(),
  evidence: z.array(z.string()).default([]),
  grounded: z.boolean().default(false),
});
export type ClaimShape = z.infer<typeof ClaimShape>;

export const LintDuration = z.object({
  estimated_seconds: z.number(),
  syllables: z.number(),
  min_seconds: z.number(),
  max_seconds: z.number(),
  syllables_per_second: z.number(),
});
export type LintDuration = z.infer<typeof LintDuration>;

export const LintResult = z.object({
  ok: z.boolean(),
  issues: z.array(LintIssue).default([]),
  claims: z.array(ClaimShape).default([]),
  /** Shorts only: spoken-length estimate at the fast delivery rate (server-authoritative). */
  duration: LintDuration.nullish(),
});
export type LintResult = z.infer<typeof LintResult>;

export const RenderOptions = z
  .object({
    voice_id: z.string().nullish(),
    caption_style: z.string().nullish(),
    caption_position: z.string().nullish(),
    captions_enabled: z.boolean().nullish(),
    watermark: z.string().nullish(),
    beat_assets: z.record(z.string(), z.string()).nullish(),
  })
  .default({});
export type RenderOptions = z.infer<typeof RenderOptions>;

export const StudioAssetRef = z.object({
  id: Id,
  kind: z.string(),
  url: z.string().nullish(),
  rights_source: z.string().nullish(),
});

export const DraftBeat = z.object({
  index: z.number(),
  role: z.string(),
  start: z.number(),
  end: z.number(),
  narration: z.string().default(""),
  on_screen_text: z.string().default(""),
  visual: z.string().nullish(),
  claim_ids: z.array(z.string()).default([]),
  asset: StudioAssetRef.nullish(),
});
export type DraftBeat = z.infer<typeof DraftBeat>;

export const RenderState = z.object({
  state: z.enum(["idle", "rendering", "done", "failed"]).catch("idle"),
  error: z.string().nullish(),
  media_url: z.string().nullish(),
  thumbnail_url: z.string().nullish(),
  duration: z.number().nullish(),
  voice_id: z.string().nullish(),
  loudness_lufs: z.number().nullish(),
});
export type RenderState = z.infer<typeof RenderState>;

export const DraftView = z.object({
  id: Id,
  status: z.string(),
  kind: ContentKind,
  persona: z.string().nullish(),
  template_id: z.string().nullish(),
  hook_type: z.string().nullish(),
  run_id: Id.nullish(),
  product: z
    .object({
      id: z.number(),
      name: z.string(),
      price: z.number().nullish(),
      image_url: z.string().nullish(),
      category_name: z.string().nullish(),
      rating: z.number().nullish(),
      review_count: z.number().nullish(),
      is_rocket: z.boolean().nullish(),
      price_checked_at: IsoTime.nullish(),
    })
    .nullish(),
  account: z.object({ id: Id, platform: Platform, handle: z.string() }).nullish(),
  hooks: z.array(z.string()).default([]),
  chosen_hook: z.number().default(0),
  beats: z.array(DraftBeat).default([]),
  caption: z.string().default(""),
  body: z.string().default(""),
  hashtags: z.array(z.string()).default([]),
  title: z.string().default(""),
  disclosure: z.string().default(""),
  duration_target: z.number().nullish(),
  claims: z.array(ClaimShape).default([]),
  render_options: RenderOptions,
  render: RenderState.default({ state: "idle" }),
  lint: LintResult.default({ ok: true, issues: [], claims: [] }),
  /** AI scene step (GPT Image still → Higgsfield clip) for scene beats; null when never run. */
  ai_visuals: z
    .object({
      enabled: z.boolean().default(false),
      motion: z.boolean().default(false),
      beats: z
        .record(
          z.string(),
          z.object({
            image: z.string().nullish(),
            video: z.string().nullish(),
            provider: z.string().nullish(),
            video_provider: z.string().nullish(),
            video_error: z.string().nullish(),
          }),
        )
        .default({}),
      errors: z.array(z.string()).default([]),
      at: z.string().nullish(),
    })
    .nullish(),
  gate: z
    .object({
      verdict: Verdict,
      stage: z.string().nullish(),
      decided_at: IsoTime.nullish(),
      findings: z.array(Finding).default([]),
    })
    .nullish(),
  publications: z
    .array(z.object({ id: Id, platform: Platform, state: PublicationState, scheduled_at: IsoTime.nullish() }))
    .default([]),
});
export type DraftView = z.infer<typeof DraftView>;

export const StudioPublishResult = z.object({
  verdict: Verdict,
  actions: z.array(z.string()).default([]),
  findings: z.array(Finding).default([]),
  publications: z
    .array(z.object({ id: Id, platform: Platform, state: PublicationState, scheduled_at: IsoTime.nullish() }))
    .default([]),
  content_id: Id.nullish(),
});
export type StudioPublishResult = z.infer<typeof StudioPublishResult>;

export const StudioAsset = z.object({
  id: Id,
  kind: z.string(),
  rights_source: z.string().nullish(),
  license: z.string().nullish(),
  product_id: z.number().nullish(),
  tags: z.array(z.string()).default([]),
  attribution: z.string().nullish(),
  url: z.string().nullish(),
  created_at: IsoTime.nullish(),
  expires_at: IsoTime.nullish(),
});
export type StudioAsset = z.infer<typeof StudioAsset>;
export const StudioAssetList = z.object({ items: z.array(StudioAsset) });

export const TranscribeResult = z.object({
  asset_id: Id.nullish(),
  text: z.string(),
  language: z.string().nullish(),
  engine: z.string().nullish(),
  words: z.array(z.unknown()).default([]),
});
export type TranscribeResult = z.infer<typeof TranscribeResult>;

export const RenderAck = z.object({ state: z.string() });
