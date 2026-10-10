/**
 * Mock AI integrations (src/acvm/integrations.py): key registration → "verification" → active, pause/resume,
 * delete, ElevenLabs voice mapping. A key containing "bad" fails verification so the error state can be seen.
 */
import type { z } from "zod";
import type * as S from "@/lib/schemas";

type IntegrationIn = z.input<typeof S.Integration>;

interface Spec {
  name: string;
  label: string;
  slots: Array<[string, string]>;
  automates: string[];
  guardrails: string[];
  model: string;
  unit_cost: string;
  key_url: string;
}

export const INTEGRATION_SPECS: Spec[] = [
  {
    name: "anthropic",
    label: "Claude (대본·검수)",
    slots: [["anthropic_api_key", "Claude API 키"]],
    automates: ["대본 작성(Writer)·검수(Critic)를 Claude가 수행", "자연어 명령 해석", "게이트 근거 판정·키프레임 비전 검사(R-03)"],
    guardrails: [],
    model: "claude-sonnet-5-5 / claude-opus-5-5",
    unit_cost: "토큰 과금",
    key_url: "https://console.anthropic.com/settings/keys",
  },
  {
    name: "gemini",
    label: "Google Gemini",
    slots: [["gemini_api_key", "Google Gemini API 키"]],
    automates: ["Claude 키가 없으면 대본 작성·검수·명령 해석·게이트 판정을 Gemini가 수행", "Claude 장애·한도 초과 시 Gemini로 자동 대체", "직접 촬영 영상 대본 추출(STT)"],
    guardrails: ["거절(안전 판단)된 요청은 다른 모델로 우회하지 않습니다"],
    model: "gemini-3.8-flash",
    unit_cost: "입력 700원·출력 4,200원 / 100만 토큰",
    key_url: "https://aistudio.google.com/apikey",
  },
  {
    name: "openai_image",
    label: "OpenAI GPT Image",
    slots: [["openai_api_key", "OpenAI API 키 (GPT Image)"]],
    automates: ["문제 상황 장면(비상품 비트) 배경 이미지를 대본에 맞춰 자동 생성", "생성 이미지는 권리 원장에 'AI 생성 배경'으로 기록", "영상에 'AI 생성 장면' 표시 자동 삽입"],
    guardrails: ["상품 이미지는 절대 생성·변형하지 않습니다(상품 비트는 실제 상품 사진만)", "실존 인물 얼굴·브랜드·문구는 생성하지 않습니다"],
    model: "gpt-image-2.5-flare",
    unit_cost: "이미지 1장 약 90원",
    key_url: "https://platform.openai.com/api-keys",
  },
  {
    name: "higgsfield",
    label: "Higgsfield 영상",
    slots: [
      ["higgsfield_key_id", "Higgsfield API Key ID"],
      ["higgsfield_key_secret", "Higgsfield API Key Secret"],
    ],
    automates: ["AI 장면 이미지를 5초 모션 영상으로 자동 변환해 해당 비트에 배치", "스튜디오 툴 › 이미지→영상 'AI 모션' (AI 장면 이미지 전용)"],
    guardrails: ["상품 사진은 AI 모션에 쓰지 않습니다(카메라 모션만 허용)"],
    model: "kling-video/v2.5-turbo/standard/image-to-video",
    unit_cost: "5초 클립 1개 약 450원",
    key_url: "https://console.higgsfield.ai",
  },
  {
    name: "elevenlabs",
    label: "ElevenLabs 음성",
    slots: [["elevenlabs_api_key", "ElevenLabs API 키"]],
    automates: ["TTS가 즉시 ElevenLabs 실제 음성으로 전환", "빠른 보이스 9종을 계정 음성에 자동 매핑(느린 내레이션형 음성 제외)", "미리듣기 음성 자동 갱신"],
    guardrails: [],
    model: "eleven_multilingual_v2",
    unit_cost: "1,000자 약 300원",
    key_url: "https://elevenlabs.io/app/settings/api-keys",
  },
];

export const INTEGRATION_SLOT_LABELS: Array<[string, string]> = INTEGRATION_SPECS.flatMap((s) => s.slots);

const MOCK_VOICES: Record<string, { voice_id: string; name: string }> = {
  "ko-F-30s-brisk": { voice_id: "el_jisoo", name: "Jisoo" },
  "ko-F-30s-rapid": { voice_id: "el_hana", name: "Hana" },
  "ko-F-20s-bright": { voice_id: "el_yuna", name: "Yuna" },
  "ko-F-20s-energetic": { voice_id: "el_bora", name: "Bora" },
  "ko-F-40s-lively": { voice_id: "el_sumi", name: "Sumi" },
  "ko-M-30s-brisk": { voice_id: "el_minho", name: "Minho" },
  "ko-M-30s-rapid": { voice_id: "el_jun", name: "Jun" },
  "ko-M-20s-energetic": { voice_id: "el_taeo", name: "Taeo" },
  "ko-M-40s-punchy": { voice_id: "el_sanghun", name: "Sanghun" },
};

interface MockIntegrationState {
  enabled: Record<string, boolean>;
  checks: Record<string, { ok: boolean; detail: string; at: string }>;
  voices: Record<string, { voice_id: string; name: string }>;
}

const state: MockIntegrationState = {
  enabled: {},
  checks: {
    anthropic: { ok: true, detail: "키 확인됨", at: new Date(Date.now() - 3 * 3600_000).toISOString() },
    elevenlabs: { ok: true, detail: "키 확인됨 · 이번 달 18,420/100,000자 사용", at: new Date(Date.now() - 3 * 3600_000).toISOString() },
  },
  voices: { ...MOCK_VOICES },
};

function spec(name: string): Spec | undefined {
  return INTEGRATION_SPECS.find((s) => s.name === name);
}

export function integrationForSlot(slot: string): string | null {
  return INTEGRATION_SPECS.find((s) => s.slots.some(([n]) => n === slot))?.name ?? null;
}

export function integrationStatus(secrets: Record<string, boolean>, name: string): IntegrationIn {
  const sp = spec(name)!;
  const configured = sp.slots.every(([n]) => !!secrets[n]);
  const enabled = state.enabled[name] ?? true;
  const chk = configured ? state.checks[name] : undefined;
  let st: IntegrationIn["state"] = "missing";
  let detail = "키를 등록하면 아래 기능이 자동으로 켜집니다";
  if (configured && !enabled) {
    st = "paused";
    detail = "일시 중지됨 — 키는 보관 중";
  } else if (configured && !chk) {
    st = "unverified";
    detail = "등록됨 · 연결 확인 전";
  } else if (configured && chk) {
    st = chk.ok ? "active" : "error";
    detail = chk.detail;
  }
  return {
    name,
    label: sp.label,
    state: st,
    detail,
    enabled,
    configured,
    active: st === "active" || st === "unverified",
    checked_at: chk?.at ?? null,
    slots: sp.slots.map(([n, label]) => ({ name: n, label, configured: !!secrets[n] })),
    automates: sp.automates,
    guardrails: sp.guardrails,
    model: sp.model,
    unit_cost: sp.unit_cost,
    key_url: sp.key_url,
    ...(name === "elevenlabs" ? { voices: configured ? state.voices : {} } : {}),
  };
}

function active(secrets: Record<string, boolean>, name: string): boolean {
  return !!integrationStatus(secrets, name).active;
}

export function llmProvider(secrets: Record<string, boolean>): string {
  const c = active(secrets, "anthropic");
  const g = active(secrets, "gemini");
  return c && g ? "anthropic+gemini" : c ? "anthropic" : g ? "gemini" : "offline";
}

export function integrationsList(secrets: Record<string, boolean>) {
  return { items: INTEGRATION_SPECS.map((s) => integrationStatus(secrets, s.name)), llm: { provider: llmProvider(secrets) } };
}

/** After a slot was written (value given) or deleted (value null). */
export function onSecretChanged(secrets: Record<string, boolean>, slot: string, value: string | null): IntegrationIn | null {
  const name = integrationForSlot(slot);
  if (!name) return null;
  const sp = spec(name)!;
  const actions: string[] = [];
  if (value !== null && sp.slots.every(([n]) => !!secrets[n])) {
    const ok = !value.toLowerCase().includes("bad");
    state.checks[name] = {
      ok,
      detail: ok ? `키 확인됨 · 모델 ${sp.model}` : "키가 유효하지 않거나 권한이 없습니다",
      at: new Date().toISOString(),
    };
    if (ok && (name === "anthropic" || name === "gemini")) actions.push(`LLM: ${llmProvider(secrets)}`);
    if (ok && name === "elevenlabs") {
      state.voices = { ...MOCK_VOICES };
      actions.push("TTS 재선택", "보이스 9종 자동 매핑");
    }
  } else if (value === null) {
    delete state.checks[name];
  }
  return { ...integrationStatus(secrets, name), actions };
}

export function verifyIntegration(secrets: Record<string, boolean>, name: string): IntegrationIn {
  const sp = spec(name)!;
  if (sp.slots.every(([n]) => !!secrets[n])) {
    const prev = state.checks[name];
    state.checks[name] = { ok: prev?.ok ?? true, detail: prev?.detail ?? `키 확인됨 · 모델 ${sp.model}`, at: new Date().toISOString() };
  }
  return { ...integrationStatus(secrets, name), actions: [] };
}

export function setIntegrationEnabled(secrets: Record<string, boolean>, name: string, enabled: boolean): IntegrationIn {
  state.enabled[name] = enabled;
  return integrationStatus(secrets, name);
}

export function remapVoices(secrets: Record<string, boolean>) {
  state.voices = { ...MOCK_VOICES };
  return { mapped: Object.keys(state.voices).length, voices: state.voices, integration: integrationStatus(secrets, "elevenlabs") };
}

export function studioIntegrations(secrets: Record<string, boolean>) {
  const a = Object.fromEntries(INTEGRATION_SPECS.map((s) => [s.name, active(secrets, s.name)])) as Record<string, boolean>;
  const parts: string[] = [];
  if (a.openai_image) parts.push(`문제 장면(비상품 비트)에 GPT Image 배경 자동 생성${a.higgsfield ? " + Higgsfield 모션" : ""}`);
  if (a.elevenlabs) parts.push("ElevenLabs 실제 음성");
  if (a.anthropic || a.gemini) parts.push(`AI 대본 작성·검수(${[a.anthropic && "Claude", a.gemini && "Gemini"].filter(Boolean).join("·")})`);
  return {
    anthropic: a.anthropic,
    gemini: a.gemini,
    openai_image: a.openai_image,
    higgsfield: a.higgsfield,
    elevenlabs: a.elevenlabs,
    llm: llmProvider(secrets),
    ai_scenes: a.openai_image,
    ai_motion: a.higgsfield,
    stt: a.gemini,
    note: parts.join(" · ") || "AI 키 미등록 — 운영 준비 › AI 연동에서 키를 등록하면 자동으로 켜집니다",
  };
}

export function isIntegrationActive(secrets: Record<string, boolean>, name: string): boolean {
  return active(secrets, name);
}
