export type ApiErrorKind = "network" | "timeout" | "http" | "schema";

/** Normalised error thrown by the API client (real or mock). */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly detail?: string;
  readonly issues?: string[];
  readonly path?: string;

  constructor(
    kind: ApiErrorKind,
    message: string,
    opts: { status?: number; detail?: string; issues?: string[]; path?: string } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = opts.status;
    this.detail = opts.detail;
    this.issues = opts.issues;
    this.path = opts.path;
  }

  get isAuth(): boolean {
    return this.kind === "http" && (this.status === 401 || this.status === 403);
  }

  /** Worth retrying automatically: network failures, timeouts, 5xx, 408/429. */
  get retryable(): boolean {
    if (this.kind === "network" || this.kind === "timeout") return true;
    if (this.kind === "http" && this.status !== undefined) {
      return this.status >= 500 || this.status === 408 || this.status === 429;
    }
    return false;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Korean, operator-facing description of an error (title + optional detail line). */
export function describeError(e: unknown): { title: string; detail?: string } {
  if (!isApiError(e)) {
    const msg = e instanceof Error ? e.message : String(e);
    return { title: "알 수 없는 오류가 발생했습니다", detail: msg };
  }
  switch (e.kind) {
    case "network":
      return {
        title: "API 서버에 연결할 수 없습니다",
        detail: "백엔드가 실행 중인지, NEXT_PUBLIC_ACVM_API 주소와 CORS 설정이 올바른지 확인하세요.",
      };
    case "timeout":
      return { title: "응답 시간이 초과되었습니다", detail: "잠시 후 다시 시도하세요." };
    case "schema":
      return {
        title: "API 응답 형식이 계약과 다릅니다",
        detail: e.issues?.slice(0, 3).join(" · "),
      };
    case "http": {
      const s = e.status ?? 0;
      if (s === 401)
        return {
          title: "인증이 필요합니다 (401)",
          detail: "설정에서 개발용 토큰을 입력하거나 Cloudflare Access에 다시 로그인하세요.",
        };
      if (s === 403) return { title: "이 작업을 수행할 권한이 없습니다 (403)", detail: e.detail };
      if (s === 404) return { title: "요청한 항목을 찾을 수 없습니다 (404)", detail: e.detail };
      if (s === 409) return { title: "상태가 변경되어 처리할 수 없습니다 (409)", detail: e.detail };
      if (s === 422) return { title: "입력값을 확인하세요 (422)", detail: e.detail };
      if (s === 429) return { title: "요청이 너무 많습니다 (429)", detail: "잠시 후 다시 시도하세요." };
      if (s === 413) return { title: "파일이 너무 큽니다 (413)", detail: e.detail };
      if (s === 415) return { title: "지원하지 않는 형식입니다 (415)", detail: e.detail };
      if (s === 501) return { title: "서버에 이 기능이 아직 준비되지 않았습니다 (501)", detail: e.detail };
      if (s === 503) return { title: "일시적으로 사용할 수 없습니다 (503)", detail: e.detail };
      if (s >= 500) return { title: `서버 오류가 발생했습니다 (${s})`, detail: e.detail };
      return { title: `요청이 실패했습니다 (${s})`, detail: e.detail };
    }
  }
}
