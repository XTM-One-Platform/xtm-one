/**
 * The card a chart sits in: its title, what it reads (a hint), the
 * switches that change what it draws, and a figure that sums it up - the AI
 * Adoption cards' anatomy, so the two dashboards read alike.
 */
import type { ReactNode } from "react";
import { Info, type LucideIcon } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

interface ChartCardProps {
  /** Already translated. */
  title: string;
  icon: LucideIcon;
  /** Already translated. */
  hint?: string;
  /** The switches, right of the title. */
  actions?: ReactNode;
  /** The figure the chart sums up to, under the title. */
  summary?: ReactNode;
  /** A refetch is under way: the chart shows the last answer meanwhile. */
  isFetching?: boolean;
  className?: string;
  children: ReactNode;
}

export function ChartCard({ title, icon: Icon, hint, actions, summary, isFetching, className, children }: ChartCardProps) {
  return (
    <Card className={cn("flex h-full flex-col", className)}>
      <div className="flex flex-wrap items-start justify-between gap-2 px-4 pt-4">
        <div className="min-w-0 space-y-0.5">
          <h2 className="flex min-w-0 items-center gap-2 content-base font-semibold text-default-primary">
            <Icon className="h-4 w-4 shrink-0 text-accent" />
            <span className="truncate">{title}</span>
            {hint && (
              <Tip content={hint}>
                <Info tabIndex={0} aria-label={hint} className="h-3.5 w-3.5 shrink-0 cursor-help text-default-disabled outline-none hover:text-default-secondary focus-visible:text-default-secondary" />
              </Tip>
            )}
            {isFetching && <Spinner size="sm" tone="inherit" className="shrink-0 text-accent" />}
          </h2>
          {summary && <div className="content-compact text-default-secondary">{summary}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
      </div>
      <CardContent className="min-h-0 flex-1 p-4">{children}</CardContent>
    </Card>
  );
}

/** The chart area's empty and loading states, at the chart's own height. */
export function ChartPlaceholder({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "flex h-[260px] items-center justify-center rounded-sm border border-dashed border-elevation-subtle-soft p-4 text-center content-compact text-default-secondary",
        className,
      )}
    >
      {children}
    </div>
  );
}
