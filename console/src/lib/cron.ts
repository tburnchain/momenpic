/** Human-readable Korean description of simple 5-field cron expressions (KST). */
const DOW = ["일", "월", "화", "수", "목", "금", "토"];

function isNum(s: string) {
  return /^\d+$/.test(s);
}

export function describeCron(expr: string | null | undefined): string {
  if (!expr) return "—";
  const f = expr.trim().split(/\s+/);
  if (f.length !== 5) return expr;
  const [min, hour, dom, mon, dow] = f;
  const time = isNum(min) && isNum(hour) ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;

  if (/^\*\/\d+$/.test(min) && hour === "*" && dom === "*" && mon === "*" && dow === "*") return `${min.slice(2)}분마다`;
  if (min === "0" && /^\*\/\d+$/.test(hour) && dom === "*" && mon === "*" && dow === "*") return `${hour.slice(2)}시간마다 정각`;
  if (!time) return expr;
  if (dom === "*" && mon === "*" && dow === "*") return `매일 ${time}`;
  if (dom === "*" && mon === "*" && dow === "1-5") return `평일 ${time}`;
  if (dom === "*" && mon === "*" && (dow === "0,6" || dow === "6,0")) return `주말 ${time}`;
  if (dom === "*" && mon === "*" && /^[0-7](,[0-7])*$/.test(dow)) {
    return `매주 ${dow
      .split(",")
      .map((d) => DOW[Number(d) % 7])
      .join("·")}요일 ${time}`;
  }
  if (isNum(dom) && mon === "*" && dow === "*") return `매월 ${dom}일 ${time}`;
  return expr;
}

/** Light validation: five whitespace-separated fields made of cron characters. */
export function isValidCron(expr: string): boolean {
  const f = expr.trim().split(/\s+/);
  return f.length === 5 && f.every((x) => /^[\d*/,\-]+$/.test(x));
}
