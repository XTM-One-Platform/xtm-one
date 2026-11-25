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
 * FleetPage - one fleet, on `/fleets/:fleetId`.
 *
 * The page is the fleet's graph, drawn with the agent-to-agent map's graph and
 * its docked toolbar (the four layouts of phase 3a), restricted to the fleet's
 * home members and connected agents; in its Data flow view, what starts each
 * member and where its work goes (`FleetDataFlow`, #9829; a tab of its own
 * since #9980, never a switch inside the Graph tab); or, in its schedule view,
 * the schedule of those agents. Beside the graph and the data flow, the members
 * panel lists each member's health (`GET /fleets/{id}/health`), what needs a
 * look first.
 *
 * Edition: the header's Settings button opens the fleet's
 * settings side panel (`FleetSettingsPanel`), and the members panel
 * (`FleetMembersPanel`) adds, removes, moves and re-parents members and
 * connects agents and fleets. The owner and `config` grantees edit; anyone
 * else reads both with the reason. Handovers between home members are edited
 * on the graph itself: draw an edge from a member the
 * reader can manage, click an edge to retype or delete it, the same editing as
 * the agent-to-agent map (`useHandoverEdgeEditing`); ghosts stay view only and
 * a refused write (a member another fleet does not expose) says why. A right
 * click on a node opens its menu: open the agent, focus on its
 * handovers (a double click toggles the same focus), and for a home member of a
 * fleet the reader manages, add a handover from it, change its parent, remove
 * it from the fleet (`FleetNodeMenu`, `lib/fleet-node-menu`). The node's "i"
 * and the menu's "View details" open the agent definition drawer of the
 * Agentic Flow map (`useAgentDetailsDrawer`). Dragging a
 * home member onto another makes it report to that member, the same
 * re-parent as the members panel's (`useReparentFleetMember`). No info
 * block on the page: the header names the fleet and its workspace, the graph and the
 * roster say the rest.
 *
 * Duplicate (#9174): the header's Duplicate button forks the fleet, a
 * built-in one included, into an editable personal copy (`ForkFleetDialog`);
 * a forked fleet names its source under the title (`forkProvenance`).
 *
 * About (#9174): the fleet's documentation, built-in or custom, always
 * reachable after setup (`FleetAboutView`); always AI generated, its manager regenerates it.
 *
 * Managers also read the fleet's cost under the members panel
 * (`FleetCostCard`): total and by agent over 7, 30 or 90 days.
 *
 * ROI (#9785): a fifth view for the fleet's managers (`can_manage`, which
 * covers administrators; design dev-docs/roi-outcomes.md 6.3 "Per fleet"),
 * `FleetRoiView`. It and the cost card read one window, the `?window=` URL
 * parameter, kept when the view changes.
 *
 * The header is the workspace page's (`EntityPageHeader`), its
 * workspace a chip back to it (`FleetWorkspaceChip`); the strip's open work
 * items are the fleet's, the population its members panel counts from.
 */

import { useCallback, useMemo, useState } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import RouteNotFound from "@/pages/RouteNotFound";
import { Boxes, CopyPlus, Crown, GitFork, ListChecks, Lock, RotateCcw, Settings2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { BackButton } from "@/components/ui/back-button";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { FleetHeaderActions, FleetHeaderStatus } from "@/components/fleets/FleetHeaderControl";
import { FleetAutonomyStrip } from "@/components/fleets/FleetAutonomyStrip";
import { FleetResetDialog } from "@/components/fleets/FleetResetDialog";
import { FleetSetupDialog } from "@/components/fleets/FleetSetupDialog";
import { useMediaQuery } from "@/hooks/useCompactToolbar";
import { useRailHold } from "@/hooks/useNavPrefs";
import { canControlFleet } from "@/components/fleets/fleet-control";
import { useAuth } from "@/hooks/useAuth";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { FleetWorkspaceChip } from "@/components/workspaces/FleetWorkspaceChip";
import { FleetMembersPanel } from "@/components/fleets/FleetMembersPanel";
import { FleetMembersView } from "@/components/fleets/FleetMembersView";
import { FleetSettingsPanel } from "@/components/fleets/FleetSettingsPanel";
import { FleetCostCard } from "@/components/fleets/FleetCostCard";
import { FleetDataFlow, FleetDataFlowLegend } from "@/components/fleets/FleetDataFlow";
import { FleetRoiView } from "@/components/fleets/FleetRoiView";
import { RoiRecordsList } from "@/components/roi/RoiRecordsList";
import { ROI_RECORDS_SEGMENT } from "@/lib/roi-records";
import { ForkFleetDialog } from "@/components/fleets/ForkFleetDialog";
import { DuplicateButton } from "@/components/ui/duplicate-button";
import { forkProvenance } from "@/components/fleets/fork-fleet-form";
import { FleetSourceChangesNotice } from "@/components/fleets/FleetSourceChangesNotice";
import { EmptyState } from "@/components/ui/empty-state";
import { PageSkeleton, Skeleton } from "@/components/ui/skeleton";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { ScheduleView } from "@/components/ui/schedule-view";
import { AgentFlowGraph, AgentFlowLegend } from "@/components/ui/agent-flow";
import { useA2ALiaisons } from "@/hooks/api/useA2AApi";
import { a2aPartnerDetail, a2aPartnerPath, a2aPartnerSubLabel, withA2ALiaisons } from "@/lib/agent-graph-a2a";
import { AgentGraphToolbar } from "@/components/ui/agent-graph-toolbar";
import type { GraphLayout } from "@/lib/agent-graph-layout";
import { FLEET_VIEW_DEFAULTS } from "@/lib/agent-graph-view-state";
import { usePersistedGraphView } from "@/hooks/usePersistedGraphView";
import { useAgentDelegations, useAgents } from "@/hooks/api/useAgentApi";
import { useNow } from "@/hooks/useTick";
import { liveHandoverEdges } from "@/lib/live-handovers";
import { useActiveAgentIds } from "@/hooks/api/useAssignmentApi";
import {
  isFleetsUnavailable,
  useFleet,
  useFleetFlow,
  useFleetGraph,
  useFleetHealth,
  useFleetMembers,
  useRefreshFleetReads,
} from "@/hooks/api/useFleetApi";
import { useHandoverEdgeEditing } from "@/hooks/useHandoverEdgeEditing";
import { useAgentDetailsDrawer } from "@/hooks/useAgentDetailsDrawer";
import { FleetNodeMenu, type FleetNodeMenuTarget } from "@/components/fleets/FleetNodeMenu";
import { fleetEditRights, graphHandoversEditable, graphReparent } from "@/components/fleets/fleet-edition";
import { useReparentFleetMember } from "@/components/fleets/useReparentFleetMember";
import {
  crossingEdgeWarnings,
  fleetEdgeIds,
  fleetGhostNodes,
  fleetScopedAgents,
  internalHandoverCount,
} from "@/lib/fleet-graph";
import { edgelessIds, fleetMiniGraph } from "@/lib/fleet-mini-graph";
import { isNotFoundError } from "@/lib/api";
import {
  FLEET_DEFAULT_VIEW,
  FLEET_TAB_LABELS,
  FLEET_TABS,
  fleetViewPath,
  isKnownFleetTab,
  readFleetTab,
  type FleetView,
} from "@/lib/fleet-operations";
import { FLEET_WINDOW_PARAM } from "@/lib/fleet-roi-window";
import { FleetOperations } from "@/components/fleets/FleetOperations";
import { FleetAboutView } from "@/components/fleets/FleetAboutView";
import { FleetModifyWithAiButton, FleetModifyWithAiReason, useFleetModifyWithAi } from "@/components/fleets/FleetModifyWithAi";
import { FleetStudioRail } from "@/components/fleets/FleetStudioRail";
import { useFormatter } from "@/i18n";

// The Operations view: the fleet's runs, delegations, work items,
// shared state and worksets, the Operations page's lists locked on the fleet.
// Members: one table of every member with the settings it inherits or overrides.
// ROI: the fleet's ROI view, offered to its managers only (#9785).
// Data flow: what starts each member and where its work goes (#9980).
// Approval: human approval, its cursor and what waits (#10176). Sam's order
// and cap (8 Nov 2026): About / Graph / Data flow / Members / Approval /
// Schedule / Operations / ROI, eight tabs at most, none added after.
const MANAGER_VIEW_OPTIONS: SegmentedFilterOption<FleetView>[] = FLEET_TABS.map((key) => ({ key, label: FLEET_TAB_LABELS[key] }));
const VIEW_OPTIONS = MANAGER_VIEW_OPTIONS.filter((option) => option.key !== "roi");

/** `?ai=1`: the Modify with AI panel is open; `?c=`: its conversation (#10162 R5). */
const FLEET_AI_PARAM = "ai";
const FLEET_AI_CONVERSATION_PARAM = "c";

export default function FleetPage() {
  const { fleetId, tab: tabSegment, list: listSegment } = useParams<{ fleetId: string; tab?: string; list?: string }>();
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const location = useLocation();
  // Opened from its workspace's chip, the back link returns to the workspace.
  const back = location.state as { backTo?: string; backLabel?: string } | null;
  const [searchParams, setSearchParams] = useSearchParams();
  // The Graph tab's layout, kept in the URL (`?layout=`) and in the user's
  // preference for this fleet; Force by default, as on the map.
  const [graphView, updateGraphView] = usePersistedGraphView(`fleet:${fleetId ?? ""}`, FLEET_VIEW_DEFAULTS);
  const layout = graphView.layout;
  const setLayout = useCallback((next: GraphLayout) => updateGraphView({ layout: next }), [updateGraphView]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // The section the settings open on: the ROI view's "Set the fleet default" lands on Outcomes (UX #9790 B1).
  const [settingsSection, setSettingsSection] = useState<"outcomes" | undefined>(undefined);
  const [forkOpen, setForkOpen] = useState(false);
  // "Run setup again" (#1151): the one fleet setup, reachable from the menu once the fleet runs.
  const [setupOpen, setSetupOpen] = useState(false);
  // "Reset to factory state" (#1508): a built-in fleet, administrators only.
  const [resetOpen, setResetOpen] = useState(false);
  const { isAdmin } = useAuth();
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason();
  // Modify with AI (#10162): the docked panel's open state and conversation live in the URL (`?ai=1&c=`).
  const modifyWithAi = useFleetModifyWithAi();
  const aiPanelOpen = searchParams.get(FLEET_AI_PARAM) === "1";
  const aiConversationId = searchParams.get(FLEET_AI_CONVERSATION_PARAM);
  const setAiPanel = useCallback(
    (open: boolean, conversationId?: string | null) =>
      setSearchParams(
        (previous) => {
          const query = new URLSearchParams(previous);
          if (open) query.set(FLEET_AI_PARAM, "1");
          else query.delete(FLEET_AI_PARAM);
          const conv = conversationId === undefined ? previous.get(FLEET_AI_CONVERSATION_PARAM) : conversationId;
          if (open && conv) query.set(FLEET_AI_CONVERSATION_PARAM, conv);
          else query.delete(FLEET_AI_CONVERSATION_PARAM);
          return query;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  const { data: fleet, isLoading: fleetLoading, error: fleetError } = useFleet(fleetId);
  const { data: graph, isLoading: graphLoading } = useFleetGraph(fleet ? fleetId : null);
  const { data: health } = useFleetHealth(fleet ? fleetId : null);
  const { data: fleetMembers, isLoading: membersLoading } = useFleetMembers(fleet ? fleetId : null);
  // Every agent an admin may see, so a member outside the admin's own library still draws.
  const { data: agents, isLoading: agentsLoading } = useAgents({ scope: "all" });
  const { data: activeAgentIdList } = useActiveAgentIds();
  // The newest handovers: the graph keeps live (bright and fast) only an edge
  // with one running, pending or ended in the last 15 minutes, as on the map.
  const { data: delegations } = useAgentDelegations({ limit: 100 });
  const now = useNow();
  const liveEdges = useMemo(() => liveHandoverEdges(delegations, now), [delegations, now]);

  // The tab is the path segment (#10203 S2); a former `?view=` / `?ops=` / `?mode=` link is not read (#797).
  const requestedView = readFleetTab(tabSegment);
  // Read only while the Data flow view shows it: no other view asks for it.
  const flowQuery = useFleetFlow(fleet ? fleetId : null, requestedView === "flow");
  // Another view is a new history entry (back and forward move between tabs)
  // and starts clean: the Operations lists' filters stay behind; the shared
  // window (`?window=`) stays, so the ROI view and the cost card agree.
  const setView = useCallback(
    (next: FleetView) => {
      if (!fleetId) return;
      const query = new URLSearchParams();
      const window = searchParams.get(FLEET_WINDOW_PARAM);
      if (window) query.set(FLEET_WINDOW_PARAM, window);
      // The Modify with AI panel stays open on its conversation across the tabs.
      for (const key of [FLEET_AI_PARAM, FLEET_AI_CONVERSATION_PARAM]) {
        const value = searchParams.get(key);
        if (value) query.set(key, value);
      }
      const search = query.toString();
      navigate({ pathname: fleetViewPath(fleetId, next), search: search ? `?${search}` : "" }, { state: location.state });
    },
    [fleetId, searchParams, navigate, location.state],
  );

  const fleetAgents = useMemo(() => fleetScopedAgents(agents, graph), [agents, graph]);
  const fleetAgentIdSet = useMemo(() => new Set(fleetAgents.map((a) => a.id)), [fleetAgents]);
  // Connected and external agents: drawn as ghosts, labelled with their home fleet.
  const fleetGhosts = useMemo(() => fleetGhostNodes(graph), [graph]);
  // The A2A partners of the members and connected agents, drawn outside the
  // fleet as ghosts; nothing for a fleet with no A2A binding (#12337).
  const { data: liaisons } = useA2ALiaisons();
  const a2a = useMemo(() => withA2ALiaisons(fleetAgents, liaisons), [fleetAgents, liaisons]);
  const ghostNodes = useMemo(() => {
    if (a2a.partners.size === 0) return fleetGhosts;
    const out = new Map(fleetGhosts);
    for (const [id, p] of a2a.partners) out.set(id, t_i18n(a2aPartnerSubLabel(p.direction)));
    return out;
  }, [fleetGhosts, a2a.partners, t_i18n]);
  // A partner's tooltip names its integration and its full card URLs.
  const partnerDetails = useMemo(
    () => new Map([...a2a.partners].map(([id, p]) => [id, a2aPartnerDetail(p)])),
    [a2a.partners],
  );
  // Only the fleet graph's edges are drawn; a crossing one that is not exposed
  // or not governed is dotted, its tooltip saying which.
  const edgeIds = useMemo(() => fleetEdgeIds(graph), [graph]);
  const crossingWarnings = useMemo(() => crossingEdgeWarnings(graph), [graph]);
  const dashedEdgeIds = useMemo(() => new Set(crossingWarnings.keys()), [crossingWarnings]);
  const dashedEdgeTooltip = useCallback(
    (edgeId: string) =>
      crossingWarnings.get(edgeId) === "not_exposed"
        ? t_i18n("Cross-fleet handover to an agent its fleet does not expose")
        : t_i18n("Cross-fleet handover this fleet does not govern"),
    [crossingWarnings, t_i18n],
  );
  const activeAgentIds = useMemo(
    () => new Set((activeAgentIdList ?? []).filter((id) => fleetAgentIdSet.has(id))),
    [activeAgentIdList, fleetAgentIdSet],
  );
  const orchestratorName = useMemo(
    () => (fleet?.orchestrator_agent_id ? (agents ?? []).find((a) => a.id === fleet.orchestrator_agent_id)?.name ?? null : null),
    [agents, fleet],
  );

  const openAgent = useCallback(
    (id: string) => navigate(`/agents/${id}`, { state: { backTo: `/fleets/${fleetId}`, backLabel: "Back to the fleet" } }),
    [navigate, fleetId],
  );
  // A partner node opens its integration (name, direction, card URLs), an agent its page.
  const openNode = useCallback(
    (id: string) => {
      const partner = a2a.partners.get(id);
      if (partner) navigate(a2aPartnerPath(partner), { state: { backTo: `/fleets/${fleetId}`, backLabel: "Back to the fleet" } });
      else openAgent(id);
    },
    [a2a.partners, navigate, fleetId, openAgent],
  );
  // The agent definition drawer, the Agentic Flow map's: the node's
  // "i", Enter on a focused node and the node menu's "View details" open it.
  const details = useAgentDetailsDrawer({
    agents,
    activeAgentIds,
    onOpenAgent: openAgent,
    onOpenRun: (id) => navigate(`/operations/executions/${id}`, { state: { backTo: `/fleets/${fleetId}`, backLabel: "Back to the fleet" } }),
  });

  // Handovers are edited from the home members only: a ghost belongs to
  // another fleet (or none) and is never an edge source here.
  const homeAgents = useMemo(() => fleetAgents.filter((a) => !fleetGhosts.has(a.id)), [fleetAgents, fleetGhosts]);
  const refreshFleetReads = useRefreshFleetReads(fleet ? fleetId : null);
  const handoverEditing = useHandoverEdgeEditing(homeAgents, { onSaved: refreshFleetReads });
  // An archived fleet is read as it was, a built-in one is rewritten by the
  // seed: no edit from the graph of either.
  const editable = graphHandoversEditable(fleet);

  // Drag re-parent: a home member dropped on another reports to it.
  // Only for a reader who edits the members (manager, fleet not archived).
  const members = useMemo(() => fleetMembers ?? [], [fleetMembers]);
  const canEditMembers = fleetEditRights(fleet).members.canEdit;
  const reparent = useReparentFleetMember(fleetId ?? "", members);
  const draggableAgentIds = useMemo(
    () =>
      canEditMembers
        ? new Set(homeAgents.map((a) => a.id).filter((id) => members.some((m) => m.role === "member" && m.agent_id === id)))
        : undefined,
    [canEditMembers, homeAgents, members],
  );
  const canDropMember = useCallback(
    (draggedId: string, targetId: string) => graphReparent(members, draggedId, targetId) !== null,
    [members],
  );
  const dropMember = useCallback(
    (draggedId: string, targetId: string): Promise<boolean> => {
      // The graph rings the new parent only once the PATCH succeeds.
      const drop = graphReparent(members, draggedId, targetId);
      return drop ? reparent(drop.member, drop.parentId) : Promise.resolve(false);
    },
    [members, reparent],
  );

  // The node menu and the focus it sets: the agent alone. The graph dims
  // everything but the agent and its handover neighbours itself; passing the
  // neighbours too widened the focus by a second hop, so a member next to a
  // hub lit the whole fleet and nothing was dimmed (#9760 UX round 1, B4).
  const [nodeMenu, setNodeMenu] = useState<FleetNodeMenuTarget | null>(null);
  const [focusAgentId, setFocusAgentId] = useState<string | null>(null);
  const focusedAgentIds = useMemo(() => (focusAgentId ? new Set([focusAgentId]) : null), [focusAgentId]);
  const toggleFocus = useCallback((agentId: string) => setFocusAgentId((id) => (id === agentId ? null : agentId)), []);
  // The mini graph folds a dense fleet into labelled group nodes (the agents
  // other fleets lend, then a parent's leaf members), so it stays a compact
  // map (#9760 UX rounds 3 and 4, B1); the Graph view keeps every agent.
  const mini = useMemo(
    () =>
      fleetMiniGraph(fleetAgents, fleetGhosts, edgeIds, dashedEdgeIds, {
        fromFleet: (count, fleetName) =>
          t_i18n("{count, plural, one {# agent from {fleet}} other {# agents from {fleet}}}", { count, fleet: fleetName }),
        fromOtherFleets: (count) =>
          t_i18n("{count, plural, one {# agent from other fleets} other {# agents from other fleets}}", { count }),
        more: (count) => t_i18n("{count, plural, one {# more agent} other {# more agents}}", { count }),
      }),
    [fleetAgents, fleetGhosts, edgeIds, dashedEdgeIds, t_i18n],
  );
  // The mini graph's members with no handover, named under its key: a force
  // layout sets them apart, and the key says why (#10147 UX r3 B2).
  const miniEdgeless = useMemo(
    () => {
      const ids = new Set(edgelessIds(mini.agents, mini.edgeIds));
      return mini.agents.length > 1 ? mini.agents.filter((a) => ids.has(a.id)).map((a) => a.name) : [];
    },
    [mini],
  );
  // The About mini graph's way to the full graph: the Graph view, focused on
  // the agent clicked when there is one.
  const openFullGraph = useCallback(
    (agentId?: string | null) => {
      // A group node is no agent: the Graph view opens unfocused.
      setFocusAgentId(agentId && !mini.groupIds.has(agentId) ? agentId : null);
      setView("graph");
    },
    [setView, mini],
  );
  const openNodeMenu = useCallback((agentId: string, event: React.MouseEvent) => {
    // The menu is placed in the graph card's coordinate space (its `relative` wrapper).
    const wrapper = (event.currentTarget as Element).closest("[data-fleet-graph]");
    const box = wrapper?.getBoundingClientRect();
    setNodeMenu({ agentId, x: event.clientX - (box?.left ?? 0), y: event.clientY - (box?.top ?? 0) });
  }, []);

  const provenance = fleet ? forkProvenance(fleet) : null;

  // Below 1280 px an open Modify with AI panel shows the navigation as a rail, so the page keeps a
  // readable column beside it (UX r1 B1); the person's own preference is left as it was.
  const wideViewport = useMediaQuery("(min-width: 1280px)", true);
  useRailHold(!!fleet?.can_manage && aiPanelOpen && !wideViewport);

  // An unknown tab segment is not found: no redirect (#797).
  if (fleetId && !isKnownFleetTab(tabSegment)) return <RouteNotFound />;

  if (fleetLoading) return <PageSkeleton cards={2} />;

  if (isFleetsUnavailable(fleetError)) {
    return (
      <div className="space-y-6">
        <BackButton to="/fleets" label="Back to Fleets" />
        <EmptyState
          icon={Lock}
          title={t_i18n("Fleets need the agentic AI licence")}
          description={t_i18n("A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept. Fleets are part of the agentic AI features of the Enterprise Edition.")}
        />
      </div>
    );
  }

  if (!fleet || isNotFoundError(fleetError)) {
    return (
      <div className="space-y-6">
        <BackButton to="/fleets" label="Back to Fleets" />
        <EmptyState
          icon={Boxes}
          title={t_i18n("Fleet not found")}
          description={t_i18n("This fleet does not exist or is not shared with you.")}
        />
      </div>
    );
  }

  // The ROI view is its managers' (administrators included): a shared link
  // read by anyone else lands on the graph, never on a refusal.
  const canReadRoi = !!fleet.can_manage;
  const openSettings = () => {
    setSettingsSection(undefined);
    setSettingsOpen(true);
  };
  // Platform Health takes no write, a fork included (`read_only_reason`, UX r1 B3 on #464).
  const readOnlyReason = fleet.read_only_reason ?? null;
  const moreActions: OverflowMenuItem[] = [
    ...(readOnlyReason ? [] : [{
      id: "duplicate",
      label: t_i18n("Duplicate"),
      icon: CopyPlus,
      onSelect: () => setForkOpen(true),
      disabled: aiOff,
      disabledReason: aiOffReason ?? undefined,
    }]),
    { id: "settings", label: t_i18n("Settings"), icon: Settings2, onSelect: openSettings },
  ];
  // Rows with no button in the wide row: the menu keeps them at 1280 px and above too (#1508 UX r1 B1, A4).
  const menuOnlyActions: OverflowMenuItem[] = [
    // The header's primary is "Set up" while nothing is on; once something runs, the setup stays here (#1151).
    ...(canControlFleet(fleet, isAdmin) && (health?.automations?.enabled ?? 0) > 0
      ? [{ id: "run-setup-again", label: t_i18n("Run setup again"), icon: ListChecks, onSelect: () => setSetupOpen(true) }]
      : []),
    // A custom fleet has no factory state: the reset is a built-in fleet's, for administrators (#1508).
    ...(fleet.is_builtin && isAdmin && canControlFleet(fleet, isAdmin)
      ? [{ id: "reset-factory", label: t_i18n("Reset to factory state"), icon: RotateCcw, onSelect: () => setResetOpen(true) }]
      : []),
  ];
  // ROI is its managers' tab: anyone else lands on the page's default, About,
  // the address replaced too (UX A1 on #10205).
  if (requestedView === "roi" && !canReadRoi) {
    return <Navigate to={fleetViewPath(fleet.id, FLEET_DEFAULT_VIEW)} replace state={location.state} />;
  }
  const view: FleetView = requestedView;
  // The Data flow view shares the Graph view's card and members panel, its own toolbar and drawing inside.
  const dataFlow = view === "flow";

  const showAi = !!fleet.can_manage;
  const aiOpen = showAi && aiPanelOpen;
  // Below 1280 px the open panel leaves the page a narrow column: the actions take their own row so the
  // title, description (two lines) and meta get the full width, and the tabs keep one row (UX r2 B1).
  const narrowWithAi = aiOpen && !wideViewport;
  // At 1280 px and above the open panel still takes 400 px of the row: the actions keep the title's line
  // only and the title one line, so the description and meta row run under the buttons and the header
  // stays within its 140 px budget at 1440 (UX r4 B1).
  const wideWithAi = aiOpen && wideViewport;
  // An orchestrator the reader cannot see is not "No orchestrator".
  const orchestratorLabel =
    orchestratorName ?? (fleet.orchestrator_agent_id ? t_i18n("An agent not shared with you") : t_i18n("No orchestrator"));

  return (
    // The Modify with AI panel docks on the right of the page, the page keeps the rest (UX 5dcb88e3 A).
    <div className="flex items-start gap-6" data-testid="fleet-page-layout">
    <div className="min-w-0 flex-1 space-y-6">
      <EntityPageHeader
        backTo={back?.backTo ?? "/fleets"}
        backLabel={back?.backLabel ?? "Back to Fleets"}
        icon={Boxes}
        title={fleet.name}
        wrapTitle={!wideWithAi}
        stackActions={narrowWithAi}
        actionsBesideTitle={wideWithAi}
        metaOneLine
        description={fleet.description}
        hintLabel={t_i18n("What a fleet is")}
        hint={
          fleet.workspace_name
            ? t_i18n("A fleet is a team of agents. It works in the workspace {name}, where everything its agents produce is kept.", {
                name: fleet.workspace_name,
              })
            : t_i18n("A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept.")
        }
        badges={
          <>
            {/* One meta row at 1024 (UX R2 B1 #10287): the named facts shrink and truncate, the badges keep their width. */}
            <span className="flex min-w-0 items-center gap-1 content-compact text-default-secondary" data-testid="fleet-orchestrator">
              <Crown className="h-3 w-3 shrink-0" aria-hidden />
              {/* Never a false "No orchestrator" while the agents load. */}
              {fleet.orchestrator_agent_id && agentsLoading ? (
                <Skeleton className="h-3 w-24" />
              ) : (
                // The full name in the shared Tip, reachable by keyboard: the orchestrator is a link to its agent (UX R3 A2 on #10287).
                <Tip content={orchestratorLabel}>
                  {orchestratorName && fleet.orchestrator_agent_id ? (
                    <Link
                      to={`/agents/${fleet.orchestrator_agent_id}`}
                      className="truncate rounded-sm text-default-secondary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                      data-testid="fleet-orchestrator-link"
                    >
                      {orchestratorLabel}
                    </Link>
                  ) : (
                    <span className="truncate">{orchestratorLabel}</span>
                  )}
                </Tip>
              )}
            </span>
            {/* Whether the fleet is set up, first: what the actions on the right act on (#9769 S2). */}
            <FleetHeaderStatus automations={health?.automations} />
            {fleet.is_builtin && (
              <span className="inline-flex shrink-0">
                <BuiltinBadge />
              </span>
            )}
            {readOnlyReason && (
              <span className="inline-flex shrink-0">
                <ReadOnlyBadge tooltip={t_i18n("Platform Health is built in and read-only.")} />
              </span>
            )}
            {fleet.is_archived && (
              <Badge variant="secondary" className="shrink-0">
                {t_i18n("Archived")}
              </Badge>
            )}
            {provenance && (
              <span className="flex min-w-0 items-center gap-1 content-compact text-default-secondary" data-testid="fleet-provenance">
                <GitFork className="h-3 w-3 shrink-0" aria-hidden />
                {/* The shared Tip opens on hover of the line and on focus of its link (UX R3 A2 on #10287). */}
                <Tip content={`${provenance.isBuiltin ? t_i18n("Forked from the built-in fleet") : t_i18n("Forked from")} ${provenance.name}`}>
                  <span className="truncate">
                    {provenance.isBuiltin
                      ? t_i18n("Forked from the built-in fleet")
                      : t_i18n("Forked from")}{" "}
                    <Link to={`/fleets/${provenance.sourceId}`} className="text-accent underline-offset-2 hover:underline focus-visible:underline">
                      {provenance.name}
                    </Link>
                  </span>
                </Tip>
              </span>
            )}
          </>
        }
        links={
          fleet.workspace_id && fleet.workspace_name ? (
            <FleetWorkspaceChip
              target={{ kind: "workspace", id: fleet.workspace_id, name: fleet.workspace_name }}
              from={{ path: `/fleets/${fleet.id}`, label: "Back to the fleet" }}
            />
          ) : undefined
        }
        actions={
          <>
            {/* Modify with AI first, a button at every width (#10162 R1); a manager's only (R3). */}
            {showAi && (
              <FleetModifyWithAiButton
                open={aiOpen}
                onToggle={() => setAiPanel(!aiOpen)}
                disabled={modifyWithAi.disabled}
                // The reason line shows only while the panel is closed: never a dangling describedby.
                reason={aiOpen ? null : modifyWithAi.reason}
              />
            )}
            {/* Below 1280 px Duplicate and Settings fold into More actions, the state actions stay buttons (design round 3 B7);
                at every width while the Modify with AI panel takes its room (UX r1 B2). */}
            {!aiOpen && (
              <EntityHeaderWideOnly>
                {!readOnlyReason && (
                  <DuplicateButton size="default" onDuplicate={() => setForkOpen(true)} hint={t_i18n("Create an editable copy of this fleet")} />
                )}
                <Button type="button" variant="outline" className="gap-1.5" onClick={openSettings}>
                  <Settings2 className="h-4 w-4" aria-hidden />
                  {t_i18n("Settings")}
                </Button>
              </EntityHeaderWideOnly>
            )}
            <EntityHeaderMoreActions items={moreActions} menuOnly={menuOnlyActions} always={aiOpen} />
            {/* One setup per fleet (#1151): the primary opens it, the CTEM loop fleet's own guided setup included. */}
            <FleetHeaderActions
              fleetId={fleet.id}
              fleetName={fleet.name}
              fleetSlug={fleet.slug}
              automations={health?.automations}
              canControl={canControlFleet(fleet, isAdmin)}
            />
          </>
        }
      />
      {/* Why Modify with AI is off, under the row, in the Create with a prompt wording (R4). */}
      {showAi && !aiOpen && (
        <div className="-mt-4 flex">
          <FleetModifyWithAiReason reason={modifyWithAi.reason} />
        </div>
      )}
      {/* A fork whose source moved since: what changed, never applied to the copy (#9174). */}
      {provenance && <FleetSourceChangesNotice fleetId={fleet.id} source={provenance} />}
      {forkOpen && <ForkFleetDialog fleet={fleet} open={forkOpen} onOpenChange={setForkOpen} />}
      <FleetSetupDialog fleetId={fleet.id} fleetName={fleet.name} fleetSlug={fleet.slug} open={setupOpen} onOpenChange={setSetupOpen} />
      {fleet.is_builtin && isAdmin && (
        <FleetResetDialog fleetId={fleet.id} fleetName={fleet.name} open={resetOpen} onOpenChange={setResetOpen} />
      )}
      <FleetSettingsPanel
        fleet={fleet}
        members={fleetMembers ?? []}
        agents={agents ?? []}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialSection={settingsSection}
      />

      {/* As wide as its two options, not a grey band across the page. */}
      <SegmentedFilter
        options={canReadRoi ? MANAGER_VIEW_OPTIONS : VIEW_OPTIONS}
        value={view}
        onChange={setView}
        ariaLabel={t_i18n("Fleet view")}
        // Below 1280 px with the panel open the column is about 570 px: dense pills, and the tabs a longer
        // locale cannot fit go into a "More tabs" menu, never a second row nor a cut label (UX r3 B2, r4 B3).
        dense={narrowWithAi}
        overflow={aiOpen ? "menu" : "wrap"}
        className="w-fit"
        testId="fleet-view-tabs"
      />

      {view === "operations" ? (
        <FleetOperations fleetId={fleet.id} fleetName={fleet.name} />
      ) : view === "roi" && listSegment === ROI_RECORDS_SEGMENT ? (
        // The ROI records the review-rate figure opens (#10570), runs and work items alike.
        <RoiRecordsList scope="fleet" id={fleet.id} />
      ) : view === "roi" ? (
        <FleetRoiView fleetId={fleet.id} onSetFleetDefault={() => { setSettingsSection("outcomes"); setSettingsOpen(true); }} />
      ) : view === "about" ? (
        <FleetAboutView
          fleetId={fleet.id}
          onOpenGraph={() => openFullGraph(null)}
          onOpenMembers={() => setView("members")}
          onOpenView={setView}
          readOnly={!!readOnlyReason}
          healthStarts={fleet.health_starts}
          orchestratorId={fleet.orchestrator_agent_id}
          flow={
            // The Graph view's own drawing as a fixed mini graph (#9757): the
            // same members, handovers, ghosts and live edges, in a force
            // layout computed once and frozen (#10123). The wheel scrolls the
            // page; a click opens the Graph view on the node clicked. Zoom,
            // editing and drag stay there.
            fleetAgents.length > 0 && !graphLoading && !agentsLoading ? (
              <>
                <AgentFlowGraph
                  agents={mini.agents}
                  activeAgentIds={activeAgentIds}
                  liveEdges={liveEdges}
                  staticView
                  onStaticOpen={openFullGraph}
                  layout="force"
                  hideLegend
                  wrapNames
                  ghostNodes={mini.ghostNodes}
                  viewedFleetId={fleet.id}
                  edgeIds={mini.edgeIds}
                  dashedEdgeIds={mini.dashedEdgeIds}
                  dashedEdgeTooltip={dashedEdgeTooltip}
                />
                {/* A compact key under the mini graph, not a toggle. */}
                <div className="border-t border-elevation-subtle-soft px-4 py-2" data-testid="about-flow-key">
                  <AgentFlowLegend variant="compact" />
                  {miniEdgeless.length > 0 && (
                    <p className="mt-1 text-xs text-default-secondary" data-testid="about-flow-edgeless">
                      {t_i18n("{count, plural, one {No handover with another member, works on its own: {names}} other {No handover with another member, work on their own: {names}}}", { count: miniEdgeless.length, names: miniEdgeless.join(", ") })}
                    </p>
                  )}
                </div>
              </>
            ) : undefined
          }
        />
      ) : view === "approval" ? (
        // Human approval is its own tab (#10176): the header carries nothing of it.
        <FleetAutonomyStrip
          fleetId={fleet.id}
          level={fleet.autonomy_level}
          canManage={canControlFleet(fleet, isAdmin)}
          isAdmin={isAdmin}
        />
      ) : view === "members" ? (
        <FleetMembersView fleet={fleet} members={fleetMembers ?? []} loading={membersLoading} onOpenAgent={openAgent} />
      ) : view === "schedule" ? (
        <ScheduleView agentIds={fleetAgentIdSet} fleetId={fleet.id} />
      ) : graphLoading || agentsLoading ? (
        <PageSkeleton cards={2} />
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
          {/* The card anchors and clips the node menu: it flips against the card's height. */}
          {/* A flex column: the graph under the toolbar fills the card, stretched by the members
              panel at xl, so a pan never shows a clip line or a blank band inside it. */}
          {/* Data flow: the card follows the flow's height (UX #9882 r1 B6), no band under the last row. */}
          <Card className={`relative flex flex-col overflow-hidden${dataFlow ? " self-start" : ""}`} data-fleet-graph>
            {/* Empty or locked: the content fills the card, so the inset empty state sits in its
                middle rather than at its top; drawn, the graph takes the height under the toolbar. */}
            <CardContent className="flex min-h-0 flex-1 flex-col p-0">
              {/* The toolbar above the canvas: its Legend opens downwards over the graph, never clipped.
                  Not drawn over an empty or locked graph: no layout, counters or legend over nothing. */}
              {fleetAgents.length > 0 && (
                <AgentGraphToolbar
                  layout={dataFlow ? undefined : layout}
                  onLayoutChange={setLayout}
                  legend={
                    dataFlow ? (
                      <FleetDataFlowLegend />
                    ) : (
                      <AgentFlowLegend variant="list" showGeneralAssistant={false} showAllTypes={false} showFleetMarks showLive showA2A={a2a.partners.size > 0} />
                    )
                  }
                  counters={dataFlow ? undefined : [
                    { key: "agents", label: t_i18n("agents"), value: homeAgents.length },
                    { key: "edges", label: t_i18n("handovers"), value: internalHandoverCount(graph) },
                    { key: "runs", label: t_i18n("{count, plural, one {run in flight} other {runs in flight}}", { count: activeAgentIds.size }), value: activeAgentIds.size, live: true },
                    { key: "items", label: t_i18n("open work items"), value: health?.workspace_open_work_items ?? 0 },
                  ]}
                />
              )}
              {fleetAgents.length === 0 ? (
                // Nothing to draw: say why rather than show a blank canvas. A fleet
                // whose members exist but are not shared with the reader is not empty.
                // Inset: the graph card is the surface, no card in a card.
                (fleetMembers ?? []).some((m) => m.role === "member") ? (
                  <EmptyState
                    variant="inset"
                    icon={Lock}
                    title="The members of this fleet are not shared with you"
                    description="Ask the fleet's owner to share its agents with you."
                  />
                ) : (
                  <EmptyState
                    variant="inset"
                    icon={Boxes}
                    title="No member yet"
                    description="A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept."
                  />
                )
              ) : dataFlow ? (
                <FleetDataFlow
                  flow={flowQuery.data}
                  loading={flowQuery.isLoading}
                  error={flowQuery.isError}
                  onRetry={() => void flowQuery.refetch()}
                  onOpenAgent={openAgent}
                />
              ) : (
                <AgentFlowGraph
                  agents={a2a.agents}
                  activeAgentIds={activeAgentIds}
                  liveEdges={liveEdges}
                  editableAgentIds={editable ? handoverEditing.editableAgentIds : undefined}
                  onAddEdge={editable ? handoverEditing.onAddEdge : undefined}
                  onEditEdge={editable ? handoverEditing.onEditEdge : undefined}
                  onNavigateToAgent={openNode}
                  onToggleFocus={toggleFocus}
                  onShowDetails={(id) => (a2a.partners.has(id) ? openNode(id) : details.show(id))}
                  selectedNodeId={details.selectedId}
                  onNodeContextMenu={openNodeMenu}
                  draggableNodeIds={draggableAgentIds}
                  canDropNode={canDropMember}
                  onNodeDrop={draggableAgentIds ? dropMember : undefined}
                  nodeDragHint={t_i18n("Drag onto a member to make it report to that member")}
                  focusedAgentIds={focusedAgentIds}
                  layout={layout}
                  hideLegend
                  fillHeight
                  ghostNodes={ghostNodes}
                  ghostDetails={partnerDetails}
                  viewedFleetId={fleet?.id ?? null}
                  edgeIds={edgeIds}
                  edgeTypeOverrides={a2a.edgeTypes}
                  dashedEdgeIds={dashedEdgeIds}
                  dashedEdgeTooltip={dashedEdgeTooltip}
                />
              )}
              {/* Always mounted: its dialogs outlive the menu that opens them. */}
              {(
                <FleetNodeMenu
                  target={nodeMenu}
                  fleet={fleet}
                  members={fleetMembers ?? []}
                  homeAgents={homeAgents}
                  agents={agents ?? []}
                  handoverEditing={handoverEditing}
                  focused={!!nodeMenu && focusAgentId === nodeMenu.agentId}
                  onToggleFocus={toggleFocus}
                  onOpenAgent={openAgent}
                  onShowDetails={details.show}
                  onClose={() => setNodeMenu(null)}
                />
              )}
            </CardContent>
          </Card>

          <div className="min-w-0 space-y-6">
            <FleetMembersPanel
              fleet={fleet}
              members={fleetMembers ?? []}
              health={health?.members ?? []}
              agents={agents ?? []}
              onOpenAgent={openAgent}
              onConfigureMembers={() => setView("members")}
            />
            {/* The cost read is a manager's: the API refuses anyone else. */}
            {fleet.can_manage && <FleetCostCard fleetId={fleet.id} />}
          </div>
        </div>
      )}
      {details.element}
      {handoverEditing.confirmElement}
    </div>
      {aiOpen && (
        <FleetStudioRail
          fleet={fleet}
          initialConversationId={aiConversationId}
          onConversationChange={(id) => setAiPanel(true, id)}
          onCollapse={() => setAiPanel(false)}
          onShowApprovalTab={() => setView("approval")}
          // Docked under the app header while the page scrolls beside it; its bottom stays inside the
          // window at its first position (header + page padding) and once stuck (UX r1 B7).
          className="sticky top-[calc(var(--fds-header-height,68px)+1rem)] h-[calc(100dvh-var(--fds-header-height,68px)-4rem)] rounded-sm"
        />
      )}
    </div>
  );
}
