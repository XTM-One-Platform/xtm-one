// SPDX-License-Identifier: Apache-2.0

/**
 * Grid ⇄ list toggle for asset index pages — the icon-pair counterpart to
 * `SegmentedFilter`, sitting in the same filter row as SearchInput /
 * VisibilityFilter / TagFilter. Pair it with `useViewMode()`, which persists
 * the choice.
 */
import { LayoutGrid, List } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { ViewMode } from "@/hooks/useViewMode";

interface ViewToggleProps {
  value: ViewMode;
  onChange: (mode: ViewMode) => void;
  className?: string;
}

const OPTIONS: { key: ViewMode; label: string; icon: React.ElementType }[] = [
  { key: "grid", label: "Grid view", icon: LayoutGrid },
  { key: "list", label: "List view", icon: List },
];

export function ViewToggle({ value, onChange, className }: ViewToggleProps) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="group"
      aria-label={t_i18n("View mode")}
      className={cn("flex gap-1 rounded-sm bg-elevation-hover p-0.5 items-center", className)}
    >
      {OPTIONS.map(({ key, label, icon: Icon }) => {
        const isActive = key === value;
        return (
          <Tip key={key} content={t_i18n(label)}>
            <button
              type="button"
              aria-pressed={isActive}
              aria-label={t_i18n(label)}
              onClick={() => onChange(key)}
              className={cn(
                "rounded-sm h-7 w-7 inline-flex items-center justify-center transition-colors",
                isActive
                  ? "bg-elevation-highlight text-default-primary"
                  : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
            </button>
          </Tip>
        );
      })}
    </div>
  );
}
