// SPDX-License-Identifier: Apache-2.0

/**
 * How much of itself a CTEM command center widget draws at a given grid size:
 * the posture overview and the stage cards. The rules every
 * dashboard shares (cell height, list capacity, KPI cards) are in
 * `components/dashboard-grid/widget-density.ts`.
 */
import type { WidgetSize } from "@/components/dashboard-grid/widget-density";

export interface HeroDensity {
  /** Where the posture orbit goes: beside the text, below it, or replaced by
   *  the compact score chip in the header. */
  orbit: "side" | "below" | "chip";
  /** The subtitle in full, or cut to two lines. */
  subtitle: "full" | "clamped";
  /** The product pills with their names and versions, or as logos with their
   *  health and edition (the name in a tooltip). */
  pills: "full" | "compact";
  /** The sync status with its next-sync countdown and the posture trend. */
  syncDetails: boolean;
}

/** The posture overview: greeting, sync, product health, orbit. The stages
 *  are drawn once, by the lifecycle band under it (#11464): no ribbon here. */
export function heroDensity(size: WidgetSize | null): HeroDensity {
  if (!size) return { orbit: "side", subtitle: "full", pills: "full", syncDetails: true };
  const { cols, rows } = size;
  const orbit = cols >= 9 && rows >= 11 ? "side" : rows >= 18 ? "below" : "chip";
  return {
    orbit,
    subtitle: cols >= 8 && rows >= 11 ? "full" : "clamped",
    pills: cols >= 8 ? "full" : "compact",
    syncDetails: cols >= 8,
  };
}

/** A CTEM stage card (and the Governance & Risk card, built the same way):
 *  `full` draws the facts, the chart and the score trend; `medium` drops the
 *  trend; `compact` keeps the header, four facts and the footer. */
export type StageDensity = "full" | "medium" | "compact";

export function stageDensity(size: WidgetSize | null): StageDensity {
  if (!size) return "full";
  if (size.rows >= 13) return "full";
  if (size.rows >= 10) return "medium";
  return "compact";
}

