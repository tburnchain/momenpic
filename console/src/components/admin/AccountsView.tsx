"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Field, Select, TextInput } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/misc";
import { AccountStatusPill, Pill, PlatformTag } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { int, kst, kstDate, relative } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { ACCOUNT_STATUS, PLATFORM_LABEL } from "@/lib/labels";
import { useAccounts, useCreateAccount, usePatchAccount, usePrincipal, usePutCredentials, useSystemStatus } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import {
  ACCOUNT_STATUSES,
  AccountCreate,
  DAILY_CAP_MAX,
  PLATFORMS,
  type Account,
  type AccountStatus,
  type Platform,
  type SystemStatus,
} from "@/lib/schemas";

/** §7 operating caps per account per day (≤ 30 % of the API limit). Kakao = manual queue. */
const RECOMMENDED_CAP: Record<Platform, number | null> = {
  threads: 20,
  instagram: 5,
  youtube: 3,
  tiktok: 3,
  facebook: 3,
  kakao: null,
};

const DAY = 86_400_000;

function ageDays(created: string | null | undefined, now: number): number | null {
  if (!created || !now) return null;
  const t = new Date(created).getTime();
  return Number.isNaN(t) ? null : Math.floor((now - t) / DAY);
}

function EditDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const patch = usePatchAccount();
  const toast = useToast();
  const [status, setStatus] = useState<AccountStatus>(account.status);
  const [cap, setCap] = useState(String(account.daily_cap));
  const capNum = Number(cap);
  const capValid = Number.isInteger(capNum) && capNum >= 1 && capNum <= DAILY_CAP_MAX;
  const rec = RECOMMENDED_CAP[account.platform];

  const save = () => {
    const body: { status?: AccountStatus; daily_cap?: number } = {};
    if (status !== account.status) body.status = status;
    if (capNum !== account.daily_cap) body.daily_cap = capNum;
    if (!Object.keys(body).length) return onClose();
    patch.mutate(
      { id: account.id, body },
      {
        onSuccess: () => {
          toast("계정을 업데이트했습니다");
          onClose();
        },
      },
    );
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`${account.handle} 편집`}
      description={`${PLATFORM_LABEL[account.platform]} · 니치 ${account.niche ?? "—"}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            취소
          </Button>
          <Button variant="primary" onClick={save} loading={patch.isPending} disabled={!capValid}>
            저장
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (capValid) save();
        }}
      >
        <Field label="상태" htmlFor="acc-status" hint="cooldown 전환 시 48시간 동안 예약 게시가 보류됩니다.">
          <Select id="acc-status" value={status} onChange={(e) => setStatus(e.target.value as AccountStatus)}>
            {ACCOUNT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {ACCOUNT_STATUS[s].label} ({s})
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="일일 게시 상한"
          htmlFor="acc-cap"
          error={capValid ? null : `1~${DAILY_CAP_MAX} 사이 정수를 입력하세요`}
          hint={rec !== null ? `권장 상한 ${rec}건/일 (API 한도의 30% 이하, §7)` : "카카오는 수동 게시 큐로 운영됩니다"}
        >
          <TextInput id="acc-cap" type="number" inputMode="numeric" min={1} max={DAILY_CAP_MAX} value={cap} onChange={(e) => setCap(e.target.value)} aria-invalid={!capValid} />
        </Field>
        {rec !== null && capValid && capNum > rec ? (
          <Notice tone="warn">권장 상한({rec}건)보다 높습니다. 스팸 판정·계정 정지 위험이 커집니다.</Notice>
        ) : null}
        {patch.error ? <ErrorBanner error={patch.error} compact /> : null}
      </form>
    </Dialog>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const create = useCreateAccount();
  const toast = useToast();
  const [platform, setPlatform] = useState<Platform>("threads");
  const [handle, setHandle] = useState("");
  const [niche, setNiche] = useState("");
  const [tokenRef, setTokenRef] = useState("");
  const [cap, setCap] = useState("20");
  const [persona, setPersona] = useState("30대 여성");
  const [errors, setErrors] = useState<Record<string, string>>({});

  const submit = () => {
    const parsed = AccountCreate.safeParse({
      platform,
      handle,
      niche,
      token_ref: tokenRef,
      daily_cap: Number(cap),
      persona_default: persona || undefined,
    });
    if (!parsed.success) {
      const errs: Record<string, string> = {};
      for (const i of parsed.error.issues) errs[String(i.path[0])] ??= i.message;
      setErrors(errs);
      return;
    }
    setErrors({});
    create.mutate(parsed.data, {
      onSuccess: () => {
        toast("계정을 등록했습니다");
        onClose();
      },
    });
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="계정 추가"
      description="계정 = 브랜드. 니치별 실제 브랜드로 운영하고, 한도 우회를 위한 다계정 생성은 금지입니다 (§7.1)."
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            취소
          </Button>
          <Button variant="primary" icon="plus" onClick={submit} loading={create.isPending}>
            등록
          </Button>
        </>
      }
    >
      <form
        className="grid gap-4 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        noValidate
      >
        <Field label="플랫폼" htmlFor="new-platform">
          <Select
            id="new-platform"
            value={platform}
            onChange={(e) => {
              const p = e.target.value as Platform;
              setPlatform(p);
              setCap(String(RECOMMENDED_CAP[p] ?? 5));
            }}
          >
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {PLATFORM_LABEL[p]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="핸들" htmlFor="new-handle" error={errors.handle}>
          <TextInput id="new-handle" value={handle} onChange={(e) => setHandle(e.target.value)} placeholder="@kitchen_pick" autoComplete="off" aria-invalid={!!errors.handle} />
        </Field>
        <Field label="니치" htmlFor="new-niche" error={errors.niche}>
          <TextInput id="new-niche" value={niche} onChange={(e) => setNiche(e.target.value)} placeholder="주방" aria-invalid={!!errors.niche} />
        </Field>
        <Field label="기본 페르소나" htmlFor="new-persona" error={errors.persona_default}>
          <TextInput id="new-persona" value={persona} onChange={(e) => setPersona(e.target.value)} placeholder="30대 여성" />
        </Field>
        <Field
          label="토큰 참조 (Vault 경로)"
          htmlFor="new-token"
          className="sm:col-span-2"
          error={errors.token_ref}
          hint="토큰 원문은 절대 입력하지 마세요. DB에는 Vault 경로만 저장됩니다 (§10)."
        >
          <TextInput
            id="new-token"
            value={tokenRef}
            onChange={(e) => setTokenRef(e.target.value)}
            placeholder="kv/acvm/threads/kitchen_pick"
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
            aria-invalid={!!errors.token_ref}
          />
        </Field>
        <Field
          label="일일 게시 상한"
          htmlFor="new-cap"
          error={errors.daily_cap}
          hint={RECOMMENDED_CAP[platform] !== null ? `권장 ${RECOMMENDED_CAP[platform]}건/일` : "수동 게시 큐"}
        >
          <TextInput id="new-cap" type="number" inputMode="numeric" min={1} max={DAILY_CAP_MAX} value={cap} onChange={(e) => setCap(e.target.value)} aria-invalid={!!errors.daily_cap} />
        </Field>
        <div className="sm:col-span-2">
          <Notice>신규 계정은 첫 14일 동안 모든 콘텐츠가 HOLD(사람 승인)로 갑니다 (H-01).</Notice>
        </div>
        {create.error ? (
          <div className="sm:col-span-2">
            <ErrorBanner error={create.error} compact />
          </div>
        ) : null}
      </form>
    </Dialog>
  );
}

type CredentialRow = SystemStatus["accounts"][number];

const CREDENTIAL_PILL: Record<CredentialRow["credential"], { label: string; tone: "good" | "critical" | "neutral" }> = {
  configured: { label: "토큰 등록됨", tone: "good" },
  missing: { label: "토큰 없음", tone: "critical" },
  not_required: { label: "토큰 불필요", tone: "neutral" },
};

/** Write-only token entry: the value goes to the API once, is never displayed, and the field is cleared. */
function TokenDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const put = usePutCredentials();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [userId, setUserId] = useState("");
  const [volatile, setVolatile] = useState(false);
  const ok = token.trim().length >= 10;

  const submit = () => {
    if (!ok) return;
    const t = token.trim();
    setToken("");
    put.mutate(
      { id: account.id, token: t, platformUserId: userId.trim() || undefined },
      {
        onSuccess: (r) => {
          if (r.persistent) {
            toast(`${account.handle} 토큰을 등록했습니다`);
            onClose();
          } else {
            setVolatile(true);
            toast("토큰 등록됨 — Vault 미연결: 재시작 시 사라집니다", "info");
          }
        },
      },
    );
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`${account.handle} 토큰 등록`}
      description={`${PLATFORM_LABEL[account.platform]} · 쓰기 전용 — 저장 후 다시 표시되지 않습니다 (§10)`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {volatile ? "닫기" : "취소"}
          </Button>
          <Button variant="primary" icon="key" onClick={submit} loading={put.isPending} disabled={!ok}>
            등록
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Field
          label="액세스 토큰 또는 토큰 번들(JSON)"
          htmlFor="cred-token"
          hint="원문 토큰 또는 access_token · refresh_token · expires_at · client_id · client_secret 를 담은 JSON"
        >
          <input
            id="cred-token"
            type="password"
            autoComplete="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="h-10 w-full rounded-lg border border-line-strong bg-surface px-3 font-mono text-sm text-ink focus:border-accent focus:outline-none"
          />
        </Field>
        <Field label="플랫폼 사용자 ID (선택)" htmlFor="cred-uid" hint="Threads/Instagram user id, YouTube channel id 등">
          <TextInput id="cred-uid" value={userId} onChange={(e) => setUserId(e.target.value)} autoComplete="off" spellCheck={false} className="font-mono" />
        </Field>
        {volatile ? (
          <Notice tone="warn" title="Vault 미연결: 재시작 시 사라집니다">
            토큰은 메모리에만 저장되었습니다. 운영 환경에서는 ACVM_SECURITY__SECRET_BACKEND=vault 를 설정하세요.
          </Notice>
        ) : null}
        {put.error ? <ErrorBanner error={put.error} compact /> : null}
      </form>
    </Dialog>
  );
}

function UsageBar({ used, cap }: { used: number; cap: number }) {
  const frac = cap > 0 ? Math.min(1, used / cap) : 0;
  const fill = frac >= 1 ? "bg-critical" : frac >= 0.8 ? "bg-serious" : "bg-accent";
  return (
    <div className="w-28">
      <div
        className="h-1.5 overflow-hidden rounded-full bg-accent-soft"
        role="meter"
        aria-label="24시간 게시 사용량"
        aria-valuemin={0}
        aria-valuemax={cap}
        aria-valuenow={used}
        aria-valuetext={`${used}/${cap}건`}
      >
        <div className={`h-full rounded-full ${fill}`} style={{ width: `${Math.max(frac * 100, used > 0 ? 4 : 0)}%` }} />
      </div>
      <p className="mt-1 text-right text-[11px] tabular-nums text-muted">
        24h {used}/{cap}
      </p>
    </div>
  );
}

export function AccountsView() {
  const q = useAccounts();
  const now = useNow();
  const { can } = usePrincipal();
  const manage = can("manageAccounts");
  const [editing, setEditing] = useState<Account | null>(null);
  const [creating, setCreating] = useState(false);
  const [tokenFor, setTokenFor] = useState<Account | null>(null);
  const status = useSystemStatus();
  const cred = new Map((status.data?.accounts ?? []).map((a) => [a.id, a]));
  const items = q.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="계정"
        description="플랫폼 계정별 상태 · 일일 상한 · 쿨다운. 운영 상한은 API 한도의 30% 이하로 설정합니다 (§7)."
        actions={
          manage ? (
            <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>
              계정 추가
            </Button>
          ) : null
        }
      />
      {!manage && q.data ? (
        <Notice className="mb-4">계정 추가·편집은 {ALLOWED_ROLES.manageAccounts}만 할 수 있습니다.</Notice>
      ) : null}
      <Card>
        {q.isPending ? (
          <div className="p-5">
            <LoadingBlock rows={7} />
          </div>
        ) : q.error ? (
          <div className="p-4">
            <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
          </div>
        ) : !items.length ? (
          <EmptyState icon="users" title="등록된 계정이 없습니다" action={manage ? <Button icon="plus" onClick={() => setCreating(true)}>계정 추가</Button> : null} />
        ) : (
          <ul className="divide-y divide-line">
            {items.map((a) => {
              const age = ageDays(a.created_at, now);
              const rec = RECOMMENDED_CAP[a.platform];
              const c = cred.get(a.id);
              return (
                <li key={a.id} className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:px-5">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <PlatformTag platform={a.platform} className="font-medium" />
                      <span className="font-medium text-ink">{a.handle}</span>
                      <AccountStatusPill status={a.status} />
                      {c ? (
                        <Pill tone={CREDENTIAL_PILL[c.credential].tone} icon={c.credential === "missing" ? "alert" : "key"}>
                          {CREDENTIAL_PILL[c.credential].label}
                        </Pill>
                      ) : null}
                      {age !== null && age < 14 ? (
                        <Pill tone="warn" icon="pause" title="H-01: 신규 계정 첫 14일은 모든 콘텐츠 HOLD">
                          신규 {age + 1}일차
                        </Pill>
                      ) : null}
                    </div>
                    <p className="mt-1 text-xs text-muted">
                      니치 {a.niche ?? "—"} · 등록 {kstDate(a.created_at)}
                      {a.cooldown_until ? ` · 쿨다운 해제 ${relative(a.cooldown_until, now)} (${kst(a.cooldown_until)})` : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-4">
                    {c ? <UsageBar used={c.used_24h} cap={c.daily_cap} /> : null}
                    <div className="text-right">
                      <p className="text-sm font-semibold tabular-nums">{int(a.daily_cap)}건/일</p>
                      <p className={`text-xs ${rec !== null && a.daily_cap > rec ? "text-serious-ink" : "text-muted"}`}>
                        {rec !== null ? `권장 ${rec}` : "수동 큐"}
                      </p>
                    </div>
                    {manage ? (
                      <div className="flex gap-1.5">
                        {a.platform !== "kakao" ? (
                          <Button
                            size="sm"
                            icon="key"
                            variant={c?.credential === "missing" ? "primary" : "secondary"}
                            onClick={() => setTokenFor(a)}
                            aria-label={`${a.handle} 토큰 등록`}
                          >
                            토큰 등록
                          </Button>
                        ) : null}
                        <Button size="sm" onClick={() => setEditing(a)} aria-label={`${a.handle} 편집`}>
                          편집
                        </Button>
                      </div>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
      {editing ? <EditDialog account={editing} onClose={() => setEditing(null)} /> : null}
      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {tokenFor ? <TokenDialog account={tokenFor} onClose={() => setTokenFor(null)} /> : null}
    </>
  );
}
