/**
 * Build-time configuration. NEXT_PUBLIC_* values are inlined by Next.js at build time, so they
 * must be present when running `next build` (see Dockerfile build args).
 */
const DEFAULT_API = "http://localhost:8000/api/v1";

export const API_BASE: string = (process.env.NEXT_PUBLIC_ACVM_API || DEFAULT_API).replace(/\/+$/, "");

const mockFlag = (process.env.NEXT_PUBLIC_ACVM_MOCK ?? "").toLowerCase();
export const MOCK_MODE: boolean = mockFlag === "1" || mockFlag === "true";

/** Per-request timeout. Planning a command has a p95 SLO of 20 s (§9.2), so leave headroom. */
export const REQUEST_TIMEOUT_MS = 30_000;

/** HOLD review budget per item (§8.4: ~20 s × 30 items ≈ 10 min/day). */
export const HOLD_SECONDS_PER_ITEM = 20;
