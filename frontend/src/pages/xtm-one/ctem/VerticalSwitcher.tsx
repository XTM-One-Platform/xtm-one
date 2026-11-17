// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM vertical switcher - a compact segmented control (top-right of the home).
 *
 * Switches the whole command center between exposure verticals (Cyber default,
 * FIMI/disinformation, Fraud). The choice is per-user and persisted; the posture,
 * stage cards, agents and briefing all reshape to the selected vertical.
 */
import { ShieldCheck, Megaphone, Banknote, type LucideIcon } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { CTEM_VERTICALS, CTEM_VERTICAL_CONFIG, type CtemVerticalKey } from "@/lib/constants";
import type { CtemVertical } from "@/types";

const ICONS: Record<CtemVerticalKey, LucideIcon> = {
  cyber: ShieldCheck,
  fimi: Megaphone,
  fraud: Banknote,
};

export function VerticalSwitcher({
  value,
  onChange,
  size = "md",
  className,
}: {
  value: CtemVertical;
  onChange: (v: CtemVertical) => void;
  size?: "sm" | "md";
  /** Extra classes of the group (a toolbar sets its row height). */
  className?: string;
}) {
  const sm = size === "sm";
  return (
    <div
      className={cn(
        "inline-flex items-center gap-0.5 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm",
        sm ? "p-px" : "p-0.5",
        className,
      )}
      role="group"
      aria-label="CTEM vertical"
    >
      {CTEM_VERTICALS.map((v) => {
        const cfg = CTEM_VERTICAL_CONFIG[v];
        const Icon = ICONS[v];
        const active = value === v;
        return (
          <Tip key={v} content={cfg.tagline}>
            <button
              type="button"
              aria-pressed={active}
              aria-label={cfg.short}
              onClick={() => onChange(v)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-sm font-medium transition-colors",
                sm ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 content-compact",
                active
                  ? "bg-accent/15 text-accent shadow-[0_0_12px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]"
                  : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
              )}
            >
              <Icon className={cn("shrink-0", sm ? "h-3 w-3" : "h-3.5 w-3.5")} />
              <span className="hidden sm:inline">{cfg.short}</span>
            </button>
          </Tip>
        );
      })}
    </div>
  );
}
