/**
 * In-browser mock of the ACVM API, used when NEXT_PUBLIC_ACVM_MOCK=1.
 * State lives in memory for the lifetime of the tab; mutations (approve, HOLD decisions,
 * account edits, schedules, manual queue) update it and append audit entries, so the demo
 * behaves like the real system end-to-end. Responses are validated by api.ts like real ones.
 */
import { ApiError } from "@/lib/errors";
import { breakeven, simulate } from "@/lib/quant";
import { getMockRole } from "@/lib/session";
import type { HttpMethod, Query } from "@/lib/api";
import {
  buildPlan,
  createDb,
  learningFixture,
  makeContents,
  moneyFixture,
  mulberry32,
  parseCommand,
  pipelineFixture,
  riskFixture,
  rulesFixture,
  uuid,
  type AccountIn,
  type MockDb,
} from "./fixtures";
import {
  LEGAL_CHECKLIST,
  SECRET_SLOTS,
  alertsFeed,
  contentDossier,
  createOps,
  listPublications,
  systemStatus,
  takedown,
  workspace,
  type OpsState,
} from "./ops";
import {
  addUploaded,
  allAssets,
  autoStudio,
  createDraft,
  assetRights,
  deriveAsset,
  draftDossier,
  getDraftView,
  hasDraft,
  lintText,
  listAssets,
  publishDraft,
  renderDraft,
  studioOptions,
  studioProducts,
  styleTile,
  transcribeAsset,
  updateDraft,
  voiceWav,
} from "./studio";
import {
  integrationsList,
  isIntegrationActive,
  onSecretChanged,
  remapVoices,
  setIntegrationEnabled,
  studioIntegrations,
  verifyIntegration,
} from "./integrations";
import { addMember, emailTaken, memberList, patchMember, removeMember } from "./members";

let db: MockDb | null = null;
let ops: OpsState | null = null;
const rnd = mulberry32(Date.now() % 100_000);

function getDb(): MockDb {
  if (!db) {
    db = createDb(Date.now());
    ops = createOps(db, Date.now());
  }
  return db;
}

function getOps(): OpsState {
  getDb();
  return ops as OpsState;
}

/** Test hook: rebuild the in-memory database. */
export function resetMockDb(now = Date.now()): MockDb {
  db = createDb(now);
  ops = createOps(db, now);
  return db;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function http(status: number, detail: string): never {
  throw new ApiError("http", `HTTP ${status}`, { status, detail });
}

const MOCK_NAMES: Record<string, string> = { owner: "대표", operator: "운영 담당", reviewer: "검수 담당", viewer: "열람자" };

function principal() {
  const role = typeof window === "undefined" ? "owner" : getMockRole();
  return {
    subject: `${role}@acvm.local`,
    email: `${role}@acvm.local`,
    name: MOCK_NAMES[role] ?? role,
    role,
    workspace_id: "ws_demo",
    user_id: role === "owner" ? "usr_owner" : `usr_${role}`,
    auth: "mock",
  };
}

function audit(action: string, target: string, payload: unknown) {
  getDb().audit.unshift({ at: new Date().toISOString(), actor: principal().subject, action, target, payload });
}

function requireRole(roles: string[]) {
  const p = principal();
  if (!roles.includes(p.role)) http(403, `${p.role} 역할은 이 작업을 수행할 수 없습니다`);
}

function num(q: Query | undefined, key: string, fallback: number): number {
  const v = q?.[key];
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(n) ? n : fallback;
}

function body<T>(b: unknown): T {
  return (b ?? {}) as T;
}

type Handler = (m: RegExpMatchArray, q: Query | undefined, b: unknown) => unknown;

const routes: Array<[HttpMethod, RegExp, Handler]> = [
  ["GET", /^\/me$/, () => principal()],
  ["GET", /^\/auth\/config$/, () => ({ mode: "mock", signup: "approval", password_min: 8, password_rule: "8자 이상, 영문 대문자·소문자·숫자·특수문자 중 3종류 이상" })],
  [
    "POST",
    /^\/auth\/login$/,
    (_m, _q, b) => {
      const v = body<{ email?: string; password?: string }>(b);
      if (!v.email || !v.password) http(422, "이메일과 비밀번호를 입력하세요");
      return principal();
    },
  ],
  [
    "POST",
    /^\/auth\/signup$/,
    (_m, _q, b) => {
      const v = body<{ email?: string; password?: string; name?: string }>(b);
      if (!v.email || !v.password || v.password.length < 8) http(422, "비밀번호는 8자 이상이어야 합니다");
      if (emailTaken(v.email)) http(409, "이미 가입된 이메일입니다");
      const m = addMember({ email: v.email, name: v.name }, "pending");
      return { status: "pending", message: "가입 신청이 접수되었습니다. 소유자가 승인하면 로그인할 수 있습니다.", email: m.email };
    },
  ],
  ["POST", /^\/auth\/logout$/, () => ({ ok: true })],
  ["POST", /^\/auth\/password$/, () => ({ ok: true })],
  [
    "GET",
    /^\/users$/,
    () => {
      requireRole(["owner"]);
      return memberList();
    },
  ],
  [
    "POST",
    /^\/users$/,
    (_m, _q, b) => {
      requireRole(["owner"]);
      const v = body<{ email?: string; name?: string; role?: "owner" | "operator" | "reviewer" | "viewer"; password?: string }>(b);
      if (!v.email || !v.password || v.password.length < 8) http(422, "이메일과 8자 이상 비밀번호가 필요합니다");
      if (emailTaken(v.email)) http(409, "이미 가입된 이메일입니다");
      audit("member.create", v.email, { role: v.role });
      return addMember({ email: v.email, name: v.name, role: v.role }, "active");
    },
  ],
  [
    "PATCH",
    /^\/users\/([^/]+)$/,
    (m, _q, b) => {
      requireRole(["owner"]);
      const id = decodeURIComponent(m[1]);
      const out = patchMember(id, body<{ role?: "owner" | "operator" | "reviewer" | "viewer"; status?: "pending" | "active" | "disabled"; name?: string }>(b));
      if (!out) http(404, "member not found");
      audit("member.update", id, body(b));
      return out;
    },
  ],
  [
    "DELETE",
    /^\/users\/([^/]+)$/,
    (m) => {
      requireRole(["owner"]);
      const id = decodeURIComponent(m[1]);
      if (id === "usr_owner") http(403, "자기 자신은 삭제할 수 없습니다");
      if (!removeMember(id)) http(404, "member not found");
      audit("member.delete", id, {});
      return { ok: true };
    },
  ],

  /* ---------------------------------------------------------------- commands & runs */
  [
    "POST",
    /^\/commands$/,
    (_m, _q, b) => {
      requireRole(["owner", "operator"]);
      const text = String(body<{ text?: string }>(b).text ?? "").trim();
      if (text.length < 2) http(422, "text: 명령을 입력하세요");
      const d = getDb();
      const runId = uuid(rnd);
      const spec = { ...parseCommand(text), runId };
      const plan = buildPlan(spec);
      const status = plan.approval.mode === "auto" ? "approved" : "awaiting_approval";
      d.runs.unshift({
        id: runId,
        raw_command: text,
        status,
        est_cost_krw: plan.est_cost_krw,
        cost_krw: null,
        started_at: status === "approved" ? new Date().toISOString() : null,
        ended_at: null,
        summary: status === "approved" ? "자동 승인 — 실행 대기열 진입" : "콘솔 승인 필요",
      });
      d.plans[runId] = plan;
      d.contents[runId] =
        status === "approved" && plan.intent !== "analyze"
          ? makeContents(plan, spec.category, Array.from({ length: plan.count }, () => "rendering" as const), Date.now(), 1)
          : [];
      d.costs[runId] = { spent_krw: 0, cap_krw: plan.budget_cap_krw ?? null };
      audit("command.submit", `run:${runId}`, { text, approval: plan.approval.mode });
      return { run_id: runId, status, plan, approval: plan.approval, explanation: plan.explanation };
    },
  ],
  [
    "GET",
    /^\/runs$/,
    (_m, q) => ({ items: getDb().runs.slice(0, num(q, "limit", 50)) }),
  ],
  [
    "GET",
    /^\/runs\/([^/]+)$/,
    (m) => {
      const d = getDb();
      const id = decodeURIComponent(m[1]);
      const run = d.runs.find((r) => r.id === id);
      if (!run) http(404, `run ${id} not found`);
      return {
        run: { ...run, plan: d.plans[id] ?? null },
        contents: d.contents[id] ?? [],
        cost: d.costs[id] ?? { spent_krw: 0, cap_krw: null },
      };
    },
  ],
  [
    "POST",
    /^\/runs\/([^/]+)\/(approve|reject)$/,
    (m, _q, b) => {
      requireRole(["owner", "reviewer"]);
      const d = getDb();
      const id = decodeURIComponent(m[1]);
      const run = d.runs.find((r) => r.id === id);
      if (!run) http(404, `run ${id} not found`);
      if (run.status !== "awaiting_approval") http(409, `현재 상태(${run.status})에서는 결정할 수 없습니다`);
      if (m[2] === "approve") {
        run.status = "running";
        run.started_at = new Date().toISOString();
        run.summary = "콘솔 승인 — 생산 시작";
        const plan = d.plans[id];
        if (plan && plan.intent !== "analyze") {
          const spec = parseCommand(run.raw_command);
          d.contents[id] = makeContents(plan, spec.category, Array.from({ length: plan.count }, () => "rendering" as const), Date.now(), 3);
        }
        audit("plan.approve", `run:${id}`, {});
      } else {
        const reason = String(body<{ reason?: string }>(b).reason ?? "").trim();
        if (!reason) http(422, "reason: 반려 사유를 입력하세요");
        run.status = "rejected";
        run.ended_at = new Date().toISOString();
        run.summary = `반려: ${reason}`;
        audit("plan.reject", `run:${id}`, { reason });
      }
      return { status: run.status };
    },
  ],

  /* ---------------------------------------------------------------- HOLD */
  ["GET", /^\/holds$/, () => ({ items: getDb().holds })],
  [
    "POST",
    /^\/holds\/([^/]+)\/decision$/,
    (m, _q, b) => {
      requireRole(["owner", "reviewer"]);
      const d = getDb();
      const id = decodeURIComponent(m[1]);
      const idx = d.holds.findIndex((h) => String(h.id) === id);
      if (idx < 0) http(404, `hold ${id} not found (이미 처리되었을 수 있습니다)`);
      const { approve, note } = body<{ approve?: boolean; note?: string }>(b);
      if (typeof approve !== "boolean") http(422, "approve: boolean 필드가 필요합니다");
      d.holds.splice(idx, 1);
      audit(approve ? "hold.approve" : "hold.reject", `hold:${id}`, { note: note ?? null });
      return { status: approve ? "approved" : "rejected" };
    },
  ],

  /* ---------------------------------------------------------------- dashboards */
  ["GET", /^\/dashboards\/money$/, (_m, q) => moneyFixture(num(q, "days", 30), Date.now())],
  ["GET", /^\/dashboards\/pipeline$/, () => pipelineFixture()],
  ["GET", /^\/dashboards\/risk$/, () => riskFixture(Date.now(), getDb().accounts)],
  ["GET", /^\/dashboards\/learning$/, () => learningFixture()],

  /* ---------------------------------------------------------------- accounts */
  ["GET", /^\/accounts$/, () => ({ items: getDb().accounts })],
  [
    "POST",
    /^\/accounts$/,
    (_m, _q, b) => {
      requireRole(["owner"]);
      const v = body<{ platform: AccountIn["platform"]; handle: string; niche: string; daily_cap: number }>(b);
      const d = getDb();
      const handle = v.handle.startsWith("@") || v.platform === "kakao" ? v.handle : `@${v.handle}`;
      if (d.accounts.some((a) => a.platform === v.platform && a.handle === handle)) http(409, "이미 등록된 계정입니다");
      const acc: AccountIn = {
        id: `acc_${uuid(rnd).slice(0, 8)}`,
        platform: v.platform,
        handle,
        niche: v.niche,
        status: "active",
        daily_cap: v.daily_cap,
        cooldown_until: null,
        created_at: new Date().toISOString(),
      };
      d.accounts.push(acc);
      audit("account.create", `account:${acc.id}`, { platform: v.platform, handle });
      return acc;
    },
  ],
  [
    "PATCH",
    /^\/accounts\/([^/]+)$/,
    (m, _q, b) => {
      requireRole(["owner"]);
      const d = getDb();
      const acc = d.accounts.find((a) => String(a.id) === decodeURIComponent(m[1]));
      if (!acc) http(404, "account not found");
      const v = body<{ status?: AccountIn["status"]; daily_cap?: number }>(b);
      if (v.status) {
        acc.status = v.status;
        acc.cooldown_until = v.status === "cooldown" ? new Date(Date.now() + 48 * 3_600_000).toISOString() : null;
      }
      if (typeof v.daily_cap === "number") acc.daily_cap = v.daily_cap;
      audit("account.patch", `account:${acc.id}`, v);
      return acc;
    },
  ],

  /* ---------------------------------------------------------------- schedules */
  ["GET", /^\/schedules$/, () => ({ items: getDb().schedules })],
  [
    "POST",
    /^\/schedules$/,
    (_m, _q, b) => {
      requireRole(["owner", "operator"]);
      const v = body<{ text?: string; cron?: string }>(b);
      if (!v.text?.trim()) http(422, "text: 명령을 입력하세요");
      if (!v.cron || v.cron.trim().split(/\s+/).length !== 5) http(422, "cron: 5개 필드 cron 식이 필요합니다");
      const s = { id: `sch_${uuid(rnd).slice(0, 6)}`, raw_command: v.text.trim(), cron: v.cron.trim(), active: true, created_by: principal().subject };
      getDb().schedules.push(s);
      audit("schedule.create", `schedule:${s.id}`, { cron: s.cron });
      return s;
    },
  ],
  [
    "DELETE",
    /^\/schedules\/([^/]+)$/,
    (m) => {
      requireRole(["owner", "operator"]);
      const d = getDb();
      const id = decodeURIComponent(m[1]);
      const sch = d.schedules.find((s) => String(s.id) === id);
      if (!sch) http(404, "schedule not found");
      // Matches the backend: DELETE deactivates the recurring command (history is kept).
      sch.active = false;
      audit("schedule.disable", id, {});
      return { id, active: false };
    },
  ],

  /* ---------------------------------------------------------------- manual queue */
  ["GET", /^\/manual-queue$/, () => ({ items: getDb().manual })],
  [
    "POST",
    /^\/manual-queue\/([^/]+)\/done$/,
    (m, _q, b) => {
      requireRole(["owner", "operator"]);
      const d = getDb();
      const id = decodeURIComponent(m[1]);
      const permalink = String(body<{ permalink?: string }>(b).permalink ?? "");
      if (!/^https?:\/\//.test(permalink)) http(422, "permalink: http(s) URL이 필요합니다");
      const before = d.manual.length;
      d.manual = d.manual.filter((x) => String(x.publication_id) !== id);
      if (d.manual.length === before) http(404, "publication not in manual queue");
      audit("manual.done", `publication:${id}`, { permalink });
      return { status: "PUBLISHED" };
    },
  ],

  /* ---------------------------------------------------------------- rules / audit / quant */
  /* ---------------------------------------------------------------- operations */
  ["GET", /^\/system\/status$/, () => systemStatus(getDb(), getOps(), Date.now())],
  [
    "POST",
    /^\/system\/checklist$/,
    (_m, _q, b) => {
      requireRole(["owner"]);
      const v = body<{ item?: string; done?: boolean }>(b);
      if (!v.item || !(v.item in LEGAL_CHECKLIST)) http(422, "unknown checklist item");
      getOps().checklist[v.item] = !!v.done;
      audit("checklist.set", v.item, { done: !!v.done });
      return { item: v.item, done: !!v.done };
    },
  ],
  [
    "PUT",
    /^\/system\/secrets\/([^/]+)$/,
    (m, _q, b) => {
      requireRole(["owner"]);
      const name = decodeURIComponent(m[1]);
      if (!SECRET_SLOTS.some(([n]) => n === name)) http(404, "unknown secret slot");
      const value = String(body<{ value?: string }>(b).value ?? "");
      if (value.length < 8) http(422, "value: 8자 이상이어야 합니다");
      getOps().secrets[name] = true;
      audit("secret.set", name, {});
      return { name, configured: true, persistent: false, integration: onSecretChanged(getOps().secrets, name, value) };
    },
  ],
  [
    "DELETE",
    /^\/system\/secrets\/([^/]+)$/,
    (m) => {
      requireRole(["owner"]);
      const name = decodeURIComponent(m[1]);
      if (!SECRET_SLOTS.some(([n]) => n === name)) http(404, "unknown secret slot");
      const removed = !!getOps().secrets[name];
      getOps().secrets[name] = false;
      audit("secret.delete", name, {});
      return { name, configured: false, removed, integration: onSecretChanged(getOps().secrets, name, null) };
    },
  ],
  ["GET", /^\/system\/integrations$/, () => integrationsList(getOps().secrets)],
  [
    "POST",
    /^\/system\/integrations\/elevenlabs\/voices$/,
    () => {
      requireRole(["owner"]);
      if (!isIntegrationActive(getOps().secrets, "elevenlabs")) http(409, "ElevenLabs 키가 등록·확인되지 않았습니다");
      return remapVoices(getOps().secrets);
    },
  ],
  [
    "POST",
    /^\/system\/integrations\/([^/]+)\/verify$/,
    (m) => {
      requireRole(["owner"]);
      const name = decodeURIComponent(m[1]);
      if (!integrationsList(getOps().secrets).items.some((i) => i.name === name)) http(404, "unknown integration");
      return verifyIntegration(getOps().secrets, name);
    },
  ],
  [
    "PUT",
    /^\/system\/integrations\/([^/]+)$/,
    (m, _q, b) => {
      requireRole(["owner"]);
      const name = decodeURIComponent(m[1]);
      if (!integrationsList(getOps().secrets).items.some((i) => i.name === name)) http(404, "unknown integration");
      const enabled = !!body<{ enabled?: boolean }>(b).enabled;
      audit("integration.enabled", name, { enabled });
      return setIntegrationEnabled(getOps().secrets, name, enabled);
    },
  ],
  ["GET", /^\/workspace$/, () => workspace(getDb(), getOps(), Date.now())],
  [
    "PATCH",
    /^\/workspace$/,
    (_m, _q, b) => {
      requireRole(["owner"]);
      const v = body<{ name?: string; daily_budget_krw?: number }>(b);
      const o = getOps();
      if (v.name !== undefined) {
        if (!v.name.trim()) http(422, "name: 비어 있을 수 없습니다");
        o.workspace.name = v.name.trim();
      }
      if (v.daily_budget_krw !== undefined) {
        if (!(v.daily_budget_krw >= 0 && v.daily_budget_krw <= 10_000_000)) http(422, "daily_budget_krw: 0~10,000,000");
        o.workspace.daily_budget_krw = v.daily_budget_krw;
      }
      audit("workspace.update", "ws_demo", v);
      return workspace(getDb(), o, Date.now());
    },
  ],
  [
    "PUT",
    /^\/accounts\/([^/]+)\/credentials$/,
    (m, _q, b) => {
      requireRole(["owner"]);
      const id = decodeURIComponent(m[1]);
      if (!getDb().accounts.some((a) => String(a.id) === id)) http(404, "account not found");
      const token = String(body<{ token?: string }>(b).token ?? "");
      if (token.length < 10) http(422, "token: 10자 이상이어야 합니다");
      getOps().credentials[id] = true;
      audit("account.credentials.set", id, {});
      return { account_id: id, configured: true, persistent: false };
    },
  ],
  [
    "GET",
    /^\/publications$/,
    (_m, q) =>
      listPublications(getOps(), {
        state: typeof q?.state === "string" ? q.state : undefined,
        platform: typeof q?.platform === "string" ? q.platform : undefined,
        account_id: typeof q?.account_id === "string" ? q.account_id : undefined,
        since: typeof q?.since === "string" ? q.since : undefined,
        until: typeof q?.until === "string" ? q.until : undefined,
        limit: num(q, "limit", 200),
      }),
  ],
  [
    "POST",
    /^\/publications\/([^/]+)\/takedown$/,
    (m, _q, b) => {
      requireRole(["owner", "reviewer"]);
      const reason = String(body<{ reason?: string }>(b).reason ?? "").trim();
      if (reason.length < 2) http(422, "reason: 2자 이상 입력하세요");
      const id = decodeURIComponent(m[1]);
      const res = takedown(getDb(), getOps(), id, reason);
      audit("publication.takedown", id, { reason });
      return res;
    },
  ],
  [
    "GET",
    /^\/contents\/([^/]+)$/,
    (m) => {
      const id = decodeURIComponent(m[1]);
      return hasDraft(id) ? draftDossier(id) : contentDossier(getDb(), getOps(), id, Date.now());
    },
  ],
  ["GET", /^\/alerts$/, () => alertsFeed(getDb(), getOps(), Date.now())],

  /* ---------------------------------------------------------------- shorts studio */
  ["GET", /^\/studio\/options$/, () => ({ ...studioOptions(getDb()), integrations: studioIntegrations(getOps().secrets) })],
  [
    "GET",
    /^\/studio\/products$/,
    (_m, q) => {
      if (q?.refresh === true || q?.refresh === "true") requireRole(["owner", "operator"]);
      return studioProducts({
        category: typeof q?.category === "string" ? q.category : undefined,
        q: typeof q?.q === "string" ? q.q : undefined,
        limit: num(q, "limit", 30),
      });
    },
  ],
  [
    "POST",
    /^\/studio\/drafts$/,
    (_m, _q, b) => {
      requireRole(["owner", "operator"]);
      const v = body<{ product_id: number; kind?: "short" | "text"; persona?: string; account_id?: string }>(b);
      const d = createDraft(getDb(), v);
      audit("studio.draft", String(d.id), { product_id: v.product_id, kind: v.kind ?? "short" });
      return d;
    },
  ],
  ["GET", /^\/studio\/drafts\/([^/]+)$/, (m) => getDraftView(decodeURIComponent(m[1]))],
  [
    "PUT",
    /^\/studio\/drafts\/([^/]+)$/,
    (m, _q, b) => {
      requireRole(["owner", "operator"]);
      return updateDraft(decodeURIComponent(m[1]), body<Record<string, unknown>>(b), allAssets());
    },
  ],
  [
    "POST",
    /^\/studio\/lint$/,
    (_m, _q, b) => {
      const v = body<{ product_id: number; text: string; narrations?: string[] }>(b);
      return lintText(v.product_id, v.text ?? "", v.narrations ?? null);
    },
  ],
  [
    "POST",
    /^\/studio\/drafts\/([^/]+)\/render$/,
    (m) => {
      requireRole(["owner", "operator"]);
      return renderDraft(decodeURIComponent(m[1]));
    },
  ],
  [
    "POST",
    /^\/studio\/drafts\/([^/]+)\/publish$/,
    (m, _q, b) => {
      requireRole(["owner", "operator"]);
      const v = body<{ targets?: Array<{ account_id: string }> }>(b);
      const id = decodeURIComponent(m[1]);
      const res = publishDraft(getDb(), id, v.targets?.map((t) => t.account_id));
      audit("studio.publish", id, { verdict: res.verdict });
      return res;
    },
  ],
  [
    "POST",
    /^\/studio\/auto$/,
    (_m, _q, b) => {
      requireRole(["owner", "operator"]);
      return autoStudio(getDb(), body(b));
    },
  ],
  [
    "GET",
    /^\/studio\/assets$/,
    (_m, q) => listAssets(q?.product_id !== undefined && q?.product_id !== null ? num(q, "product_id", 0) : null),
  ],
  [
    "POST",
    /^\/studio\/assets\/([^/]+)\/cut$/,
    (m, _q, b) => {
      requireRole(["owner", "operator"]);
      const v = body<{ start: number; end: number }>(b);
      if (!(v.end > v.start)) http(422, "끝 시간은 시작 시간보다 커야 합니다");
      return deriveAsset(decodeURIComponent(m[1]), "cut", { start: v.start, end: v.end });
    },
  ],
  [
    "POST",
    /^\/studio\/assets\/([^/]+)\/i2v$/,
    (m, _q, b) => {
      requireRole(["owner", "operator"]);
      const v = body<{ motion: string; seconds: number; engine?: "camera" | "ai" }>(b);
      if (v.engine === "ai") {
        if (!isIntegrationActive(getOps().secrets, "higgsfield")) http(409, "Higgsfield 키가 등록·확인되지 않았습니다 (운영 준비 › AI 연동)");
        if (assetRights(decodeURIComponent(m[1])) !== "ai_generated_background") {
          http(403, "AI 모션은 AI 생성 장면 이미지에만 쓸 수 있습니다 — 상품 사진·직접 촬영본은 카메라 모션을 사용하세요");
        }
        return deriveAsset(decodeURIComponent(m[1]), "higgsfield_i2v", { seconds: v.seconds });
      }
      return deriveAsset(decodeURIComponent(m[1]), `i2v_${v.motion}`, { seconds: v.seconds });
    },
  ],
  [
    "POST",
    /^\/studio\/assets\/([^/]+)\/transcribe$/,
    (m) => {
      requireRole(["owner", "operator"]);
      return transcribeAsset(decodeURIComponent(m[1]));
    },
  ],

  ["GET", /^\/rules$/, () => rulesFixture()],
  ["GET", /^\/audit$/, (_m, q) => ({ items: getDb().audit.slice(0, num(q, "limit", 200)) })],
  [
    "GET",
    /^\/quant\/breakeven$/,
    (_m, q) =>
      breakeven({
        c_unit: num(q, "c_unit", 150),
        ctr: num(q, "ctr", 0.008),
        cvr: num(q, "cvr", 0.03),
        aov: num(q, "aov", 30000),
        r: num(q, "r", 0.03),
      }),
  ],
  [
    "GET",
    /^\/quant\/simulate$/,
    (_m, q) =>
      simulate({
        c_unit: num(q, "c_unit", 150),
        ctr: num(q, "ctr", 0.008),
        cvr: num(q, "cvr", 0.03),
        aov: num(q, "aov", 30000),
        r: num(q, "r", 0.03),
        median: num(q, "median", 600),
        sigma: num(q, "sigma", 2),
        n: num(q, "n", 300),
      }),
  ],
];

/** Mock transport: resolves to the raw JSON body the real API would return. */
export async function mockRequest(
  method: HttpMethod,
  path: string,
  query: Query | undefined,
  reqBody: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  await sleep(method === "GET" ? 120 + Math.floor(rnd() * 220) : 280 + Math.floor(rnd() * 320), signal);
  for (const [m, re, handler] of routes) {
    if (m !== method) continue;
    const match = path.match(re);
    if (match) {
      // Deep copy so callers can never mutate the in-memory store by reference.
      return JSON.parse(JSON.stringify(handler(match, query, reqBody))) as unknown;
    }
  }
  http(404, `mock: no route for ${method} ${path}`);
}

/** Mock binary endpoints: voice preview (WAV) and caption style tile (SVG). */
export async function mockBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  await sleep(150, signal);
  const voice = /^\/studio\/voices\/([^/]+)\/preview$/.exec(path);
  if (voice) return voiceWav(decodeURIComponent(voice[1]));
  const style = /^\/studio\/caption-styles\/([^/]+)\/preview\.png$/.exec(path);
  if (style) return styleTile(decodeURIComponent(style[1]));
  http(404, `mock: no binary route for ${path}`);
}

/** Mock upload: simulated progress; the file plays back from a local object URL. */
export async function mockUpload(
  file: File,
  attest: boolean,
  productId: number | null,
  onProgress?: (fraction: number) => void,
): Promise<unknown> {
  requireRole(["owner", "operator"]);
  if (!attest) http(422, "권리 보유 확인이 필요합니다 — 타인 영상·이미지는 업로드할 수 없습니다 (설계서 §8.2)");
  const types = ["video/mp4", "video/quicktime", "video/webm", "image/png", "image/jpeg", "image/webp"];
  if (!types.includes(file.type)) http(415, `지원하지 않는 형식: ${file.type || "알 수 없음"} (mp4·mov·webm·png·jpg·webp)`);
  if (file.size > 300 * 1024 * 1024) http(413, "파일이 너무 큽니다 (최대 300MB)");
  for (let i = 1; i <= 5; i++) {
    await sleep(120);
    onProgress?.(i / 5);
  }
  const url = typeof URL !== "undefined" && "createObjectURL" in URL ? URL.createObjectURL(file) : null;
  const a = addUploaded({ name: file.name, type: file.type, url }, productId);
  audit("asset.upload", String(a.id), { mime: file.type, size: file.size, rights: "own_footage" });
  return JSON.parse(JSON.stringify(a));
}
