"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, useSyncExternalStore } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Field, TextInput } from "@/components/ui/Field";
import { Notice } from "@/components/ui/Feedback";
import { RolePill } from "@/components/ui/Pill";
import { Segmented } from "@/components/ui/misc";
import { useToast } from "@/components/ui/Toast";
import { API_BASE, MOCK_MODE } from "@/lib/config";
import { ROLE_LABEL } from "@/lib/labels";
import { isApiError } from "@/lib/errors";
import { useAuthConfig, useChangePassword, useLogout, usePrincipal } from "@/lib/queries";
import type { Role } from "@/lib/schemas";
import { getDevRole, getMockRole, getToken, setDevRole, setMockRole, setToken, subscribeSession } from "@/lib/session";
import { useTheme, type ThemePref } from "./theme";

const ROLES: Role[] = ["owner", "operator", "reviewer", "viewer"];

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title="계정 · 표시 설정" description="내 계정 · API 연결 · 화면 테마" size="md">
      <SettingsBody onClose={onClose} />
    </Dialog>
  );
}

function SettingsBody({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { me, query } = usePrincipal();
  const { pref, setPref } = useTheme();
  const storedToken = useSyncExternalStore(subscribeSession, getToken, () => null);
  const mockRole = useSyncExternalStore(subscribeSession, getMockRole, () => "owner" as Role);
  const devRole = useSyncExternalStore(subscribeSession, getDevRole, () => null);
  const [draft, setDraft] = useState("");

  const saveToken = () => {
    setToken(draft);
    setDraft("");
    void qc.invalidateQueries();
    toast(draft.trim() ? "토큰을 저장했습니다" : "토큰을 삭제했습니다", "info");
  };

  return (
    <div className="space-y-6">
      <section aria-labelledby="set-conn" className="space-y-2">
        <h3 id="set-conn" className="text-sm font-semibold">
          연결
        </h3>
        <div className="rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-xs text-ink-2 break-all">
          {MOCK_MODE ? "모의 모드 (NEXT_PUBLIC_ACVM_MOCK=1) — 백엔드 호출 없음" : API_BASE}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted">현재 사용자</span>
          {me ? (
            <>
              <span className="font-medium">{me.name ? `${me.name} · ` : ""}{me.email ?? me.subject}</span>
              <RolePill role={me.role} />
              {me.workspace_id ? <span className="text-xs text-muted">워크스페이스 {me.workspace_id}</span> : null}
            </>
          ) : query.isPending ? (
            <span className="text-muted">확인 중…</span>
          ) : (
            <span className="text-critical-ink">확인 실패</span>
          )}
        </div>
      </section>

      {MOCK_MODE ? (
        <section aria-labelledby="set-role" className="space-y-2">
          <h3 id="set-role" className="text-sm font-semibold">
            데모 역할 (모의 모드 전용)
          </h3>
          <p className="text-[13px] text-muted">RBAC 화면 차이를 확인하려면 역할을 바꿔 보세요. 실제 API에서는 GET /me 가 역할을 결정합니다.</p>
          <Segmented
            label="데모 역할"
            value={mockRole}
            onChange={(r) => {
              setMockRole(r);
              void qc.invalidateQueries({ queryKey: ["me"] });
            }}
            options={ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] }))}
          />
        </section>
      ) : me?.auth === "session" ? (
        <AccountSection onClose={onClose} />
      ) : (
        <section aria-labelledby="set-auth" className="space-y-3">
          <h3 id="set-auth" className="text-sm font-semibold">
            인증
          </h3>
          <Notice>
            운영 환경에서는 Cloudflare Access 세션으로 인증되므로 토큰이 필요 없습니다. 개발 환경에서만 Bearer 토큰을 입력하세요 (이 브라우저의
            localStorage에 저장).
          </Notice>
          <form
            className="flex flex-col gap-2 sm:flex-row sm:items-end"
            onSubmit={(e) => {
              e.preventDefault();
              saveToken();
            }}
          >
            <Field label="개발용 Bearer 토큰" htmlFor="dev-token" className="flex-1" hint={storedToken ? `저장됨 · ****${storedToken.slice(-4)}` : "저장된 토큰 없음"}>
              <TextInput
                id="dev-token"
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="dev token"
              />
            </Field>
            <div className="flex gap-2 sm:mb-6">
              <Button type="submit" variant="primary" disabled={!draft.trim()}>
                저장
              </Button>
              {storedToken ? (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setToken(null);
                    void qc.invalidateQueries();
                    toast("토큰을 삭제했습니다", "info");
                  }}
                >
                  삭제
                </Button>
              ) : null}
            </div>
          </form>
          {storedToken ? (
            <div className="space-y-2">
              <p className="text-[13px] font-medium text-ink-2">개발용 역할 (X-Dev-Role)</p>
              <p className="text-xs text-muted">API가 env=dev/test일 때만 적용됩니다. RBAC 화면을 역할별로 확인할 때 사용하세요.</p>
              <Segmented<Role | "none">
                label="개발용 역할"
                size="sm"
                value={devRole ?? "none"}
                onChange={(r) => {
                  setDevRole(r === "none" ? null : r);
                  void qc.invalidateQueries();
                }}
                options={[{ value: "none" as const, label: "기본" }, ...ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] }))]}
              />
            </div>
          ) : null}
        </section>
      )}

      <section aria-labelledby="set-theme" className="space-y-2">
        <h3 id="set-theme" className="text-sm font-semibold">
          테마
        </h3>
        <Segmented<ThemePref>
          label="테마"
          value={pref}
          onChange={setPref}
          options={[
            { value: "system", label: "시스템" },
            { value: "light", label: "라이트" },
            { value: "dark", label: "다크" },
          ]}
        />
      </section>

      <div className="flex justify-end">
        <Button onClick={onClose}>닫기</Button>
      </div>
    </div>
  );
}

/** Signed-in member: change password (other sessions are signed out) and log out. */
function AccountSection({ onClose }: { onClose: () => void }) {
  const change = useChangePassword();
  const logout = useLogout();
  const cfg = useAuthConfig();
  const router = useRouter();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const mismatch = confirm.length > 0 && confirm !== next;

  return (
    <section aria-labelledby="set-account" className="space-y-3">
      <h3 id="set-account" className="text-sm font-semibold">
        비밀번호 변경
      </h3>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (mismatch || !current || !next) return;
          change.mutate(
            { current, next },
            {
              onSuccess: () => {
                setCurrent("");
                setNext("");
                setConfirm("");
                toast("비밀번호를 바꿨습니다 — 다른 기기의 로그인은 해제되었습니다", "good");
              },
            },
          );
        }}
      >
        <Field label="현재 비밀번호" htmlFor="pw-current">
          <TextInput id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <Field label="새 비밀번호" htmlFor="pw-new" hint={cfg.data?.password_rule}>
          <TextInput id="pw-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        <Field label="새 비밀번호 확인" htmlFor="pw-confirm" error={mismatch ? "비밀번호가 일치하지 않습니다" : null}>
          <TextInput id="pw-confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        {change.error ? (
          <p role="alert" className="text-[13px] text-critical-ink">
            {isApiError(change.error) && change.error.detail ? change.error.detail : "비밀번호를 바꾸지 못했습니다"}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={change.isPending} disabled={mismatch || !current || !next || !confirm}>
            비밀번호 변경
          </Button>
          <Button
            variant="ghost"
            icon="logout"
            loading={logout.isPending}
            onClick={() =>
              logout.mutate(undefined, {
                onSettled: () => {
                  onClose();
                  router.replace("/login");
                },
              })
            }
          >
            로그아웃
          </Button>
        </div>
      </form>
    </section>
  );
}
