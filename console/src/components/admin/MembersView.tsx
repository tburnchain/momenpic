"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Field, Select, TextInput } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { isApiError } from "@/lib/errors";
import { kst } from "@/lib/format";
import { ROLE_LABEL } from "@/lib/labels";
import { useAuthConfig, useMemberMutations, useMembers, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { Member, Role } from "@/lib/schemas";

const ROLES: Role[] = ["owner", "reviewer", "operator", "viewer"];
const STATUS: Record<Member["status"], { label: string; tone: "good" | "warn" | "neutral" }> = {
  active: { label: "활성", tone: "good" },
  pending: { label: "승인 대기", tone: "warn" },
  disabled: { label: "정지", tone: "neutral" },
};

function msg(e: unknown, fallback: string): string {
  return isApiError(e) && typeof e.detail === "string" && e.detail ? e.detail : fallback;
}

function MemberRow({ m, self }: { m: Member; self: boolean }) {
  const { update, remove } = useMemberMutations();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const st = STATUS[m.status];
  const act = (body: { role?: string; status?: string }, done: string) =>
    update.mutate({ id: m.id, ...body }, { onSuccess: () => toast(done, "good"), onError: (e) => toast(msg(e, "변경하지 못했습니다"), "critical") });

  return (
    <li className="flex flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
          {m.name || m.email.split("@")[0]}
          <Pill tone={st.tone}>{st.label}</Pill>
          {self ? <Pill tone="info">나</Pill> : null}
          {!m.has_password ? <Pill tone="neutral">비밀번호 없음 · 로그인 불가</Pill> : null}
        </p>
        <p className="truncate text-[13px] text-ink-2">{m.email}</p>
        <p className="text-xs text-muted">
          가입 {kst(m.created_at)} · 최근 로그인 {m.last_login_at ? kst(m.last_login_at) : "없음"}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`role-${m.id}`}>
          {m.email} 역할
        </label>
        <Select
          id={`role-${m.id}`}
          className="h-9 w-[120px] text-sm"
          value={m.role}
          disabled={self || update.isPending}
          onChange={(e) => act({ role: e.target.value }, `${m.email} 역할을 ${ROLE_LABEL[e.target.value as Role]}(으)로 바꿨습니다`)}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_LABEL[r]}
            </option>
          ))}
        </Select>
        {m.status === "pending" ? (
          <Button size="sm" variant="primary" icon="check" loading={update.isPending} onClick={() => act({ status: "active" }, `${m.email} 가입을 승인했습니다`)}>
            승인
          </Button>
        ) : null}
        {m.status === "active" && !self ? (
          <Button size="sm" variant="ghost" icon="pause" onClick={() => act({ status: "disabled" }, `${m.email} 계정을 정지했습니다`)}>
            정지
          </Button>
        ) : null}
        {m.status === "disabled" ? (
          <Button size="sm" variant="ghost" icon="play" onClick={() => act({ status: "active" }, `${m.email} 계정을 다시 활성화했습니다`)}>
            활성화
          </Button>
        ) : null}
        {!self ? (
          confirm ? (
            <Button
              size="sm"
              variant="danger"
              icon="trash"
              loading={remove.isPending}
              onClick={() =>
                remove.mutate(m.id, {
                  onSuccess: () => toast(`${m.email} 회원을 삭제했습니다`, "info"),
                  onError: (e) => toast(msg(e, "삭제하지 못했습니다"), "critical"),
                  onSettled: () => setConfirm(false),
                })
              }
            >
              정말 삭제
            </Button>
          ) : (
            <Button size="sm" variant="ghost" icon="trash" onClick={() => setConfirm(true)}>
              {m.status === "pending" ? "거절" : "삭제"}
            </Button>
          )
        ) : null}
      </div>
    </li>
  );
}

function CreateMember({ rule }: { rule: string }) {
  const { create } = useMemberMutations();
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const [password, setPassword] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate(
      { email: email.trim(), name: name.trim(), role, password },
      {
        onSuccess: (m) => {
          toast(`${m.email} 계정을 만들었습니다 — 초기 비밀번호를 본인에게 직접 전달하고 바꾸게 하세요`, "good");
          setEmail("");
          setName("");
          setPassword("");
        },
      },
    );
  };

  return (
    <form className="grid gap-3 sm:grid-cols-2 xl:grid-cols-[1.3fr_1fr_140px_1.2fr_auto] xl:items-end" onSubmit={submit}>
      <Field label="이메일" htmlFor="nm-email">
        <TextInput id="nm-email" type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
      </Field>
      <Field label="이름" htmlFor="nm-name">
        <TextInput id="nm-name" autoComplete="off" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="역할" htmlFor="nm-role">
        <Select id="nm-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_LABEL[r]}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="초기 비밀번호" htmlFor="nm-pw" hint={rule}>
        <TextInput id="nm-pw" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <Button type="submit" variant="primary" icon="plus" loading={create.isPending} disabled={!email.trim() || !password} className="xl:mb-6">
        계정 만들기
      </Button>
      {create.error ? (
        <p role="alert" className="text-[13px] text-critical-ink sm:col-span-2 xl:col-span-5">
          {msg(create.error, "계정을 만들지 못했습니다")}
        </p>
      ) : null}
    </form>
  );
}

/** 회원 관리 — approve sign-ups, assign roles, suspend or remove members (owner only). */
export function MembersView() {
  const { can, me } = usePrincipal();
  const admin = can("admin");
  const q = useMembers(admin);
  const cfg = useAuthConfig();
  const items = q.data?.items ?? [];
  const pending = items.filter((m) => m.status === "pending");
  const others = items.filter((m) => m.status !== "pending");

  return (
    <>
      <PageHeader
        title="회원 관리"
        description="가입 신청을 승인하고 역할을 정합니다. 소유자 — 전체, 검수자 — 승인·HOLD, 운영자 — 명령·스튜디오, 열람자 — 조회."
        actions={q.data ? <Pill tone={pending.length ? "warn" : "neutral"}>승인 대기 {pending.length}명</Pill> : null}
      />
      {!admin ? (
        <Notice tone="warn">회원 관리는 {ALLOWED_ROLES.admin}만 할 수 있습니다.</Notice>
      ) : q.isPending ? (
        <Card>
          <CardBody className="pt-5">
            <LoadingBlock rows={6} />
          </CardBody>
        </Card>
      ) : q.error ? (
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : (
        <div className="space-y-5">
          {pending.length ? (
            <Card aria-labelledby="mem-pending">
              <CardHeader id="mem-pending" title="승인 대기" description="승인하면 선택한 역할로 바로 로그인할 수 있습니다. 모르는 신청은 거절하세요." />
              <CardBody>
                <ul className="divide-y divide-line rounded-lg border border-line">
                  {pending.map((m) => (
                    <MemberRow key={m.id} m={m} self={m.id === me?.user_id} />
                  ))}
                </ul>
              </CardBody>
            </Card>
          ) : null}
          <Card aria-labelledby="mem-all">
            <CardHeader id="mem-all" title={`회원 ${others.length}명`} description="자기 자신과 마지막 소유자는 강등·정지·삭제할 수 없습니다." />
            <CardBody>
              {others.length ? (
                <ul className="divide-y divide-line rounded-lg border border-line">
                  {others.map((m) => (
                    <MemberRow key={m.id} m={m} self={m.id === me?.user_id} />
                  ))}
                </ul>
              ) : (
                <EmptyState icon="users" title="회원이 없습니다" className="py-8" />
              )}
            </CardBody>
          </Card>
          <Card aria-labelledby="mem-new">
            <CardHeader id="mem-new" title="계정 직접 만들기" description="가입 신청 없이 바로 쓸 계정을 만듭니다 (즉시 활성)." />
            <CardBody>
              <CreateMember rule={cfg.data?.password_rule ?? ""} />
            </CardBody>
          </Card>
        </div>
      )}
    </>
  );
}
