// SPDX-License-Identifier: Apache-2.0

/**
 * The layout a dashboard built on the grid (`components/dashboard-grid`) draws,
 * and how a change is saved: the CTEM command center (one layout per vertical)
 * and the Copilot dashboard (one layout).
 *
 * The saved layout comes from the cached user. A change is drawn at once from
 * a local override: preference writes are queued (`useUpdateProfile`), and
 * the cached user only catches up with a queued one once the write ahead of it
 * settles, so quick changes read from it would start from a layout already
 * replaced. The overrides last only while writes are in flight: once the
 * latest one settles (the only one whose callbacks run), the cached user holds
 * every layout written - each write's optimistic patch ran before it was sent
 * - or, after a failure, what the server has.
 */
import { useMemo, useState } from "react";
import { defaultLayout, resolveLayout, type WidgetRegistry } from "@/components/dashboard-grid/layout";
import type { DashboardLayout } from "@/types";

export function useDashboardLayoutState<Id extends string>({
  key,
  saved,
  registry,
  write,
}: {
  /** Which of the dashboard's layouts is on screen (a vertical; any constant
   *  for a dashboard with one layout). */
  key: string;
  /** The saved layout, as the user carries it (unchecked). */
  saved: { items?: unknown; hidden?: unknown } | null | undefined;
  registry: WidgetRegistry<Id>;
  /** Save `layout` (`null` resets the default one), calling `settled` once the
   *  write is over, whatever its outcome. */
  write: (layout: DashboardLayout<Id> | null, settled: () => void) => void;
}): { layout: DashboardLayout<Id>; onChange: (layout: DashboardLayout<Id> | null) => void } {
  const [overrides, setOverrides] = useState<Record<string, DashboardLayout<Id>>>({});
  const savedLayout = useMemo(() => resolveLayout(saved, registry), [saved, registry]);
  const onChange = (layout: DashboardLayout<Id> | null) => {
    setOverrides((current) => ({ ...current, [key]: layout ?? defaultLayout(registry) }));
    // Every key's override goes, not only this one's: `settled` runs for the
    // latest write only, and by then the cached user holds every key's layout.
    write(layout, () => setOverrides({}));
  };
  return { layout: overrides[key] ?? savedLayout, onChange };
}
