// SPDX-License-Identifier: Apache-2.0

/**
 * Grid ⇄ list toggle for asset index pages — the icon-pair counterpart to
 * `SegmentedFilter`, sitting in the same filter row as SearchInput /
 * VisibilityFilter / TagFilter. Pair it with `useViewMode()`, which persists
 * the choice.
 */
import { LayoutGrid, List } from "lucide-react";
import { Icon } from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { ViewMode } from "@/hooks/useViewMode";

interface ViewToggleProps {
  value: ViewMode;
  onChange: (mode: ViewMode) => void;
  /** The views the page offers, in order (default grid and list; Work Items adds graph). */
  modes?: readonly ViewMode[];
  className?: string;
}

/** The label of each view, a catalog key. @i18n-keys */
const LABELS = {
  grid: "Grid view",
  list: "List view",
  graph: "Graph view",
  kanban: "Kanban view",
} as const satisfies Record<ViewMode, string>;

/** The graph view's glyph, through the design system's `<Icon>` (no new lucide import; UX r1 B4 on #979). */
function GraphGlyph({ className }: { className?: string }) {
  return <Icon name="network" size={16} className={className} aria-hidden />;
}

/** The Kanban view's glyph, through the design system's `<Icon>` (#1503). */
function KanbanGlyph({ className }: { className?: string }) {
  return <Icon name="columns-3" size={16} className={className} aria-hidden />;
}

const OPTIONS: readonly { key: ViewMode; icon: React.ElementType }[] = [
  { key: "grid", icon: LayoutGrid },
  { key: "list", icon: List },
  { key: "graph", icon: GraphGlyph },
  { key: "kanban", icon: KanbanGlyph },
];

const DEFAULT_MODES: readonly ViewMode[] = ["grid", "list"];

export function ViewToggle({ value, onChange, modes = DEFAULT_MODES, className }: ViewToggleProps) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="group"
      aria-label={t_i18n("View mode")}
      className={cn("flex gap-1 rounded-sm bg-elevation-hover p-0.5 items-center", className)}
    >
      {OPTIONS.filter((option) => modes.includes(option.key)).map(({ key, icon: Icon }) => {
        const isActive = key === value;
        const label = LABELS[key];
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
