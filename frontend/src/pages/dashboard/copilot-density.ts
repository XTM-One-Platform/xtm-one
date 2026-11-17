/**
 * How much of itself a Copilot dashboard widget draws at a given grid size:
 * its status header. The rules every dashboard shares (cell height, list
 * capacity, KPI cards) are in `components/dashboard-grid/widget-density.ts`.
 */
import type { WidgetSize } from "../../components/dashboard-grid/widget-density.ts";

export interface CopilotHeroDensity {
  /** The ecosystem orbit beside the text, or its count as a chip in the
   *  header line. */
  orbit: boolean;
  /** The subtitle in full, or cut to two lines. */
  subtitle: "full" | "clamped";
  /** The rotating line of the latest runs under the call to action. */
  ticker: boolean;
}

/** The status header: greeting, subtitle, call to action, latest runs, the
 *  orbit of the connected integrations. */
export function copilotHeroDensity(size: WidgetSize | null): CopilotHeroDensity {
  if (!size) return { orbit: true, subtitle: "full", ticker: true };
  const { cols, rows } = size;
  return {
    // The orbit is 176px square, in 32px of padding: it needs the width beside
    // the text and the height of an eight-row header.
    orbit: cols >= 8 && rows >= 8,
    subtitle: cols >= 8 ? "full" : "clamped",
    ticker: rows >= 7,
  };
}
