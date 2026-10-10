"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock, Notice, Skeleton } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { PageHeader, Segmented, Thumb } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { api } from "@/lib/api";
import { krw } from "@/lib/format";
import { PLATFORM_LABEL } from "@/lib/labels";
import { qk, studioKeys, useDraft, usePrincipal, useStudioOptions } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { StudioProduct, StudioPublishResult } from "@/lib/schemas";
import { PublishResult, TargetChips, type TargetAccount } from "./PreviewPanel";
import { ProductBrowser } from "./ProductBrowser";
import { TEXT_PLATFORMS } from "./shared";
import { StudioEditor } from "./StudioEditor";

type Kind = "short" | "text";

/** Shorts Studio: product → AI draft (every step pre-filled) → edit → render → gate → schedule. */
export function StudioView() {
  const router = useRouter();
  const params = useSearchParams();
  const draftId = params.get("draft");
  const qc = useQueryClient();
  const toast = useToast();
  const { can, role } = usePrincipal();
  const canCommand = can("command");

  const options = useStudioOptions();
  const draftQ = useDraft(draftId);
  const draft = draftQ.data;

  const [picked, setPicked] = useState<StudioProduct | null>(null);
  const [kindChoice, setKindChoice] = useState<Kind>("short");
  const [persona, setPersona] = useState("");
  const [targets, setTargets] = useState<string[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [confirmAuto, setConfirmAuto] = useState(false);
  const [autoResult, setAutoResult] = useState<StudioPublishResult | null>(null);

  const kind: Kind = draft && !picked ? draft.kind : kindChoice;
  const product = picked ?? (draft?.product ? ({ ...draft.product } as StudioProduct) : null);

  const accounts: TargetAccount[] = (options.data?.accounts ?? [])
    .filter((a) => a.status === "active" && (kind === "short" || TEXT_PLATFORMS.has(a.platform)))
    .map((a) => ({ id: a.id, platform: a.platform, handle: a.handle, status: a.status }));
  const validTargets = targets.filter((t) => accounts.some((a) => a.id === t));

  const openDraft = (id: string) => {
    setPicked(null);
    setAutoResult(null);
    router.replace(`/studio?draft=${encodeURIComponent(id)}`);
  };

  const create = useMutation({
    mutationFn: (p: StudioProduct) =>
      api.createDraft({
        product_id: p.id,
        kind,
        persona: persona || undefined,
        account_id: validTargets[0],
      }),
    onSuccess: (d) => {
      qc.setQueryData(studioKeys.draft(d.id), d);
      toast("AI 초안을 만들었습니다 — 모든 단계가 채워져 있어요");
      openDraft(d.id);
    },
  });

  const auto = useMutation({
    mutationFn: (p: StudioProduct) =>
      api.autoStudio({ product_id: p.id, kind, persona: persona || undefined, targets: validTargets }),
    onSuccess: (res) => {
      setAutoResult(res);
      void qc.invalidateQueries({ queryKey: qk.publicationsAll });
      void qc.invalidateQueries({ queryKey: qk.holds });
      toast(res.verdict === "PASS" ? "전부 자동 완료 — 예약 게시했습니다" : `전부 자동 완료 — 게이트 판정 ${res.verdict}`, res.verdict === "PASS" ? "good" : "info");
    },
  });

  const newVariant = () => {
    const p = draft?.product;
    if (p) create.mutate({ ...p } as StudioProduct);
  };

  const opts = options.data;

  return (
    <>
      <PageHeader
        title="쇼츠 스튜디오"
        description="상품 하나를 고르면 AI가 대본·소스·보이스·자막·워터마크를 모두 채웁니다. 그대로 ‘전부 자동’으로 보내거나, 단계별로 고친 뒤 생성·게시하세요."
      />

      {/* Top bar */}
      <Card className="mb-5">
        <CardBody className="space-y-4 pt-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              className="flex min-w-0 flex-1 items-center gap-3 rounded-xl border border-line-strong bg-surface p-2.5 text-left hover:border-accent"
              aria-label="상품 선택"
            >
              {product ? (
                <>
                  <Thumb src={product.image_url} alt={product.name} className="w-12 shrink-0 rounded-lg" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold text-ink">{product.name}</span>
                    <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                      {krw(product.price)}
                      {product.pos !== null && product.pos !== undefined ? <Pill tone={product.pos >= 0.7 ? "good" : "neutral"}>POS {product.pos.toFixed(2)}</Pill> : null}
                      {product.is_rocket ? <Pill tone="info">로켓</Pill> : null}
                    </span>
                  </span>
                </>
              ) : (
                <span className="flex items-center gap-2 px-1 text-sm text-muted">
                  <Icon name="grid" size={16} /> 상품 선택
                </span>
              )}
              <Icon name="chevronDown" size={16} className="text-muted" />
            </button>
            <div className="flex flex-wrap items-center gap-2">
              <Segmented<Kind>
                label="형식"
                value={kind}
                onChange={(k) => {
                  setKindChoice(k);
                  if (draft && !picked && draft.product) setPicked({ ...draft.product } as StudioProduct);
                }}
                options={[
                  { value: "short", label: "쇼츠" },
                  { value: "text", label: "글" },
                ]}
              />
              <label htmlFor="studio-persona" className="sr-only">
                페르소나
              </label>
              <select
                id="studio-persona"
                value={persona}
                onChange={(e) => setPersona(e.target.value)}
                className="h-10 rounded-lg border border-line-strong bg-surface px-3 text-sm text-ink focus:border-accent focus:outline-none"
              >
                <option value="">페르소나 자동</option>
                {(opts?.personas ?? []).map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <p className="text-[13px] font-semibold text-ink-2">게시 대상 {kind === "text" ? <span className="font-normal text-muted">· 글은 스레드·카카오만</span> : null}</p>
            {options.isPending ? (
              <Skeleton className="h-8 w-2/3" />
            ) : draft && !picked ? (
              <p className="text-xs text-muted">
                {validTargets.length ? `${validTargets.length}개 계정 선택됨` : "선택 안 함 (초안 기본 계정)"} — 아래 ‘검수 후 게시’에서 바꿀 수 있습니다.
              </p>
            ) : (
              <TargetChips accounts={accounts} selected={validTargets} disabled={!canCommand} onToggle={(id) => setTargets((t) => (t.includes(id) ? t.filter((x) => x !== id) : [...t, id]))} />
            )}
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs text-muted">
              {canCommand
                ? "‘전부 자동 생성’ = 초안 → 렌더 → 게이트(M7) → 예약을 한 번에. 게이트를 통과하지 못하면 게시되지 않습니다."
                : `스튜디오 실행은 ${ALLOWED_ROLES.command}만 할 수 있습니다${role ? ` (현재 역할: ${role})` : ""}.`}
            </p>
            <div className="flex gap-2">
              <Button icon="sparkle" disabled={!product || !canCommand || create.isPending || auto.isPending} loading={create.isPending} onClick={() => product && create.mutate(product)}>
                AI 초안 만들기
              </Button>
              <Button variant="primary" icon="play" disabled={!product || !canCommand || auto.isPending || create.isPending} loading={auto.isPending} onClick={() => setConfirmAuto(true)}>
                전부 자동 생성
              </Button>
            </div>
          </div>
          {create.error ? <ErrorBanner error={create.error} compact /> : null}
          {auto.error ? <ErrorBanner error={auto.error} compact /> : null}
          {auto.isPending ? <Notice>초안 → 렌더 → 게이트 → 예약을 진행 중입니다. 쇼츠는 30초~1분 정도 걸립니다…</Notice> : null}
          {autoResult ? (
            <div className="space-y-2">
              <PublishResult result={autoResult} contentId={autoResult.content_id} />
              {autoResult.content_id ? (
                <Button size="sm" icon="chevronRight" onClick={() => openDraft(String(autoResult.content_id))}>
                  스튜디오에서 열기
                </Button>
              ) : null}
            </div>
          ) : null}
        </CardBody>
      </Card>

      {/* Body */}
      {options.error ? (
        <ErrorBanner error={options.error} onRetry={() => void options.refetch()} retrying={options.isFetching} />
      ) : draftId ? (
        draftQ.isPending || !opts ? (
          <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
            <LoadingBlock rows={10} />
            <Skeleton className="aspect-[9/16] max-h-[560px]" />
          </div>
        ) : draftQ.error ? (
          <ErrorBanner error={draftQ.error} onRetry={() => void draftQ.refetch()} retrying={draftQ.isFetching} />
        ) : draft ? (
          <StudioEditor
            key={draft.id}
            draft={draft}
            options={opts}
            targets={validTargets}
            setTargets={setTargets}
            targetAccounts={accounts}
            onNewVariant={newVariant}
            newVariantPending={create.isPending}
          />
        ) : null
      ) : (
        <Card>
          <CardBody className="pt-5">
            {product ? (
              <EmptyState icon="sparkle" title={`‘${product.name}’ 준비 완료`}>
                ‘AI 초안 만들기’로 단계별 편집을 시작하거나 ‘전부 자동 생성’으로 바로 게이트까지 보내세요.
              </EmptyState>
            ) : (
              <>
                <p className="mb-3 text-sm font-semibold text-ink">상품을 골라 시작하세요</p>
                <ProductBrowser selectedId={null} onSelect={(p) => setPicked(p)} canRefresh={canCommand} />
              </>
            )}
          </CardBody>
        </Card>
      )}

      <Dialog open={pickerOpen} onClose={() => setPickerOpen(false)} title="상품 선택" description="POS 높은 순 · 가격은 게시 직전에 다시 확인됩니다 (C-03)" size="lg">
        <ProductBrowser
          selectedId={product?.id ?? null}
          canRefresh={canCommand}
          onSelect={(p) => {
            setPicked(p);
            setPickerOpen(false);
          }}
        />
      </Dialog>

      <Dialog
        open={confirmAuto}
        onClose={() => setConfirmAuto(false)}
        title="전부 자동으로 생성할까요?"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmAuto(false)}>
              취소
            </Button>
            <Button
              variant="primary"
              icon="play"
              onClick={() => {
                setConfirmAuto(false);
                if (product) auto.mutate(product);
              }}
            >
              실행
            </Button>
          </>
        }
      >
        <div className="space-y-2 text-sm text-ink-2">
          <p>
            <strong className="text-ink">{product?.name}</strong> · {kind === "short" ? "쇼츠" : "글"}
            {persona ? ` · ${persona}` : ""}
          </p>
          <p>
            게이트(M7)를 통과하면{" "}
            {validTargets.length
              ? validTargets
                  .map((id) => accounts.find((a) => a.id === id))
                  .filter(Boolean)
                  .map((a) => `${PLATFORM_LABEL[a!.platform]} ${a!.handle}`)
                  .join(", ")
              : "기본 계정"}
            에 시간대를 분산해 예약 게시합니다. HOLD·BLOCK이면 게시되지 않습니다.
          </p>
        </div>
      </Dialog>
    </>
  );
}
