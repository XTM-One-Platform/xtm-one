// SPDX-License-Identifier: Apache-2.0

/**
 * CopyIconButton — icon-only copy-to-clipboard control with visual feedback.
 *
 * Always visible (never hover-gated). Copies `value` to the clipboard via
 * `navigator.clipboard.writeText()` with a `document.execCommand("copy")`
 * fallback for environments where the async Clipboard API is unavailable
 * (insecure origins, older browsers). On success the icon briefly switches to
 * a checkmark; on failure it shows a non-blocking error indicator. Both states
 * revert after `COPY_FEEDBACK_DELAY`.
 *
 * Usage:
 *   <CopyIconButton value={persona} label="Copy persona" />
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, Copy } from "lucide-react";

import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { cn } from "@/lib/utils";

type CopyState = "idle" | "copied" | "error";

export async function writeToClipboard(text: string): Promise<void> {
  // Prefer the async Clipboard API, but fall through to the legacy fallback
  // when it is missing OR rejects at runtime — an insecure origin, a denied
  // permission, or an unfocused document surfaces as a rejected promise (not a
  // missing method), and those are exactly the cases the fallback exists for.
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the execCommand fallback below
    }
  }
  // Fallback for insecure origins / older browsers / denied async writes.
  // Capture the current focus so we can restore it after the temp textarea
  // steals it - otherwise keyboard users are dropped to <body> post-copy.
  const previouslyFocused = document.activeElement as HTMLElement | null;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    if (!document.execCommand("copy")) throw new Error("execCommand copy failed");
  } finally {
    document.body.removeChild(textarea);
    previouslyFocused?.focus?.();
  }
}

interface CopyIconButtonProps {
  /** The content copied to the clipboard when clicked. */
  value: string;
  /** Accessible label / tooltip, e.g. "Copy persona". */
  label: string;
  /** Optional extra classes for the button. */
  className?: string;
}

export function CopyIconButton({ value, label, className }: CopyIconButtonProps) {
  const { t_i18n } = useFormatter();
  const [state, setState] = useState<CopyState>("idle");
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    },
    [],
  );

  const handleCopy = useCallback(async () => {
    try {
      await writeToClipboard(value ?? "");
      setState("copied");
    } catch {
      setState("error");
    }
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setState("idle"), COPY_FEEDBACK_DELAY);
  }, [value]);

  const tooltip = state === "copied" ? t_i18n("Copied!") : state === "error" ? t_i18n("Copy failed") : label;
  const Icon = state === "copied" ? Check : state === "error" ? AlertCircle : Copy;

  return (
    <Tip content={tooltip}>
      <button
        type="button"
        aria-label={label}
        onClick={handleCopy}
        className={cn(
          "rounded-sm p-1 text-default-secondary transition-colors hover:bg-elevation-hover hover:text-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
          state === "copied" && "text-feedback-success-primary hover:text-feedback-success-primary",
          state === "error" && "text-feedback-error-primary hover:text-feedback-error-primary",
          className,
        )}
      >
        <Icon className="h-3.5 w-3.5" />
      </button>
    </Tip>
  );
}
