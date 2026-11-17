// SPDX-License-Identifier: Apache-2.0

import type { ElementType, KeyboardEventHandler, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * One choice of a radio group drawn as a card: an icon, a title and a
 * description, the selected card tinted with the accent (or the error colour
 * for a destructive choice). The one radio card of the app: the emergency stop
 * scope and the fork member mode draw it from here, never with page-local
 * markup (`option-card.test.ts` fails a copy of its class string).
 *
 * The caller owns the group: `role="radiogroup"` with its label, and the arrow
 * keys when it wants a roving tab stop (`tabIndex`, `id`).
 *
 * ```tsx
 * <div role="radiogroup" aria-label={t_i18n("Scope")} className="grid gap-2">
 *   <OptionCard selected={scope === "self"} onSelect={() => setScope("self")}
 *     icon={UserIcon} title={t_i18n("My activity only")} description={...} />
 * </div>
 * ```
 */
export function OptionCard({
  selected,
  onSelect,
  icon: Icon,
  title,
  description,
  disabled,
  danger,
  id,
  tabIndex,
  onKeyDown,
}: {
  selected: boolean;
  onSelect: () => void;
  icon: ElementType;
  title: ReactNode;
  description: ReactNode;
  disabled?: boolean;
  /** A destructive choice: the selected card takes the error colour. */
  danger?: boolean;
  id?: string;
  /** For a roving tab stop: 0 on the selected card, -1 on the others. */
  tabIndex?: number;
  onKeyDown?: KeyboardEventHandler<HTMLButtonElement>;
}) {
  return (
    <button
      id={id}
      type="button"
      role="radio"
      aria-checked={selected}
      tabIndex={tabIndex}
      disabled={disabled}
      onClick={onSelect}
      onKeyDown={onKeyDown}
      className={cn(
        "flex w-full items-start gap-3 rounded-sm border px-3 py-2.5 text-left transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
        "disabled:cursor-not-allowed disabled:opacity-60",
        selected
          ? danger
            ? "border-feedback-error-primary/60 bg-feedback-error-primary/10"
            : "border-accent/60 bg-accent/10"
          : "border-elevation-subtle hover:border-elevation-subtle-soft hover:bg-elevation-hover",
      )}
    >
      <Icon
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          selected ? (danger ? "text-feedback-error-primary" : "text-accent") : "text-default-secondary",
        )}
        aria-hidden
      />
      <span className="space-y-0.5">
        <span className="block content-base font-medium text-default-primary">{title}</span>
        <span className="block content-compact leading-relaxed text-default-secondary">{description}</span>
      </span>
    </button>
  );
}
