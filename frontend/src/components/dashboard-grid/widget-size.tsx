import type { ReactNode } from "react";
import type { WidgetSize } from "./widget-density";
import { useWidgetSize } from "./widget-size-context";

/** The grid size of the enclosing widget (`widget-size-context.ts`), for
 *  markup written inline in a dashboard. */
export function WidgetSized({ children }: { children: (size: WidgetSize | null) => ReactNode }) {
  return <>{children(useWidgetSize())}</>;
}
