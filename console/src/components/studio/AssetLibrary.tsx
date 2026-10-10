"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Segmented, Thumb } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { MAX_UPLOAD_BYTES, UPLOAD_TYPES, uploadAsset } from "@/lib/api";
import { kstDate } from "@/lib/format";
import { studioKeys, useStudioAssets } from "@/lib/queries";
import type { StudioAsset } from "@/lib/schemas";
import { RIGHTS_LABEL } from "./shared";

export function AssetThumb({ asset, className = "" }: { asset: Pick<StudioAsset, "kind" | "url"> | null | undefined; className?: string }) {
  if (asset?.kind === "video" && asset.url) {
    return (
      <video
        src={asset.url}
        muted
        playsInline
        preload="metadata"
        className={`h-full w-full bg-black object-cover ${className}`}
        aria-label="영상 소스 미리보기"
      />
    );
  }
  if (asset?.kind === "video") {
    return (
      <div className={`flex h-full w-full items-center justify-center bg-surface-3 text-muted ${className}`} role="img" aria-label="영상 (미리보기 없음)">
        <Icon name="film" size={20} />
      </div>
    );
  }
  return <Thumb src={asset?.url} alt="이미지 소스" className={`h-full w-full ${className}`} />;
}

/** Own-footage upload with the mandatory rights attestation (§8.2). */
export function UploadBox({ productId, onUploaded }: { productId: number | null; onUploaded?: (a: StudioAsset) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const id = useId();
  const [attest, setAttest] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [local, setLocal] = useState<string | null>(null);

  const onFile = async (file: File | undefined) => {
    setError(null);
    setLocal(null);
    if (!file) return;
    if (!UPLOAD_TYPES.includes(file.type)) {
      setLocal(`지원하지 않는 형식입니다 (${file.type || "알 수 없음"}) — mp4·mov·webm·png·jpg·webp`);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setLocal("파일이 너무 큽니다 (최대 300MB)");
      return;
    }
    setProgress(0);
    try {
      const a = await uploadAsset(file, { attest, productId, onProgress: setProgress });
      toast("업로드 완료 — 권리 원장에 '직접 촬영'으로 기록했습니다");
      await qc.invalidateQueries({ queryKey: studioKeys.assetsAll });
      onUploaded?.(a);
    } catch (e) {
      setError(e);
    } finally {
      setProgress(null);
    }
  };

  return (
    <div className="space-y-2 rounded-xl border border-dashed border-line-strong p-3">
      <label className="flex items-start gap-2 text-[13px] text-ink">
        <input type="checkbox" checked={attest} onChange={(e) => setAttest(e.target.checked)} className="mt-0.5 h-4 w-4 accent-[var(--accent)]" />
        <span>
          <strong>직접 촬영했거나 권리를 보유한 영상입니다</strong> (타인 영상 업로드 금지)
        </span>
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <label
          htmlFor={id}
          className={`inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-lg border px-3 text-sm font-medium ${
            attest && progress === null ? "border-line-strong bg-surface text-ink hover:bg-surface-2" : "pointer-events-none border-line bg-surface-2 text-muted opacity-60"
          }`}
        >
          <Icon name="download" size={15} className="rotate-180" /> 파일 선택
        </label>
        <input
          id={id}
          type="file"
          accept={UPLOAD_TYPES.join(",")}
          disabled={!attest || progress !== null}
          className="sr-only"
          onChange={(e) => {
            void onFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
        <span className="text-xs text-muted">mp4 · mov · webm · png · jpg · webp, 최대 300MB</span>
      </div>
      {progress !== null ? (
        <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)} aria-label="업로드 진행률">
          <div className="h-1.5 overflow-hidden rounded-full bg-accent-soft">
            <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${Math.max(3, progress * 100)}%` }} />
          </div>
          <p className="mt-1 text-xs tabular-nums text-muted">업로드 중 {Math.round(progress * 100)}%</p>
        </div>
      ) : null}
      {local ? <p className="text-xs text-critical-ink">{local}</p> : null}
      {error ? <ErrorBanner error={error} compact /> : null}
    </div>
  );
}

type KindFilter = "all" | "video" | "image";

/**
 * Asset library drawer. In pick mode (`onPick`) clicking an asset assigns it; otherwise it is a browser.
 */
export function AssetLibrary({
  open,
  onClose,
  productId,
  title = "소스 라이브러리",
  onPick,
  filter,
  canUpload,
}: {
  open: boolean;
  onClose: () => void;
  productId: number | null;
  title?: string;
  onPick?: (a: StudioAsset) => void;
  filter?: (a: StudioAsset) => boolean;
  canUpload: boolean;
}) {
  const q = useStudioAssets(productId);
  const [kind, setKind] = useState<KindFilter>("all");
  const items = (q.data?.items ?? []).filter((a) => (kind === "all" || a.kind === kind) && (!filter || filter(a)));

  return (
    <Dialog open={open} onClose={onClose} variant="side" title={title} description="권리 원장에 등록된 소스만 렌더에 사용됩니다 (R-01)">
      <div className="space-y-4">
        {canUpload ? <UploadBox productId={productId} /> : <Notice>업로드는 소유자·운영자만 할 수 있습니다.</Notice>}
        <Segmented<KindFilter>
          label="종류"
          size="sm"
          value={kind}
          onChange={setKind}
          options={[
            { value: "all", label: "전체" },
            { value: "video", label: "영상" },
            { value: "image", label: "이미지" },
          ]}
        />
        {q.isPending ? (
          <LoadingBlock rows={4} />
        ) : q.error ? (
          <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />
        ) : !items.length ? (
          <EmptyState icon="film" title="소스가 없습니다">
            직접 촬영한 B-roll을 올리면 자동 선택보다 우선 사용할 수 있어요.
          </EmptyState>
        ) : (
          <ul className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
            {items.map((a) => (
              <li key={a.id}>
                <button
                  type="button"
                  disabled={!onPick}
                  onClick={() => onPick?.(a)}
                  className="group block w-full overflow-hidden rounded-xl border border-line text-left transition-colors enabled:hover:border-accent disabled:cursor-default"
                >
                  <div className="aspect-[9/12] overflow-hidden bg-surface-2">
                    <AssetThumb asset={a} />
                  </div>
                  <div className="space-y-1 p-2">
                    <div className="flex flex-wrap gap-1">
                      <Pill tone={a.kind === "video" ? "info" : "neutral"} icon={a.kind === "video" ? "film" : undefined}>
                        {a.kind === "video" ? "영상" : "이미지"}
                      </Pill>
                    </div>
                    <p className="truncate text-[11px] text-muted">{RIGHTS_LABEL[a.rights_source ?? ""] ?? a.rights_source}</p>
                    <p className="truncate text-[11px] text-muted">{kstDate(a.created_at)}</p>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
        {onPick ? (
          <div className="flex justify-end">
            <Button variant="ghost" onClick={onClose}>
              닫기
            </Button>
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}
