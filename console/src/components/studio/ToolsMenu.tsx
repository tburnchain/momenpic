"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, Notice } from "@/components/ui/Feedback";
import { Icon, type IconName } from "@/components/ui/Icon";
import { CopyButton, Segmented } from "@/components/ui/misc";
import { useToast } from "@/components/ui/Toast";
import { api } from "@/lib/api";
import { studioKeys, useStudioAssets, useStudioOptions } from "@/lib/queries";
import type { StudioAsset } from "@/lib/schemas";
import { AssetThumb, UploadBox } from "./AssetLibrary";
import { RIGHTS_LABEL } from "./shared";

type Tool = "cut" | "transcribe" | "i2v";

const TOOLS: Array<{ id: Tool; label: string; icon: IconName; hint: string }> = [
  { id: "cut", label: "컷편집", icon: "film", hint: "내 영상의 구간 자르기" },
  { id: "transcribe", label: "대본 추출 (내 영상)", icon: "text", hint: "직접 촬영한 영상만" },
  { id: "i2v", label: "이미지→영상 (모션)", icon: "play", hint: "카메라 모션만 적용" },
];

function AssetPicker({
  productId,
  selected,
  onSelect,
  filter,
  empty,
}: {
  productId: number | null;
  selected: string | null;
  onSelect: (a: StudioAsset) => void;
  filter: (a: StudioAsset) => boolean;
  empty: string;
}) {
  const q = useStudioAssets(productId);
  const items = (q.data?.items ?? []).filter(filter);
  if (q.isPending) return <p className="text-sm text-muted">불러오는 중…</p>;
  if (q.error) return <ErrorBanner error={q.error} compact />;
  if (!items.length) return <EmptyState icon="film" title={empty} className="py-6" />;
  return (
    <ul className="flex gap-2 overflow-x-auto pb-1" role="radiogroup" aria-label="소스 선택">
      {items.map((a) => (
        <li key={a.id} className="shrink-0">
          <button
            type="button"
            role="radio"
            aria-checked={selected === a.id}
            onClick={() => onSelect(a)}
            className={`block w-24 overflow-hidden rounded-lg border-2 text-left ${selected === a.id ? "border-accent" : "border-transparent hover:border-line-strong"}`}
          >
            <div className="aspect-[3/4] bg-surface-2">
              <AssetThumb asset={a} />
            </div>
            <p className="truncate px-1 py-0.5 text-[10px] text-muted">{RIGHTS_LABEL[a.rights_source ?? ""] ?? a.rights_source}</p>
          </button>
        </li>
      ))}
    </ul>
  );
}

function CutTool({ productId }: { productId: number | null }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [asset, setAsset] = useState<StudioAsset | null>(null);
  const [duration, setDuration] = useState(60);
  const [range, setRange] = useState<[number, number]>([0, 10]);
  const video = useRef<HTMLVideoElement>(null);
  const cut = useMutation({
    mutationFn: () => api.cutAsset(asset!.id, range[0], range[1]),
    onSuccess: () => {
      toast("잘라낸 구간을 새 소스로 저장했습니다");
      void qc.invalidateQueries({ queryKey: studioKeys.assetsAll });
    },
  });
  const step = 0.1;
  return (
    <div className="space-y-4">
      <AssetPicker productId={productId} selected={asset?.id ?? null} onSelect={(a) => setAsset(a)} filter={(a) => a.kind === "video"} empty="자를 영상이 없습니다" />
      {asset ? (
        <>
          {asset.url ? (
            <video
              ref={video}
              key={asset.id}
              src={asset.url}
              controls
              playsInline
              className="mx-auto max-h-72 rounded-lg bg-black"
              onLoadedMetadata={(e) => {
                const d = e.currentTarget.duration;
                if (Number.isFinite(d) && d > 0) {
                  setDuration(d);
                  setRange([0, Math.min(d, 10)]);
                }
              }}
            />
          ) : (
            <Notice>이 소스는 미리보기 URL이 없습니다. 시간을 직접 지정하세요.</Notice>
          )}
          <div className="space-y-2">
            <p className="text-sm tabular-nums text-ink">
              구간 <strong>{range[0].toFixed(1)}초</strong> – <strong>{range[1].toFixed(1)}초</strong>{" "}
              <span className="text-muted">({(range[1] - range[0]).toFixed(1)}초)</span>
            </p>
            <label className="block text-xs text-muted">
              시작
              <input
                type="range"
                min={0}
                max={duration}
                step={step}
                value={range[0]}
                onChange={(e) => {
                  const v = Math.min(Number(e.target.value), range[1] - step);
                  setRange([v, range[1]]);
                  if (video.current) video.current.currentTime = v;
                }}
                className="w-full"
                aria-valuetext={`${range[0].toFixed(1)}초`}
              />
            </label>
            <label className="block text-xs text-muted">
              끝
              <input
                type="range"
                min={0}
                max={duration}
                step={step}
                value={range[1]}
                onChange={(e) => {
                  const v = Math.max(Number(e.target.value), range[0] + step);
                  setRange([range[0], v]);
                  if (video.current) video.current.currentTime = v;
                }}
                className="w-full"
                aria-valuetext={`${range[1].toFixed(1)}초`}
              />
            </label>
          </div>
          <Button variant="primary" icon="film" loading={cut.isPending} onClick={() => cut.mutate()} disabled={range[1] <= range[0]}>
            이 구간으로 자르기
          </Button>
          {cut.error ? <ErrorBanner error={cut.error} compact /> : null}
        </>
      ) : null}
    </div>
  );
}

function TranscribeTool({ productId, onUseText }: { productId: number | null; onUseText?: (text: string) => void }) {
  const [asset, setAsset] = useState<StudioAsset | null>(null);
  const tr = useMutation({ mutationFn: () => api.transcribe(asset!.id) });
  return (
    <div className="space-y-4">
      <Notice>대본 추출은 권리 원장에 ‘직접 촬영’으로 등록된 내 영상에만 쓸 수 있습니다. 타인 영상의 대본을 가져와 재사용하지 않습니다 (§8.2).</Notice>
      <AssetPicker
        productId={productId}
        selected={asset?.id ?? null}
        onSelect={(a) => {
          setAsset(a);
          tr.reset();
        }}
        filter={(a) => a.kind === "video" && a.rights_source === "own_footage"}
        empty="직접 촬영한 영상이 없습니다 — 먼저 업로드하세요"
      />
      <UploadBox productId={productId} />
      {asset ? (
        <Button variant="primary" icon="text" loading={tr.isPending} onClick={() => tr.mutate()}>
          대본 추출
        </Button>
      ) : null}
      {tr.error ? <ErrorBanner error={tr.error} compact /> : null}
      {tr.data ? (
        <div className="space-y-2">
          <p className="text-xs text-muted">엔진: {tr.data.engine || "—"}</p>
          <textarea readOnly rows={6} value={tr.data.text} className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink" aria-label="추출된 대본" />
          <div className="flex gap-2">
            <CopyButton text={tr.data.text} label="대본 복사" />
            {onUseText ? (
              <Button size="sm" onClick={() => onUseText(tr.data.text)}>
                2번 비트 내레이션에 넣기
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

const MOTIONS = [
  { value: "zoom_in", label: "줌인" },
  { value: "zoom_out", label: "줌아웃" },
  { value: "pan_left", label: "← 패닝" },
  { value: "pan_right", label: "패닝 →" },
  { value: "pan_up", label: "↑ 패닝" },
] as const;

const AI_SCENE = "ai_generated_background";

function I2VTool({ productId }: { productId: number | null }) {
  const qc = useQueryClient();
  const toast = useToast();
  const options = useStudioOptions();
  const aiMotion = !!options.data?.integrations?.ai_motion;
  const [asset, setAsset] = useState<StudioAsset | null>(null);
  const [motion, setMotion] = useState<(typeof MOTIONS)[number]["value"]>("zoom_in");
  const [seconds, setSeconds] = useState(4);
  const [engine, setEngine] = useState<"camera" | "ai">("camera");
  const aiAllowed = aiMotion && asset?.rights_source === AI_SCENE;
  const useEngine = engine === "ai" && aiAllowed ? "ai" : "camera";
  const run = useMutation({
    mutationFn: () => api.imageToVideo(asset!.id, motion, useEngine === "ai" ? Math.max(5, Math.min(10, seconds)) : seconds, useEngine),
    onSuccess: () => {
      toast(useEngine === "ai" ? "AI 모션 영상을 새 소스로 저장했습니다" : "모션 영상을 새 소스로 저장했습니다");
      void qc.invalidateQueries({ queryKey: studioKeys.assetsAll });
    },
  });
  return (
    <div className="space-y-4">
      <Notice>
        상품 이미지는 변형하지 않습니다 — 상품 사진·직접 촬영본은 카메라 모션(줌·패닝)만 적용합니다. AI 모션(Higgsfield)은 AI가 만든 장면 이미지에만 쓸 수 있습니다
        (실물과 다른 묘사는 기만광고 위험, §4 M5).
      </Notice>
      <AssetPicker productId={productId} selected={asset?.id ?? null} onSelect={setAsset} filter={(a) => a.kind === "image"} empty="이미지 소스가 없습니다" />
      {asset ? (
        <>
          <Segmented
            label="엔진"
            size="sm"
            value={useEngine}
            onChange={(v) => setEngine(v)}
            options={[
              { value: "camera", label: "카메라 모션 (무료)" },
              { value: "ai", label: aiAllowed ? "AI 모션 (Higgsfield)" : "AI 모션 — AI 장면 이미지 전용", disabled: !aiAllowed },
            ]}
          />
          {!aiMotion ? <p className="text-xs text-muted">AI 모션은 운영 준비 › AI 연동에서 Higgsfield 키를 등록하면 켜집니다.</p> : null}
          <Segmented label="모션" size="sm" value={motion} onChange={setMotion} options={MOTIONS.map((m) => ({ value: m.value, label: m.label }))} />
          <label className="block text-sm text-ink">
            길이 <strong className="tabular-nums">{seconds}초</strong>
            <input type="range" min={1} max={15} step={1} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} className="mt-1 w-full" aria-valuetext={`${seconds}초`} />
          </label>
          {useEngine === "ai" ? <p className="text-xs text-muted">AI 모션은 5~10초 클립으로 생성되며 1~3분 걸립니다 (비용은 일 예산에 반영).</p> : null}
          <Button variant="primary" icon={useEngine === "ai" ? "sparkle" : "play"} loading={run.isPending} onClick={() => run.mutate()}>
            {useEngine === "ai" ? "AI 모션 영상 만들기" : "모션 영상 만들기"}
          </Button>
          {run.error ? <ErrorBanner error={run.error} compact /> : null}
        </>
      ) : null}
    </div>
  );
}

/** "툴" menu: 컷편집 · 대본 추출 · 이미지→영상, each a dialog over the asset library. */
export function ToolsMenu({ productId, disabled, onUseTranscript }: { productId: number | null; disabled?: boolean; onUseTranscript?: (t: string) => void }) {
  const [open, setOpen] = useState(false);
  const [tool, setTool] = useState<Tool | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

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

  const meta = TOOLS.find((t) => t.id === tool);

  return (
    <div ref={wrap} className="relative">
      <Button icon="settings" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="menu" disabled={disabled}>
        툴 <Icon name="chevronDown" size={14} />
      </Button>
      {open ? (
        <ul role="menu" className="absolute right-0 top-11 z-40 w-60 overflow-hidden rounded-xl border border-line bg-surface py-1 shadow-xl">
          {TOOLS.map((t) => (
            <li key={t.id} role="none">
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setTool(t.id);
                  setOpen(false);
                }}
                className="flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-surface-2"
              >
                <Icon name={t.icon} size={16} className="mt-0.5 text-ink-2" />
                <span>
                  <span className="block text-sm font-medium text-ink">{t.label}</span>
                  <span className="block text-xs text-muted">{t.hint}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <Dialog open={!!tool} onClose={() => setTool(null)} title={meta?.label ?? ""} description={meta?.hint} size="lg">
        {tool === "cut" ? <CutTool productId={productId} /> : null}
        {tool === "transcribe" ? (
          <TranscribeTool
            productId={productId}
            onUseText={
              onUseTranscript
                ? (t) => {
                    onUseTranscript(t);
                    setTool(null);
                  }
                : undefined
            }
          />
        ) : null}
        {tool === "i2v" ? <I2VTool productId={productId} /> : null}
      </Dialog>
    </div>
  );
}
