// SPDX-License-Identifier: Apache-2.0

import { Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Wraps a control with the shared explanation when AI is off, and renders it
 * untouched otherwise — `Tip` passes children through on empty content, so
 * this is safe to leave in place unconditionally.
 *
 * `wrap` defaults to true because the wrapped control is normally natively
 * disabled, and a disabled element swallows the hover the tooltip needs.
 */
export function AiDisabledTip({
  children,
  side = "top",
  wrap = true,
  wrapClassName,
}: {
  children: React.ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  wrap?: boolean;
  wrapClassName?: string;
}) {
  const reason = useAiDisabledReason();
  return (
    <Tip
      content={reason ?? undefined}
      side={side}
      wrap={wrap && !!reason}
      wrapClassName={wrapClassName}
    >
      {children}
    </Tip>
  );
}

/**
 * A sentence-length notice, for surfaces with room to explain rather than just
 * mark — the sign-in screen (FR-011) and read-only settings sections (FR-022).
 * Renders nothing when AI is on.
 */
export function AiDisabledNotice({
  className,
  id,
}: {
  className?: string;
  /** Target for an `aria-describedby` on the section this explains. */
  id?: string;
}) {
  const reason = useAiDisabledReason();
  if (!reason) return null;
  return (
    <div
      id={id}
      role="status"
      className={cn(
        "flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.07] px-3 py-2 text-left content-compact text-feedback-warning-tertiary",
        className,
      )}
    >
      <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
      <span>{reason}</span>
    </div>
  );
}

/**
 * Makes an AI action visibly unavailable and non-startable (FR-018).
 *
 * Wraps a Create / Test / Evaluate / Sync affordance: dimmed, explained by the
 * one shared tooltip, and click-captured so it cannot start. Renders children
 * untouched when AI is on.
 *
 * Deliberately not folded into `useAgenticAIEnabled()`: that gate offers a
 * licence upsell, and no licence enables AI here (FR-031), so reusing it would
 * tell the administrator to buy something they already have.
 */
export function AiActionGate({ children }: { children: React.ReactNode }) {
  const reason = useAiDisabledReason();
  if (!reason) return <>{children}</>;
  return (
    <Tip content={reason}>
      {/* The trigger needs a real box. `display: contents` generates none, so
          it is not a hit-test target and Radix's pointer handlers never fire —
          the control ended up dimmed and silent, which is the opposite of
          FR-018's shared explanation. The outer span keeps pointer events (so
          hover works) and captures the click; only the inner one is inert. */}
      <span
        aria-disabled="true"
        tabIndex={0}
        className="inline-flex"
        onClickCapture={(e) => {
          e.preventDefault();
          e.stopPropagation();
        }}
      >
        <span className="pointer-events-none opacity-50">{children}</span>
      </span>
    </Tip>
  );
}

/**
 * Renders an AI settings section read-only instead of hiding it (FR-022).
 *
 * A native `<fieldset disabled>` does the work: it disables every descendant
 * form control — including the buttons Radix renders underneath its triggers —
 * so the section still shows what is configured while nothing can be changed.
 * Hiding it would leave an administrator unable to see the configuration they
 * are being told is inactive.
 *
 * `min-w-0` and the reset are load-bearing: a fieldset carries default margin,
 * padding, border and an intrinsic min-width that would break flex/grid
 * children.
 */
export function AiReadOnly({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const reason = useAiDisabledReason();
  if (!reason) return <>{children}</>;
  return (
    <div className="space-y-4">
      <AiDisabledNotice id="ai-disabled-reason" />
      <fieldset
        disabled
        aria-describedby="ai-disabled-reason"
        className={cn("m-0 min-w-0 border-0 p-0", className)}
      >
        {children}
      </fieldset>
    </div>
  );
}

/**
 * The persistent state marker (FR-020) — shown to every user, administrator or
 * not. Renders nothing when AI is on, so callers need no condition of their own.
 */
export function AiDisabledBadge({ className }: { className?: string }) {
  const reason = useAiDisabledReason();
  const { t_i18n } = useFormatter();
  if (!reason) return null;
  return (
    <Tip content={reason}>
      <Badge
        variant="outline"
        className={cn(
          "content-caption border-feedback-alert-primary/40 bg-feedback-alert-primary/10 text-feedback-warning-tertiary gap-1",
          className,
        )}
      >
        <Sparkles className="h-2.5 w-2.5" />
        {t_i18n("AI off")}
      </Badge>
    </Tip>
  );
}
