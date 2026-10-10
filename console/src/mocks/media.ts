/**
 * Self-contained SVG placeholders for mock mode (data: URIs — no network, no external hosts).
 * Posters mimic the M6 render template: 9:16 frame with the mandatory D-03 "광고" overlay
 * fixed at the top for the first 3 seconds.
 */

const GRADIENTS: [string, string][] = [
  ["#1c5cab", "#2a78d6"],
  ["#b24a1f", "#eb6834"],
  ["#0f7a55", "#1baf7a"],
  ["#4a3aa7", "#7a6ae0"],
  ["#a8335f", "#e87ba4"],
  ["#3d3d3a", "#6b6a65"],
];

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function wrap(text: string, perLine: number, maxLines: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > perLine && cur) {
      lines.push(cur);
      cur = w;
    } else {
      cur = (cur + " " + w).trim();
    }
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    kept[maxLines - 1] = kept[maxLines - 1].replace(/.{0,1}$/, "…");
    return kept;
  }
  return lines;
}

function toDataUri(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export const VIDEO_DISCLOSURE = "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다";

/** 9:16 short-form poster with disclosure overlay, hook and product name. */
export function posterSvg(hook: string, product: string, seed: number): string {
  const [a, b] = GRADIENTS[Math.abs(seed) % GRADIENTS.length];
  const lines = wrap(hook, 9, 4);
  const startY = 220 - (lines.length - 1) * 17;
  const hookText = lines
    .map(
      (l, i) =>
        `<text x="135" y="${startY + i * 34}" text-anchor="middle" font-size="25" font-weight="700" fill="#fff">${esc(l)}</text>`,
    )
    .join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 270 480" width="270" height="480" font-family="Pretendard, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif">
<defs><linearGradient id="g" x1="0" y1="0" x2="0.6" y2="1"><stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs>
<rect width="270" height="480" fill="url(#g)"/>
<rect width="270" height="30" fill="#000" fill-opacity="0.55"/>
<text x="135" y="19.5" text-anchor="middle" font-size="10.5" fill="#fff">${esc(VIDEO_DISCLOSURE)}</text>
${hookText}
<rect x="20" y="392" width="230" height="58" rx="10" fill="#000" fill-opacity="0.35"/>
<text x="135" y="417" text-anchor="middle" font-size="13" fill="#fff" fill-opacity="0.9">${esc(product)}</text>
<text x="135" y="437" text-anchor="middle" font-size="11" fill="#fff" fill-opacity="0.7">AI 생성 콘텐츠 · 실물 이미지 미변형</text>
</svg>`;
  return toDataUri(svg);
}

/** Square product image placeholder. */
export function productSvg(name: string, seed: number): string {
  const [a, b] = GRADIENTS[Math.abs(seed + 2) % GRADIENTS.length];
  const initial = esc(Array.from(name.trim())[0] ?? "?");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200" font-family="Pretendard, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs>
<rect width="200" height="200" rx="16" fill="url(#g)"/>
<text x="100" y="124" text-anchor="middle" font-size="76" font-weight="700" fill="#fff" fill-opacity="0.92">${initial}</text>
</svg>`;
  return toDataUri(svg);
}
