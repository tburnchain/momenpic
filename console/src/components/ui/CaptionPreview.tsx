"use client";

import { Icon } from "./Icon";

/** D-01: the disclosure must be the first line of the caption/body. */
export function hasDisclosure(line: string): boolean {
  return /쿠팡\s*파트너스/.test(line) && /수수료/.test(line);
}

/** Caption with the first line (disclosure slot) highlighted and checked. */
export function CaptionPreview({ text, compact = false }: { text: string | null | undefined; compact?: boolean }) {
  if (!text) return <p className="text-[13px] text-muted">캡션 없음</p>;
  const [first, ...rest] = text.split("\n");
  const ok = hasDisclosure(first);
  const body = rest.join("\n").trim();
  return (
    <div className="space-y-2">
      <div className="overflow-hidden rounded-lg border border-line text-sm leading-relaxed">
        <p
          className={`px-3 py-2 font-medium ${body ? "border-b" : ""} ${
            ok ? "border-good/30 bg-good-soft text-ink" : "border-critical/30 bg-critical-soft text-ink"
          }`}
        >
          <span className="sr-only">첫 줄: </span>
          {first}
        </p>
        {body ? (
          <p className={`whitespace-pre-wrap px-3 py-2 text-ink-2 ${compact ? "max-h-40 overflow-y-auto" : ""}`}>{body}</p>
        ) : null}
      </div>
      <p className={`flex items-center gap-1.5 text-xs ${ok ? "text-good-ink" : "text-critical-ink"}`}>
        <Icon name={ok ? "check" : "alert"} size={13} />
        {ok ? "D-01 대가성 문구가 첫 줄에 있습니다" : "첫 줄에 대가성 문구가 없습니다 — 게시 전 확인이 필요합니다"}
      </p>
    </div>
  );
}
