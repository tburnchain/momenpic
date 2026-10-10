"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { fetchBlob } from "@/lib/api";

/* ------------------------------------------------------------------ authed blob URLs */

const blobCache = new Map<string, Promise<string>>();

/** Object URL for an authenticated binary endpoint (cached per path for the session). */
export function blobUrl(path: string): Promise<string> {
  let p = blobCache.get(path);
  if (!p) {
    p = fetchBlob(path).then((b) => URL.createObjectURL(b));
    p.catch(() => blobCache.delete(path));
    blobCache.set(path, p);
  }
  return p;
}

/** Resolves an authenticated image/audio endpoint to an object URL (null while loading or on error). */
export function useBlobUrl(path: string | null): { url: string | null; failed: boolean } {
  const [state, setState] = useState<{ path: string | null; url: string | null; failed: boolean }>({ path: null, url: null, failed: false });
  useEffect(() => {
    if (!path) return;
    let alive = true;
    blobUrl(path).then(
      (url) => alive && setState({ path, url, failed: false }),
      () => alive && setState({ path, url: null, failed: true }),
    );
    return () => {
      alive = false;
    };
  }, [path]);
  return state.path === path ? { url: state.url, failed: state.failed } : { url: null, failed: false };
}

/* ------------------------------------------------------------------ vocabulary */

export const BEAT_ROLE: Record<string, string> = {
  hook: "훅",
  problem: "문제 제기",
  amplify: "불편 증폭",
  product: "상품 등장",
  reveal: "상품 공개",
  benefits: "장점",
  benefit: "장점",
  demo: "사용 장면",
  proof: "근거",
  cta: "CTA",
};

export const EVIDENCE_LABEL: Record<string, string> = {
  review_count: "리뷰수",
  rating: "평점",
  price: "가격(시각)",
  price_checked_at: "가격(시각)",
  is_rocket: "로켓배송",
  is_free_shipping: "무료배송",
  name: "상품명",
  category: "카테고리",
};

export function evidenceLabel(key: string): string {
  if (key.startsWith("spec.")) return "스펙";
  return EVIDENCE_LABEL[key] ?? key;
}

export const RIGHTS_LABEL: Record<string, string> = {
  own_footage: "직접 촬영",
  coupang_product_image: "쿠팡 상품 이미지",
  licensed_stock: "라이선스 스톡",
  ai_generated_background: "AI 생성 장면",
  generated_card: "생성 카드",
};

export const POSITION_LABEL: Record<string, string> = { upper: "상단", middle: "중앙", bottom: "하단" };

export const HOOK_TYPE_LABEL: Record<string, string> = {
  question: "질문",
  shock: "충격",
  before_after: "전후비교",
  price_twist: "가격반전",
  empathy: "공감",
};

export const STUDIO_CATEGORIES: Array<{ id: string; label: string }> = [
  { id: "", label: "전체" },
  { id: "1013", label: "주방" },
  { id: "1018", label: "차량" },
  { id: "1014", label: "정리수납" },
  { id: "1015", label: "인테리어" },
  { id: "1016", label: "가전" },
  { id: "1029", label: "반려동물" },
];

/** Fast short-form delivery: ~8 Korean syllables per second (server: RenderSpec.target_speech_sps). Fallback only —
 * the editor prefers ``options.limits.syllables_per_second`` and the server's lint estimate. */
export const SYLLABLES_PER_SECOND = 8;
/** CTA hold appended after the last spoken word (server: estimate_seconds cta_tail). */
export const CTA_TAIL_SECONDS = 1.2;

export function syllableCount(text: string): number {
  return Array.from(text.replace(/[\s.,!?~·…"'“”‘’()\[\]{}:;\-–—/\\|#@*+=<>]/g, "")).length;
}

/** Text platforms per backend TEXT_CAPABLE (Threads, Kakao); shorts go anywhere. */
export const TEXT_PLATFORMS = new Set(["threads", "kakao"]);

/** CSS approximation of a caption style id for the live 9:16 mock (real tiles come from preview.png). */
export function captionCss(style: string | null | undefined): { className: string; style?: CSSProperties } {
  const stroke = (w: number): CSSProperties => ({
    WebkitTextStroke: `${w}px #000`,
    paintOrder: "stroke fill",
    textShadow: "0 2px 6px rgba(0,0,0,.45)",
  });
  switch (style) {
    case "box":
    case "box_dark":
      return { className: "rounded-md bg-black/65 px-2 py-1 font-bold text-white" };
    case "box_white":
      return { className: "rounded-md bg-white/95 px-2 py-1 font-bold text-[#111]" };
    case "news_bar":
      return { className: "bg-[#1b2a5a] px-2 py-1 font-bold text-white" };
    case "highlight":
      return { className: "bg-[#ffe14d] px-1.5 py-0.5 font-extrabold text-[#111]" };
    case "yellow":
    case "highlight_yellow":
      return { className: "font-extrabold text-[#ffe14d]", style: stroke(2) };
    case "mint_neon":
      return { className: "font-extrabold text-[#5ff5d0]", style: { textShadow: "0 0 8px rgba(95,245,208,.8), 0 2px 4px rgba(0,0,0,.6)" } };
    case "pink_pop":
      return { className: "font-black text-white", style: { ...stroke(3), textShadow: "0 0 0 transparent" } };
    case "bold":
      return { className: "font-black text-white", style: stroke(3) };
    case "minimal":
      return { className: "text-[13px] font-medium text-white", style: { textShadow: "0 1px 4px rgba(0,0,0,.6)" } };
    case "pop":
      return { className: "font-black text-[#ffd400]", style: stroke(2) };
    default:
      return { className: "font-extrabold text-white", style: stroke(2) };
  }
}
