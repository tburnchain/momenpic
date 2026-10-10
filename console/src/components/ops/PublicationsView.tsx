"use client";

import Link from "next/link";
import { useState } from "react";
import { Button, IconButton } from "@/components/ui/Button";
import { CaptionPreview } from "@/components/ui/CaptionPreview";
import { Card } from "@/components/ui/Card";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Field, Select, TextArea } from "@/components/ui/Field";
import { Icon } from "@/components/ui/Icon";
import { CopyButton, PageHeader, Segmented, Thumb } from "@/components/ui/misc";
import { PlatformTag, PublicationStatePill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import type { PublicationQuery } from "@/lib/api";
import {
  DAY_MS,
  compact,
  int,
  kst,
  kstDayKey,
  kstDayLabel,
  kstTime,
  kstWeekStart,
  krw,
  relative,
} from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { KIND_LABEL, PLATFORM_LABEL, platformColor, placementLabel, publicationState } from "@/lib/labels";
import { usePrincipal, usePublications, useTakedown } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import { PLATFORMS, type PublicationItem } from "@/lib/schemas";

type StateFilter = "all" | "scheduled" | "inflight" | "published" | "problem" | "manual" | "removed";

const STATE_FILTERS: Record<StateFilter, { label: string; states?: string }> = {
  all: { label: "전체" },
  scheduled: { label: "예약", states: "QUEUED" },
  inflight: { label: "진행 중", states: "UPLOADING,PROCESSING" },
  published: { label: "게시됨", states: "PUBLISHED" },
  problem: { label: "실패·만료", states: "FAILED,EXPIRED" },
  manual: { label: "수동 대기", states: "MANUAL_PENDING" },
  removed: { label: "중단·취소", states: "TAKEN_DOWN,CANCELLED" },
};

const TAKEDOWNABLE = new Set(["PUBLISHED", "QUEUED", "UPLOADING", "PROCESSING", "MANUAL_PENDING"]);

function errorObj(p: PublicationItem): Record<string, unknown> | null {
  return p.error && Object.keys(p.error).length ? p.error : null;
}

function needsManualTakedown(p: PublicationItem): boolean {
  return !!errorObj(p)?.manual_takedown_required;
}

function Metrics({ p, compactView = false }: { p: PublicationItem; compactView?: boolean }) {
  const m = p.metrics;
  if (m.views === undefined && m.link_clicks === undefined) return <span className="text-muted">—</span>;
  if (compactView) {
    return (
      <span className="tabular-nums">
        {compact(m.views)}회 · 클릭 {compact(m.link_clicks)}
      </span>
    );
  }
  const tiles: Array<[string, number | null | undefined]> = [
    ["조회", m.views],
    ["링크 클릭", m.link_clicks],
    ["좋아요", m.likes],
    ["공유", m.shares],
  ];
  return (
    <dl className="grid grid-cols-4 gap-2">
      {tiles.map(([k, v]) => (
        <div key={k} className="rounded-lg bg-surface-2 px-2.5 py-2">
          <dt className="text-[11px] text-muted">{k}</dt>
          <dd className="text-sm font-semibold tabular-nums text-ink">{int(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function PublicationDrawer({ pub, onClose }: { pub: PublicationItem; onClose: () => void }) {
  const { can, role } = usePrincipal();
  const takedown = useTakedown();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const err = errorObj(pub);
  const canTakedown = can("takedown") && TAKEDOWNABLE.has(pub.state) && !needsManualTakedown(pub);

  return (
    <Dialog
      open
      onClose={onClose}
      variant="side"
      title={
        <span className="flex flex-wrap items-center gap-2">
          <PlatformTag platform={pub.platform} />
          <span className="font-normal text-ink-2">{pub.account_handle}</span>
          <PublicationStatePill state={pub.state} />
        </span>
      }
      description={<span className="font-mono text-xs">{pub.id}</span>}
    >
      <div className="space-y-5">
        <div className="flex items-start gap-3">
          <Thumb src={pub.product?.image_url} alt={pub.product?.name ?? "상품"} className="w-14 shrink-0 rounded-lg" />
          <div className="min-w-0">
            <p className="font-medium text-ink">{pub.product?.name ?? "상품 정보 없음"}</p>
            <p className="text-xs text-muted">
              {krw(pub.product?.price)} · {pub.kind ? (KIND_LABEL[pub.kind] ?? pub.kind) : "—"}
            </p>
            {pub.hook ? <p className="mt-1 text-sm text-ink">“{pub.hook}”</p> : null}
          </div>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted">예약 (KST)</dt>
          <dd className="tabular-nums">{kst(pub.scheduled_at)}</dd>
          <dt className="text-muted">게시 (KST)</dt>
          <dd className="tabular-nums">{kst(pub.published_at)}</dd>
          <dt className="text-muted">링크 위치</dt>
          <dd>{pub.link_placement ? placementLabel(pub.link_placement) : "—"}</dd>
          <dt className="text-muted">시도</dt>
          <dd className="tabular-nums">{int(pub.attempts)}회</dd>
          <dt className="text-muted">subId</dt>
          <dd className="flex min-w-0 items-center gap-2">
            <span className="truncate font-mono text-xs">{pub.sub_id ?? "—"}</span>
            {pub.sub_id ? <CopyButton text={pub.sub_id} label="복사" /> : null}
          </dd>
        </dl>

        {pub.state === "PUBLISHED" ? <Metrics p={pub} /> : null}

        <div className="flex flex-wrap gap-2">
          {pub.permalink ? (
            <a href={pub.permalink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-sm text-accent-ink hover:underline">
              게시물 열기 <Icon name="external" size={13} />
              <span className="sr-only">(새 창)</span>
            </a>
          ) : null}
          <Link href={`/contents/${encodeURIComponent(pub.content_id)}`} className="inline-flex items-center gap-1 text-sm text-accent-ink hover:underline">
            콘텐츠 상세 <Icon name="chevronRight" size={13} />
          </Link>
        </div>

        <section aria-labelledby="pub-caption">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <h3 id="pub-caption" className="text-[13px] font-semibold text-ink-2">
              캡션
            </h3>
            {pub.caption ? <CopyButton text={pub.caption} label="캡션 복사" /> : null}
          </div>
          <CaptionPreview text={pub.caption} compact />
        </section>

        {needsManualTakedown(pub) ? (
          <Notice tone="warn" title="수동 삭제 필요">
            {PLATFORM_LABEL[pub.platform]}는 게시물 삭제 API가 없습니다. 게시 중단 요청은 기록됐지만 게시물은 아직 공개 상태(PUBLISHED)입니다 — 플랫폼 앱에서 직접 삭제하세요.
          </Notice>
        ) : null}
        {err && !needsManualTakedown(pub) ? (
          <section aria-labelledby="pub-error">
            <h3 id="pub-error" className="mb-1.5 text-[13px] font-semibold text-critical-ink">
              오류 / 기록
            </h3>
            <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-xs text-ink-2">{JSON.stringify(err, null, 2)}</pre>
          </section>
        ) : null}

        {TAKEDOWNABLE.has(pub.state) && !needsManualTakedown(pub) ? (
          canTakedown ? (
            <section aria-labelledby="pub-takedown" className="space-y-2 rounded-xl border border-critical/30 p-3">
              <h3 id="pub-takedown" className="text-sm font-semibold text-ink">
                게시 중단 (테이크다운)
              </h3>
              <p className="text-xs text-muted">
                게시됨 → 비공개/삭제, 예약 → 취소. 인스타그램·틱톡은 삭제 API가 없어 수동 삭제가 필요합니다. 감사 로그에 기록됩니다.
              </p>
              <Field label="사유 (필수)" htmlFor="takedown-reason">
                <TextArea id="takedown-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="예: 가격 표기 오류, 품절" maxLength={300} />
              </Field>
              <Button
                variant="danger"
                icon="ban"
                loading={takedown.isPending}
                disabled={reason.trim().length < 2}
                onClick={() =>
                  takedown.mutate(
                    { id: pub.id, reason: reason.trim() },
                    {
                      onSuccess: (r) => {
                        setReason("");
                        const manual = !!(r.error && (r.error as Record<string, unknown>).manual_takedown_required);
                        toast(manual ? "중단 요청 기록 — 플랫폼에서 수동 삭제가 필요합니다" : `게시 중단 완료 (${publicationState(r.state).label})`, manual ? "info" : "good");
                      },
                    },
                  )
                }
              >
                게시 중단
              </Button>
              {takedown.error ? <ErrorBanner error={takedown.error} compact /> : null}
            </section>
          ) : (
            <p className="text-xs text-muted">
              게시 중단은 {ALLOWED_ROLES.takedown}만 할 수 있습니다{role ? ` (현재 역할: ${role})` : ""}.
            </p>
          )
        ) : null}
      </div>
    </Dialog>
  );
}

function ListView({ items, onOpen, now }: { items: PublicationItem[]; onOpen: (id: string) => void; now: number }) {
  return (
    <>
      <table className="hidden w-full text-left text-sm md:table">
        <caption className="sr-only">게시 목록</caption>
        <thead className="border-b border-line text-[13px] text-muted">
          <tr>
            <th scope="col" className="px-5 py-3 font-medium">예약 / 게시 (KST)</th>
            <th scope="col" className="px-3 py-3 font-medium">채널</th>
            <th scope="col" className="px-3 py-3 font-medium">상품 · 훅</th>
            <th scope="col" className="px-3 py-3 font-medium">상태</th>
            <th scope="col" className="px-5 py-3 text-right font-medium">성과</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => (
            <tr key={p.id} className="cursor-pointer border-b border-line last:border-0 hover:bg-surface-2" onClick={() => onOpen(p.id)}>
              <td className="whitespace-nowrap px-5 py-3 align-top tabular-nums text-ink-2">
                <div>{kst(p.scheduled_at)}</div>
                {p.published_at ? <div className="text-xs text-good-ink">게시 {kstTime(p.published_at)}</div> : <div className="text-xs text-muted">{relative(p.scheduled_at, now)}</div>}
              </td>
              <td className="px-3 py-3 align-top">
                <PlatformTag platform={p.platform} className="text-[13px]" />
                <div className="text-xs text-muted">{p.account_handle}</div>
              </td>
              <td className="max-w-md px-3 py-3 align-top">
                <button
                  type="button"
                  className="text-left font-medium text-ink hover:text-accent-ink hover:underline"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpen(p.id);
                  }}
                >
                  {p.product?.name ?? p.content_id}
                </button>
                {p.hook ? <p className="truncate text-xs text-muted">“{p.hook}”</p> : null}
              </td>
              <td className="px-3 py-3 align-top">
                <PublicationStatePill state={p.state} />
                {needsManualTakedown(p) ? <div className="mt-1 text-xs text-warn-ink">수동 삭제 필요</div> : null}
              </td>
              <td className="whitespace-nowrap px-5 py-3 text-right align-top text-[13px]">
                <Metrics p={p} compactView />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="divide-y divide-line md:hidden">
        {items.map((p) => (
          <li key={p.id}>
            <button type="button" onClick={() => onOpen(p.id)} className="block w-full space-y-1.5 px-4 py-3.5 text-left active:bg-surface-2">
              <span className="flex items-center justify-between gap-2">
                <PlatformTag platform={p.platform} className="text-[13px]" />
                <PublicationStatePill state={p.state} />
              </span>
              <span className="block text-sm font-medium text-ink">{p.product?.name ?? p.content_id}</span>
              <span className="block text-xs tabular-nums text-muted">
                {kst(p.scheduled_at)} · {p.account_handle}
                {p.state === "PUBLISHED" ? ` · ${compact(p.metrics.views)}회` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

function WeekView({ items, weekStart, now, onOpen }: { items: PublicationItem[]; weekStart: number; now: number; onOpen: (id: string) => void }) {
  const days = Array.from({ length: 7 }, (_, i) => weekStart + i * DAY_MS);
  const today = kstDayKey(now);
  const byDay = new Map<string, PublicationItem[]>();
  for (const p of items) {
    const k = kstDayKey(p.scheduled_at);
    byDay.set(k, [...(byDay.get(k) ?? []), p]);
  }
  return (
    <div className="grid gap-px overflow-hidden rounded-xl bg-[var(--line)] md:grid-cols-7">
      {days.map((d) => {
        const key = kstDayKey(d);
        const list = (byDay.get(key) ?? []).sort((a, b) => String(a.scheduled_at).localeCompare(String(b.scheduled_at)));
        const isToday = key === today;
        return (
          <section key={key} aria-label={`${kstDayLabel(d)} ${list.length}건`} className="min-h-28 bg-surface p-2.5">
            <h3 className={`mb-2 flex items-center justify-between text-xs font-semibold ${isToday ? "text-accent-ink" : "text-ink-2"}`}>
              <span>
                {kstDayLabel(d)}
                {isToday ? " · 오늘" : ""}
              </span>
              <span className="tabular-nums text-muted">{list.length || ""}</span>
            </h3>
            <ul className="space-y-1.5">
              {list.map((p) => {
                const st = publicationState(p.state);
                return (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => onOpen(p.id)}
                      title={`${PLATFORM_LABEL[p.platform]} ${p.account_handle} · ${st.label}`}
                      className="flex w-full items-start gap-1.5 rounded-md border border-line bg-surface-2/60 px-1.5 py-1 text-left text-[11px] leading-tight hover:border-accent"
                    >
                      <span className="mt-[3px] h-2 w-2 shrink-0 rounded-[2px]" style={{ background: platformColor(p.platform) }} aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="block font-semibold tabular-nums text-ink">
                          {kstTime(p.scheduled_at)} <span className="font-normal text-muted">{st.label}</span>
                        </span>
                        <span className="block truncate text-ink-2">{p.product?.name ?? p.content_id}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

export function PublicationsView() {
  const now = useNow();
  const [filter, setFilter] = useState<StateFilter>("all");
  const [platform, setPlatform] = useState("");
  const [view, setView] = useState<"list" | "week">("list");
  const [weekOffset, setWeekOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  const weekStart = now ? kstWeekStart(now) + weekOffset * 7 * DAY_MS : 0;
  const query: PublicationQuery =
    view === "week"
      ? {
          state: STATE_FILTERS[filter].states,
          platform: platform || undefined,
          since: new Date(weekStart).toISOString(),
          until: new Date(weekStart + 7 * DAY_MS).toISOString(),
          limit: 1000,
        }
      : { state: STATE_FILTERS[filter].states, platform: platform || undefined, limit: 300 };
  const q = usePublications(query, view === "list" || now > 0);
  const items = q.data?.items ?? [];
  const open = openId ? items.find((p) => p.id === openId) : undefined;

  return (
    <>
      <PageHeader
        title="게시 현황"
        description="플랫폼별 게시 상태 · 예약 캘린더(KST) · 성과. 행을 누르면 캡션·링크·게시 중단을 볼 수 있습니다."
      />
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="-mx-1 overflow-x-auto px-1 pb-1">
          <Segmented<StateFilter>
            label="상태 필터"
            value={filter}
            onChange={setFilter}
            size="sm"
            options={(Object.keys(STATE_FILTERS) as StateFilter[]).map((f) => ({ value: f, label: STATE_FILTERS[f].label }))}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="pub-platform" className="sr-only">
            플랫폼
          </label>
          <div className="w-40">
            <Select id="pub-platform" value={platform} onChange={(e) => setPlatform(e.target.value)} className="h-9">
              <option value="">모든 플랫폼</option>
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {PLATFORM_LABEL[p]}
                </option>
              ))}
            </Select>
          </div>
          <Segmented
            label="보기"
            value={view}
            onChange={setView}
            size="sm"
            options={[
              { value: "list" as const, label: "목록" },
              { value: "week" as const, label: "주간 캘린더" },
            ]}
          />
        </div>
      </div>

      {view === "week" ? (
        <div className="mb-3 flex items-center gap-2">
          <IconButton icon="chevronLeft" label="이전 주" onClick={() => setWeekOffset((w) => w - 1)} />
          <p className="min-w-44 text-center text-sm font-medium tabular-nums text-ink" aria-live="polite">
            {weekStart ? `${kstDayLabel(weekStart)} – ${kstDayLabel(weekStart + 6 * DAY_MS)}` : "—"}
          </p>
          <IconButton icon="chevronRight" label="다음 주" onClick={() => setWeekOffset((w) => w + 1)} />
          {weekOffset !== 0 ? (
            <Button size="sm" variant="ghost" onClick={() => setWeekOffset(0)}>
              이번 주
            </Button>
          ) : null}
          {q.isFetching ? <span className="text-xs text-muted">갱신 중…</span> : null}
        </div>
      ) : null}

      {q.error && !q.data ? (
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : !q.data ? (
        <Card>
          <div className="p-5">
            <LoadingBlock rows={8} />
          </div>
        </Card>
      ) : view === "week" ? (
        <div className={q.isPlaceholderData ? "opacity-60 transition-opacity" : ""}>
          {weekStart ? <WeekView items={items} weekStart={weekStart} now={now} onOpen={setOpenId} /> : <LoadingBlock rows={4} />}
        </div>
      ) : (
        <Card className={q.isPlaceholderData ? "opacity-60 transition-opacity" : ""}>
          {items.length ? (
            <>
              <p className="border-b border-line px-5 py-2 text-xs text-muted" aria-live="polite">
                {items.length}건 · 최신 예약순
              </p>
              <ListView items={items} onOpen={setOpenId} now={now} />
            </>
          ) : (
            <EmptyState icon="calendar" title="조건에 맞는 게시가 없습니다">
              승인된 콘텐츠가 게이트를 통과하면 플랫폼별 게시가 예약됩니다.
            </EmptyState>
          )}
        </Card>
      )}

      {open ? <PublicationDrawer key={open.id} pub={open} onClose={() => setOpenId(null)} /> : null}
    </>
  );
}
