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
 * MapTab - the platform's agent command map (merged "possibilities" + "now").
 *
 * A single live, editable view of the autonomous fleet:
 *   - the handover topology + per-agent capabilities, editable inline and
 *     enriched with trigger chips and a per-agent detail drawer;
 *   - live runtime overlays on that same graph - running pulses and edges
 *     where a handover recently fired;
 *   - the toolbar above the graph: layout (force, hierarchy top-down or
 *     left-to-right, radial), the visibility quick filter Mine | Company |
 *     Shared | All (`?vis=`, landing on the first segment holding an agent),
 *     the fleets layer and
 *     focus; the counters of the scope shown in a thin status line under it;
 *   - a merged real-time feed of assignment runs, inter-agent delegations,
 *     and work items, below the graph;
 *   - the Fleets layer, on by default: the members of a fleet drawn as one
 *     collapsed fleet node that opens the fleet page (`lib/fleet-layer`);
 *     a toolbar switch draws every agent again;
 *   - a second view, the schedule of the same agents (`/flows/map/schedule`,
 *     `?agent=<id>` narrows it to one agent): the former Schedule tab.
 *
 * The page carries no info block (amendment 3): no handover warnings
 * stack, no hints paragraph, no stat tiles; the graph is the page.
 *
 * Interactive edge creation/editing: users can click nodes to create new
 * handover edges and click edges to change type or delete. Permissions enforce
 * that only editable agents can be edge sources.
 */

import { workItemsPath } from "@/lib/task-routes";
import { useMemo, useCallback, useState, useEffect, useRef } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { mapViewPath, readMapView, type MapView } from "@/lib/map-view";
import { Radio } from "lucide-react";
import { ScheduleView } from "@/components/ui/schedule-view";
import { AgentFocusPicker } from "@/components/ui/agent-focus-picker";
import { scheduleScope } from "@/lib/schedule-scope";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { EmptyState } from "@/components/ui/empty-state";
import { PageSkeleton } from "@/components/ui/skeleton";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { AgentFlowGraph, AgentFlowLegend, type AgentFlowGraphHandle } from "@/components/ui/agent-flow";
import {
  AgentGraphFilterControls,
  AgentGraphForces,
  AgentGraphStatusLine,
  AgentGraphToolbar,
} from "@/components/ui/agent-graph-toolbar";
import { automaticForces, type GraphForces, type GraphLayout } from "@/lib/agent-graph-layout";
import {
  activeFilterCount,
  agentsInVisibility,
  filterGraphAgents,
  type AgentGraphFilters,
} from "@/lib/agent-graph-filters";
import { VisibilityEmptyHint, VisibilityFilter } from "@/components/ui/visibility-filter";
import { useBuiltinCompanyTip } from "@/components/ui/builtin-fold-filter";
import type { BuiltinFoldState } from "@/hooks/useEntityFilter";
import {
  builtinLanding,
  companySplitKeeping,
  defaultBuiltinChoice,
  foldBuiltins,
  readBuiltinParam,
  readStoredBuiltin,
  storeBuiltin,
  writeBuiltinParam,
  type BuiltinChoice,
} from "@/lib/builtin-fold";
import {
  countSegments,
  inSegment,
  writeVisibility,
  type Visibility,
} from "@/lib/visibility";
import { MAP_VIEW_DEFAULTS, mapSegmentNamed } from "@/lib/agent-graph-view-state";
import { FlowEventRow, type FlowEvent } from "@/components/ui/flow-event-row";
import {
  useAgents,
  useAgentDelegations,
} from "@/hooks/api/useAgentApi";
import {
  useAssignments,
  useActiveAgentIds,
  useAllAssignmentRuns,
} from "@/hooks/api/useAssignmentApi";
import { useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { usePersistedGraphView } from "@/hooks/usePersistedGraphView";
import { useNow, useTick } from "@/hooks/useTick";
import { liveHandoverEdges } from "@/lib/live-handovers";
import { useHandoverEdgeEditing } from "@/hooks/useHandoverEdgeEditing";
import { useAgentDetailsDrawer } from "@/hooks/useAgentDetailsDrawer";
import { useFormatter } from "@/i18n";
import { buildAgentSummaries } from "@/lib/agent-summary";
import type { AgentFlowFocusMode } from "@/lib/agent-flow-focus";
import { runsEveryHandoverType } from "@/lib/handover-types";
import { generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { collapseFleets, drawnIds, fleetIdOfNode, remapHandovers } from "@/lib/fleet-layer";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useA2ALiaisons } from "@/hooks/api/useA2AApi";
import { a2aPartnerDetail, a2aPartnerPath, a2aPartnerSubLabel, withA2ALiaisons } from "@/lib/agent-graph-a2a";
import { summaryPreview } from "@/pages/chat/answerMarkers";

const RUNNING_RUN_STATUSES = ["running", "pending", "delayed", "awaiting_human"];
const OPEN_WORK_ITEM_STATUSES = ["open", "claimed", "in_progress"];
/** Read by `t_i18n(DELEGATION_VERB[type])`. @i18n-keys */
const DELEGATION_VERB = {
  consult: "Consulted",
  transfer: "Transferred",
  background_task: "Delegated",
} as const;

// How a focus shows on the map: its agents alone, or the whole map with the rest dimmed.
const FOCUS_MODE_OPTIONS: SegmentedFilterOption<AgentFlowFocusMode>[] = [
  { key: "isolate", label: "Show only" },
  { key: "dim", label: "Highlight" },
];

// The views of the map: the handover graph or the schedule of its agents,
// path segments (`lib/map-view`, #10203 S2).
const VIEW_OPTIONS: SegmentedFilterOption<MapView>[] = [
  { key: "graph", label: "Graph" },
  { key: "schedule", label: "Schedule" },
];

// The built-in fold of the map (#11882): the same rule as the Agents and
// Fleets lists (`lib/builtin-fold.ts`, #11831), stored per user under "map";
// the API's `is_builtin`, never a tag.
const MAP_BUILTIN_LIST = "map";
const isBuiltinAgent = (agent: { is_builtin: boolean }) => agent.is_builtin;

// Activity feed: cap at the last N events and paginate them in place.
const ACTIVITY_FEED_LIMIT = 50;
const ACTIVITY_PAGE_SIZE = 10;

export default function MapTab() {
  const { data: agents, isLoading } = useAgents();
  const { data: assignments } = useAssignments();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const { data: activeAgentIdList } = useActiveAgentIds();
  const { data: runs } = useAllAssignmentRuns(40);
  const { data: delegations } = useAgentDelegations({ limit: 40 });
  const { data: workItems } = useWorkItems({ limit: 30 });
  // Every open item, not the newest 30 of the activity feed: the "With open
  // work items" filter and the status line count on it.
  const { data: openWorkItems } = useWorkItems({ open_only: true, limit: 500 });
  const { user } = useAuth();
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  // Stable, so a layout switch does not re-render the memoised activity rows
  // and their time tooltips.
  const openEvent = useCallback((href: string) => navigate(href), [navigate]);
  const [searchParams, setSearchParams] = useSearchParams();
  const [focusedIds, setFocusedIds] = useState<Set<string>>(() => {
    const param = searchParams.get("focus");
    return param ? new Set([param]) : new Set();
  });
  const [focusMode, setFocusMode] = useState<AgentFlowFocusMode>("isolate");
  // The view (layout, segment, filters, forces, Fleets layer) is kept in the
  // URL and in the user's preference for the map. Force and Mine by
  // default: components packed in shelves read best at the
  // production shape; the hierarchies are one click away in the toolbar.
  const [graphView, updateGraphView] = usePersistedGraphView("map", MAP_VIEW_DEFAULTS);
  const { layout, filters, forces, fleetsLayer } = graphView;
  const setLayout = useCallback((next: GraphLayout) => updateGraphView({ layout: next }), [updateGraphView]);
  const setFilters = useCallback((next: AgentGraphFilters) => updateGraphView({ filters: next }), [updateGraphView]);
  const setForces = useCallback((next: GraphForces | null) => updateGraphView({ forces: next }), [updateGraphView]);
  const setFleetsLayer = useCallback((next: boolean) => updateGraphView({ fleetsLayer: next }), [updateGraphView]);
  // A layout click moves the drawing at once and renders the page once the
  // move ends; when the graph animates
  // nothing, the layout is set at once.
  const graphRef = useRef<AgentFlowGraphHandle>(null);
  const switchLayout = useCallback(
    (next: GraphLayout) => {
      if (!graphRef.current?.switchLayout(next, () => setLayout(next))) setLayout(next);
    },
    [setLayout],
  );
  const [activityPage, setActivityPage] = useState(0);
  useTick(); // keep relative timestamps in the activity feed fresh

  // The visibility quick filter of the toolbar. A segment the URL names
  // (`?vis=`, a [DEPRECATED] alias read once, or a focus link, opened on All)
  // wins; otherwise the map lands on every visit, Graph and Schedule, by the
  // rule of the lists once the agents are loaded (`builtinLanding`, below). A
  // pick is written to `?vis=` only, never restored from the preference
  // (#12071), so an empty Mine is never the landing.
  const [visibilitySettled, setVisibilitySettled] = useState(() => mapSegmentNamed(searchParams));
  const setVisibility = useCallback(
    (next: Visibility) => {
      setVisibilitySettled(true);
      updateGraphView({ visibility: next });
    },
    [updateGraphView],
  );

  // The view is the path segment (`/flows/map/schedule`, #10203 S2); the query keeps the map's state.
  const { mapView } = useParams<{ mapView?: string }>();
  const view: MapView = readMapView(mapView);
  const setView = useCallback(
    (next: MapView) => {
      const params = new URLSearchParams(searchParams);
      if (next !== "schedule") params.delete("agent");
      const query = params.toString();
      navigate({ pathname: mapViewPath(next), search: query ? `?${query}` : "" });
    },
    [searchParams, navigate],
  );
  const clearScheduleAgent = useCallback(
    () =>
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          params.delete("agent");
          return params;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  // Clear the ?focus param from the URL after it has been consumed by the
  // initializer. A focus link that names no segment opens on All, so the
  // focused agent is drawn whoever owns it.
  useEffect(() => {
    if (searchParams.has("focus")) {
      const next = new URLSearchParams(searchParams);
      next.delete("focus");
      setSearchParams(searchParams.has("vis") ? next : writeVisibility(next, "all"), { replace: true });
    }
  }, [searchParams, setSearchParams]);

  // The Fleets layer: what the graph draws, fleet members collapsed into their
  // fleet's node. Off, or with no fleet member visible, the agents as they are.
  const hasFleetMembers = useMemo(() => (agents ?? []).some((a) => !!a.fleet), [agents]);
  // Only a fleet the caller can open is collapsed; without the licence (403)
  // or for a fleet not shared with the caller, its members stay agent nodes.
  // The list is optional here: if it fails, the map draws every agent.
  const { data: readableFleets, isLoading: fleetsLoading } = useFleets({
    enabled: hasFleetMembers,
    optional: true,
  });
  const readableFleetIds = useMemo(() => new Set((readableFleets ?? []).map((f) => f.id)), [readableFleets]);
  // The switch shows only when it would collapse something.
  const hasCollapsibleFleet = useMemo(
    () => (agents ?? []).some((a) => !!a.fleet && readableFleetIds.has(a.fleet.fleet_id)),
    [agents, readableFleetIds],
  );
  // The General Assistant's stand-in stays its own node, so the graph still
  // draws one node for the answering agent.
  const standInIds = useMemo(() => {
    const standIn = generalAssistantStandIn(gaConfig?.linked_agent_id, agents ?? []);
    return new Set(standIn ? [standIn.id] : []);
  }, [gaConfig, agents]);
  // The Filters dropdown (kind, status, activity), applied to the agents
  // before the Fleets layer collapses them; the General Assistant's stand-in
  // is always kept.
  const dropdownFilteredAgents = useMemo(() => {
    const withOpenWorkItems = new Set(
      (openWorkItems ?? [])
        .filter((w) => OPEN_WORK_ITEM_STATUSES.includes(w.status) && w.owner_agent_id)
        .map((w) => w.owner_agent_id as string),
    );
    return filterGraphAgents(agents ?? [], filters, {
      withOpenWorkItems,
      withRunsInFlight: new Set(activeAgentIdList ?? []),
      keep: standInIds,
    });
  }, [agents, filters, openWorkItems, activeAgentIdList, standInIds]);
  // Segment counts under the dropdown filters, so an empty Mine says so only
  // when ownership alone empties the map.
  const visibilityCounts = useMemo(
    () => countSegments(dropdownFilteredAgents, user?.id),
    [dropdownFilteredAgents, user?.id],
  );
  // The built-in fold (#11882): the organisation's own company agents decide
  // the landing and the default choice; `?builtin=`, then the reader's stored
  // choice for the map, win over it. The General Assistant's stand-in is a
  // built-in there, so a catalogue of built-ins only lands as on the Agents
  // list (Company, built-ins shown); the fold still keeps it drawn, so the
  // counts the fold lowers leave it in (`companySplitKeeping`).
  const { landing: companyAgents, fold: companyFold } = useMemo(
    () => companySplitKeeping(dropdownFilteredAgents, user?.id, isBuiltinAgent, standInIds),
    [dropdownFilteredAgents, user?.id, standInIds],
  );
  const [namedBuiltin, setNamedBuiltin] = useState<BuiltinChoice | null>(() => readBuiltinParam(searchParams));
  const storedBuiltin = useMemo(() => readStoredBuiltin(MAP_BUILTIN_LIST, user?.id), [user?.id]);
  const builtinChoice: BuiltinChoice = namedBuiltin ?? storedBuiltin ?? defaultBuiltinChoice(companyAgents.custom);
  const setBuiltinChoice = useCallback(
    (choice: BuiltinChoice) => {
      setNamedBuiltin(choice);
      storeBuiltin(MAP_BUILTIN_LIST, user?.id, choice);
      setSearchParams((prev) => writeBuiltinParam(prev, choice), { replace: true });
    },
    [user?.id, setSearchParams],
  );
  // Company counts the organisation's own agents while built-ins are hidden.
  const shownCounts = useMemo(
    () =>
      builtinChoice === "hide"
        ? { ...visibilityCounts, company: visibilityCounts.company - companyFold.builtin }
        : visibilityCounts,
    [visibilityCounts, builtinChoice, companyFold.builtin],
  );
  // The landing segment, the rule of the Agents and Fleets lists
  // (`builtinLanding`); the page draws its skeleton until the agents load, so
  // an empty Mine never flashes first.
  const landing: Visibility | null =
    !visibilitySettled && agents !== undefined && user?.id !== undefined
      ? builtinLanding(visibilityCounts, companyAgents)
      : null;
  const visibility: Visibility = landing ?? graphView.visibility ?? "mine";
  useEffect(() => {
    if (landing === null) return;
    setVisibilitySettled(true);
    // Written to `?vis=`; the next visit with no `?vis=` lands again from its own counts.
    updateGraphView({ visibility: landing });
  }, [landing, updateGraphView]);
  // Then the visibility segment: the agents of Mine, Company, Shared or All,
  // and the handovers between them; the graph draws no edge to an agent out
  // of the segment.
  // Then the fold: on Company with built-ins hidden, the built-in agents are
  // left out (the General Assistant's stand-in stays drawn), so the Fleets
  // layer and the Focus picker work on the organisation's own agents.
  const segmentAgents = useMemo(
    () => agentsInVisibility(dropdownFilteredAgents, visibility, user?.id, standInIds),
    [dropdownFilteredAgents, visibility, user?.id, standInIds],
  );
  const fold = useMemo(
    () =>
      foldBuiltins(segmentAgents, visibility, builtinChoice, (a) => isBuiltinAgent(a) && !standInIds.has(a.id), false),
    [segmentAgents, visibility, builtinChoice, standInIds],
  );
  const filteredAgents = fold.shown;
  const builtinFold: BuiltinFoldState = {
    choice: builtinChoice,
    setChoice: setBuiltinChoice,
    hidden: fold.hidden,
    builtinInCompany: companyFold.builtin,
    revealedBySearch: false,
  };
  const companyTip = useBuiltinCompanyTip(builtinFold);
  // The toggle sits in the Filters panel: nothing is drawn under the map (#11905).
  const builtinSwitch = { shown: builtinChoice === "show", onChange: (shown: boolean) => setBuiltinChoice(shown ? "show" : "hide") };
  // The stand-in drawn although it is not in the segment: the map's answering
  // node, not one of the segment's agents, so the counter leaves it out and
  // equals the segment's count.
  const keptOutOfSegment = useMemo(
    () => filteredAgents.filter((a) => standInIds.has(a.id) && !inSegment(a, visibility, user?.id)).length,
    [filteredAgents, standInIds, visibility, user?.id],
  );
  // The focus picker offers only the agents the filters keep.
  const sortedAgents = useMemo(
    () => [...filteredAgents].sort((a, b) => a.name.localeCompare(b.name)),
    [filteredAgents],
  );
  // With a filter or a segment other than All on, the status line counts the
  // activity of the agents kept only; the stand-in drawn out of its segment
  // is not one of them.
  const keptAgentIds = useMemo(
    () =>
      activeFilterCount(filters) > 0 || visibility !== "all"
        ? new Set(
            filteredAgents
              .filter((a) => !(standInIds.has(a.id) && !inSegment(a, visibility, user?.id)))
              .map((a) => a.id),
          )
        : null,
    [filters, visibility, filteredAgents, standInIds, user?.id],
  );
  const layer = useMemo(
    () =>
      fleetsLayer && hasCollapsibleFleet
        ? collapseFleets(filteredAgents, { canOpenFleet: (id) => readableFleetIds.has(id), keepIds: standInIds })
        : null,
    [filteredAgents, fleetsLayer, hasCollapsibleFleet, readableFleetIds, standInIds],
  );
  const layerAgents = useMemo(() => layer?.agents ?? filteredAgents, [layer, filteredAgents]);
  // The A2A partners of the drawn agents, outside the platform (#12337).
  const { data: liaisons } = useA2ALiaisons();
  const a2a = useMemo(
    () => withA2ALiaisons(layerAgents, liaisons, (id) => layer?.nodeOf(id) ?? id),
    [layerAgents, liaisons, layer],
  );
  const drawnAgents = a2a.agents;
  const partnerNodes = useMemo(
    () => new Map([...a2a.partners].map(([id, p]) => [id, t_i18n(a2aPartnerSubLabel(p.direction))])),
    [a2a.partners, t_i18n],
  );
  // A partner's tooltip names its integration and its full card URLs.
  const partnerDetails = useMemo(
    () => new Map([...a2a.partners].map(([id, p]) => [id, a2aPartnerDetail(p)])),
    [a2a.partners],
  );
  // What the Forces panel shows: the chosen forces, or the automatic ones.
  const shownForces = useMemo<GraphForces>(() => (forces ? forces : automaticForces(drawnAgents.length)), [forces, drawnAgents.length]);
  const fleetNodeIds = useMemo(() => new Set(layer?.fleetNodes.keys() ?? []), [layer]);
  const nodeOf = useCallback((id: string) => layer?.nodeOf(id) ?? id, [layer]);
  // The General Assistant's handovers to a member end on the member's fleet node.
  const drawnGa = useMemo(() => {
    if (!layer || !gaConfig) return gaConfig;
    const rewired = remapHandovers(gaConfig.handover_agent_ids, gaConfig.handover_types, null, layer.nodeOf, null);
    return { ...gaConfig, handover_agent_ids: rewired.ids, handover_types: rewired.types };
  }, [layer, gaConfig]);

  // A node opens its agent, or its fleet page for a collapsed fleet node.
  const openNode = useCallback(
    (id: string) => {
      // A partner opens its integration: its name, direction and card URLs.
      const partner = a2a.partners.get(id);
      if (partner) {
        navigate(a2aPartnerPath(partner), { state: { backTo: "/flows", backLabel: "Back to Agentic Flow" } });
        return;
      }
      const fleetId = fleetIdOfNode(id);
      navigate(fleetId ? `/fleets/${fleetId}` : `/agents/${id}`, {
        state: { backTo: "/flows", backLabel: "Back to Agentic Flow" },
      });
    },
    [navigate, a2a.partners],
  );

  // Agent name lookup for badges + activity feed
  const agentNameMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of agents ?? []) map.set(a.id, a.name);
    return map;
  }, [agents]);

  // Agent icon lookup so the activity feed can render an agent chip per event.
  const agentIconMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of agents ?? []) map.set(a.id, a.icon);
    return map;
  }, [agents]);

  // Per-agent assignment/trigger summary for node chips + detail drawer.
  const agentSummaries = useMemo(
    () => buildAgentSummaries(assignments ?? []),
    [assignments],
  );

  const toggleFocus = useCallback((id: string) => {
    setFocusedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);
  const clearFocus = useCallback(() => setFocusedIds(new Set()), []);
  const nameOfAgent = useCallback((id: string) => agentNameMap.get(id), [agentNameMap]);

  // The Schedule view shows the agents the toolbar keeps: the segment, the
  // filters and the focus, as the graph draws them.
  const scheduleAgentIds = useMemo(
    () => scheduleScope({ keptAgentIds, focusedIds }),
    [keptAgentIds, focusedIds],
  );

  // A double click on a fleet node focuses or releases its members together.
  const toggleDrawnFocus = useCallback(
    (id: string) => {
      const memberIds = layer?.fleetNodes.get(id)?.memberIds;
      if (!memberIds) {
        toggleFocus(id);
        return;
      }
      setFocusedIds((prev) => {
        const next = new Set(prev);
        const release = memberIds.some((memberId) => next.has(memberId));
        for (const memberId of memberIds) {
          if (release) next.delete(memberId);
          else next.add(memberId);
        }
        return next;
      });
    },
    [layer, toggleFocus],
  );

  const activeAgentIds = useMemo(
    () => new Set(activeAgentIdList ?? []),
    [activeAgentIdList],
  );
  // A fleet node runs when one of its members does.
  const drawnActiveIds = useMemo(() => drawnIds(activeAgentIds, nodeOf), [activeAgentIds, nodeOf]);
  // The focus as drawn ids, kept stable across renders.
  const drawnFocusIds = useMemo(
    () => (focusedIds.size > 0 ? drawnIds(focusedIds, nodeOf) : null),
    [focusedIds, nodeOf],
  );

  // Edges with a handover running, pending or ended in the last 15 minutes
  // (source -> target), highlighted live; never "one of the last 40 rows".
  const now = useNow();
  const liveEdges = useMemo(
    () => liveHandoverEdges(delegations, now, nodeOf),
    [delegations, now, nodeOf],
  );

  // Counters of the toolbar docked under the graph.
  const counts = useMemo(() => {
    const kept = (id: string | null | undefined) => !keptAgentIds || (!!id && keptAgentIds.has(id));
    const runningRuns = (runs ?? []).filter((r) =>
      RUNNING_RUN_STATUSES.includes(r.status) && kept(r.agent_id),
    ).length;
    const handoffs = (delegations ?? []).filter(
      // A handover in flight counts when both its ends are kept, as the graph
      // draws an edge only between two agents on the map.
      (d) => (d.status === "running" || d.status === "pending") && kept(d.parent_agent_id) && kept(d.target_agent_id),
    ).length;
    const openItems = (openWorkItems ?? []).filter((w) =>
      OPEN_WORK_ITEM_STATUSES.includes(w.status) && kept(w.owner_agent_id),
    ).length;
    // Handover edges drawn on the graph, counted with the graph's own rule:
    // a known agent other than the source, plus the General Assistant's
    // edges when it is drawn as its own node (no stand-in agent).
    // Counted on what is drawn: with the Fleets layer on, a fleet is one node.
    const agentIds = new Set(drawnAgents.map((a) => a.id));
    const gaDrawn = !!gaConfig && !generalAssistantStandIn(gaConfig.linked_agent_id, drawnAgents);
    const gaTargets = new Set((gaConfig?.handover_agent_ids ?? []).map(nodeOf));
    const handovers = drawnAgents.reduce(
      (total, a) => total + (a.handover_agent_ids ?? []).filter((id) => agentIds.has(id) && id !== a.id && !a2a.edgeTypes.has(`${a.id}->${id}`)).length,
      0,
    ) + (gaDrawn ? [...gaTargets].filter((id) => agentIds.has(id)).length : 0);
    return {
      agents: drawnAgents.length - fleetNodeIds.size - a2a.partners.size - keptOutOfSegment,
      fleets: fleetNodeIds.size,
      handovers,
      runningRuns,
      handoffs,
      openItems,
    };
  }, [drawnAgents, fleetNodeIds, keptOutOfSegment, nodeOf, runs, delegations, openWorkItems, keptAgentIds, gaConfig, a2a]);

  // The legend behind the toolbar's Legend button.
  const legend = useMemo(
    () => (
      <AgentFlowLegend
        variant="list"
        showLive
        showGeneralAssistant={!generalAssistantStandIn(gaConfig?.linked_agent_id, agents ?? [])}
        showAllTypes={(agents ?? []).some((a) => runsEveryHandoverType(a.handover_agent_ids ?? [], a.handover_types))}
        showA2A={a2a.partners.size > 0}
      />
    ),
    [agents, gaConfig, a2a.partners.size],
  );

  // Merged real-time activity feed (runs + delegations + work items).
  const events = useMemo<FlowEvent[]>(() => {
    const out: FlowEvent[] = [];

    for (const r of runs ?? []) {
      out.push({
        id: `run-${r.id}`,
        kind: "run",
        status: r.status,
        outcome: r.outcome,
        title:
          r.assignment_name ??
          t_i18n(r.parent_agent_name ? "Delegated run" : "Agent activity"),
        context: r.agent_name,
        iconKey: r.agent_icon,
        // Marker-free preview — raw ``[[FILE:uuid]]`` ids must not leak
        // into the one-line activity feed row.
        summary: r.result_summary ? summaryPreview(r.result_summary, 200) || undefined : undefined,
        timestamp: r.completed_at ?? r.started_at ?? r.created_at,
        href: `/operations/executions/${r.id}`,
      });
    }

    for (const d of delegations ?? []) {
      const runId =
        d.metadata && typeof d.metadata.assignment_run_id === "string"
          ? d.metadata.assignment_run_id
          : undefined;
      out.push({
        id: `deleg-${d.id}`,
        kind: "delegation",
        status: d.status,
        outcome: d.outcome,
        title:
          d.description ||
          t_i18n((DELEGATION_VERB as Record<string, string>)[d.type] ?? "Handover"),
        sourceLabel: agentNameMap.get(d.parent_agent_id) ?? t_i18n("Agent"),
        targetLabel: agentNameMap.get(d.target_agent_id) ?? t_i18n("Agent"),
        iconKey:
          agentIconMap.get(d.parent_agent_id) ??
          agentIconMap.get(d.target_agent_id),
        timestamp: d.completed_at ?? d.started_at ?? d.created_at,
        href: runId ? `/operations/executions/${runId}` : undefined,
      });
    }

    for (const w of workItems ?? []) {
      const creator = w.created_by_agent_id
        ? agentNameMap.get(w.created_by_agent_id)
        : undefined;
      out.push({
        id: `wi-${w.id}`,
        kind: "work_item",
        status: w.status,
        title: w.title,
        context: creator ? t_i18n("Work item by {name}", { name: creator }) : t_i18n("Work item"),
        iconKey: w.created_by_agent_id
          ? agentIconMap.get(w.created_by_agent_id)
          : undefined,
        timestamp: w.last_activity_at ?? w.updated_at ?? w.created_at,
        href: workItemsPath(),
      });
    }

    return out
      .sort((a, b) => {
        const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
        const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
        return tb - ta;
      })
      .slice(0, ACTIVITY_FEED_LIMIT);
  }, [runs, delegations, workItems, agentNameMap, agentIconMap, t_i18n]);

  // Paginate the feed in place so it never grows into a long scrolling list.
  const pagedEvents = useMemo(
    () =>
      events.slice(
        activityPage * ACTIVITY_PAGE_SIZE,
        (activityPage + 1) * ACTIVITY_PAGE_SIZE,
      ),
    [events, activityPage],
  );

  // Clamp the page when the feed shrinks (e.g. a live refresh drops items).
  useEffect(() => {
    const maxPage = Math.max(
      0,
      Math.ceil(events.length / ACTIVITY_PAGE_SIZE) - 1,
    );
    if (activityPage > maxPage) setActivityPage(maxPage);
  }, [events.length, activityPage]);

  // Handover edits on the graph: draw, retype, delete an edge. One
  // implementation with the fleet page graph.
  const handoverEditing = useHandoverEdgeEditing(agents);

  // The agent definition drawer: one with the fleet page graph.
  const details = useAgentDetailsDrawer({
    agents,
    activeAgentIds,
    onOpenAgent: (id) => navigate(`/agents/${id}`, { state: { backTo: "/flows", backLabel: "Back to Agentic Flow" } }),
    onOpenRun: (id) => navigate(`/operations/executions/${id}`, { state: { backTo: "/flows", backLabel: "Back to Agentic Flow" } }),
  });

  return (
    <div className="space-y-6">
      {/* As wide as its two options, not a grey band across the page. */}
      <SegmentedFilter
        options={VIEW_OPTIONS}
        value={view}
        onChange={setView}
        ariaLabel={t_i18n("Map view")}
        className="w-fit"
      />
      {view === "schedule" ? (
        <>
          {/* The Graph view's toolbar controls that apply to a schedule, on one
              state with it: the segment, the filters and the focus.
              No overflow-hidden here: the card ends under the toolbar row, so it
              would clip the Filters panel and the Focus list (UX B1/B2); the
              stacking context keeps both above the KPI tiles below. */}
          <Card className="relative z-10">
            <CardContent className="p-0">
              <AgentGraphToolbar ariaLabel={t_i18n("Schedule toolbar")}>
                <VisibilityFilter value={visibility} onChange={setVisibility} counts={shownCounts} tips={companyTip} />
                <AgentGraphFilterControls filters={filters} onChange={setFilters} builtin={builtinSwitch} />
                <AgentFocusPicker
                  agents={sortedAgents}
                  focusedIds={focusedIds}
                  onToggle={toggleFocus}
                  onClear={clearFocus}
                  nameOf={nameOfAgent}
                />
              </AgentGraphToolbar>
              <VisibilityEmptyHint
                value={visibility}
                counts={shownCounts}
                onShowAll={() => setVisibility("all")}
                className="py-2"
              />
            </CardContent>
          </Card>
          {isLoading ? (
            <PageSkeleton cards={2} />
          ) : (
            <ScheduleView
              agentIds={scheduleAgentIds}
              agentId={searchParams.get("agent")}
              onClearAgent={clearScheduleAgent}
            />
          )}
        </>
      ) : isLoading || fleetsLoading ? (
        // With fleet members, wait for the fleet list (or its failure) so the
        // graph is laid out once, not again when fleets collapse.
        <PageSkeleton cards={2} />
      ) : (
        <>
          <Card className="overflow-hidden">
            <CardContent className="p-0">
              <AgentGraphToolbar layout={layout} onLayoutChange={switchLayout} legend={legend}>
              <VisibilityFilter value={visibility} onChange={setVisibility} counts={shownCounts} tips={companyTip} />
              <AgentGraphForces forces={shownForces} onChange={setForces} />
              <AgentGraphFilterControls filters={filters} onChange={setFilters} builtin={builtinSwitch} />
              {/* Fleets layer: a fleet's members as one node, or every agent */}
              {hasCollapsibleFleet && (
                <label className="flex items-center gap-2 content-compact text-default-secondary">
                  <Switch checked={fleetsLayer} onCheckedChange={setFleetsLayer} />
                  {t_i18n("Fleets")}
                </label>
              )}
              {/* Focus filter - multi-select */}
              <AgentFocusPicker
                agents={sortedAgents}
                focusedIds={focusedIds}
                onToggle={toggleFocus}
                onClear={clearFocus}
                nameOf={nameOfAgent}
              >
                <SegmentedFilter
                  options={FOCUS_MODE_OPTIONS}
                  value={focusMode}
                  onChange={setFocusMode}
                  ariaLabel={t_i18n("Focus display")}
                />
              </AgentFocusPicker>
              </AgentGraphToolbar>
              <VisibilityEmptyHint
                value={visibility}
                counts={shownCounts}
                onShowAll={() => setVisibility("all")}
                className="border-b border-elevation-subtle-soft py-2"
              />
              <AgentFlowGraph
                ref={graphRef}
                agents={drawnAgents}
                generalAssistant={drawnGa}
                activeAgentIds={drawnActiveIds}
                editableAgentIds={handoverEditing.editableAgentIds}
                agentSummaries={agentSummaries}
                liveEdges={liveEdges}
                onAddEdge={handoverEditing.onAddEdge}
                onEditEdge={handoverEditing.onEditEdge}
                onNavigateToAgent={openNode}
                onToggleFocus={toggleDrawnFocus}
                onShowDetails={(id) => (fleetIdOfNode(id) || a2a.partners.has(id) ? openNode(id) : details.show(id))}
                selectedNodeId={details.selectedId}
                focusedAgentIds={drawnFocusIds}
                focusMode={focusMode}
                layout={layout}
                layoutForces={shownForces}
                hideLegend
                viewOnlyNodeIds={fleetNodeIds}
                ghostNodes={partnerNodes}
                ghostDetails={partnerDetails}
                edgeTypeOverrides={a2a.edgeTypes}
              />
              <AgentGraphStatusLine
                counters={[
                  { key: "agents", label: t_i18n("agents"), value: counts.agents },
                  ...(counts.fleets > 0 ? [{ key: "fleets", label: t_i18n("fleets"), value: counts.fleets }] : []),
                  { key: "edges", label: t_i18n("handovers"), value: counts.handovers },
                  { key: "runs", label: t_i18n("{count, plural, one {run in flight} other {runs in flight}}", { count: counts.runningRuns }), value: counts.runningRuns, live: true },
                  { key: "handovers", label: t_i18n("handovers in flight"), value: counts.handoffs, live: true },
                  { key: "items", label: t_i18n("open work items"), value: counts.openItems },
                ]}
              />
            </CardContent>
          </Card>

          {/* Activity stream */}
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle className="text-base">{t_i18n("Activity stream")}</CardTitle>
              {/* The total once, at the top; the pager under the events never repeats it (#11191). */}
              {events.length > 0 && <TotalChip total={events.length} className="ml-auto mr-2" />}
              <Button
                variant="ghost"
                size="sm"
                className="h-7 content-compact"
                onClick={() => navigate("/operations/executions")}
              >
                {t_i18n("Open Operations")}
              </Button>
            </CardHeader>
            <CardContent>
              {events.length === 0 ? (
                <EmptyState
                  variant="inset"
                  icon={Radio}
                  title="Nothing happening right now"
                  description="Runs, inter-agent handovers, and work items will appear here in real time as your agents act."
                />
              ) : (
                <>
                  <div className="space-y-1.5">
                    {pagedEvents.map((event) => (
                      <FlowEventRow
                        key={event.id}
                        event={event}
                        onOpen={openEvent}
                      />
                    ))}
                  </div>
                  <Pagination
                    page={activityPage}
                    pageSize={ACTIVITY_PAGE_SIZE}
                    total={events.length}
                    onChange={setActivityPage}
                  />
                </>
              )}
            </CardContent>
          </Card>
        </>
      )}

      {details.element}
      {handoverEditing.confirmElement}
    </div>
  );
}
