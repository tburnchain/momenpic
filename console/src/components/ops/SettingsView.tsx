"use client";

import Link from "next/link";
import { useState } from "react";
import { SettingsDialog } from "@/components/shell/SettingsDialog";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader, DefList } from "@/components/ui/Card";
import { ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Field, TextInput } from "@/components/ui/Field";
import { Meter, PageHeader, StatTile } from "@/components/ui/misc";
import { useToast } from "@/components/ui/Toast";
import { krw, pct } from "@/lib/format";
import { usePatchWorkspace, usePrincipal, useWorkspace } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { Workspace } from "@/lib/schemas";

function WorkspaceForm({ ws }: { ws: Workspace }) {
  const patch = usePatchWorkspace();
  const toast = useToast();
  const [name, setName] = useState(ws.name);
  const [budget, setBudget] = useState(String(Math.round(ws.daily_budget_krw)));
  const budgetNum = Number(budget);
  const budgetOk = Number.isFinite(budgetNum) && budgetNum >= 0 && budgetNum <= 10_000_000;
  const nameOk = name.trim().length >= 1 && name.trim().length <= 80;
  const dirty = name.trim() !== ws.name || budgetNum !== Math.round(ws.daily_budget_krw);

  return (
    <form
      className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_14rem_auto] sm:items-start"
      onSubmit={(e) => {
        e.preventDefault();
        if (!budgetOk || !nameOk || !dirty) return;
        const body: { name?: string; daily_budget_krw?: number } = {};
        if (name.trim() !== ws.name) body.name = name.trim();
        if (budgetNum !== Math.round(ws.daily_budget_krw)) body.daily_budget_krw = budgetNum;
        patch.mutate(body, { onSuccess: () => toast("워크스페이스 설정을 저장했습니다") });
      }}
    >
      <Field label="워크스페이스 이름" htmlFor="ws-name" error={nameOk ? null : "1~80자"}>
        <TextInput id="ws-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} aria-invalid={!nameOk} />
      </Field>
      <Field label="일 예산 (KRW)" htmlFor="ws-budget" error={budgetOk ? null : "0 ~ 10,000,000"} hint={budgetOk ? krw(budgetNum) : undefined}>
        <TextInput
          id="ws-budget"
          type="number"
          inputMode="numeric"
          min={0}
          max={10_000_000}
          step={1000}
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          aria-invalid={!budgetOk}
        />
      </Field>
      <Button type="submit" variant="primary" loading={patch.isPending} disabled={!budgetOk || !nameOk || !dirty} className="sm:mt-[26px]">
        저장
      </Button>
      {patch.error ? (
        <div className="sm:col-span-3">
          <ErrorBanner error={patch.error} compact />
        </div>
      ) : null}
    </form>
  );
}

export function SettingsView() {
  const q = useWorkspace();
  const { can, me } = usePrincipal();
  const admin = can("admin");
  const [dialog, setDialog] = useState(false);
  const ws = q.data;
  const frac = ws && ws.daily_budget_krw > 0 ? ws.today_spent_krw / ws.daily_budget_krw : 0;

  return (
    <>
      <PageHeader
        title="설정"
        description="워크스페이스 이름과 일 예산. 명령별 예산 상한과 별개로, 일 예산을 넘으면 남은 생산이 일시정지됩니다 (§5.2)."
        actions={
          <Button icon="settings" onClick={() => setDialog(true)}>
            연결 · 표시 설정
          </Button>
        }
      />
      {q.isPending ? (
        <Card>
          <CardBody className="pt-5">
            <LoadingBlock rows={6} />
          </CardBody>
        </Card>
      ) : q.error ? (
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : ws ? (
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-line bg-surface p-4 shadow-card">
              <p className="text-[13px] text-muted">오늘 지출 / 일 예산</p>
              <p className="mt-1.5 text-xl font-semibold tracking-tight sm:text-2xl">
                {krw(ws.today_spent_krw)} <span className="text-base font-normal text-muted">/ {krw(ws.daily_budget_krw)}</span>
              </p>
              <Meter className="mt-2.5" value={ws.today_spent_krw} max={ws.daily_budget_krw} label="오늘 지출 대비 일 예산" valueText={pct(frac, 0)} />
              <p className="mt-1.5 text-xs text-muted">{pct(frac, 0)} 사용 · KST 자정 초기화</p>
            </div>
            <StatTile label="최근 30일 지출" value={krw(ws.last30d_spent_krw)} sub="생산 변동비 합계 (LLM·TTS·렌더)" icon="calculator" />
            <StatTile label="플랜" value={ws.plan ?? "—"} sub={<span className="font-mono">{ws.id}</span>} icon="grid" />
          </div>

          <Card aria-labelledby="ws-form">
            <CardHeader id="ws-form" title="워크스페이스" description="변경 사항은 감사 로그에 기록됩니다." />
            <CardBody>
              {admin ? (
                <WorkspaceForm key={`${ws.name}-${ws.daily_budget_krw}`} ws={ws} />
              ) : (
                <>
                  <DefList items={[["이름", ws.name], ["일 예산", krw(ws.daily_budget_krw)]]} />
                  <Notice className="mt-3">설정 변경은 {ALLOWED_ROLES.admin}만 할 수 있습니다{me ? ` (현재 역할: ${me.role})` : ""}.</Notice>
                </>
              )}
            </CardBody>
          </Card>

          <Card aria-labelledby="ws-links">
            <CardHeader id="ws-links" title="관련 설정" />
            <CardBody className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
              <Link href="/setup" className="text-accent-ink hover:underline">
                운영 준비 — 비밀값 · G0 체크리스트
              </Link>
              <Link href="/accounts" className="text-accent-ink hover:underline">
                계정 — 토큰 · 일일 상한
              </Link>
              <Link href="/rules" className="text-accent-ink hover:underline">
                규칙 — M7 게이트 rule_set
              </Link>
            </CardBody>
          </Card>
        </div>
      ) : null}
      <SettingsDialog open={dialog} onClose={() => setDialog(false)} />
    </>
  );
}
