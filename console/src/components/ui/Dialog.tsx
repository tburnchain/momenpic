"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";
import { IconButton } from "./Button";

/**
 * Modal dialog on the native <dialog> element: focus trapping, Escape-to-close and inert
 * background come from the browser. Focus returns to the opener on close.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  variant = "center",
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** "side" = right-hand drawer on desktop, bottom sheet on phones. */
  variant?: "center" | "side";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      const opener = document.activeElement as HTMLElement | null;
      el.showModal();
      return () => {
        if (el.open) el.close();
        opener?.focus?.();
      };
    }
    if (!open && el.open) el.close();
  }, [open]);

  const width = size === "sm" ? "max-w-sm" : size === "lg" ? "max-w-2xl" : "max-w-lg";
  const frame =
    variant === "side"
      ? "mb-0 mt-auto w-full max-w-none rounded-t-2xl sm:m-0 sm:ml-auto sm:h-dvh sm:max-h-dvh sm:w-[min(560px,100%)] sm:rounded-none sm:rounded-l-2xl"
      : `m-auto w-[calc(100%-2rem)] ${width} rounded-2xl`;
  const inner = variant === "side" ? "max-h-[90dvh] sm:h-dvh sm:max-h-dvh" : "max-h-[85dvh]";

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descId : undefined}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        // Click on the backdrop (the dialog element itself, outside the panel) closes.
        if (e.target === e.currentTarget) onClose();
      }}
      className={`${frame} border border-line bg-surface p-0 text-ink shadow-2xl backdrop:backdrop-blur-[1px]`}
    >
      {open ? (
        <div className={`flex ${inner} flex-col`}>
          <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold">
                {title}
              </h2>
              {description ? (
                <p id={descId} className="mt-0.5 text-sm text-muted">
                  {description}
                </p>
              ) : null}
            </div>
            <IconButton icon="x" label="닫기" size="sm" onClick={onClose} />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  );
}
