"use client";

import Link from "next/link";
import { useState, useSyncExternalStore } from "react";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Meter, PageHeader } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { kst, krw, pct } from "@/lib/format";
import { usePrincipal, usePutSecret, useSetChecklist, useSystemStatus } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import { INTEGRATION_SLOTS, type CheckStatus, type SystemStatus } from "@/lib/schemas";
import { SETUP_DISMISS_KEY, SETUP_NOTICE_KEY } from "@/lib/setup";
import { IntegrationsCard } from "./IntegrationsCard";

const STATUS_META: Record<CheckStatus, { label: string; icon: IconName; tone: "good" | "warn" | "critical"; text: string }> = {
  fail: { label: "실패", icon: "ban", tone: "critical", text: "text-critical-ink" },
  warn: { label: "주의", icon: "alert", tone: "warn", text: "text-warn-ink" },
  ok: { label: "정상", icon: "check", tone: "good", text: "text-good-ink" },
};

function noopSubscribe() {
  return () => {};
}

function readNotice(): string | null {
  try {
    return window.sessionStorage.getItem(SETUP_NOTICE_KEY);
  } catch {
    return null;
  }
}

/** Shown once after the owner was auto-redirected here from the command screen. */
function RedirectNotice({ fails }: { fails: number }) {
  const flagged = useSyncExternalStore(noopSubscribe, readNotice, () => null);
  const [hidden, setHidden] = useState(false);
  if (!flagged || hidden) return null;
  const close = (forever: boolean) => {
    try {
      window.sessionStorage.removeItem(SETUP_NOTICE_KEY);
      if (forever) window.localStorage.setItem(SETUP_DISMISS_KEY, "1");
    } catch {
      /* ignore */
    }
    setHidden(true);
  };
  return (
    <div className="mb-5 flex flex-col gap-3 rounded-xl border border-critical/30 bg-critical-soft p-4 sm:flex-row sm:items-center sm:justify-between">
      <p className="flex items-start gap-2 text-sm text-ink">
        <Icon name="alert" size={16} className="mt-0.5 text-critical-ink" />
        실패한 준비 항목이 {fails}개 있어 운영 준비 화면으로 이동했습니다. 해결 전까지 링크 생성·게시가 막힐 수 있습니다.
      </p>
      <div className="flex shrink-0 gap-2">
        <Button size="sm" variant="ghost" onClick={() => close(true)}>
          다시 이동하지 않기
        </Button>
        <ButtonLink size="sm" href="/" onClick={() => close(false)}>
          명령 화면으로
        </ButtonLink>
      </div>
    </div>
  );
}

function ModeChips({ mode }: { mode: SystemStatus["mode"] }) {
  const chips: Array<{ label: string; value: string; ok: boolean }> = [
    { label: "환경", value: mode.env ?? "—", ok: mode.env === "prod" || mode.env === "production" },
    { label: "오케스트레이터", value: mode.orchestrator ?? "—", ok: mode.orchestrator === "temporal" },
    { label: "쿠팡", value: mode.coupang_sandbox ? "샌드박스" : "실서비스", ok: mode.coupang_sandbox === false },
    { label: "게시", value: mode.platform_dry_run ? "드라이런" : "실게시", ok: mode.platform_dry_run === false },
    { label: "LLM", value: mode.llm_backend ?? "—", ok: mode.llm_backend === "anthropic" },
    { label: "인증", value: mode.auth_mode ?? "—", ok: mode.auth_mode === "cloudflare_access" },
    { label: "TikTok 감사", value: mode.tiktok_audited ? "통과" : "미통과", ok: !!mode.tiktok_audited },
  ];
  return (
    <ul className="flex flex-wrap gap-2" aria-label="실행 모드">
      {chips.map((c) => (
        <li key={c.label} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[13px]">
          <span className={`h-1.5 w-1.5 rounded-full ${c.ok ? "bg-good" : "bg-warn"}`} aria-hidden />
          <span className="text-muted">{c.label}</span>
          <span className="font-medium text-ink">{c.value}</span>
        </li>
      ))}
    </ul>
  );
}

function ChecksCard({ checks }: { checks: SystemStatus["checks"] }) {
  const groups: CheckStatus[] = ["fail", "warn", "ok"];
  return (
    <Card aria-labelledby="setup-checks">
      <CardHeader id="setup-checks" title="준비 상태 점검" description="통합 연결 · 툴체인 · 보안 설정. 실패 항목은 해당 기능을 막습니다." />
      <CardBody className="space-y-5">
        {groups.map((g) => {
          const items = checks.filter((c) => c.status === g);
          if (!items.length) return null;
          const meta = STATUS_META[g];
          const list = (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {items.map((c) => (
                <li key={c.id} className="flex items-start gap-3 px-3 py-2.5">
                  <Icon name={meta.icon} size={16} className={`mt-0.5 ${meta.text}`} />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink">{c.label}</p>
                    {c.detail ? <p className="break-all text-[13px] text-ink-2">{c.detail}</p> : null}
                    {c.fix ? (
                      <p className="mt-1 flex items-start gap-1.5 text-xs text-muted">
                        <span className="shrink-0 font-semibold text-ink-2">해결</span>
                        <span className="break-all font-mono">{c.fix}</span>
                      </p>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          );
          return g === "ok" ? (
            <details key={g} className="group">
              <summary className="mb-2 cursor-pointer list-none text-[13px] font-semibold text-ink-2 [&::-webkit-details-marker]:hidden">
                <Icon name="chevronRight" size={13} className="mr-1 inline transition-transform group-open:rotate-90" />
                {meta.label} {items.length}
              </summary>
              {list}
            </details>
          ) : (
            <section key={g} aria-label={`${meta.label} ${items.length}개`}>
              <h3 className={`mb-2 text-[13px] font-semibold ${meta.text}`}>
                {meta.label} {items.length}
              </h3>
              {list}
            </section>
          );
        })}
      </CardBody>
    </Card>
  );
}

function G0Card({ g0, canEdit }: { g0: SystemStatus["g0"]; canEdit: boolean }) {
  const set = useSetChecklist();
  const toast = useToast();
  return (
    <Card aria-labelledby="setup-g0">
      <CardHeader
        id="setup-g0"
        title="G0 출시 게이트"
        description="§8.3 법·정책 체크리스트 + 쿠팡 실 API 연결"
        actions={
          g0.passed ? (
            <Pill tone="good" icon="check">
              G0 통과
            </Pill>
          ) : (
            <Pill tone="critical" icon="ban">
              G0 미통과
            </Pill>
          )
        }
      />
      <CardBody>
        <ul className="space-y-2">
          {g0.items.map((it) => {
            const auto = it.id === "coupang_api";
            const id = `g0-${it.id}`;
            const pending = set.isPending && set.variables?.item === it.id;
            return (
              <li key={it.id} className="flex items-start gap-3 rounded-lg border border-line px-3 py-2.5">
                <input
                  id={id}
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]"
                  checked={it.done}
                  disabled={auto || !canEdit || pending}
                  onChange={(e) =>
                    set.mutate(
                      { item: it.id, done: e.target.checked },
                      { onError: () => toast("체크리스트를 저장하지 못했습니다", "critical") },
                    )
                  }
                />
                <label htmlFor={id} className="min-w-0 flex-1 text-sm text-ink">
                  {it.label}
                  {auto ? <span className="mt-0.5 block text-xs text-muted">자동 판정 — 쿠팡 키 등록 + 샌드박스 해제 시 완료</span> : null}
                </label>
                {it.done ? <Icon name="check" size={16} className="text-good-ink" label="완료" /> : null}
              </li>
            );
          })}
        </ul>
        {!canEdit ? <p className="mt-3 text-xs text-muted">체크리스트 변경은 {ALLOWED_ROLES.admin}만 할 수 있습니다.</p> : null}
        {set.error ? <ErrorBanner error={set.error} compact className="mt-3" /> : null}
      </CardBody>
    </Card>
  );
}

function SecretRow({ slot, canEdit }: { slot: SystemStatus["secrets"][number]; canEdit: boolean }) {
  const put = usePutSecret();
  const toast = useToast();
  const [value, setValue] = useState("");
  const [volatile, setVolatile] = useState(false);
  const id = `secret-${slot.name}`;
  const tooShort = value.length > 0 && value.length < 8;

  return (
    <li className="flex flex-col gap-2 px-3 py-3 lg:flex-row lg:items-center">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
          {slot.label}
          {slot.configured ? (
            <Pill tone="good" icon="check">
              설정됨
            </Pill>
          ) : (
            <Pill tone="critical" icon="ban">
              미설정
            </Pill>
          )}
        </p>
        <p className="font-mono text-[11px] text-muted">{slot.name}</p>
        {volatile ? <p className="mt-1 text-xs text-warn-ink">Vault 미연결: 재시작 시 사라집니다</p> : null}
      </div>
      {canEdit ? (
        <form
          className="flex w-full gap-2 lg:w-[360px]"
          onSubmit={(e) => {
            e.preventDefault();
            if (value.length < 8) return;
            const v = value;
            setValue(""); // write-only: never keep the secret around after submit
            put.mutate(
              { name: slot.name, value: v },
              {
                onSuccess: (r) => {
                  setVolatile(!r.persistent);
                  toast(r.persistent ? `${slot.label} 저장됨` : `${slot.label} 저장됨 — Vault 미연결: 재시작 시 사라집니다`, r.persistent ? "good" : "info");
                },
                onError: () => toast(`${slot.label} 저장 실패`, "critical"),
              },
            );
          }}
        >
          <label htmlFor={id} className="sr-only">
            {slot.label} 값
          </label>
          <input
            id={id}
            type="password"
            autoComplete="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={slot.configured ? "새 값으로 교체 (8자 이상)" : "값 입력 (8자 이상)"}
            aria-invalid={tooShort}
            className="h-9 min-w-0 flex-1 rounded-lg border border-line-strong bg-surface px-3 font-mono text-sm text-ink placeholder:font-sans placeholder:text-muted focus:border-accent focus:outline-none aria-[invalid=true]:border-critical"
          />
          <Button type="submit" size="sm" variant="primary" loading={put.isPending} disabled={value.length < 8}>
            저장
          </Button>
        </form>
      ) : null}
      {put.error ? <ErrorBanner error={put.error} compact className="lg:basis-full" /> : null}
    </li>
  );
}

function SecretsCard({ secrets, canEdit }: { secrets: SystemStatus["secrets"]; canEdit: boolean }) {
  const configured = secrets.filter((s) => s.configured).length;
  return (
    <Card aria-labelledby="setup-secrets">
      <CardHeader
        id="setup-secrets"
        title="비밀값"
        description="쓰기 전용 — 값은 비밀 저장소(Vault)로만 전송되고 콘솔에 다시 표시되지 않습니다 (§10)."
        actions={<Pill tone={configured === secrets.length ? "good" : "warn"}>{configured}/{secrets.length} 설정</Pill>}
      />
      <CardBody>
        {!canEdit ? <Notice className="mb-3">비밀값 등록은 {ALLOWED_ROLES.admin}만 할 수 있습니다. 설정 여부만 표시됩니다.</Notice> : null}
        <ul className="divide-y divide-line rounded-lg border border-line">
          {secrets.map((s) => (
            <SecretRow key={s.name} slot={s} canEdit={canEdit} />
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

function BudgetCard({ budget }: { budget: SystemStatus["budget"] }) {
  const frac = budget.daily_budget_krw > 0 ? budget.today_spent_krw / budget.daily_budget_krw : 0;
  return (
    <Card aria-labelledby="setup-budget">
      <CardHeader
        id="setup-budget"
        title="오늘 예산"
        actions={
          <Link href="/settings" className="text-[13px] text-accent-ink hover:underline">
            변경
          </Link>
        }
      />
      <CardBody>
        <p className="text-xl font-semibold tracking-tight">
          {krw(budget.today_spent_krw)} <span className="text-sm font-normal text-muted">/ {krw(budget.daily_budget_krw)}</span>
        </p>
        <Meter className="mt-2" value={budget.today_spent_krw} max={budget.daily_budget_krw} label="오늘 지출 대비 일 예산" valueText={pct(frac, 0)} />
        <p className="mt-1.5 text-xs text-muted">워크스페이스 일 예산의 {pct(frac, 0)} 사용 (KST 자정 기준)</p>
      </CardBody>
    </Card>
  );
}

function BreakersCard({ breakers }: { breakers: SystemStatus["breakers"] }) {
  if (!breakers.length) return null;
  return (
    <Card aria-labelledby="setup-breakers">
      <CardHeader id="setup-breakers" title="쿠팡 API 서킷 브레이커" description="연속 403 시 즉시 개방 · 1시간 후 단건 재시도 (§9.5)" />
      <CardBody>
        <ul className="space-y-2">
          {breakers.map((b) => (
            <li key={b.name} className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono text-[13px] text-ink">{b.name}</span>
              <Pill tone={b.state === "open" ? "critical" : b.state === "half_open" ? "warn" : "good"} dot>
                {b.state ?? "—"}
              </Pill>
              {b.failures ? <span className="text-xs text-muted">실패 {b.failures}회</span> : null}
              {b.opened_at ? <span className="text-xs text-muted">개방 {typeof b.opened_at === "string" ? kst(b.opened_at) : String(b.opened_at)}</span> : null}
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

export function SetupView() {
  const q = useSystemStatus();
  const { can } = usePrincipal();
  const admin = can("admin");
  const d = q.data;

  return (
    <>
      <PageHeader
        title="운영 준비"
        description="실서비스 전에 확인할 항목 — 통합 연결, 비밀값, G0 법·정책 게이트, 예산."
        actions={
          d ? (
            <span className="flex gap-1.5">
              <Pill tone="critical" icon="ban">
                실패 {d.summary.fail}
              </Pill>
              <Pill tone="warn" icon="alert">
                주의 {d.summary.warn}
              </Pill>
              <Pill tone="good" icon="check">
                정상 {d.summary.ok}
              </Pill>
            </span>
          ) : null
        }
      />
      {d ? <RedirectNotice fails={d.summary.fail} /> : null}
      {q.isPending ? (
        <Card>
          <CardBody className="pt-5">
            <LoadingBlock rows={10} />
          </CardBody>
        </Card>
      ) : q.error ? (
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : d ? (
        <div className="space-y-5">
          <ModeChips mode={d.mode} />
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
            <ChecksCard checks={d.checks} />
            <div className="space-y-5">
              <G0Card g0={d.g0} canEdit={admin} />
              <BudgetCard budget={d.budget} />
              <BreakersCard breakers={d.breakers} />
            </div>
          </div>
          <IntegrationsCard canEdit={admin} />
          <SecretsCard secrets={d.secrets.filter((s) => !INTEGRATION_SLOTS.has(s.name))} canEdit={admin} />
          {d.accounts.some((a) => a.credential === "missing") ? (
            <Notice tone="warn" title="토큰이 없는 계정이 있습니다">
              {d.accounts.filter((a) => a.credential === "missing").length}개 계정은 게시할 수 없습니다.{" "}
              <Link href="/accounts" className="text-accent-ink hover:underline">
                계정 화면에서 토큰 등록
              </Link>
            </Notice>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
