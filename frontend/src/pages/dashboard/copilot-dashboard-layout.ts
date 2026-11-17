/**
 * The widgets of the Copilot dashboard grid (the home of a deployment that is
 * not XTM One) and their default places. The grid, and how a saved layout
 * becomes the one drawn, are shared with the CTEM command center in
 * `components/dashboard-grid/layout.ts`.
 *
 * Each person builds their own dashboard (`User.copilot_dashboard_layout`,
 * written through `PUT /settings/preferences`). The backend accepts the widget
 * ids of `services/dashboard_layouts.py` only, the same as below.
 */
import type { WidgetSpec } from "@/components/dashboard-grid/layout";
import type { CopilotWidgetId } from "@/types";

const kpi = (x: number, w: number): WidgetSpec => ({ x, y: 8, w, h: 4, minW: 2, minH: 3 });
// The agent and conversation lists keep room for their empty state and its button.
const list = (x: number, w: number, minH = 7): WidgetSpec => ({ x, y: 12, w, h: 12, minW: 3, minH });

/** Every widget of the dashboard, with its default place: the default layout
 *  is the dashboard as it was before it could be rearranged (the five KPI
 *  cards share the twelve columns 2-2-3-3-2). */
export const COPILOT_WIDGETS: Record<CopilotWidgetId, WidgetSpec> = {
  hero: { x: 0, y: 0, w: 12, h: 8, minW: 6, minH: 6 },
  kpi_agents: kpi(0, 2),
  kpi_conversations: kpi(2, 2),
  kpi_executions: kpi(4, 3),
  kpi_integrations: kpi(7, 3),
  kpi_documents: kpi(10, 2),
  agents: list(0, 3),
  conversations: list(3, 3),
  live_activity: list(6, 6, 6),
  digests: { x: 0, y: 24, w: 12, h: 9, minW: 4, minH: 6 },
  // The Value card (#9836 R6b): under the digests, half the width, so a saved layout gains it without moving a widget.
  value: { x: 0, y: 33, w: 6, h: 7, minW: 4, minH: 6 },
  // Top value drivers (#12009): the right half of the Value row, same height, the same report.
  value_drivers: { x: 6, y: 33, w: 6, h: 7, minW: 4, minH: 6 },
  // Fleets (#12063, UX design rounds 1 and 2): full width under the Value row, so a saved layout gains it
  // without moving a widget; the page draws it at its content height (`fleetsDrawnLayout`).
  fleets: { x: 0, y: 40, w: 12, h: 9, minW: 6, minH: 4 },
};
