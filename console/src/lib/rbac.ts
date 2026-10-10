/**
 * Client-side RBAC (§10). This only *hides* controls; the API enforces the same matrix.
 *   owner     — everything
 *   reviewer  — plan approval, HOLD decisions (publishing approval)
 *   operator  — commands & schedules, manual (Kakao) publishing queue
 *   viewer    — read-only
 *   admin     — owner only: G0 checklist, secret slots, workspace budget, account tokens
 *   takedown  — owner·reviewer (publishing approval authority, §10)
 * Rules are read-only for every role in the UI (changes go through the backend rule-set release).
 */
import type { Role } from "./schemas";

export type Action =
  | "command"
  | "approvePlan"
  | "reviewHold"
  | "manageSchedules"
  | "manualPublish"
  | "manageAccounts"
  | "admin"
  | "takedown"
  | "manageAssets";

const GRANTS: Record<Role, ReadonlySet<Action>> = {
  owner: new Set<Action>([
    "command",
    "approvePlan",
    "reviewHold",
    "manageSchedules",
    "manualPublish",
    "manageAccounts",
    "admin",
    "takedown",
    "manageAssets",
  ]),
  reviewer: new Set<Action>(["approvePlan", "reviewHold", "takedown"]),
  operator: new Set<Action>(["command", "manageSchedules", "manualPublish", "manageAssets"]),
  viewer: new Set<Action>(),
};

export function can(role: Role | null | undefined, action: Action): boolean {
  return !!role && GRANTS[role].has(action);
}

/** Which roles may perform an action — for "권한 없음" explanations. */
export const ALLOWED_ROLES: Record<Action, string> = {
  command: "소유자·운영자",
  approvePlan: "소유자·검수자",
  reviewHold: "소유자·검수자",
  manageSchedules: "소유자·운영자",
  manualPublish: "소유자·운영자",
  manageAccounts: "소유자",
  admin: "소유자",
  takedown: "소유자·검수자",
  manageAssets: "소유자·운영자",
};
