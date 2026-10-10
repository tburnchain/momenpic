"use client";

import { useState } from "react";
import { Button, buttonClass } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { CopyButton, PageHeader, Thumb } from "@/components/ui/misc";
import { Pill, PlatformTag } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { kst, relative } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { useManualDone, useManualQueue, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { ManualItem } from "@/lib/schemas";

function isUrl(s: string): boolean {
  try {
    const u = new URL(s);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
}

function QueueItem({ item, canPublish, now }: { item: ManualItem; canPublish: boolean; now: number }) {
  const done = useManualDone();
  const toast = useToast();
  const [permalink, setPermalink] = useState("");
  const valid = isUrl(permalink.trim());
  const overdue = item.scheduled_at ? new Date(item.scheduled_at).getTime() < now && now > 0 : false;
  const inputId = `permalink-${item.publication_id}`;

  return (
    <li className="grid gap-4 p-4 sm:p-5 md:grid-cols-[9rem_minmax(0,1fr)]">
      <div className="space-y-2">
        <div className="mx-auto w-32 md:w-full">
          <Thumb src={item.media_url} alt="게시할 미디어 미리보기" ratio="portrait" className="rounded-lg" />
        </div>
        {item.media_url ? (
          <div className="flex gap-1.5 md:flex-col">
            <a href={item.media_url} target="_blank" rel="noopener noreferrer" className={buttonClass("secondary", "sm", "flex-1")}>
              <Icon name="external" size={14} /> 열기
            </a>
            <a href={item.media_url} download={`acvm-${item.publication_id}`} className={buttonClass("secondary", "sm", "flex-1")}>
              <Icon name="download" size={14} /> 다운로드
            </a>
          </div>
        ) : (
          <p className="text-center text-xs text-muted">텍스트 전용 게시물</p>
        )}
      </div>

      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <PlatformTag platform={item.platform} className="font-medium" />
          <span className="text-sm text-ink-2">{item.account_handle}</span>
          {item.scheduled_at ? (
            <Pill tone={overdue ? "serious" : "neutral"} icon="clock" title={kst(item.scheduled_at)}>
              {overdue ? `예정 시각 지남 · ${relative(item.scheduled_at, now)}` : `${kst(item.scheduled_at)} 예정`}
            </Pill>
          ) : null}
          <span className="font-mono text-xs text-muted">{item.publication_id}</span>
          {item.content_id ? (
            <a href={`/contents/${encodeURIComponent(item.content_id)}`} className="text-xs text-accent-ink hover:underline">
              콘텐츠 상세
            </a>
          ) : null}
        </div>
        {item.link_url ? (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted">제휴 링크</span>
            <code className="break-all rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-ink">{item.link_url}</code>
            <CopyButton text={item.link_url} label="링크 복사" />
          </div>
        ) : null}

        <div className="rounded-lg border border-line">
          <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
            <span className="text-xs font-medium text-muted">캡션 (첫 줄 대가성 문구 포함 — 수정하지 마세요)</span>
            <CopyButton text={item.caption} label="캡션 복사" />
          </div>
          <p className="max-h-48 overflow-y-auto whitespace-pre-wrap px-3 py-2 text-sm leading-relaxed text-ink">{item.caption}</p>
        </div>

        {canPublish ? (
          <form
            className="flex flex-col gap-2 sm:flex-row sm:items-start"
            onSubmit={(e) => {
              e.preventDefault();
              if (!valid) return;
              done.mutate(
                { id: item.publication_id, permalink: permalink.trim() },
                { onSuccess: () => toast("게시 완료로 기록했습니다") },
              );
            }}
          >
            <div className="flex-1">
              <label htmlFor={inputId} className="sr-only">
                게시물 URL
              </label>
              <input
                id={inputId}
                type="url"
                inputMode="url"
                value={permalink}
                onChange={(e) => setPermalink(e.target.value)}
                placeholder="게시 후 게시물 URL 붙여넣기 (https://…)"
                aria-invalid={permalink.length > 0 && !valid}
                aria-describedby={`${inputId}-hint`}
                className="h-10 w-full rounded-lg border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none aria-[invalid=true]:border-critical"
              />
              <p id={`${inputId}-hint`} className={`mt-1 text-xs ${permalink && !valid ? "text-critical-ink" : "text-muted"}`}>
                {permalink && !valid ? "http(s) URL 형식이 아닙니다" : "URL은 수익 귀속(subId)과 감사 로그에 사용됩니다."}
              </p>
            </div>
            <Button type="submit" variant="success" icon="check" loading={done.isPending} disabled={!valid}>
              게시 완료
            </Button>
          </form>
        ) : null}
        {done.error ? <ErrorBanner error={done.error} compact /> : null}
      </div>
    </li>
  );
}

export function ManualQueueView() {
  const q = useManualQueue();
  const now = useNow();
  const { can } = usePrincipal();
  const canPublish = can("manualPublish");
  const items = q.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="수동 게시"
        description="공식 게시 API가 없는 채널(카카오 등)은 사람이 원클릭으로 게시합니다. 브라우저 매크로는 사용하지 않습니다 (§4 M8)."
      />
      <div className="space-y-4">
        <ol className="grid gap-2 text-[13px] text-ink-2 sm:grid-cols-4">
          {["캡션 복사", "미디어 다운로드", "카카오 채널에 게시", "게시물 URL 붙여넣고 완료"].map((s, i) => (
            <li key={s} className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2">
              <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-accent-soft text-[11px] font-bold text-accent-ink">{i + 1}</span>
              {s}
            </li>
          ))}
        </ol>
        {!canPublish && q.data ? <Notice>게시 완료 처리는 {ALLOWED_ROLES.manualPublish}만 할 수 있습니다.</Notice> : null}
        <Card>
          {q.isPending ? (
            <div className="p-5">
              <LoadingBlock rows={6} />
            </div>
          ) : q.error ? (
            <div className="p-4">
              <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
            </div>
          ) : !items.length ? (
            <EmptyState icon="send" title="수동 게시 대기 항목이 없습니다">
              카카오 대상 게시가 게이트를 통과하면 여기에 쌓입니다.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-line">
              {items.map((it) => (
                <QueueItem key={it.publication_id} item={it} canPublish={canPublish} now={now} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
