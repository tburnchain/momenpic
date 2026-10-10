"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Notice } from "@/components/ui/Feedback";
import { Field, TextInput } from "@/components/ui/Field";
import { isApiError } from "@/lib/errors";
import { useAuthConfig, useLogin, useSignup } from "@/lib/queries";

function errorText(e: unknown, fallback: string): string {
  if (isApiError(e)) {
    if (e.kind === "network" || e.kind === "timeout") return "서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.";
    if (typeof e.detail === "string" && e.detail) return e.detail;
  }
  return fallback;
}

/** Only same-site relative paths may be used as the post-login destination (no open redirect). */
function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login") || next.startsWith("/signup")) {
    return "/";
  }
  return next;
}

export function AuthFrame({ title, subtitle, children, footer }: { title: string; subtitle?: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-page px-4 py-10">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 flex items-center gap-2.5">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-accent-fill text-sm font-bold text-white" aria-hidden>
            AV
          </span>
          <span className="leading-tight">
            <span className="block text-lg font-bold tracking-tight text-ink">ACVM 콘솔</span>
            <span className="block text-xs text-muted">제휴 콘텐츠 자판기</span>
          </span>
        </div>
        <div className="rounded-2xl border border-line bg-surface p-6 shadow-sm">
          <h1 className="text-xl font-bold tracking-tight text-ink">{title}</h1>
          {subtitle ? <p className="mt-1 text-[13px] text-muted">{subtitle}</p> : null}
          <div className="mt-5">{children}</div>
        </div>
        {footer ? <div className="mt-4 text-center text-[13px] text-muted">{footer}</div> : null}
      </div>
    </div>
  );
}

export function LoginView() {
  const router = useRouter();
  const params = useSearchParams();
  const cfg = useAuthConfig();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const signupOpen = (cfg.data?.signup ?? "approval") !== "closed";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password) return;
    login.mutate(
      { email: email.trim(), password },
      {
        onSuccess: () => {
          setPassword("");
          router.replace(safeNext(params.get("next")));
        },
        onError: () => setPassword(""),
      },
    );
  };

  return (
    <AuthFrame
      title="로그인"
      subtitle="가입한 이메일과 비밀번호로 로그인하세요."
      footer={
        signupOpen ? (
          <>
            계정이 없으신가요?{" "}
            <Link href="/signup" className="font-medium text-accent-ink hover:underline">
              회원가입
            </Link>
          </>
        ) : (
          "새 계정은 소유자에게 요청하세요."
        )
      }
    >
      <form className="space-y-4" onSubmit={submit} noValidate>
        {params.get("signed_up") ? <Notice tone="good">가입 신청이 접수되었습니다. 승인되면 로그인할 수 있습니다.</Notice> : null}
        <Field label="이메일" htmlFor="login-email">
          <TextInput
            id="login-email"
            type="email"
            autoComplete="username"
            inputMode="email"
            autoFocus
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="비밀번호" htmlFor="login-password">
          <TextInput
            id="login-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        {login.error ? (
          <p role="alert" className="rounded-lg bg-critical-soft px-3 py-2 text-[13px] text-critical-ink">
            {errorText(login.error, "로그인하지 못했습니다")}
          </p>
        ) : null}
        <Button type="submit" variant="primary" className="w-full" loading={login.isPending} disabled={!email.trim() || !password}>
          로그인
        </Button>
      </form>
    </AuthFrame>
  );
}

export function SignupView() {
  const router = useRouter();
  const cfg = useAuthConfig();
  const signup = useSignup();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const rule = cfg.data?.password_rule || "8자 이상, 영문 대문자·소문자·숫자·특수문자 중 3종류 이상";
  const mismatch = confirm.length > 0 && confirm !== password;
  const closed = cfg.data?.signup === "closed";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mismatch || !email.trim() || !password) return;
    signup.mutate(
      { email: email.trim(), password, name: name.trim() },
      {
        onSuccess: (r) => {
          setPassword("");
          setConfirm("");
          router.replace(r.status === "active" ? "/login" : "/login?signed_up=1");
        },
      },
    );
  };

  return (
    <AuthFrame
      title="회원가입"
      subtitle={
        cfg.data?.signup === "open" ? "가입하면 바로 로그인할 수 있습니다 (조회 권한)." : "가입 신청 후 소유자가 승인하면 로그인할 수 있습니다."
      }
      footer={
        <>
          이미 계정이 있으신가요?{" "}
          <Link href="/login" className="font-medium text-accent-ink hover:underline">
            로그인
          </Link>
        </>
      }
    >
      {closed ? (
        <Notice tone="warn">회원가입이 닫혀 있습니다. 소유자에게 계정 생성을 요청하세요.</Notice>
      ) : (
        <form className="space-y-4" onSubmit={submit} noValidate>
          <Field label="이름" htmlFor="su-name">
            <TextInput id="su-name" autoComplete="name" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="이메일" htmlFor="su-email">
            <TextInput
              id="su-email"
              type="email"
              autoComplete="email"
              inputMode="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="비밀번호" htmlFor="su-password" hint={rule}>
            <TextInput
              id="su-password"
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Field label="비밀번호 확인" htmlFor="su-confirm" error={mismatch ? "비밀번호가 일치하지 않습니다" : null}>
            <TextInput
              id="su-confirm"
              type="password"
              autoComplete="new-password"
              required
              aria-invalid={mismatch}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </Field>
          {signup.error ? (
            <p role="alert" className="rounded-lg bg-critical-soft px-3 py-2 text-[13px] text-critical-ink">
              {errorText(signup.error, "가입하지 못했습니다")}
            </p>
          ) : null}
          <Button
            type="submit"
            variant="primary"
            className="w-full"
            loading={signup.isPending}
            disabled={mismatch || !email.trim() || !password || !confirm}
          >
            가입 신청
          </Button>
        </form>
      )}
    </AuthFrame>
  );
}
