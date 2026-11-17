// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { CTEM_METRIC_INFO, type CtemMetricInfo } from "@/lib/constants";
import { InfoPopover } from "@/components/ui/info-popover";

/**
 * Tiny info ("i") icon next to a CTEM metric or score. HOVER (or focus) opens a
 * small popover explaining WHERE the value comes from (OpenCTI / OpenAEV /
 * derived / XTM One) and HOW it is computed, so the dashboard is fully
 * self-explanatory. Tapping also opens it (touch), and a click outside / Escape
 * closes it.
 *
 * By default it reads the provenance registered for `label` in CTEM_METRIC_INFO,
 * but `content` / `title` / `source` can override it so the same icon explains
 * things that are not registered metrics (e.g. a stage's score methodology).
 *
 * Renders nothing when there is nothing to explain, so it can be dropped next to
 * any value safely. Built on the shared InfoPopover (portal, placement and flip,
 * Escape, click outside, scroll, focus), so it is never clipped by the card's
 * overflow-hidden and never triggers the parent card's navigation.
 */
const SOURCE_STYLES: Record<CtemMetricInfo["source"], string> = {
  OpenCTI: "bg-accent/15 text-accent",
  OpenAEV: "bg-filigran-ia-primary/15 text-filigran-ia-primary",
  "OpenCTI + OpenAEV": "bg-accent/15 text-accent",
  Derived: "bg-feedback-alert-primary/15 text-feedback-warning-tertiary",
  "XTM One": "bg-accent/15 text-accent",
};

const POPOVER_WIDTH = 288;

export function MetricInfo({
  label,
  className,
  content,
  title,
  source,
}: {
  label: string;
  className?: string;
  /** Override the explanation text (for values not registered in
   *  CTEM_METRIC_INFO, e.g. a stage's score methodology). */
  content?: string;
  /** Override the popover header label. */
  title?: string;
  /** Override the source badge; omit (and leave unregistered) to hide it. */
  source?: CtemMetricInfo["source"];
}) {
  const info = CTEM_METRIC_INFO[label];
  const how = content ?? info?.how;
  const headerLabel = title ?? label;
  const src = source ?? info?.source;
  if (!how) return null;

  return (
    <InfoPopover
      label={`${headerLabel} - how it is computed`}
      icon={<Icon name="info" size={16} />}
      heading={headerLabel}
      headingClassName="text-[11px]"
      headingAside={
        src && (
          <span className={cn("text-[9px] font-semibold uppercase tracking-wider rounded-sm px-1.5 py-0.5 shrink-0", SOURCE_STYLES[src])}>
            {src}
          </span>
        )
      }
      width={POPOVER_WIDTH}
      hoverOpens
      triggerClassName={cn(
        "inline-flex items-center justify-center text-default-disabled hover:text-accent transition-colors shrink-0 cursor-help",
        className,
      )}
      popoverClassName="p-3"
    >
      <p className="text-[11px] text-default-secondary leading-relaxed">{how}</p>
    </InfoPopover>
  );
}
