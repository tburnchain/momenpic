"use client";

import { useState } from "react";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import type { StudioAsset } from "@/lib/schemas";
import { AssetLibrary, AssetThumb } from "./AssetLibrary";
import { BEAT_ROLE } from "./shared";

export interface SourceBeat {
  role: string;
  start: number;
  end: number;
  asset: Pick<StudioAsset, "id" | "kind" | "url"> | null;
}

/** ① 영상소스 — which visual each beat uses: "자동 선택" (M5 priority order) unless overridden. */
export function SourcePanel({
  beats,
  overrides,
  limit,
  productId,
  readOnly,
  canUpload,
  onAssign,
  aiNote,
  aiErrors = [],
}: {
  aiNote?: string | null;
  aiErrors?: string[];
  beats: SourceBeat[];
  overrides: Record<string, string>;
  limit: number;
  productId: number | null;
  readOnly: boolean;
  canUpload: boolean;
  onAssign: (beatIndex: number, asset: StudioAsset | null) => void;
}) {
  const [picking, setPicking] = useState<number | null>(null);
  const used = new Set(Object.values(overrides)).size;

  return (
    <Card aria-labelledby="src-title">
      <CardHeader
        id="src-title"
        title={
          <span className="flex items-center gap-2">
            <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-accent-fill text-xs font-bold text-white">1</span>
            영상소스
          </span>
        }
        description="비워 두면 권리 원장 우선순위(직접 촬영 → 상품 이미지 → 스톡 → AI 생성 장면 → 카드)로 자동 선택됩니다."
        actions={<span className="text-xs tabular-nums text-muted">지정 {used}/{limit}</span>}
      />
      <CardBody>
        {aiNote ? (
          <p className="mb-3 flex items-start gap-1.5 rounded-lg bg-accent-soft px-3 py-2 text-xs text-accent-ink">
            <Icon name="sparkle" size={13} className="mt-0.5 shrink-0" />
            <span>
              {aiNote}
              {aiErrors.length ? <span className="block text-warn-ink">최근 생성 실패 — 카드로 대체됨: {aiErrors[0]}</span> : null}
            </span>
          </p>
        ) : null}
        {beats.length ? (
          <ol className="grid grid-cols-5 gap-2">
            {beats.map((b, i) => {
              const overridden = !!overrides[String(i)];
              const full = !overridden && used >= limit;
              return (
                <li key={i} className="min-w-0">
                  <button
                    type="button"
                    disabled={readOnly || full}
                    onClick={() => setPicking(i)}
                    title={full ? `직접 지정은 최대 ${limit}개` : "소스 변경"}
                    className={`relative block aspect-[9/14] w-full overflow-hidden rounded-lg border transition-colors enabled:hover:border-accent disabled:cursor-not-allowed ${
                      overridden ? "border-accent" : "border-dashed border-line-strong"
                    }`}
                  >
                    {overridden && b.asset ? (
                      <AssetThumb asset={b.asset} />
                    ) : (
                      <span className="flex h-full flex-col items-center justify-center gap-1 bg-surface-2 px-1 text-center text-[11px] text-muted">
                        <Icon name="sparkle" size={14} />
                        자동 선택
                      </span>
                    )}
                  </button>
                  <p className="mt-1 truncate text-center text-[11px] font-medium text-ink-2">{BEAT_ROLE[b.role] ?? b.role}</p>
                  <p className="text-center text-[10px] tabular-nums text-muted">
                    {b.start}–{b.end}초
                  </p>
                  {overridden && !readOnly ? (
                    <button type="button" onClick={() => onAssign(i, null)} className="mx-auto block text-[11px] text-accent-ink hover:underline">
                      자동으로
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="text-sm text-muted">글 콘텐츠는 영상 소스가 필요 없습니다.</p>
        )}
      </CardBody>
      <AssetLibrary
        open={picking !== null}
        onClose={() => setPicking(null)}
        productId={productId}
        canUpload={canUpload}
        title={picking !== null ? `${picking + 1}번 비트 (${BEAT_ROLE[beats[picking]?.role ?? ""] ?? ""}) 소스 선택` : "소스 선택"}
        onPick={(a) => {
          if (picking !== null) onAssign(picking, a);
          setPicking(null);
        }}
      />
    </Card>
  );
}
