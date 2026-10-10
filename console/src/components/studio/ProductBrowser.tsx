"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Thumb } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { api } from "@/lib/api";
import { int, krw, relative } from "@/lib/format";
import { useDebounced, useNow } from "@/lib/hooks";
import { studioKeys, useStudioProducts } from "@/lib/queries";
import type { StudioProduct } from "@/lib/schemas";
import { STUDIO_CATEGORIES } from "./shared";

function ProductCard({ p, selected, onSelect, now }: { p: StudioProduct; selected: boolean; onSelect: () => void; now: number }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`flex w-full gap-3 rounded-xl border p-2.5 text-left transition-colors ${selected ? "border-accent bg-accent-soft" : "border-line bg-surface hover:border-line-strong"}`}
    >
      <Thumb src={p.image_url} alt={p.name} className="w-16 shrink-0 rounded-lg" />
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-sm font-medium text-ink">{p.name}</span>
        <span className="mt-0.5 block text-sm font-semibold tabular-nums text-ink">{krw(p.price)}</span>
        <span className="mt-1 flex flex-wrap items-center gap-1">
          {p.pos !== null && p.pos !== undefined ? (
            <Pill tone={p.pos >= 0.7 ? "good" : "neutral"} title="상품 기회 점수 (POS)">
              POS {p.pos.toFixed(2)}
            </Pill>
          ) : null}
          {p.is_rocket ? <Pill tone="info">로켓</Pill> : null}
          {p.rating ? (
            <span className="text-[11px] text-muted">
              ★ {p.rating} · 리뷰 {int(p.review_count)}
            </span>
          ) : null}
        </span>
        {p.price_checked_at ? <span className="mt-0.5 block text-[10px] text-muted">가격 확인 {relative(p.price_checked_at, now)}</span> : null}
      </span>
    </button>
  );
}

/** Product search: category chips + query, cards with image, price, POS and 로켓 badges. */
export function ProductBrowser({
  selectedId,
  onSelect,
  canRefresh,
}: {
  selectedId: number | null;
  onSelect: (p: StudioProduct) => void;
  canRefresh: boolean;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const now = useNow();
  const [category, setCategory] = useState("");
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 300);
  const params = { category: category || undefined, q: q || undefined };
  const res = useStudioProducts(params);
  const [confirm, setConfirm] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<unknown>(null);

  const refresh = async () => {
    setConfirm(false);
    setRefreshing(true);
    setRefreshError(null);
    try {
      const data = await api.studioProducts({ ...params, refresh: true, limit: 40 });
      qc.setQueryData(studioKeys.products(params), data);
      toast(`쿠팡에서 상품 ${data.items.length}개를 새로 가져왔습니다`);
    } catch (e) {
      setRefreshError(e);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor="studio-product-q" className="sr-only">
          상품 검색
        </label>
        <input
          id="studio-product-q"
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="상품명 검색 (예: 밀봉 클립)"
          className="h-10 min-w-0 flex-1 rounded-lg border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none"
        />
        {canRefresh ? (
          <Button icon="refresh" loading={refreshing} onClick={() => setConfirm(true)} title="쿠팡 파트너스 API로 최신 상품 가져오기">
            쿠팡에서 새로고침
          </Button>
        ) : null}
      </div>
      <div role="group" aria-label="카테고리" className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1">
        {STUDIO_CATEGORIES.map((c) => (
          <button
            key={c.id || "all"}
            type="button"
            aria-pressed={category === c.id}
            onClick={() => setCategory(c.id)}
            className={`h-8 shrink-0 rounded-full border px-3 text-[13px] font-medium ${
              category === c.id ? "border-accent bg-accent-soft text-accent-ink" : "border-line-strong bg-surface text-ink-2 hover:border-accent"
            }`}
          >
            {c.label}
          </button>
        ))}
      </div>
      {refreshError ? <ErrorBanner error={refreshError} compact /> : null}
      {res.isPending ? (
        <LoadingBlock rows={5} />
      ) : res.error ? (
        <ErrorBanner error={res.error} onRetry={() => void res.refetch()} retrying={res.isFetching} />
      ) : !res.data.items.length ? (
        <EmptyState icon="grid" title="조건에 맞는 상품이 없습니다">
          {canRefresh ? "‘쿠팡에서 새로고침’으로 최신 상품을 가져올 수 있습니다." : "다른 카테고리나 검색어를 시도하세요."}
        </EmptyState>
      ) : (
        <ul className={`grid gap-2 sm:grid-cols-2 xl:grid-cols-3 ${res.isPlaceholderData ? "opacity-60" : ""}`}>
          {res.data.items.map((p) => (
            <li key={p.id}>
              <ProductCard p={p} selected={p.id === selectedId} onSelect={() => onSelect(p)} now={now} />
            </li>
          ))}
        </ul>
      )}
      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="쿠팡 API로 새로고침할까요?"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              취소
            </Button>
            <Button variant="primary" icon="refresh" onClick={() => void refresh()}>
              새로고침
            </Button>
          </>
        }
      >
        <p className="flex items-start gap-2 text-sm text-ink-2">
          <Icon name="alert" size={16} className="mt-0.5 text-warn-ink" />
          쿠팡 검색 API는 시간당 호출 한도가 빡빡합니다(시간당 10회 수준, 연속 403 시 제재 위험). 호출 예산 1회를 사용합니다.
        </p>
      </Dialog>
    </div>
  );
}
