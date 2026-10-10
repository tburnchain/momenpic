"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";

type ToastTone = "good" | "info" | "critical";
interface ToastItem {
  id: number;
  tone: ToastTone;
  message: string;
}

const ToastContext = createContext<((message: string, tone?: ToastTone) => void) | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const push = useCallback((message: string, tone: ToastTone = "good") => {
    seq.current += 1;
    const id = seq.current;
    setItems((xs) => [...xs.slice(-3), { id, tone, message }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "critical" ? 6000 : 3200);
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed inset-x-0 top-16 z-50 flex flex-col items-center gap-2 px-4 lg:top-4"
      >
        {items.map((t) => (
          <div
            key={t.id}
            role={t.tone === "critical" ? "alert" : "status"}
            className="pointer-events-auto flex max-w-md items-center gap-2 rounded-xl border border-line bg-surface px-4 py-2.5 text-sm text-ink shadow-xl"
          >
            <Icon
              name={t.tone === "critical" ? "alert" : t.tone === "info" ? "info" : "check"}
              size={16}
              className={t.tone === "critical" ? "text-critical-ink" : t.tone === "info" ? "text-accent-ink" : "text-good-ink"}
            />
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
