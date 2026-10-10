"use client";

import { useSyncExternalStore } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type AccountCreate,
  type AccountPatch,
  type FunnelParams,
  type PublicationQuery,
  type SimulateParams,
} from "./api";
import { can, type Action } from "./rbac";

export const qk = {
  me: ["me"] as const,
  runs: (limit: number) => ["runs", limit] as const,
  runsAll: ["runs"] as const,
  run: (id: string) => ["run", id] as const,
  holds: ["holds"] as const,
  money: (days: number) => ["dash", "money", days] as const,
  pipeline: ["dash", "pipeline"] as const,
  risk: ["dash", "risk"] as const,
  learning: ["dash", "learning"] as const,
  accounts: ["accounts"] as const,
  schedules: ["schedules"] as const,
  manual: ["manual-queue"] as const,
  rules: ["rules"] as const,
  audit: (limit: number) => ["audit", limit] as const,
  breakeven: (p: FunnelParams) => ["quant", "breakeven", p] as const,
  simulate: (p: SimulateParams) => ["quant", "simulate", p] as const,
  status: ["system", "status"] as const,
  integrations: ["system", "integrations"] as const,
  workspace: ["workspace"] as const,
  publications: (q: PublicationQuery) => ["publications", q] as const,
  publicationsAll: ["publications"] as const,
  content: (id: string) => ["content", id] as const,
  alerts: ["alerts"] as const,
  authConfig: ["auth", "config"] as const,
  members: ["members"] as const,
};

/* ------------------------------------------------------------------ principal */

export function useMe() {
  return useQuery({ queryKey: qk.me, queryFn: ({ signal }) => api.me(signal), staleTime: 5 * 60_000 });
}

/* ------------------------------------------------------------------ members & login */

export function useAuthConfig() {
  return useQuery({ queryKey: qk.authConfig, queryFn: ({ signal }) => api.authConfig(signal), staleTime: 10 * 60_000 });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { email: string; password: string }) => api.login(v.email, v.password),
    onSuccess: (me) => {
      qc.clear(); // nothing from a previous identity may leak into this session
      qc.setQueryData(qk.me, me);
    },
  });
}

export function useSignup() {
  return useMutation({ mutationFn: (v: { email: string; password: string; name?: string }) => api.signup(v) });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api.logout(), onSettled: () => qc.clear() });
}

export function useChangePassword() {
  return useMutation({ mutationFn: (v: { current: string; next: string }) => api.changePassword(v.current, v.next) });
}

export function useMembers(enabled = true) {
  return useQuery({ queryKey: qk.members, queryFn: ({ signal }) => api.members(signal), staleTime: 15_000, enabled });
}

export function useMemberMutations() {
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries({ queryKey: qk.members });
  return {
    update: useMutation({
      mutationFn: (v: { id: string; role?: string; status?: string; name?: string }) => {
        const { id, ...body } = v;
        return api.updateMember(id, body);
      },
      onSuccess: done,
    }),
    create: useMutation({
      mutationFn: (v: { email: string; password: string; name?: string; role: string }) => api.createMember(v),
      onSuccess: done,
    }),
    remove: useMutation({ mutationFn: (id: string) => api.deleteMember(id), onSuccess: done }),
  };
}

/** Current principal + permission check. Unknown principal ⇒ every action hidden. */
const noopSubscribe = () => () => {};

/**
 * True only after hydration. Role-gated UI must render identically on the server (no session) and on the
 * client's first pass, otherwise React reports a hydration mismatch and re-renders the whole tree.
 */
export function useHydrated(): boolean {
  return useSyncExternalStore(noopSubscribe, () => true, () => false);
}

export function usePrincipal() {
  const q = useMe();
  const hydrated = useHydrated();
  const role = hydrated ? (q.data?.role ?? null) : null;
  return {
    me: q.data,
    role,
    can: (action: Action) => can(role, action),
    query: q,
  };
}

/* ------------------------------------------------------------------ reads */

const ACTIVE_RUN = new Set(["approved", "running", "awaiting_approval", "planned"]);

export function useRuns(limit = 50) {
  return useQuery({
    queryKey: qk.runs(limit),
    queryFn: ({ signal }) => api.runs(limit, signal),
    refetchInterval: 20_000,
  });
}

export function useRun(id: string) {
  return useQuery({
    queryKey: qk.run(id),
    queryFn: ({ signal }) => api.run(id, signal),
    refetchInterval: (query) => (query.state.data && ACTIVE_RUN.has(query.state.data.run.status) ? 5_000 : false),
  });
}

export function useHolds(enabled = true) {
  return useQuery({
    queryKey: qk.holds,
    queryFn: ({ signal }) => api.holds(signal),
    refetchInterval: 60_000,
    enabled,
  });
}

export function useMoney(days: number) {
  return useQuery({
    queryKey: qk.money(days),
    queryFn: ({ signal }) => api.money(days, signal),
    placeholderData: keepPreviousData,
  });
}
export function usePipeline() {
  return useQuery({ queryKey: qk.pipeline, queryFn: ({ signal }) => api.pipeline(signal), refetchInterval: 30_000 });
}
export function useRisk() {
  return useQuery({ queryKey: qk.risk, queryFn: ({ signal }) => api.risk(signal), refetchInterval: 60_000 });
}
export function useLearning() {
  return useQuery({ queryKey: qk.learning, queryFn: ({ signal }) => api.learning(signal) });
}
export function useAccounts() {
  return useQuery({ queryKey: qk.accounts, queryFn: ({ signal }) => api.accounts(signal) });
}
export function useSchedules() {
  return useQuery({ queryKey: qk.schedules, queryFn: ({ signal }) => api.schedules(signal) });
}
export function useManualQueue(enabled = true) {
  return useQuery({ queryKey: qk.manual, queryFn: ({ signal }) => api.manualQueue(signal), refetchInterval: 60_000, enabled });
}
export function useRules() {
  return useQuery({ queryKey: qk.rules, queryFn: ({ signal }) => api.rules(signal), staleTime: 10 * 60_000 });
}
export function useAudit(limit = 200) {
  return useQuery({ queryKey: qk.audit(limit), queryFn: ({ signal }) => api.audit(limit, signal) });
}
export function useBreakeven(p: FunnelParams) {
  return useQuery({
    queryKey: qk.breakeven(p),
    queryFn: ({ signal }) => api.breakeven(p, signal),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
  });
}
export function useSimulate(p: SimulateParams) {
  return useQuery({
    queryKey: qk.simulate(p),
    queryFn: ({ signal }) => api.simulate(p, signal),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
  });
}

/* ------------------------------------------------------------------ writes */

export function useSubmitCommand() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (text: string) => api.submitCommand(text),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.runsAll }),
  });
}

export function useRunDecision(runId: string) {
  const qc = useQueryClient();
  const settle = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: qk.run(runId) }),
      qc.invalidateQueries({ queryKey: qk.runsAll }),
    ]);
  const approve = useMutation({ mutationFn: () => api.approveRun(runId), onSuccess: settle });
  const reject = useMutation({ mutationFn: (reason: string) => api.rejectRun(runId, reason), onSuccess: settle });
  return { approve, reject };
}

export function useHoldDecision() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; approve: boolean; note?: string }) => api.holdDecision(v.id, v.approve, v.note),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.pipeline });
      void qc.invalidateQueries({ queryKey: qk.alerts });
    },
  });
}

export function useCreateAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AccountCreate) => api.createAccount(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.accounts }),
  });
}

export function usePatchAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; body: AccountPatch }) => api.patchAccount(v.id, v.body),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.accounts }),
  });
}

export function useCreateSchedule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { text: string; cron: string }) => api.createSchedule(v.text, v.cron),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.schedules }),
  });
}

export function useDeleteSchedule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.schedules }),
  });
}

export function useManualDone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; permalink: string }) => api.manualDone(v.id, v.permalink),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.manual }),
  });
}

/* ------------------------------------------------------------------ operations */

export function useSystemStatus(enabled = true) {
  return useQuery({
    queryKey: qk.status,
    queryFn: ({ signal }) => api.systemStatus(signal),
    staleTime: 30_000,
    enabled,
  });
}

export function useWorkspace() {
  return useQuery({ queryKey: qk.workspace, queryFn: ({ signal }) => api.workspace(signal) });
}

export function usePublications(q: PublicationQuery, enabled = true) {
  return useQuery({
    queryKey: qk.publications(q),
    queryFn: ({ signal }) => api.publications(q, signal),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
    enabled,
  });
}

export function useContent(id: string) {
  return useQuery({ queryKey: qk.content(id), queryFn: ({ signal }) => api.content(id, signal) });
}

export function useAlerts(enabled = true) {
  return useQuery({ queryKey: qk.alerts, queryFn: ({ signal }) => api.alerts(50, signal), refetchInterval: 60_000, enabled });
}

export function useSetChecklist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { item: string; done: boolean }) => api.setChecklist(v.item, v.done),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.status }),
  });
}

/** A key changes what the whole console can do: status, integrations and the Studio options (voices, AI tools). */
function invalidateKeyed(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: qk.status });
  void qc.invalidateQueries({ queryKey: qk.integrations });
  void qc.invalidateQueries({ queryKey: ["studio", "options"] });
}

export function usePutSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { name: string; value: string }) => api.putSecret(v.name, v.value),
    onSuccess: () => invalidateKeyed(qc),
  });
}

export function useDeleteSecret() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (name: string) => api.deleteSecret(name), onSuccess: () => invalidateKeyed(qc) });
}

export function useIntegrations(enabled = true) {
  return useQuery({
    queryKey: qk.integrations,
    queryFn: ({ signal }) => api.integrations(signal),
    staleTime: 30_000,
    enabled,
  });
}

export function useVerifyIntegration() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (name: string) => api.verifyIntegration(name), onSuccess: () => invalidateKeyed(qc) });
}

export function useSetIntegrationEnabled() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { name: string; enabled: boolean }) => api.setIntegrationEnabled(v.name, v.enabled),
    onSuccess: () => invalidateKeyed(qc),
  });
}

export function useRemapVoices() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api.remapVoices(), onSuccess: () => invalidateKeyed(qc) });
}

export function usePatchWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name?: string; daily_budget_krw?: number }) => api.patchWorkspace(body),
    onSuccess: (ws) => {
      qc.setQueryData(qk.workspace, ws);
      void qc.invalidateQueries({ queryKey: qk.status });
    },
  });
}

export function usePutCredentials() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; token: string; platformUserId?: string }) =>
      api.putCredentials(v.id, v.token, v.platformUserId),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.status }),
  });
}

export function useTakedown() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; reason: string }) => api.takedown(v.id, v.reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.publicationsAll });
      void qc.invalidateQueries({ queryKey: qk.alerts });
    },
  });
}

/* ------------------------------------------------------------------ shorts studio */

export const studioKeys = {
  options: ["studio", "options"] as const,
  products: (q: { category?: string; q?: string }) => ["studio", "products", q] as const,
  draft: (id: string) => ["studio", "draft", id] as const,
  lint: (productId: number, text: string, narrations?: string[] | null) =>
    ["studio", "lint", productId, text, narrations ?? null] as const,
  assets: (productId: number | null) => ["studio", "assets", productId] as const,
  assetsAll: ["studio", "assets"] as const,
};

export function useStudioOptions() {
  return useQuery({ queryKey: studioKeys.options, queryFn: ({ signal }) => api.studioOptions(signal), staleTime: 5 * 60_000 });
}

export function useStudioProducts(q: { category?: string; q?: string }, enabled = true) {
  return useQuery({
    queryKey: studioKeys.products(q),
    queryFn: ({ signal }) => api.studioProducts({ ...q, limit: 40 }, signal),
    placeholderData: keepPreviousData,
    enabled,
  });
}

/** Draft polling: every 1.5 s while a render is in progress. */
export function useDraft(id: string | null) {
  return useQuery({
    queryKey: studioKeys.draft(id ?? ""),
    queryFn: ({ signal }) => api.draft(id as string, signal),
    enabled: !!id,
    refetchInterval: (query) => (query.state.data?.render.state === "rendering" ? 1500 : false),
  });
}

/** Live compliance lint; pass ``narrations`` for a short so the 20–25 s window is checked too. */
export function useLiveLint(productId: number | null, text: string, narrations?: string[] | null) {
  return useQuery({
    queryKey: studioKeys.lint(productId ?? 0, text, narrations),
    queryFn: ({ signal }) => api.lint(productId as number, text, narrations, signal),
    enabled: productId !== null && text.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

export function useStudioAssets(productId: number | null) {
  return useQuery({
    queryKey: studioKeys.assets(productId),
    queryFn: ({ signal }) => api.studioAssets(productId, signal),
  });
}
