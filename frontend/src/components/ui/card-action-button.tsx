// SPDX-License-Identifier: Apache-2.0

/**
 * A small action on a card's header or window bar: an icon, an
 * optional short text, the tooltip and accessible name in one place, and the
 * design-system focus ring a keyboard user sees (`focus-visible:ring-2
 * ring-focus`, as `CopyIconButton`). One component instead of a local
 * `<button>` per card action, each with its own padding and no ring.
 *
 * `text` is the visible word next to the icon; `textClassName` lets a caller
 * hide it on a narrow container (`hidden @xl:inline`), the icon, tooltip and
 * accessible name staying.
 */
import type { ReactNode } from "react";

import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** The hover colour of the action: `accent` for a view, `danger` for a stop. */
export type CardActionTone = "accent" | "danger";

const TONE_HOVER: Record<CardActionTone, string> = {
  accent: "hover:text-accent",
  danger: "hover:text-feedback-error-primary",
};

interface CardActionButtonProps {
  /** The icon, 12-14 px, `aria-hidden`. */
  icon: ReactNode;
  /** Accessible name and tooltip, already translated. */
  label: string;
  /** Visible text after the icon (already translated); none for an icon-only action. */
  text?: string;
  /** Extra classes on the visible text, e.g. to hide it on a narrow container. */
  textClassName?: string;
  tone?: CardActionTone;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}

export function CardActionButton({
  icon,
  label,
  text,
  textClassName,
  tone = "accent",
  onClick,
  disabled,
  className,
}: CardActionButtonProps) {
  return (
    <Tip content={label}>
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        className={cn(
          "flex shrink-0 items-center gap-1 rounded-sm p-1 text-[11px] text-default-secondary transition-colors hover:bg-elevation-hover focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-60",
          text && "px-1.5 py-0.5",
          TONE_HOVER[tone],
          className,
        )}
      >
        {icon}
        {text && <span className={textClassName}>{text}</span>}
      </button>
    </Tip>
  );
}
