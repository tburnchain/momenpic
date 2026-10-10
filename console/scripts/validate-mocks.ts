/**
 * Validates every mock-mode response against the contract zod schemas.
 *   npm run validate:mocks
 * Exits non-zero (with the failing paths) if any fixture drifts from the contract.
 */
import type { z } from "zod";
import * as S from "../src/lib/schemas";
import { mockRequest, resetMockDb } from "../src/mocks/handler";
import type { HttpMethod, Query } from "../src/lib/api";

let failures = 0;
let checks = 0;

async function check<T extends z.ZodType>(label: string, schema: T, method: HttpMethod, path: string, query?: Query, body?: unknown) {
  checks++;
  try {
    const raw = await mockRequest(method, path, query, body);
    const res = schema.safeParse(raw);
    if (!res.success) {
      failures++;
      console.error(`✗ ${label}`);
      for (const issue of res.error.issues.slice(0, 10)) console.error(`    ${issue.path.join(".")}: ${issue.message}`);
      return undefined;
    }
    console.log(`✓ ${label}`);
    return res.data as z.output<T>;
  } catch (e) {
    failures++;
    console.error(`✗ ${label} threw`, e);
    return undefined;
  }
}

async function main() {
  resetMockDb();
  await check("GET /me", S.Me, "GET", "/me");
  const runs = await check("GET /runs", S.RunList, "GET", "/runs", { limit: 50 });
  for (const r of runs?.items ?? []) await check(`GET /runs/${r.id.slice(0, 8)}`, S.RunDetail, "GET", `/runs/${r.id}`);
  await check("GET /holds", S.HoldList, "GET", "/holds");
  for (const days of [7, 30, 90]) await check(`GET /dashboards/money?days=${days}`, S.MoneyDashboard, "GET", "/dashboards/money", { days });
  await check("GET /dashboards/pipeline", S.PipelineDashboard, "GET", "/dashboards/pipeline");
  await check("GET /dashboards/risk", S.RiskDashboard, "GET", "/dashboards/risk");
  await check("GET /dashboards/learning", S.LearningDashboard, "GET", "/dashboards/learning");
  await check("GET /accounts", S.AccountList, "GET", "/accounts");
  await check("GET /schedules", S.ScheduleList, "GET", "/schedules");
  await check("GET /manual-queue", S.ManualQueue, "GET", "/manual-queue");
  await check("GET /rules", S.RuleSet, "GET", "/rules");
  await check("GET /audit", S.AuditList, "GET", "/audit", { limit: 200 });
  await check("GET /quant/breakeven", S.Breakeven, "GET", "/quant/breakeven", { c_unit: 150, ctr: 0.008, cvr: 0.03, aov: 30000, r: 0.03 });
  await check("GET /quant/simulate", S.Simulation, "GET", "/quant/simulate", {
    median: 600, sigma: 2, c_unit: 150, ctr: 0.008, cvr: 0.03, aov: 30000, r: 0.03, n: 300,
  });

  await check("GET /system/status", S.SystemStatus, "GET", "/system/status");
  await check("GET /auth/config", S.AuthConfig, "GET", "/auth/config");
  await check("POST /auth/login", S.Me, "POST", "/auth/login", undefined, { email: "owner@acvm.local", password: "x".repeat(10) });
  await check("POST /auth/signup", S.SignupResult, "POST", "/auth/signup", undefined, { email: "trial@example.com", password: "Trial-Pass-1!" });
  await check("GET /users", S.MemberList, "GET", "/users");
  await check("PATCH /users/usr_new", S.Member, "PATCH", "/users/usr_new", undefined, { status: "active", role: "operator" });
  await check("POST /users", S.Member, "POST", "/users", undefined, { email: "made@example.com", password: "Made-Pass-1!", role: "viewer" });
  await check("GET /workspace", S.Workspace, "GET", "/workspace");
  const pubs = await check("GET /publications", S.PublicationList, "GET", "/publications", { limit: 500 });
  await check("GET /publications?state=QUEUED", S.PublicationList, "GET", "/publications", { state: "QUEUED", platform: "threads" });
  await check("GET /alerts", S.AlertsFeed, "GET", "/alerts", { limit: 50 });
  const contentIds = Array.from(new Set((pubs?.items ?? []).map((p) => p.content_id))).slice(0, 40);
  for (const id of contentIds) await check(`GET /contents/${id}`, S.ContentDossier, "GET", `/contents/${id}`);
  await check("POST /system/checklist", S.ChecklistResponse, "POST", "/system/checklist", undefined, { item: "ai_act", done: true });
  await check("PUT /system/secrets/link_signing_key", S.SecretPutResponse, "PUT", "/system/secrets/link_signing_key", undefined, { value: "x".repeat(32) });
  await check("GET /system/integrations", S.IntegrationList, "GET", "/system/integrations");
  await check("PUT /system/secrets/gemini_api_key", S.SecretPutResponse, "PUT", "/system/secrets/gemini_api_key", undefined, { value: "g".repeat(32) });
  await check("POST /system/integrations/gemini/verify", S.Integration, "POST", "/system/integrations/gemini/verify", undefined, {});
  await check("PUT /system/integrations/gemini", S.Integration, "PUT", "/system/integrations/gemini", undefined, { enabled: false });
  await check("POST /system/integrations/elevenlabs/voices", S.VoiceRemapResponse, "POST", "/system/integrations/elevenlabs/voices", undefined, {});
  await check("DELETE /system/secrets/gemini_api_key", S.SecretDeleteResponse, "DELETE", "/system/secrets/gemini_api_key");
  await check("PATCH /workspace", S.Workspace, "PATCH", "/workspace", undefined, { daily_budget_krw: 25000 });
  const published = pubs?.items.find((p) => p.state === "PUBLISHED" && p.platform === "instagram");
  if (published) await check("POST takedown (instagram)", S.TakedownResponse, "POST", `/publications/${published.id}/takedown`, undefined, { reason: "테스트 중지" });

  // Shorts Studio
  await check("GET /studio/options", S.StudioOptions, "GET", "/studio/options");
  const prods = await check("GET /studio/products", S.StudioProductList, "GET", "/studio/products", { category: "1013", limit: 10 });
  const pid = prods?.items[0]?.id ?? 7712001;
  const short = await check("POST /studio/drafts (short)", S.DraftView, "POST", "/studio/drafts", undefined, { product_id: pid, kind: "short" });
  await check("POST /studio/drafts (text)", S.DraftView, "POST", "/studio/drafts", undefined, { product_id: pid, kind: "text" });
  if (short) {
    await check("GET /studio/drafts/:id", S.DraftView, "GET", `/studio/drafts/${short.id}`);
    const edited = await check("PUT /studio/drafts/:id", S.DraftView, "PUT", `/studio/drafts/${short.id}`, undefined, {
      beats: [null, { narration: "이건 무조건 최고의 클립이에요." }],
      render_options: { caption_style: "box", watermark: "@kitchen_pick" },
    });
    if (edited && edited.lint.ok) console.error("  (expected lint errors for banned words)");
    await check("POST /studio/lint", S.LintResult, "POST", "/studio/lint", undefined, { product_id: pid, text: "세균 99% 제거되는 최고의 제품" });
    await check("POST /studio/drafts/:id/render", S.RenderAck, "POST", `/studio/drafts/${short.id}/render`, undefined, {});
    await check("POST /studio/drafts/:id/publish", S.StudioPublishResult, "POST", `/studio/drafts/${short.id}/publish`, undefined, {});
  }
  await check("POST /studio/auto", S.StudioPublishResult, "POST", "/studio/auto", undefined, { product_id: pid, kind: "short", targets: [{ account_id: "acc_kit_thr" }] });
  const assets = await check("GET /studio/assets", S.StudioAssetList, "GET", "/studio/assets", { product_id: pid });
  const img = assets?.items.find((x) => x.kind === "image");
  const vid = assets?.items.find((x) => x.kind === "video" && x.rights_source === "own_footage");
  if (img) await check("POST /studio/assets/:id/i2v", S.StudioAsset, "POST", `/studio/assets/${img.id}/i2v`, undefined, { motion: "zoom_in", seconds: 4 });
  if (vid) {
    await check("POST /studio/assets/:id/cut", S.StudioAsset, "POST", `/studio/assets/${vid.id}/cut`, undefined, { start: 0, end: 3 });
    await check("POST /studio/assets/:id/transcribe", S.TranscribeResult, "POST", `/studio/assets/${vid.id}/transcribe`, undefined, {});
  }

  for (const text of [
    "주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘",
    "매일 오전 9시에 차량용품 10개 글로 만들어서 스레드에 올려줘",
    "주방 카테고리 지난주 성과 분석해줘",
  ]) {
    const res = await check(`POST /commands "${text.slice(0, 18)}…"`, S.CommandResponse, "POST", "/commands", undefined, { text });
    if (res) await check(`  → GET /runs/${res.run_id.slice(0, 8)}`, S.RunDetail, "GET", `/runs/${res.run_id}`);
  }

  console.log(`\n${checks - failures}/${checks} mock responses match the contract schemas.`);
  if (failures) process.exit(1);
}

void main();
