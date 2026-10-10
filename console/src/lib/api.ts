/**
 * Typed ACVM API client.
 *
 * Every response — real or mock — is validated with the zod schemas in ./schemas, so contract
 * drift surfaces as a readable "응답 형식이 계약과 다릅니다" banner instead of a runtime crash.
 *
 * Auth
 * - dev: bearer token typed into the settings dialog (localStorage) → `Authorization: Bearer …`
 * - prod: the console sits behind Cloudflare Access; the CF_Authorization cookie is forwarded by
 *   `credentials: "include"` and the API reads the `Cf-Access-Jwt-Assertion` header injected by
 *   Access. Both paths are always active; whichever the API accepts wins.
 */
import type { z } from "zod";
import { API_BASE, MOCK_MODE, REQUEST_TIMEOUT_MS } from "./config";
import { ApiError } from "./errors";
import {
  Ack,
  AlertsFeed,
  AuthConfig,
  AccountList,
  type AccountCreate,
  type AccountPatch,
  AuditList,
  Breakeven,
  ChecklistResponse,
  CommandResponse,
  ContentDossier,
  CredentialsPutResponse,
  DecisionResponse,
  HoldList,
  Integration,
  IntegrationList,
  LearningDashboard,
  ManualQueue,
  Me,
  Member,
  MemberList,
  MoneyDashboard,
  PipelineDashboard,
  PublicationList,
  RiskDashboard,
  RuleSet,
  RunDetail,
  RunList,
  OkResponse,
  SignupResult,
  DraftView,
  LintResult,
  RenderAck,
  ScheduleList,
  SecretDeleteResponse,
  SecretPutResponse,
  StudioAsset,
  StudioAssetList,
  StudioOptions,
  StudioProductList,
  StudioPublishResult,
  TranscribeResult,
  Simulation,
  SystemStatus,
  TakedownResponse,
  VoiceRemapResponse,
  Workspace,
} from "./schemas";
import { getDevRole, getToken } from "./session";

export * from "./schemas";
export { ApiError } from "./errors";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type Query = Record<string, string | number | boolean | undefined | null>;

interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Override REQUEST_TIMEOUT_MS (render/publish/auto can take a minute). */
  timeoutMs?: number;
}

function buildQuery(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

/** Extracts a human-readable message from FastAPI-style `{detail: string | [{msg, loc}]}` bodies. */
async function readErrorDetail(res: Response): Promise<string | undefined> {
  try {
    const text = await res.text();
    if (!text) return undefined;
    try {
      const body = JSON.parse(text) as unknown;
      if (body && typeof body === "object") {
        const detail = (body as { detail?: unknown; message?: unknown }).detail ?? (body as { message?: unknown }).message;
        if (typeof detail === "string") return detail;
        if (Array.isArray(detail)) {
          return detail
            .map((d) => {
              if (d && typeof d === "object" && "msg" in d) {
                const loc = Array.isArray((d as { loc?: unknown }).loc)
                  ? ((d as { loc: unknown[] }).loc.filter((x) => x !== "body").join("."))
                  : "";
                return loc ? `${loc}: ${String((d as { msg: unknown }).msg)}` : String((d as { msg: unknown }).msg);
              }
              return JSON.stringify(d);
            })
            .join(" · ");
        }
        if (detail !== undefined) return JSON.stringify(detail);
      }
    } catch {
      /* not JSON */
    }
    return text.slice(0, 300);
  } catch {
    return undefined;
  }
}

function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

async function rawRequest(method: HttpMethod, path: string, opts: RequestOptions): Promise<unknown> {
  if (MOCK_MODE) {
    const { mockRequest } = await import("@/mocks/handler");
    return mockRequest(method, path, opts.query, opts.body, opts.signal);
  }

  const url = `${API_BASE}${path}${buildQuery(opts.query)}`;
  // X-ACVM-Client: CSRF guard for cookie (member login) sessions — cross-site pages cannot send this header.
  const headers: Record<string, string> = { Accept: "application/json", "X-ACVM-Client": "console" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
    // Dev-only RBAC override; ignored by the API outside env=dev/test.
    const devRole = getDevRole();
    if (devRole) headers["X-Dev-Role"] = devRole;
  }

  const timeout = AbortSignal.timeout(opts.timeoutMs ?? REQUEST_TIMEOUT_MS);
  const signal = opts.signal ? AbortSignal.any([opts.signal, timeout]) : timeout;

  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      credentials: "include",
      cache: "no-store",
      signal,
    });
  } catch (e) {
    // Caller-initiated cancellation (TanStack Query unmount/refetch): propagate untouched.
    if (opts.signal?.aborted && isAbort(e)) throw e;
    if (timeout.aborted) throw new ApiError("timeout", `Timed out: ${method} ${path}`, { path });
    throw new ApiError("network", `Network error: ${method} ${path}`, { path });
  }

  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw new ApiError("http", `HTTP ${res.status}: ${method} ${path}`, { status: res.status, detail, path });
  }
  if (res.status === 204) return {};
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError("schema", `Invalid JSON: ${method} ${path}`, { issues: ["본문이 JSON이 아닙니다"], path });
  }
}

export async function request<S extends z.ZodType>(
  method: HttpMethod,
  path: string,
  schema: S,
  opts: RequestOptions = {},
): Promise<z.output<S>> {
  const raw = await rawRequest(method, path, opts);
  const parsed = schema.safeParse(raw);
  if (!parsed.success) {
    const issues = parsed.error.issues
      .slice(0, 6)
      .map((i) => `${i.path.length ? i.path.join(".") : "(root)"}: ${i.message}`);
    if (process.env.NODE_ENV !== "production") {
      console.warn(`[acvm] ${method} ${path} failed schema validation`, parsed.error.issues);
    }
    throw new ApiError("schema", `Schema mismatch: ${method} ${path}`, { issues, path });
  }
  return parsed.data;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "X-ACVM-Client": "console" };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
    const devRole = getDevRole();
    if (devRole) headers["X-Dev-Role"] = devRole;
  }
  return headers;
}

/** Authenticated binary GET (voice previews, caption style tiles) — <img>/<audio> cannot send the bearer header. */
export async function fetchBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  if (MOCK_MODE) {
    const { mockBlob } = await import("@/mocks/handler");
    return mockBlob(path, signal);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { headers: authHeaders(), credentials: "include", signal });
  } catch (e) {
    if (signal?.aborted && isAbort(e)) throw e;
    throw new ApiError("network", `Network error: GET ${path}`, { path });
  }
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw new ApiError("http", `HTTP ${res.status}: GET ${path}`, { status: res.status, detail, path });
  }
  return res.blob();
}

export const UPLOAD_TYPES = ["video/mp4", "video/quicktime", "video/webm", "image/png", "image/jpeg", "image/webp"];
export const MAX_UPLOAD_BYTES = 300 * 1024 * 1024;

/**
 * Raw-body upload of the operator's own footage (studio.py: Content-Type = file MIME, X-Filename).
 * Uses XHR for upload progress. `attest` must be true — the API refuses unattested uploads (§8.2).
 */
export function uploadAsset(
  file: File,
  opts: { attest: boolean; productId?: number | null; onProgress?: (fraction: number) => void },
): Promise<StudioAsset> {
  const query = buildQuery({ rights: "own_footage", attest: opts.attest, product_id: opts.productId ?? undefined });
  if (MOCK_MODE) {
    return import("@/mocks/handler").then(async ({ mockUpload }) => {
      const raw = await mockUpload(file, opts.attest, opts.productId ?? null, opts.onProgress);
      return StudioAsset.parse(raw);
    });
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/studio/assets${query}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onProgress?.(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError("network", "Network error: POST /studio/assets", { path: "/studio/assets" }));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        body = null;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const detail =
          body && typeof body === "object" && "detail" in body
            ? String((body as { detail: unknown }).detail)
            : xhr.responseText.slice(0, 300);
        reject(new ApiError("http", `HTTP ${xhr.status}: POST /studio/assets`, { status: xhr.status, detail, path: "/studio/assets" }));
        return;
      }
      const parsed = StudioAsset.safeParse(body);
      if (!parsed.success) {
        reject(
          new ApiError("schema", "Schema mismatch: POST /studio/assets", {
            issues: parsed.error.issues.slice(0, 4).map((i) => `${i.path.join(".")}: ${i.message}`),
          }),
        );
        return;
      }
      resolve(parsed.data);
    };
    xhr.send(file);
  });
}

export interface DraftEdit {
  hooks?: string[];
  chosen_hook?: number;
  beats?: Array<{ narration?: string; on_screen_text?: string } | null>;
  caption?: string;
  body?: string;
  title?: string;
  hashtags?: string[];
  render_options?: Record<string, unknown>;
}

export interface DraftCreate {
  product_id: number;
  kind: "short" | "text";
  persona?: string;
  account_id?: string;
  platform?: string;
  template_id?: string;
  hook_type?: string;
}

const enc = encodeURIComponent;

export interface PublicationQuery {
  /** Comma-separated states, e.g. "QUEUED,PUBLISHED". */
  state?: string;
  platform?: string;
  account_id?: string;
  since?: string;
  until?: string;
  limit?: number;
}

export interface FunnelParams {
  c_unit: number;
  ctr: number;
  cvr: number;
  aov: number;
  r: number;
}
export interface SimulateParams extends FunnelParams {
  median: number;
  sigma: number;
  n: number;
}

/** One function per contract endpoint. */
export const api = {
  me: (signal?: AbortSignal) => request("GET", "/me", Me, { signal }),

  /* members & login (account.py) — the session is an HttpOnly cookie; the console never sees it */
  authConfig: (signal?: AbortSignal) => request("GET", "/auth/config", AuthConfig, { signal }),
  login: (email: string, password: string) => request("POST", "/auth/login", Me, { body: { email, password } }),
  signup: (body: { email: string; password: string; name?: string }) =>
    request("POST", "/auth/signup", SignupResult, { body }),
  logout: () => request("POST", "/auth/logout", OkResponse, { body: {} }),
  changePassword: (current: string, next: string) =>
    request("POST", "/auth/password", OkResponse, { body: { current, new: next } }),
  members: (signal?: AbortSignal) => request("GET", "/users", MemberList, { signal }),
  createMember: (body: { email: string; password: string; name?: string; role: string }) =>
    request("POST", "/users", Member, { body }),
  updateMember: (id: string, body: { role?: string; status?: string; name?: string }) =>
    request("PATCH", `/users/${enc(id)}`, Member, { body }),
  deleteMember: (id: string) => request("DELETE", `/users/${enc(id)}`, OkResponse),

  submitCommand: (text: string) => request("POST", "/commands", CommandResponse, { body: { text } }),

  runs: (limit = 50, signal?: AbortSignal) => request("GET", "/runs", RunList, { query: { limit }, signal }),
  run: (id: string, signal?: AbortSignal) => request("GET", `/runs/${enc(id)}`, RunDetail, { signal }),
  approveRun: (id: string) => request("POST", `/runs/${enc(id)}/approve`, Ack, { body: {} }),
  rejectRun: (id: string, reason: string) => request("POST", `/runs/${enc(id)}/reject`, Ack, { body: { reason } }),

  holds: (signal?: AbortSignal) => request("GET", "/holds", HoldList, { signal }),
  holdDecision: (id: string, approve: boolean, note?: string) =>
    request("POST", `/holds/${enc(id)}/decision`, DecisionResponse, {
      body: note ? { approve, note } : { approve },
    }),

  money: (days = 30, signal?: AbortSignal) =>
    request("GET", "/dashboards/money", MoneyDashboard, { query: { days }, signal }),
  pipeline: (signal?: AbortSignal) => request("GET", "/dashboards/pipeline", PipelineDashboard, { signal }),
  risk: (signal?: AbortSignal) => request("GET", "/dashboards/risk", RiskDashboard, { signal }),
  learning: (signal?: AbortSignal) => request("GET", "/dashboards/learning", LearningDashboard, { signal }),

  accounts: (signal?: AbortSignal) => request("GET", "/accounts", AccountList, { signal }),
  createAccount: (body: AccountCreate) => request("POST", "/accounts", Ack, { body }),
  patchAccount: (id: string, body: AccountPatch) => request("PATCH", `/accounts/${enc(id)}`, Ack, { body }),

  schedules: (signal?: AbortSignal) => request("GET", "/schedules", ScheduleList, { signal }),
  createSchedule: (text: string, cron: string) => request("POST", "/schedules", Ack, { body: { text, cron } }),
  deleteSchedule: (id: string) => request("DELETE", `/schedules/${enc(id)}`, Ack),

  manualQueue: (signal?: AbortSignal) => request("GET", "/manual-queue", ManualQueue, { signal }),
  manualDone: (publicationId: string, permalink: string) =>
    request("POST", `/manual-queue/${enc(publicationId)}/done`, Ack, { body: { permalink } }),

  rules: (signal?: AbortSignal) => request("GET", "/rules", RuleSet, { signal }),
  audit: (limit = 200, signal?: AbortSignal) => request("GET", "/audit", AuditList, { query: { limit }, signal }),

  breakeven: (p: FunnelParams, signal?: AbortSignal) =>
    request("GET", "/quant/breakeven", Breakeven, { query: { ...p }, signal }),
  simulate: (p: SimulateParams, signal?: AbortSignal) =>
    request("GET", "/quant/simulate", Simulation, { query: { ...p }, signal }),

  /* operations (ops.py) */
  systemStatus: (signal?: AbortSignal) => request("GET", "/system/status", SystemStatus, { signal }),
  setChecklist: (item: string, done: boolean) =>
    request("POST", "/system/checklist", ChecklistResponse, { body: { item, done } }),
  /** Write-only: the value is sent once and never stored or echoed by the console. */
  putSecret: (name: string, value: string) =>
    request("PUT", `/system/secrets/${enc(name)}`, SecretPutResponse, { body: { value }, timeoutMs: 60_000 }),
  deleteSecret: (name: string) => request("DELETE", `/system/secrets/${enc(name)}`, SecretDeleteResponse),
  integrations: (signal?: AbortSignal) => request("GET", "/system/integrations", IntegrationList, { signal }),
  verifyIntegration: (name: string) =>
    request("POST", `/system/integrations/${enc(name)}/verify`, Integration, { body: {}, timeoutMs: 60_000 }),
  setIntegrationEnabled: (name: string, enabled: boolean) =>
    request("PUT", `/system/integrations/${enc(name)}`, Integration, { body: { enabled } }),
  remapVoices: () =>
    request("POST", "/system/integrations/elevenlabs/voices", VoiceRemapResponse, { body: {}, timeoutMs: 60_000 }),
  workspace: (signal?: AbortSignal) => request("GET", "/workspace", Workspace, { signal }),
  patchWorkspace: (body: { name?: string; daily_budget_krw?: number }) =>
    request("PATCH", "/workspace", Workspace, { body }),
  /** Write-only platform token (raw access token or JSON bundle). */
  putCredentials: (accountId: string, token: string, platformUserId?: string) =>
    request("PUT", `/accounts/${enc(accountId)}/credentials`, CredentialsPutResponse, {
      body: platformUserId ? { token, platform_user_id: platformUserId } : { token },
    }),
  publications: (q: PublicationQuery, signal?: AbortSignal) =>
    request("GET", "/publications", PublicationList, { query: { ...q }, signal }),
  takedown: (id: string, reason: string) =>
    request("POST", `/publications/${enc(id)}/takedown`, TakedownResponse, { body: { reason } }),
  content: (id: string, signal?: AbortSignal) => request("GET", `/contents/${enc(id)}`, ContentDossier, { signal }),
  alerts: (limit = 50, signal?: AbortSignal) => request("GET", "/alerts", AlertsFeed, { query: { limit }, signal }),

  /* shorts studio (studio.py) */
  studioOptions: (signal?: AbortSignal) => request("GET", "/studio/options", StudioOptions, { signal }),
  studioProducts: (q: { category?: string; q?: string; refresh?: boolean; limit?: number }, signal?: AbortSignal) =>
    request("GET", "/studio/products", StudioProductList, { query: { ...q }, signal, timeoutMs: 60_000 }),
  createDraft: (body: DraftCreate) => request("POST", "/studio/drafts", DraftView, { body, timeoutMs: 120_000 }),
  draft: (id: string, signal?: AbortSignal) => request("GET", `/studio/drafts/${enc(id)}`, DraftView, { signal }),
  updateDraft: (id: string, body: DraftEdit) => request("PUT", `/studio/drafts/${enc(id)}`, DraftView, { body }),
  lint: (productId: number, text: string, narrations?: string[] | null, signal?: AbortSignal) =>
    request("POST", "/studio/lint", LintResult, {
      body: narrations ? { product_id: productId, text, narrations } : { product_id: productId, text },
      signal,
    }),
  renderDraft: (id: string) => request("POST", `/studio/drafts/${enc(id)}/render`, RenderAck, { body: {} }),
  publishDraft: (id: string, targets?: string[]) =>
    request("POST", `/studio/drafts/${enc(id)}/publish`, StudioPublishResult, {
      body: targets?.length ? { targets: targets.map((account_id) => ({ account_id })) } : {},
      timeoutMs: 180_000,
    }),
  autoStudio: (body: { product_id: number; kind: "short" | "text"; persona?: string; targets?: string[] }) =>
    request("POST", "/studio/auto", StudioPublishResult, {
      body: {
        product_id: body.product_id,
        kind: body.kind,
        persona: body.persona,
        targets: body.targets?.length ? body.targets.map((account_id) => ({ account_id })) : undefined,
      },
      timeoutMs: 240_000,
    }),
  studioAssets: (productId?: number | null, signal?: AbortSignal) =>
    request("GET", "/studio/assets", StudioAssetList, { query: { product_id: productId ?? undefined }, signal }),
  cutAsset: (id: string, start: number, end: number) =>
    request("POST", `/studio/assets/${enc(id)}/cut`, StudioAsset, { body: { start, end }, timeoutMs: 120_000 }),
  imageToVideo: (id: string, motion: string, seconds: number, engine: "camera" | "ai" = "camera") =>
    request("POST", `/studio/assets/${enc(id)}/i2v`, StudioAsset, {
      body: { motion, seconds, engine },
      timeoutMs: engine === "ai" ? 600_000 : 120_000,
    }),
  transcribe: (id: string) =>
    request("POST", `/studio/assets/${enc(id)}/transcribe`, TranscribeResult, { body: {}, timeoutMs: 180_000 }),
};
