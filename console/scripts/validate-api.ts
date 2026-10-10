/**
 * Contract smoke test against a LIVE backend: GETs every read endpoint and parses it with the same
 * zod schemas the console uses, printing any field that drifts from the contract.
 *
 *   ACVM_API=http://localhost:8000/api/v1 ACVM_TOKEN=<dev token> npm run validate:api
 *
 * Read-only: it never calls POST/PUT/PATCH/DELETE.
 */
import type { z } from "zod";
import * as S from "../src/lib/schemas";

const BASE = (process.env.ACVM_API ?? "http://localhost:8000/api/v1").replace(/\/+$/, "");
const TOKEN = process.env.ACVM_TOKEN ?? "";

let failures = 0;
let checks = 0;

async function check<T extends z.ZodType>(label: string, schema: T, path: string): Promise<z.output<T> | undefined> {
  checks++;
  try {
    const res = await fetch(`${BASE}${path}`, { headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {} });
    if (!res.ok) {
      failures++;
      console.error(`✗ ${label} → HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
      return undefined;
    }
    const parsed = schema.safeParse(await res.json());
    if (!parsed.success) {
      failures++;
      console.error(`✗ ${label}`);
      for (const i of parsed.error.issues.slice(0, 10)) console.error(`    ${i.path.join(".")}: ${i.message}`);
      return undefined;
    }
    console.log(`✓ ${label}`);
    return parsed.data as z.output<T>;
  } catch (e) {
    failures++;
    console.error(`✗ ${label} threw ${(e as Error).message}`);
    return undefined;
  }
}

async function main() {
  await check("/me", S.Me, "/me");
  const runs = await check("/runs", S.RunList, "/runs?limit=50");
  for (const r of (runs?.items ?? []).slice(0, 10)) await check(`/runs/${r.id.slice(0, 8)}`, S.RunDetail, `/runs/${r.id}`);
  await check("/holds", S.HoldList, "/holds");
  await check("/dashboards/money", S.MoneyDashboard, "/dashboards/money?days=30");
  await check("/dashboards/pipeline", S.PipelineDashboard, "/dashboards/pipeline");
  await check("/dashboards/risk", S.RiskDashboard, "/dashboards/risk");
  await check("/dashboards/learning", S.LearningDashboard, "/dashboards/learning");
  await check("/accounts", S.AccountList, "/accounts");
  await check("/schedules", S.ScheduleList, "/schedules");
  await check("/manual-queue", S.ManualQueue, "/manual-queue");
  await check("/rules", S.RuleSet, "/rules");
  await check("/audit", S.AuditList, "/audit?limit=200");
  await check("/quant/breakeven", S.Breakeven, "/quant/breakeven?c_unit=150&ctr=0.008&cvr=0.03&aov=30000&r=0.03");
  await check("/quant/simulate", S.Simulation, "/quant/simulate?median=600&sigma=2&c_unit=150&ctr=0.008&cvr=0.03&aov=30000&r=0.03&n=300");
  await check("/system/status", S.SystemStatus, "/system/status");
  await check("/system/integrations", S.IntegrationList, "/system/integrations");
  await check("/workspace", S.Workspace, "/workspace");
  const pubs = await check("/publications", S.PublicationList, "/publications?limit=200");
  await check("/alerts", S.AlertsFeed, "/alerts?limit=50");
  const ids = Array.from(new Set((pubs?.items ?? []).map((p) => p.content_id))).slice(0, 10);
  for (const id of ids) await check(`/contents/${id.slice(0, 8)}`, S.ContentDossier, `/contents/${id}`);
  console.log(`\n${checks - failures}/${checks} live responses match the console schemas (${BASE}).`);
  if (failures) process.exit(1);
}

void main();
