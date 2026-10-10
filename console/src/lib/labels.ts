/** Korean labels and visual tones for the closed vocabularies of the API. */
import { kstTime } from "./format";
import type { AccountStatus, Platform, Role, RunStatus, Verdict } from "./schemas";

export type Tone = "neutral" | "info" | "good" | "warn" | "serious" | "critical";

export const PLATFORM_LABEL: Record<Platform, string> = {
  threads: "스레드",
  instagram: "인스타그램",
  youtube: "유튜브",
  tiktok: "틱톡",
  facebook: "페이스북",
  kakao: "카카오",
};

/** Fixed categorical slot per platform — colour follows the entity everywhere (never its rank). */
export const PLATFORM_SLOT: Record<Platform, number> = {
  threads: 1,
  instagram: 2,
  youtube: 3,
  tiktok: 4,
  facebook: 5,
  kakao: 6,
};

export function platformLabel(p: string | null | undefined): string {
  if (!p) return "—";
  return PLATFORM_LABEL[p as Platform] ?? p;
}

export function platformColor(p: string | null | undefined): string {
  const slot = PLATFORM_SLOT[p as Platform];
  return slot ? `var(--s${slot})` : "var(--axis)";
}

export const RUN_STATUS: Record<RunStatus, { label: string; tone: Tone }> = {
  planned: { label: "계획됨", tone: "neutral" },
  awaiting_approval: { label: "승인 대기", tone: "warn" },
  approved: { label: "승인됨", tone: "info" },
  running: { label: "실행 중", tone: "info" },
  paused_budget: { label: "예산 일시정지", tone: "serious" },
  completed: { label: "완료", tone: "good" },
  failed: { label: "실패", tone: "critical" },
  rejected: { label: "반려", tone: "neutral" },
};

export const VERDICT: Record<Verdict, { label: string; tone: Tone; hint: string }> = {
  PASS: { label: "통과", tone: "good", hint: "모든 규칙 통과 — 배포 가능" },
  FIX: { label: "자동수정", tone: "warn", hint: "자동 수정 후 재검사" },
  HOLD: { label: "보류", tone: "serious", hint: "사람 승인 큐로 이동" },
  BLOCK: { label: "차단", tone: "critical", hint: "배포 불가" },
};

export const PUBLICATION_STATE: Record<string, { label: string; tone: Tone }> = {
  QUEUED: { label: "대기", tone: "neutral" },
  UPLOADING: { label: "업로드 중", tone: "info" },
  PROCESSING: { label: "처리 중", tone: "info" },
  PUBLISHED: { label: "게시됨", tone: "good" },
  FAILED: { label: "실패", tone: "critical" },
  EXPIRED: { label: "만료", tone: "serious" },
  MANUAL_PENDING: { label: "수동 게시 대기", tone: "warn" },
  CANCELLED: { label: "취소", tone: "neutral" },
  TAKEN_DOWN: { label: "비공개 전환", tone: "serious" },
};

export function publicationState(state: string): { label: string; tone: Tone } {
  return PUBLICATION_STATE[state] ?? { label: state, tone: "neutral" };
}

export const ACCOUNT_STATUS: Record<AccountStatus, { label: string; tone: Tone }> = {
  active: { label: "활성", tone: "good" },
  cooldown: { label: "쿨다운", tone: "warn" },
  restricted: { label: "제한", tone: "critical" },
};

export const ROLE_LABEL: Record<Role, string> = {
  owner: "소유자",
  operator: "운영자",
  reviewer: "검수자",
  viewer: "열람자",
};

const PLACEMENT: Record<string, string> = {
  body: "본문 링크",
  first_comment: "첫 댓글",
  comment: "첫 댓글",
  hub: "링크 허브",
  link_hub: "링크 허브",
  profile: "프로필 링크",
  profile_hub: "프로필 허브",
  description: "설명란",
  caption: "캡션 링크",
  manual: "수동",
};
export function placementLabel(p: string): string {
  return PLACEMENT[p] ?? p;
}
/** Placements where the link is clickable inside the post itself (§4 M3). */
export function placementClickable(p: string): boolean {
  return p === "body" || p === "caption" || p === "first_comment" || p === "comment";
}

const FORMAT: Record<string, string> = {
  text: "텍스트",
  "text+video": "텍스트+영상",
  reels: "릴스",
  shorts: "쇼츠",
  video: "영상",
  post: "게시물",
  manual: "수동 게시",
};
export function formatLabel(f: string): string {
  return FORMAT[f] ?? f;
}

export const KIND_LABEL: Record<string, string> = { text: "글", short: "쇼츠" };

const INTENT: Record<string, string> = {
  create_and_publish: "만들어서 게시",
  create_only: "만들기만",
  analyze: "분석",
};
export function intentLabel(i: string | null | undefined): string {
  if (!i) return "—";
  return INTENT[i] ?? i;
}

/** Backend schedule slot ("now" | "tonight" | "daily@HH:MM" | ISO datetime) → Korean. */
export function scheduleLabel(s: string | null | undefined): string | null {
  if (!s) return null;
  if (s === "now") return "지금";
  if (s === "tonight") return "오늘 밤";
  const daily = /^daily@(\d{1,2}):(\d{2})$/.exec(s);
  if (daily) return `매일 ${daily[1].padStart(2, "0")}:${daily[2]}`;
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
    const d = new Date(s);
    if (!Number.isNaN(d.getTime())) {
      return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(d);
    }
  }
  return s;
}

/** Runs that have not started producing yet — elapsed time is meaningless for them. */
export function runHasDuration(status: string): boolean {
  return !(status === "awaiting_approval" || status === "planned" || status === "rejected");
}

/** §3 module names for DAG nodes. */
export const MODULE_LABEL: Record<string, string> = {
  M1: "상품 인텔리전스",
  M2: "레퍼런스 DNA",
  M3: "링크",
  M4: "스크립트",
  M5: "에셋",
  M6: "렌더",
  M7: "컴플라이언스 게이트",
  M8: "배포",
  M9: "분석",
  M10: "학습",
};

/** Extracts the verdict prefix from a rule's `on_fail` text ("FIX(자동 삽입)" → FIX). */
export function verdictFromText(text: string): Verdict | null {
  const m = /^(PASS|FIX|HOLD|BLOCK)/.exec(text.trim().toUpperCase());
  return m ? (m[1] as Verdict) : null;
}

/**
 * Run summary → one line. The contract describes a sentence; the shipped backend stores a dict
 * ({selected: [...ids], verdicts: {PASS: n}, products: [...], analysis, first_publish_at}).
 */
export function summaryText(summary: unknown): string | null {
  if (summary === null || summary === undefined) return null;
  if (typeof summary === "string") return summary || null;
  if (typeof summary !== "object" || Array.isArray(summary)) return String(summary);
  const o = summary as Record<string, unknown>;
  const parts: string[] = [];
  const handled = new Set(["selected", "verdicts", "products", "analysis", "first_publish_at"]);
  if (Array.isArray(o.selected)) parts.push(`상품 ${o.selected.length}개 선택`);
  if (o.verdicts && typeof o.verdicts === "object") {
    const v = Object.entries(o.verdicts as Record<string, unknown>)
      .map(([k, n]) => `${k} ${String(n)}`)
      .join(" · ");
    if (v) parts.push(v);
  }
  if (Array.isArray(o.products)) parts.push(`결과 ${o.products.length}건`);
  if (typeof o.analysis === "string") parts.push(o.analysis === "see /dashboards" ? "분석 완료 — 대시보드 참조" : o.analysis);
  if (typeof o.first_publish_at === "string") parts.push(`첫 게시 ${kstTime(o.first_publish_at)}`);
  let extra = 0;
  for (const [k, v] of Object.entries(o)) {
    if (handled.has(k) || extra >= 3) continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
      parts.push(`${k}: ${String(v)}`);
      extra++;
    }
  }
  return parts.join(" · ") || null;
}
