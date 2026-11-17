/**
 * The grid size of the dashboard widget being drawn, handed down by
 * `DashboardGrid` so a widget can choose how much of itself to draw
 * (`widget-density.ts`). `null` outside the grid and in the stacked layout of
 * a narrow window: the widget is drawn at its natural height, in full.
 */
import { createContext, useContext } from "react";
import type { WidgetSize } from "./widget-density";

export const WidgetSizeContext = createContext<WidgetSize | null>(null);

/** The grid size of the widget this component is drawn in. */
export function useWidgetSize(): WidgetSize | null {
  return useContext(WidgetSizeContext);
}
