/**
 * Browser-side session preferences kept in localStorage:
 * - dev bearer token (prod uses Cloudflare Access cookies instead)
 * - mock-mode role override (demo RBAC without a backend)
 * Exposed as tiny external stores so components can subscribe via useSyncExternalStore.
 */
import type { Role } from "./schemas";

const TOKEN_KEY = "acvm.token";
const MOCK_ROLE_KEY = "acvm.mockRole";
const DEV_ROLE_KEY = "acvm.devRole";

type Listener = () => void;
const listeners = new Set<Listener>();

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null || value === "") window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode) — keep working without persistence */
  }
  listeners.forEach((l) => l());
}

export function subscribeSession(listener: Listener): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === TOKEN_KEY || e.key === MOCK_ROLE_KEY || e.key === DEV_ROLE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return read(TOKEN_KEY);
}

export function setToken(token: string | null) {
  write(TOKEN_KEY, token?.trim() || null);
}

const ROLES: readonly Role[] = ["owner", "operator", "reviewer", "viewer"];

export function getMockRole(): Role {
  if (typeof window === "undefined") return "owner";
  const v = read(MOCK_ROLE_KEY);
  return ROLES.includes(v as Role) ? (v as Role) : "owner";
}

export function setMockRole(role: Role) {
  write(MOCK_ROLE_KEY, role);
}

/**
 * Dev-only role override sent as `X-Dev-Role` with the dev bearer token. The API honours it only
 * when ACVM env is dev/test, so it is a convenient way to check the RBAC UI against a real backend.
 */
export function getDevRole(): Role | null {
  if (typeof window === "undefined") return null;
  const v = read(DEV_ROLE_KEY);
  return ROLES.includes(v as Role) ? (v as Role) : null;
}

export function setDevRole(role: Role | null) {
  write(DEV_ROLE_KEY, role);
}
