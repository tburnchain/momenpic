"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import { Pill, PlatformTag } from "@/components/ui/Pill";
import { kst, relative } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { ACCOUNT_STATUS } from "@/lib/labels";
import { useAlerts, usePrincipal } from "@/lib/queries";
import type { AccountStatus } from "@/lib/schemas";

function detailText(detail: unknown): string {
  if (detail === null || detail === undefined) return "";
  if (typeof detail === "string") return detail;
  if (typeof detail !== "object") return String(detail);
  const o = detail as Record<string, unknown>;
  if (Array.isArray(o.findings) && o.findings.length) return o.findings.map(String).join(" · ");
  for (const k of ["message", "reason", "detail"]) if (typeof o[k] === "string") return o[k] as string;
  return Object.entries(o)
    .filter(([, v]) => typeof v !== "object")
    .slice(0, 3)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(" · ");
}

function hrefFor(target: string | null | undefined): string | null {
  if (!target) return null;
  const [kind, id] = target.split(":", 2);
  if (!id) return null;
  if (kind === "run") return `/runs/${encodeURIComponent(id)}`;
  if (kind === "content") return `/contents/${encodeURIComponent(id)}`;
  if (kind === "account") return "/accounts";
  if (kind === "publication") return "/publications";
  return null;
}

/** Header bell: high-severity count badge + dropdown with risk feed, HOLD backlog and accounts. */
export function AlertsBell() {
  const q = useAlerts(!!usePrincipal().me);
  const now = useNow();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const d = q.data;
  const high = d?.counts.high ?? 0;
  const badge = high || d?.pending_holds || 0;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const label = d
    ? `알림: 높음 ${high}건, HOLD 대기 ${d.pending_holds}건, 주의 계정 ${d.accounts_attention.length}개`
    : "알림";

  return (
    <div ref={wrap} className="relative">
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="relative inline-flex h-10 w-10 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
      >
        <svg viewBox="0 0 24 24" width={19} height={19} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" />
        </svg>
        {badge > 0 ? (
          <span
            className={`absolute right-1 top-1 min-w-4 rounded-full px-1 text-center text-[10px] font-bold leading-4 text-white ${high ? "bg-critical" : "bg-serious-ink"}`}
            aria-hidden
          >
            {badge > 99 ? "99+" : badge}
          </span>
        ) : null}
      </button>

      {open ? (
        <div
          id={panelId}
          role="dialog"
          aria-label="알림"
          className="fixed inset-x-2 top-14 z-50 max-h-[75dvh] overflow-y-auto rounded-2xl border border-line bg-surface shadow-2xl sm:absolute sm:inset-x-auto sm:right-0 sm:top-12 sm:w-[400px]"
        >
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <p className="font-semibold text-ink">알림</p>
            {d ? (
              <span className="flex gap-1.5">
                <Pill tone={d.counts.high ? "critical" : "neutral"}>높음 {d.counts.high}</Pill>
                <Pill tone="warn">중간 {d.counts.medium}</Pill>
              </span>
            ) : null}
          </div>

          {q.isPending ? (
            <p className="px-4 py-6 text-sm text-muted">불러오는 중…</p>
          ) : q.error ? (
            <p className="px-4 py-6 text-sm text-critical-ink">알림을 불러오지 못했습니다.</p>
          ) : d ? (
            <div className="divide-y divide-line">
              <Link
                href="/holds"
                onClick={() => setOpen(false)}
                className="flex items-center justify-between gap-2 px-4 py-3 text-sm hover:bg-surface-2"
              >
                <span className="flex items-center gap-2">
                  <Icon name="inbox" size={16} className="text-serious-ink" />
                  HOLD 대기 <strong className="tabular-nums">{d.pending_holds}</strong>건
                </span>
                <span className="text-accent-ink">검토하기 →</span>
              </Link>

              {d.accounts_attention.length ? (
                <div className="px-4 py-3">
                  <p className="mb-2 text-xs font-semibold text-muted">주의가 필요한 계정</p>
                  <ul className="space-y-1.5">
                    {d.accounts_attention.map((a) => {
                      const st = ACCOUNT_STATUS[a.status as AccountStatus];
                      return (
                        <li key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
                          <PlatformTag platform={a.platform} className="text-[13px]" />
                          <span className="text-ink">{a.handle}</span>
                          <Pill tone={st?.tone ?? "warn"}>{st?.label ?? a.status}</Pill>
                          {a.until ? <span className="text-xs text-muted">해제 {relative(a.until, now)}</span> : null}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : null}

              <div className="px-4 py-3">
                <p className="mb-2 text-xs font-semibold text-muted">최근 경보</p>
                {d.items.length ? (
                  <ul className="space-y-2.5">
                    {d.items.slice(0, 12).map((it, i) => {
                      const href = hrefFor(it.target);
                      const body = (
                        <>
                          <span className="flex items-center gap-2">
                            <Pill tone={it.severity === "high" ? "critical" : "warn"} icon="alert">
                              {it.label}
                            </Pill>
                            <span className="text-xs text-muted" title={kst(it.at)}>
                              {relative(it.at, now)}
                            </span>
                          </span>
                          {detailText(it.detail) ? <span className="mt-1 block text-[13px] text-ink">{detailText(it.detail)}</span> : null}
                          {it.target ? <span className="block truncate font-mono text-[11px] text-muted">{it.target}</span> : null}
                        </>
                      );
                      return (
                        <li key={`${it.at}-${i}`}>
                          {href ? (
                            <Link href={href} onClick={() => setOpen(false)} className="-mx-2 block rounded-lg px-2 py-1 hover:bg-surface-2">
                              {body}
                            </Link>
                          ) : (
                            <div className="py-1">{body}</div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="text-sm text-muted">최근 경보가 없습니다.</p>
                )}
              </div>
              <Link href="/dashboards/risk" onClick={() => setOpen(false)} className="block px-4 py-2.5 text-center text-[13px] text-accent-ink hover:bg-surface-2">
                리스크 대시보드 보기
              </Link>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
