// SPDX-License-Identifier: Apache-2.0

/**
 * The row that holds "Running versions" and "Affected assets" on a case page (#2750):
 * side by side, 50/50 and the same height, once the page is wide enough for both
 * tables to keep every column; stacked full width below that, or when one of the two
 * cards stands alone.
 *
 * Every column stays drawn at half width: a case with a known fixed version adds Fixed
 * version and Status columns (Status steps aside below a 896 px Affected assets table),
 * so it keeps the stacked layout. Side by side, the row also measures its tables: one
 * that would scroll sideways (a long asset name, a long version, a wordy locale) sends
 * the row back to the stacked layout until the page is wider than the width it failed at.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { CtemCaseDetail } from "@/hooks/api/useCtemCasesApi";
import { cn } from "@/lib/utils";

/**
 * The content width the two cards share from: 96rem (1536 px) leaves each table about
 * 708 px, the width every column of both tables needs with en, fr, es and ja headers.
 * The case page reaches it at 1920 with the navbar open and from 1632 with the rail.
 */
const PAIRED_FROM_PX = 1536;

/** Whole class names, so Tailwind sees them: two equal tracks that never grow past half. */
const PAIRED_COLUMNS = "@min-[96rem]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]";

/** A table area wider than its box by more than a rounding pixel scrolls sideways. */
function scrollsSideways(area: Element): boolean {
  return area.scrollWidth > area.clientWidth + 1;
}

/** Whether both cards render and every column of both tables fits at half width. */
export function pairVersionsAndAssets(kase: Pick<CtemCaseDetail, "remediation_kind" | "fixed_version" | "running_versions" | "affected_assets">): boolean {
  if (kase.remediation_kind !== "upgrade") return false;
  const assets = kase.affected_assets;
  if (!assets || assets.asset_count === 0) return false;
  const versions = kase.running_versions;
  const fixKnown = Boolean(versions?.fixed_version ?? kase.fixed_version) || Object.keys(versions?.fixed_versions ?? {}).length > 0;
  if (fixKnown) return false;
  return !assets.assets.some((asset) => asset.status);
}

export function CaseVersionsAssetsRow({ pairable, children }: { pairable: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  // The widest row width at which a table scrolled sideways side by side.
  const [overflowAt, setOverflowAt] = useState<number | null>(null);
  const vetoed = overflowAt !== null && width !== null && width <= overflowAt;
  const paired = pairable && !vetoed;

  useEffect(() => {
    const node = ref.current;
    if (!node || !pairable || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const current = Math.round(node.getBoundingClientRect().width);
      setWidth(current);
      if (current < PAIRED_FROM_PX) return;
      const areas = Array.from(node.querySelectorAll('[data-testid="data-table-scroll"]'));
      if (areas.some(scrollsSideways)) setOverflowAt((previous) => Math.max(previous ?? 0, current));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    node.querySelectorAll('[data-testid="data-table-scroll"] > table').forEach((table) => observer.observe(table));
    return () => observer.disconnect();
  }, [pairable, paired]);

  return (
    <div ref={ref} className="@container" data-testid="ctem-case-versions-assets" data-paired={paired ? "" : undefined}>
      <div className={cn("grid gap-6 *:min-w-0", paired && PAIRED_COLUMNS)}>{children}</div>
    </div>
  );
}
