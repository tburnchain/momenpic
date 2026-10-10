/**
 * Mock members (src/acvm/security/accounts.py): sign-up → pending → owner approval, roles, suspension.
 * Mock mode has no real login — the demo role from ⚙ 설정 decides who "you" are.
 */
import type { z } from "zod";
import type * as S from "@/lib/schemas";

type MemberIn = z.input<typeof S.Member>;

const now = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();

const members: MemberIn[] = [
  { id: "usr_owner", email: "owner@acvm.local", name: "대표", role: "owner", status: "active", workspace_id: "ws_demo", created_at: iso(now - 40 * 86400_000), last_login_at: iso(now - 3600_000), has_password: true },
  { id: "usr_ops", email: "ops@acvm.local", name: "운영 담당", role: "operator", status: "active", workspace_id: "ws_demo", created_at: iso(now - 20 * 86400_000), last_login_at: iso(now - 86400_000), has_password: true },
  { id: "usr_new", email: "newbie@example.com", name: "신규 가입자", role: "viewer", status: "pending", workspace_id: "ws_demo", created_at: iso(now - 7200_000), last_login_at: null, has_password: true },
];

export function memberList() {
  return { items: members, pending: members.filter((m) => m.status === "pending").length };
}

export function findMember(id: string): MemberIn | undefined {
  return members.find((m) => m.id === id);
}

export function patchMember(id: string, body: { role?: MemberIn["role"]; status?: MemberIn["status"]; name?: string }): MemberIn | null {
  const m = findMember(id);
  if (!m) return null;
  if (body.role) m.role = body.role;
  if (body.status) m.status = body.status;
  if (body.name !== undefined) m.name = body.name;
  return m;
}

export function addMember(body: { email: string; name?: string; role?: MemberIn["role"] }, status: MemberIn["status"]): MemberIn {
  const m: MemberIn = {
    id: `usr_${Math.random().toString(36).slice(2, 8)}`,
    email: body.email.trim().toLowerCase(),
    name: body.name ?? "",
    role: body.role ?? "viewer",
    status,
    workspace_id: "ws_demo",
    created_at: iso(Date.now()),
    last_login_at: null,
    has_password: true,
  };
  members.push(m);
  return m;
}

export function removeMember(id: string): boolean {
  const i = members.findIndex((m) => m.id === id);
  if (i < 0) return false;
  members.splice(i, 1);
  return true;
}

export function emailTaken(email: string): boolean {
  return members.some((m) => m.email === email.trim().toLowerCase());
}
