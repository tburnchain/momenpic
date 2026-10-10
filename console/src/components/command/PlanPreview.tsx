"use client";

import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Dialog } from "@/components/ui/Dialog";
import { ErrorBanner, Notice } from "@/components/ui/Feedback";
import { Field, TextArea } from "@/components/ui/Field";
import { Icon } from "@/components/ui/Icon";
import { Meter } from "@/components/ui/misc";
import { Pill, PlatformTag, RunStatusPill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { describeCron } from "@/lib/cron";
import { hourWindow, krw, pct, shortId } from "@/lib/format";
import { KIND_LABEL, formatLabel, intentLabel, placementClickable, placementLabel, scheduleLabel } from "@/lib/labels";
import { useRunDecision, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { Approval, Plan, RunStatus } from "@/lib/schemas";
import { DagView } from "./DagView";

function SlotChip({ name, value, fallback }: { name: string; value: ReactNode; fallback?: string }) {
  const empty = value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length);
  return (
    <div className="rounded-lg border border-line bg-surface-2/70 px-3 py-2">
      <div className="font-mono text-[11px] text-muted">{name}</div>
      <div className={`mt-0.5 text-sm ${empty ? "text-muted" : "font-medium text-ink"}`}>{empty ? (fallback ?? "기본값") : value}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <h3 className="mb-2 text-[13px] font-semibold text-ink-2">{title}</h3>
      {children}
    </div>
  );
}

export function RejectDialog({
  open,
  onClose,
  onSubmit,
  pending,
  error,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => void;
  pending: boolean;
  error: unknown;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="계획 반려"
      description="반려 사유는 감사 로그에 기록되고 명령 작성자에게 표시됩니다."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            취소
          </Button>
          <Button variant="danger" icon="x" loading={pending} disabled={!reason.trim()} onClick={() => onSubmit(reason.trim())}>
            반려
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (reason.trim()) onSubmit(reason.trim());
        }}
        className="space-y-3"
      >
        <Field label="반려 사유 (필수)" htmlFor="reject-reason">
          <TextArea id="reject-reason" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="예: 차량 계정 쿨다운 해제 후 다시 요청" autoFocus required />
        </Field>
        {error ? <ErrorBanner error={error} compact /> : null}
      </form>
    </Dialog>
  );
}

/** Approve / reject controls for a plan that requires console approval. RBAC-aware. */
export function PlanDecision({ runId, status, approval }: { runId: string; status: RunStatus; approval: Approval }) {
  const { can, role } = usePrincipal();
  const toast = useToast();
  const { approve, reject } = useRunDecision(runId);
  const [rejectOpen, setRejectOpen] = useState(false);

  if (approval.mode !== "console" || status !== "awaiting_approval") return null;

  if (!can("approvePlan")) {
    return (
      <Notice tone="warn" title="콘솔 승인 대기">
        이 계획은 {ALLOWED_ROLES.approvePlan}만 승인할 수 있습니다{role ? ` (현재 역할: ${role})` : ""}.
      </Notice>
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-warn/40 bg-warn-soft p-3 sm:flex-row sm:items-center sm:justify-between">
      <p className="flex items-center gap-2 text-sm font-medium text-ink">
        <Icon name="alert" size={16} className="text-warn-ink" />
        자동 승인 조건 불충족 — 검토 후 결정하세요.
      </p>
      <div className="flex gap-2">
        <Button variant="secondary" icon="x" onClick={() => setRejectOpen(true)} disabled={approve.isPending}>
          반려
        </Button>
        <Button
          variant="success"
          icon="check"
          loading={approve.isPending}
          onClick={() =>
            approve.mutate(undefined, {
              onSuccess: () => toast("계획을 승인했습니다 — 생산을 시작합니다"),
              onError: () => toast("승인하지 못했습니다", "critical"),
            })
          }
        >
          승인하고 실행
        </Button>
      </div>
      {approve.error ? <ErrorBanner error={approve.error} compact className="sm:basis-full" /> : null}
      <RejectDialog
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        pending={reject.isPending}
        error={reject.error}
        onSubmit={(reason) =>
          reject.mutate(reason, {
            onSuccess: () => {
              setRejectOpen(false);
              toast("계획을 반려했습니다", "info");
            },
          })
        }
      />
    </div>
  );
}

export function PlanPreview({
  plan,
  status,
  runId,
  explanation,
  headerExtra,
}: {
  plan: Plan;
  status: RunStatus;
  runId: string;
  explanation?: string | null;
  headerExtra?: ReactNode;
}) {
  const s = plan.slots;
  const capFrac = plan.budget_cap_krw ? plan.est_cost_krw / plan.budget_cap_krw : null;
  const hooks = Array.isArray(plan.hooks) ? plan.hooks.join(", ") : plan.hooks;
  const text = explanation ?? plan.explanation;

  return (
    <Card aria-labelledby="plan-title">
      <CardHeader
        id="plan-title"
        title={
          <span className="flex flex-wrap items-center gap-2">
            실행 계획 <span className="font-mono text-xs font-normal text-muted">run {shortId(runId)}</span>
          </span>
        }
        actions={
          <>
            <RunStatusPill status={status} />
            {plan.approval.mode === "auto" ? (
              <Pill tone="good" icon="check">
                자동 승인
              </Pill>
            ) : (
              <Pill tone="warn" icon="alert">
                콘솔 승인 필요
              </Pill>
            )}
            {headerExtra}
          </>
        }
      />
      <CardBody className="space-y-6">
        {text ? <p className="max-w-4xl text-[15px] leading-relaxed text-ink">{text}</p> : null}

        <PlanDecision runId={runId} status={status} approval={plan.approval} />

        <Section title="슬롯 해석">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8">
            <SlotChip name="intent" value={intentLabel(s.intent ?? plan.intent)} />
            <SlotChip name="category" value={s.category} fallback="계정 니치" />
            <SlotChip name="count" value={s.count ? `${s.count}개` : null} fallback="5개" />
            <SlotChip name="kind" value={s.kind ? (KIND_LABEL[s.kind] ?? s.kind) : null} fallback="채널 기본" />
            <SlotChip name="persona" value={s.persona} fallback="계정 기본" />
            <SlotChip
              name="platforms"
              value={s.platforms.length ? s.platforms.map((p) => <PlatformTag key={p} platform={p} className="mr-2 text-[13px]" />) : null}
              fallback="M10 추천"
            />
            <SlotChip name="schedule" value={scheduleLabel(s.schedule)} fallback="시간대 분산" />
            <SlotChip name="budget" value={s.budget_krw ? krw(s.budget_krw) : null} fallback="워크스페이스 일 예산" />
          </div>
        </Section>

        <div className="grid gap-6 lg:grid-cols-3">
          <Section title="생산">
            <ul className="space-y-1.5 text-sm">
              <li>
                <span className="text-muted">상품</span>{" "}
                <span className="font-semibold">{plan.count}개</span>
                {plan.min_pos !== null && plan.min_pos !== undefined ? <span className="text-muted"> · POS ≥ {plan.min_pos}</span> : null}
                {plan.niche ? <span className="text-muted"> · {plan.niche}</span> : null}
              </li>
              <li>
                <span className="text-muted">형식</span> {KIND_LABEL[plan.kind] ?? plan.kind}
                {plan.kind === "short" && plan.length_sec ? ` ${plan.length_sec}초` : ""}
                {hooks ? <span className="text-muted"> · 훅 {hooks}안</span> : null}
              </li>
              {plan.persona ? (
                <li>
                  <span className="text-muted">페르소나</span> {plan.persona}
                </li>
              ) : null}
              {plan.template_id ? (
                <li>
                  <span className="text-muted">템플릿</span> <span className="font-mono text-[13px]">{plan.template_id}</span>
                </li>
              ) : null}
              {plan.voice_id && plan.kind === "short" ? (
                <li>
                  <span className="text-muted">보이스</span> <span className="font-mono text-[13px]">{plan.voice_id}</span>
                </li>
              ) : null}
              <li>
                <span className="text-muted">검수</span> M7 게이트 · rule_set <span className="font-mono text-[13px]">{plan.rule_set ?? "v1"}</span>
              </li>
            </ul>
          </Section>

          <Section title="스케줄">
            <ul className="space-y-1.5 text-sm">
              <li>
                <span className="text-muted">게시 시간대</span> {hourWindow(plan.schedule_window)}
              </li>
              <li>
                <span className="text-muted">지터</span> ±{plan.jitter_minutes ?? 0}분 (정각 회피)
              </li>
              <li>
                <span className="text-muted">반복</span>{" "}
                {plan.recurring_cron ? (
                  <>
                    {describeCron(plan.recurring_cron)} <span className="font-mono text-xs text-muted">({plan.recurring_cron})</span>
                  </>
                ) : (
                  "1회 실행"
                )}
              </li>
            </ul>
          </Section>

          <Section title="비용">
            <p className="text-sm">
              <span className="text-muted">추정</span> <span className="text-lg font-semibold">{krw(plan.est_cost_krw)}</span>
              <span className="text-muted"> / 상한 {krw(plan.budget_cap_krw)}</span>
            </p>
            <Meter
              className="mt-2"
              value={plan.est_cost_krw}
              max={plan.budget_cap_krw}
              label="추정 비용 대비 예산 상한"
              valueText={`${krw(plan.est_cost_krw)} / ${krw(plan.budget_cap_krw)}`}
            />
            <p className="mt-1 text-xs text-muted">
              {capFrac !== null ? `상한의 ${pct(capFrac, 0)} · ` : ""}실행 중 누적 비용이 상한에 닿으면 남은 상품은 일시정지됩니다.
            </p>
          </Section>
        </div>

        <Section title={`배포 대상 (${plan.targets.length})`}>
          {plan.targets.length ? (
            <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {plan.targets.map((t) => {
                const clickable = placementClickable(t.link_placement);
                return (
                  <li key={`${t.platform}-${t.account_id}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2.5">
                    <PlatformTag platform={t.platform} className="font-medium" />
                    <span className="text-sm text-muted">{t.account_handle}</span>
                    <span className="ml-auto flex flex-wrap gap-1.5">
                      <Pill tone="neutral">{formatLabel(t.format)}</Pill>
                      <Pill tone={clickable ? "good" : "warn"} icon={clickable ? "link" : "external"} title={clickable ? "게시물 안에서 링크 클릭 가능" : "링크 클릭 불가 — 허브/프로필 경유"}>
                        {placementLabel(t.link_placement)}
                      </Pill>
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-sm text-muted">게시 대상 없음 (생산 또는 분석만 수행)</p>
          )}
        </Section>

        <Section title={plan.approval.mode === "auto" ? "자동 승인 근거" : "콘솔 승인이 필요한 이유"}>
          <ul className="space-y-1 text-sm">
            {plan.approval.reasons.length ? (
              plan.approval.reasons.map((r) => (
                <li key={r} className="flex items-start gap-2">
                  <Icon
                    name={plan.approval.mode === "auto" ? "check" : "alert"}
                    size={15}
                    className={`mt-0.5 ${plan.approval.mode === "auto" ? "text-good-ink" : "text-warn-ink"}`}
                  />
                  {r}
                </li>
              ))
            ) : (
              <li className="text-muted">사유 없음</li>
            )}
          </ul>
        </Section>

        <Section title={`실행 DAG (${plan.steps.length}단계)`}>
          <DagView steps={plan.steps} />
        </Section>
      </CardBody>
    </Card>
  );
}
