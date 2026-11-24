/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Agentic Flow - the platform's agent command center.
 *
 * Two tabs:
 *   - Agent-to-agent map  the handover graph of the agents in scope, with its
 *                         views: the graph (force, hierarchy, radial layouts)
 *                         and the schedule (`/flows/map/schedule`)
 *   - Flows               the flow library, the way into the visual builder
 *
 * The fleets are a page of their own (`/fleets`, #9301 N2c): this page never
 * shows them.
 *
 * Tab state is URL-backed (`/flows/:tab?`) via `useTabParam`. The builder lives
 * one level deeper, on `/flows/library/:id`, so a full-height canvas is not
 * squeezed under this page's header and tab bar. The schedule is a view of the
 * map (`/flows/map/schedule`), not a tab; a segment naming no tab is not found
 * (#797).
 *
 * Only the `Flows` tab belongs to the visual builder, so only it is behind the
 * `FLOW_BUILDER` feature flag. The map predates the builder and reads agents,
 * assignments and delegations, no flow endpoint between them, so the page,
 * its nav entry and the map stay live while the builder is not announced.
 * The map is the landing tab whatever the flag says.
 */

import { useMemo, useState } from "react";
import RouteNotFound from "@/pages/RouteNotFound";
import { Workflow, LayoutGrid } from "lucide-react";
import { useFormatter } from "@/i18n";
import { useParams } from "react-router-dom";
import { useFeatureFlagResolved } from "@/hooks/useFeatureFlag";
import { isKnownMapView } from "@/lib/map-view";
import { useTabParam } from "@/hooks/useTabParam";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { PageSkeleton } from "@/components/ui/skeleton";
import LibraryTab from "@/pages/flows/LibraryTab";
import MapTab from "@/pages/flows/MapTab";
import { FlowsHeaderSlotContext } from "@/pages/flows/header-slot";

type FlowTab = "map" | "library";

const TABS: { key: FlowTab; label: string; icon: React.ElementType }[] = [
  { key: "map", label: "Agent-to-agent map", icon: Workflow },
  { key: "library", label: "Flows", icon: LayoutGrid },
];

const DEFAULT_TAB: FlowTab = "map";

export default function Flows() {
  const { t_i18n } = useFormatter();
  /* `useFeatureFlagResolved`, not `useFeatureFlag`: the flag is fail-closed, so
     an unresolved one reads as OFF. Waiting for it keeps the tab bar from
     growing a tab under the cursor once `/auth/config` lands. */
  const { enabled: flowBuilderEnabled, isResolved, isError } =
    useFeatureFlagResolved("FLOW_BUILDER");
  const tabs = useMemo(
    () => TABS.filter((t) => t.key !== "library" || flowBuilderEnabled),
    [flowBuilderEnabled],
  );
  const [rawTab] = useTabParam<FlowTab>(DEFAULT_TAB);
  const { mapView } = useParams<{ mapView?: string }>();
  // A state, not a ref: the tab renders into the slot once it is mounted.
  const [headerSlot, setHeaderSlot] = useState<HTMLElement | null>(null);

  // `useTabParam` returns the raw `:tab` path segment unvalidated: a segment that
  // names no tab (a former `/flows/schedule` included, or `/flows/library` while
  // the builder is hidden) is not found, never redirected (#797).
  // An unknown map view (`/flows/map/garbage`) is decided here too, before the
  // page header and tab row render: one full-page rendering of not found.
  const known = tabs.some((t) => t.key === rawTab) && (rawTab !== "map" || isKnownMapView(mapView));
  const tab: FlowTab = known ? rawTab : DEFAULT_TAB;

  /* Only while genuinely in flight. If `/auth/config` failed outright the flag
     is unknown, and this page, unlike the builder route, has somewhere useful
     to go: the map does not depend on it. So carry on with the library hidden
     rather than sit on a skeleton forever. */
  if (!isResolved && !isError) return <PageSkeleton />;
  if (!known) return <RouteNotFound />;

  return (
    <div className="space-y-6">
      {/* One header row (#10215, #9301 N4, as /fleets): no visible subtitle
          (amendment 3), the page's help sits in the title's "i"; the active
          tab's page-level actions render into the action slot. */}
      <ListPageHeader
        title={t_i18n("Agentic Flow")}
        infoLabel={t_i18n("About Agentic Flow")}
        info={t_i18n("How your agents hand work to each other, on the map and its schedule, and the flows that chain them.")}
        actions={<div ref={setHeaderSlot} className="flex items-center" />}
      />

      <DetailTabs tabs={tabs} active={tab} />

      <FlowsHeaderSlotContext.Provider value={headerSlot}>
        {tab === "map" && <MapTab />}
        {tab === "library" && <LibraryTab />}
      </FlowsHeaderSlotContext.Provider>
    </div>
  );
}
