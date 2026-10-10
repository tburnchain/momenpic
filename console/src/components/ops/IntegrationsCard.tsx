"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { kst } from "@/lib/format";
import type { Tone } from "@/lib/labels";
import {
  useDeleteSecret,
  useIntegrations,
  usePutSecret,
  useRemapVoices,
  useSetIntegrationEnabled,
  useStudioOptions,
  useVerifyIntegration,
} from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { Integration, IntegrationState } from "@/lib/schemas";

const STATE: Record<IntegrationState, { label: string; tone: Tone }> = {
  active: { label: "사용 중", tone: "good" },
  unverified: { label: "확인 전", tone: "info" },
  degraded: { label: "연결 불안정", tone: "warn" },
  error: { label: "키 오류", tone: "critical" },
  paused: { label: "일시 중지", tone: "neutral" },
  missing: { label: "미등록", tone: "neutral" },
};

const INPUT =
  "h-9 min-w-0 w-full rounded-lg border border-line-strong bg-surface px-3 font-mono text-sm text-ink placeholder:font-sans placeholder:text-muted focus:border-accent focus:outline-none aria-[invalid=true]:border-critical";

function summary(i: Integration): string {
  const head = `${i.label}: ${STATE[i.state].label}`;
  const acts = i.actions?.length ? ` — ${i.actions.join(" · ")}` : i.detail ? ` — ${i.detail}` : "";
  return head + acts;
}

function KeyForm({ item }: { item: Integration }) {
  const put = usePutSecret();
  const toast = useToast();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const filled = item.slots.filter((s) => (values[s.name] ?? "").length > 0);
  const tooShort = filled.some((s) => (values[s.name] ?? "").length < 8);
  // a multi-part key (Higgsfield id + secret) is saved together the first time; afterwards either part can rotate
  const needAll = item.slots.some((s) => !s.configured);
  const ready = filled.length > 0 && !tooShort && (!needAll || filled.length === item.slots.filter((s) => !s.configured).length);

  async function submit() {
    if (!ready) return;
    const batch = item.slots.filter((s) => (values[s.name] ?? "").length >= 8).map((s) => ({ name: s.name, value: values[s.name] }));
    setValues({}); // write-only: never keep a key in component state after submit
    setBusy(true);
    try {
      let last: Integration | null | undefined = null;
      for (const b of batch) last = (await put.mutateAsync(b)).integration;
      if (last) toast(summary(last), last.state === "active" ? "good" : last.state === "error" ? "critical" : "info");
    } catch {
      toast(`${item.label} 키 저장 실패`, "critical");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      {item.slots.map((s) => {
        const id = `ai-${s.name}`;
        const v = values[s.name] ?? "";
        return (
          <div key={s.name}>
            <label htmlFor={id} className="mb-1 flex items-center gap-1.5 text-xs font-medium text-ink-2">
              {s.label}
              {s.configured ? <Pill tone="good" icon="check">등록됨</Pill> : null}
            </label>
            <input
              id={id}
              type="password"
              autoComplete="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
              value={v}
              onChange={(e) => setValues((prev) => ({ ...prev, [s.name]: e.target.value }))}
              placeholder={s.configured ? "새 키로 교체" : "키 붙여넣기"}
              aria-invalid={v.length > 0 && v.length < 8}
              className={INPUT}
            />
          </div>
        );
      })}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" variant="primary" icon="key" loading={busy} disabled={!ready}>
          {item.configured ? "키 교체 · 즉시 적용" : "등록 · 즉시 적용"}
        </Button>
        {item.key_url ? (
          <a href={item.key_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
            키 발급 <Icon name="external" size={12} />
          </a>
        ) : null}
      </div>
      {put.error ? <ErrorBanner error={put.error} compact /> : null}
    </form>
  );
}

function VoiceMap({ item, canEdit }: { item: Integration; canEdit: boolean }) {
  const remap = useRemapVoices();
  const toast = useToast();
  const options = useStudioOptions();
  const names = new Map((options.data?.voices ?? []).map((v) => [v.id, v.name]));
  const entries = Object.entries(item.voices ?? {});
  if (!item.configured) return null;
  return (
    <div className="rounded-lg border border-line px-3 py-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <p className="text-xs font-semibold text-ink-2">보이스 매핑 (빠른 보이스 → 계정 음성)</p>
        {canEdit ? (
          <Button
            size="sm"
            variant="ghost"
            icon="refresh"
            loading={remap.isPending}
            disabled={item.state !== "active" && item.state !== "unverified"}
            onClick={() => remap.mutate(undefined, { onSuccess: (r) => toast(`보이스 ${r.mapped}종 다시 매핑했습니다`, "good") })}
          >
            다시 매핑
          </Button>
        ) : null}
      </div>
      {entries.length ? (
        <ul className="grid grid-cols-1 gap-x-4 gap-y-0.5 text-xs sm:grid-cols-2">
          {entries.map(([cat, v]) => (
            <li key={cat} className="flex justify-between gap-2">
              <span className="text-muted">{names.get(cat) ?? cat}</span>
              <span className="truncate font-medium text-ink">{v.name || v.voice_id}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted">아직 매핑되지 않았습니다 — 키 확인 후 자동으로 매핑됩니다.</p>
      )}
      {remap.error ? <ErrorBanner error={remap.error} compact className="mt-2" /> : null}
    </div>
  );
}

function IntegrationTile({ item, canEdit }: { item: Integration; canEdit: boolean }) {
  const verify = useVerifyIntegration();
  const toggle = useSetIntegrationEnabled();
  const del = useDeleteSecret();
  const toast = useToast();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const st = STATE[item.state];

  return (
    <li className="flex flex-col gap-3 rounded-xl border border-line p-4" aria-labelledby={`ai-${item.name}-title`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p id={`ai-${item.name}-title`} className="flex flex-wrap items-center gap-2 text-sm font-semibold text-ink">
            {item.label}
            <Pill tone={st.tone} icon={item.state === "active" ? "check" : item.state === "error" ? "ban" : undefined}>
              {st.label}
            </Pill>
          </p>
          <p className="mt-0.5 text-xs text-muted">
            {item.model}
            {item.unit_cost ? ` · ${item.unit_cost}` : ""}
          </p>
        </div>
        {item.checked_at ? <span className="text-[11px] text-muted">확인 {kst(item.checked_at)}</span> : null}
      </div>
      {item.detail ? (
        <p className={`text-[13px] ${item.state === "error" ? "text-critical-ink" : item.state === "degraded" ? "text-warn-ink" : "text-ink-2"}`}>
          {item.detail}
        </p>
      ) : null}

      <div>
        <p className="mb-1 text-xs font-semibold text-ink-2">{item.state === "active" ? "지금 자동으로 하는 일" : "등록하면 자동으로"}</p>
        <ul className="space-y-0.5 text-[13px] text-ink">
          {item.automates.map((a) => (
            <li key={a} className="flex gap-1.5">
              <Icon name={item.state === "active" ? "check" : "sparkle"} size={13} className="mt-0.5 shrink-0 text-accent-ink" />
              {a}
            </li>
          ))}
        </ul>
        {item.guardrails.length ? (
          <ul className="mt-1.5 space-y-0.5 text-xs text-muted">
            {item.guardrails.map((g) => (
              <li key={g} className="flex gap-1.5">
                <Icon name="shield" size={12} className="mt-0.5 shrink-0" />
                {g}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {item.name === "elevenlabs" ? <VoiceMap item={item} canEdit={canEdit} /> : null}

      {canEdit ? (
        <>
          <KeyForm item={item} />
          {item.configured ? (
            <div className="flex flex-wrap gap-2 border-t border-line pt-3">
              <Button
                size="sm"
                icon="refresh"
                loading={verify.isPending}
                onClick={() => verify.mutate(item.name, { onSuccess: (r) => toast(summary(r), r.state === "active" ? "good" : "critical") })}
              >
                연결 확인
              </Button>
              <Button
                size="sm"
                variant="ghost"
                icon={item.enabled ? "pause" : "play"}
                loading={toggle.isPending}
                onClick={() =>
                  toggle.mutate(
                    { name: item.name, enabled: !item.enabled },
                    { onSuccess: (r) => toast(r.enabled ? `${item.label} 자동 사용을 다시 켰습니다` : `${item.label} 자동 사용을 일시 중지했습니다`, "info") },
                  )
                }
              >
                {item.enabled ? "일시 중지" : "다시 사용"}
              </Button>
              {confirmDelete ? (
                <Button
                  size="sm"
                  variant="danger"
                  icon="trash"
                  loading={del.isPending}
                  onClick={async () => {
                    try {
                      for (const s of item.slots.filter((x) => x.configured)) await del.mutateAsync(s.name);
                      toast(`${item.label} 키를 삭제했습니다 — 관련 기능은 기본 방식으로 돌아갑니다`, "info");
                    } catch {
                      toast(`${item.label} 키 삭제 실패`, "critical");
                    } finally {
                      setConfirmDelete(false);
                    }
                  }}
                >
                  정말 삭제
                </Button>
              ) : (
                <Button size="sm" variant="ghost" icon="trash" onClick={() => setConfirmDelete(true)}>
                  키 삭제
                </Button>
              )}
            </div>
          ) : null}
          {verify.error ? <ErrorBanner error={verify.error} compact /> : null}
          {toggle.error ? <ErrorBanner error={toggle.error} compact /> : null}
          {del.error ? <ErrorBanner error={del.error} compact /> : null}
        </>
      ) : null}
    </li>
  );
}

/** 운영 준비 › AI 연동 — register a key once; the server verifies it and switches every dependent step on. */
export function IntegrationsCard({ canEdit }: { canEdit: boolean }) {
  const q = useIntegrations();
  const items = q.data?.items ?? [];
  const active = items.filter((i) => i.state === "active").length;
  const llm = q.data?.llm?.provider ?? "offline";
  return (
    <Card aria-labelledby="setup-ai">
      <CardHeader
        id="setup-ai"
        title="AI 연동"
        description="키를 등록하면 서버가 바로 연결을 확인하고 관련 단계를 자동으로 켭니다. 재시작이 필요 없고, 키는 다시 표시되지 않습니다."
        actions={
          <span className="flex flex-wrap items-center gap-2">
            <Pill tone={llm === "offline" ? "neutral" : "good"}>
              대본 AI: {llm === "offline" ? "오프라인" : llm === "anthropic+gemini" ? "Claude → Gemini 대체" : llm === "gemini" ? "Gemini" : "Claude"}
            </Pill>
            <Pill tone={active ? "good" : "neutral"}>
              {active}/{items.length || 5} 사용 중
            </Pill>
          </span>
        }
      />
      <CardBody>
        {!canEdit ? <Notice className="mb-3">키 등록은 {ALLOWED_ROLES.admin}만 할 수 있습니다. 연결 상태만 표시됩니다.</Notice> : null}
        {q.isPending ? (
          <LoadingBlock rows={6} />
        ) : q.error ? (
          <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
        ) : (
          <ul className="grid gap-4 xl:grid-cols-2">
            {items.map((i) => (
              <IntegrationTile key={i.name} item={i} canEdit={canEdit} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
