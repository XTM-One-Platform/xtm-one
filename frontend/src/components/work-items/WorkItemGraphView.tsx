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
 * Tasks > Work Items, Graph view (#968, fleet-grade in #1062): the items the
 * list's filters match and what they wait for, drawn as a progression: a
 * prerequisite left of (above, in Top down) what it releases, the arrow reading
 * "unblocks" (`lib/work-item-graph-layout`). The fleet graph's toolbar above the
 * canvas (layout, Group by, focus mode, Legend), its status line under it, the
 * shared canvas surface (`graph-canvas.css`). Nodes are items (status colour of the list badges, owner
 * agent, priority, the decision card's question when a person is asked),
 * references (pull request, issue, deployment, workflow run, with their state
 * and link) and people. Selecting a node is focus mode: its upstream and
 * downstream chains stay lit, the rest dims; a node only a person can release
 * is marked. Opening an item opens the list's own detail drawer.
 */
import "@xyflow/react/dist/style.css";

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
  useStoreApi,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";
import { WaitingForYouBadge } from "@/components/work-items/WaitingForYouBadge";

import "@/components/ui/graph-canvas.css";
import { AgentGraphStatusLine, AgentGraphToolbar, type AgentGraphCounter } from "@/components/ui/agent-graph-toolbar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useColorMode } from "@/hooks/useColorMode";
import { useFitToViewport } from "@/hooks/useFitToViewport";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { PRIORITY_LABELS, STATUS_LABELS, waitsForReader } from "@/lib/work-item-context";
import type { AgentFlowFocusMode } from "@/lib/agent-flow-focus";
import { criticalLeaves, drawnCounts, focusChains, shownGraph } from "@/lib/work-item-graph";
import { anchoredViewport, graphFitKey, type GraphPoint } from "@/lib/work-item-graph-viewport";
import {
  DEFAULT_WORK_ITEM_GRAPH_LAYOUT,
  GROUP_NODE_SIZE,
  WORK_ITEM_GRAPH_GROUP_BYS,
  WORK_ITEM_GRAPH_LAYOUTS,
  edgePath,
  graphGroups,
  groupItemCount,
  groupNodeId,
  layoutWorkItemGraph,
  type GraphGroup,
  type Point,
  type WorkItemGraphGroupBy,
  type WorkItemGraphLayout,
} from "@/lib/work-item-graph-layout";
import type {
  WorkItemGraph,
  WorkItemGraphItemNode,
  WorkItemGraphNode,
  WorkItemGraphPersonNode,
  WorkItemGraphRefNode,
  WorkItemPriority,
  WorkItemStatus,
} from "@/types";

/** The most nodes GET /work-items/graph draws; past it the answer is truncated. */
const GRAPH_NODE_CAP = 500;

/** The list's status badge tokens (WorkItemsTab STATUS_BADGE). */
const STATUS_TONE: Record<WorkItemStatus, string> = {
  open: "text-accent bg-accent/10",
  claimed: "text-filigran-ia-primary bg-filigran-ia-primary/10",
  in_progress: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  blocked: "text-feedback-error-primary bg-feedback-error-primary/10",
  done: "text-feedback-success-primary bg-feedback-success-primary/10",
  cancelled: "text-default-secondary bg-elevation-hover",
};

/** The status bar on a node's leading edge, same hues as the badges. */
const STATUS_BAR: Record<WorkItemStatus, string> = {
  open: "border-s-accent",
  claimed: "border-s-filigran-ia-primary",
  in_progress: "border-s-feedback-alert-primary",
  blocked: "border-s-feedback-error-primary",
  done: "border-s-feedback-success-primary",
  cancelled: "border-s-default-secondary",
};

/** A reference kind as a person reads it. @i18n-keys */
const REF_KIND_LABELS = {
  pull_request: "Pull request",
  issue: "Issue",
  deployment: "Deployment",
  workflow_run: "Workflow run",
} as const;

/** A reference's state as a person reads it; none = still waited for. @i18n-keys */
const REF_STATE_LABELS = {
  merged: "Merged",
  closed: "Closed",
  deployed: "Deployed",
  succeeded: "Succeeded",
  pending: "Pending",
} as const;

const ITEM_W = 260;
const ITEM_H = 92;
const ASK_H = 44;
const SMALL_W = 200;
const SMALL_H = 52;

type FocusState = "none" | "selected" | "lit" | "dim";

type GraphNodeData = {
  node: WorkItemGraphNode;
  focus: FocusState;
  /** Top down: the edges enter a node on top and leave at the bottom. */
  vertical: boolean;
  critical: boolean;
  /** The item waits for the reader (the Waiting for me population, #1001): its marker. */
  waitingForYou: boolean;
  onOpen?: (id: string) => void;
  onSelect: (id: string) => void;
};

const HIDDEN_HANDLE = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0 } as const;

/**
 * The canvas's own fit padding, on mount, after a resize and after the drawing changes.
 * The controls sit in the toolbar above the canvas (#1062), so no side is reserved.
 */
const FIT_VIEW_OPTIONS = { padding: 0.12 } as const;

/**
 * A node's keyboard side of a button: Tab reaches it, Enter and Space select it.
 * The pointer side is the canvas's `onNodeClick` (UX r1 B1 on #979): a node wrapper
 * React Flow draws without a click handler gets `pointer-events: none`, so a click
 * on the node's own element never arrived; with both, one click would select twice.
 */
function nodeKeyProps(onActivate: () => void) {
  const { role, tabIndex, onKeyDown } = buttonRoleProps(onActivate);
  return { role, tabIndex, onKeyDown };
}

function focusClass(focus: FocusState): string {
  if (focus === "dim") return "opacity-30";
  if (focus === "selected") return "ring-2 ring-focus";
  return "";
}

/** An edge enters on the leading side and leaves on the trailing one: left and right, or top and bottom in Top down. */
function Handles({ vertical = false }: { vertical?: boolean }) {
  return (
    <>
      <Handle type="target" position={vertical ? Position.Top : Position.Left} isConnectable={false} style={HIDDEN_HANDLE} />
      <Handle type="source" position={vertical ? Position.Bottom : Position.Right} isConnectable={false} style={HIDDEN_HANDLE} />
    </>
  );
}

function ItemNode({ data }: NodeProps<Node<GraphNodeData>>) {
  const { t_i18n } = useFormatter();
  const node = data.node as WorkItemGraphItemNode;
  const status = STATUS_LABELS[node.status] ?? node.status;
  const priority = node.priority ? PRIORITY_LABELS[node.priority as WorkItemPriority] : null;
  return (
    <div
      {...nodeKeyProps(() => data.onSelect(node.id))}
      aria-label={t_i18n("Work item {title}, {status}", { title: node.title, status: t_i18n(status) })}
      aria-pressed={data.focus === "selected"}
      data-testid="work-item-graph-node"
      data-node-id={node.id}
      data-focus={data.focus}
      data-critical={data.critical || undefined}
      data-waiting-for-you={data.waitingForYou || undefined}
      className={cn(
        "flex h-full w-full flex-col gap-1 rounded-md border border-elevation-subtle border-s-4 bg-elevation-default px-2.5 py-2 text-start",
        STATUS_BAR[node.status],
        node.outside_filter && "border-dashed",
        data.critical && "border-feedback-error-primary",
        focusClass(data.focus),
        BUTTON_ROLE_FOCUS_RING,
      )}
    >
      <Handles vertical={data.vertical} />
      <div className="flex min-w-0 items-center gap-1.5">
        {node.is_program_root && <Icon name="network" size={16} className="shrink-0 text-accent" aria-hidden />}
        <TruncatedText text={node.title} className="flex-1 content-compact font-medium text-default-primary" />
        {data.onOpen && (
          <button
            type="button"
            aria-label={t_i18n("Open the details of {title}", { title: node.title })}
            className={cn("nodrag shrink-0 rounded-sm p-0.5 text-default-secondary hover:text-default-primary", BUTTON_ROLE_FOCUS_RING)}
            onClick={(event) => {
              event.stopPropagation();
              data.onOpen?.(node.id);
            }}
            data-testid="work-item-graph-open"
          >
            <Icon name="maximize-2" size={16} aria-hidden />
          </button>
        )}
      </div>
      <div className="flex min-w-0 items-center gap-1">
        <Badge variant="secondary" className={cn("content-caption", STATUS_TONE[node.status])}>{t_i18n(status)}</Badge>
        {/* The list row's Waiting for you marker, same tone (#1001). */}
        {data.waitingForYou && (
          <WaitingForYouBadge testId="work-item-graph-waiting-for-you" />
        )}
        {priority && <Badge variant="secondary" className="content-caption">{t_i18n(priority)}</Badge>}
        {node.owner_agent?.name && (
          <span className="flex min-w-0 items-center gap-1 content-caption text-default-secondary">
            <Icon name="bot" size={16} className="shrink-0" aria-hidden />
            <TruncatedText text={node.owner_agent.name} />
          </span>
        )}
      </div>
      {node.ask && (
        <div className="min-w-0 rounded-sm bg-warning-box px-1.5 py-1 content-caption text-default-primary" data-testid="work-item-graph-ask">
          <TruncatedText text={node.ask.question} />
          <TruncatedText
            text={node.ask.options.map((option) => `${option.key}. ${option.label}`).join("  ")}
            className="text-default-secondary"
          />
        </div>
      )}
      {data.critical && <span className="sr-only">{t_i18n("Only a person can release it")}</span>}
    </div>
  );
}

function RefNode({ data }: NodeProps<Node<GraphNodeData>>) {
  const { t_i18n } = useFormatter();
  const node = data.node as WorkItemGraphRefNode;
  const kind = t_i18n(REF_KIND_LABELS[node.ref_kind] ?? REF_KIND_LABELS.pull_request);
  const state = t_i18n(REF_STATE_LABELS[node.state ?? "pending"] ?? REF_STATE_LABELS.pending);
  return (
    <div
      {...nodeKeyProps(() => data.onSelect(node.id))}
      aria-label={`${kind} ${node.label}, ${state}`}
      data-testid="work-item-graph-ref"
      data-focus={data.focus}
      className={cn(
        "flex h-full w-full flex-col justify-center gap-0.5 rounded-full border border-elevation-subtle bg-elevation-hover px-3 text-start",
        node.satisfied && "border-feedback-success-primary",
        focusClass(data.focus),
        BUTTON_ROLE_FOCUS_RING,
      )}
    >
      <Handles vertical={data.vertical} />
      <span className="flex min-w-0 items-center gap-1 content-caption text-default-secondary">
        {kind}
        {node.url && (
          <a
            href={node.url}
            target="_blank"
            rel="noreferrer"
            className={cn("nodrag ms-auto inline-flex rounded-sm hover:text-default-primary", BUTTON_ROLE_FOCUS_RING)}
            aria-label={t_i18n("Open {name}", { name: node.label })}
            onClick={(event) => event.stopPropagation()}
          >
            <Icon name="external-link" size={16} aria-hidden />
          </a>
        )}
      </span>
      <span className="flex min-w-0 items-center gap-1 content-compact">
        <TruncatedText text={node.label} className="text-default-primary" />
        <span className={cn("shrink-0", node.satisfied ? "text-feedback-success-primary" : "text-default-secondary")}>{state}</span>
      </span>
    </div>
  );
}

function PersonNode({ data }: NodeProps<Node<GraphNodeData>>) {
  const { t_i18n } = useFormatter();
  const node = data.node as WorkItemGraphPersonNode;
  const name = node.is_you ? t_i18n("You") : node.name || t_i18n("A person");
  return (
    <div
      {...nodeKeyProps(() => data.onSelect(node.id))}
      aria-label={t_i18n("Waits for {name}", { name })}
      data-testid="work-item-graph-person"
      data-focus={data.focus}
      className={cn(
        "flex h-full w-full items-center gap-2 rounded-full border border-feedback-alert-primary bg-warning-box px-3",
        focusClass(data.focus),
        BUTTON_ROLE_FOCUS_RING,
      )}
    >
      <Handles vertical={data.vertical} />
      <Icon name="users" size={16} className="shrink-0 text-feedback-warning-tertiary" aria-hidden />
      <TruncatedText text={name} className="content-compact text-default-primary" />
    </div>
  );
}

/**
 * A group drawn as a zone (#1062): its name, its count and its collapse button; the members sit inside.
 * The count is the group's items, the status line's population and unit (UX r1 B2 on #1067): a
 * pull request, a deployment or a person drawn beside an item is not counted.
 */
type ZoneData = {
  name: string;
  groupKey: string;
  count: number;
  onToggle: (key: string) => void;
};

function ZoneNode({ data }: NodeProps<Node<ZoneData>>) {
  const { t_i18n } = useFormatter();
  return (
    <div
      className="h-full w-full rounded-sm border border-dashed border-elevation-subtle"
      data-testid="work-item-graph-zone"
      data-group={data.groupKey}
    >
      <div className="flex min-w-0 items-center gap-2 px-2 py-1.5">
        <button
          type="button"
          aria-expanded
          aria-label={t_i18n("Collapse {name}", { name: data.name })}
          className={cn("nodrag shrink-0 rounded-sm p-0.5 text-default-secondary hover:text-default-primary", BUTTON_ROLE_FOCUS_RING)}
          onClick={(event) => {
            event.stopPropagation();
            data.onToggle(data.groupKey);
          }}
          data-testid="work-item-graph-zone-collapse"
        >
          <Icon name="chevron-down" size={16} aria-hidden />
        </button>
        <TruncatedText text={data.name} className="flex-1 content-compact font-medium text-default-primary" />
        <span className="shrink-0 content-caption tabular-nums text-default-secondary">
          {t_i18n("{count, plural, one {# item} other {# items}}", { count: data.count })}
        </span>
      </div>
    </div>
  );
}

/** A collapsed group: one node with its name and count, the edges of its members moved onto it. */
type CollapsedData = ZoneData & { vertical: boolean };

function CollapsedGroupNode({ data }: NodeProps<Node<CollapsedData>>) {
  const { t_i18n } = useFormatter();
  return (
    <div
      className="flex h-full w-full items-center gap-2 rounded-md border border-dashed border-elevation-subtle bg-elevation-default px-2.5"
      data-testid="work-item-graph-collapsed-group"
      data-group={data.groupKey}
    >
      <Handles vertical={data.vertical} />
      <button
        type="button"
        aria-expanded={false}
        aria-label={t_i18n("Expand {name}", { name: data.name })}
        className={cn("nodrag shrink-0 rounded-sm p-0.5 text-default-secondary hover:text-default-primary", BUTTON_ROLE_FOCUS_RING)}
        onClick={(event) => {
          event.stopPropagation();
          data.onToggle(data.groupKey);
        }}
        data-testid="work-item-graph-zone-expand"
      >
        <Icon name="chevron-right" size={16} aria-hidden />
      </button>
      <TruncatedText text={data.name} className="flex-1 content-compact font-medium text-default-primary" />
      <span className="shrink-0 content-caption tabular-nums text-default-secondary">
        {t_i18n("{count, plural, one {# item} other {# items}}", { count: data.count })}
      </span>
    </div>
  );
}

const NODE_TYPES = { item: ItemNode, ref: RefNode, person: PersonNode, zone: ZoneNode, collapsed: CollapsedGroupNode };

/**
 * Every edge, in every layout and grouping (UX r3 B1 on #1067): the route the
 * layout computed (`layoutWorkItemGraph`), which runs inside no card but its two
 * ends; a cross-zone edge goes around the cards instead of through them.
 */
function RoutedEdge({ id, data, style, markerEnd }: EdgeProps) {
  const points = (data as { points?: Point[] } | undefined)?.points ?? [];
  if (points.length < 2) return null;
  return <BaseEdge id={id} path={edgePath(points)} markerEnd={markerEnd} style={style} />;
}

const EDGE_TYPES = { routed: RoutedEdge };

function NetworkIcon({ className }: { className?: string }) {
  return <Icon name="network" size={16} className={className} aria-hidden />;
}

function nodeSize(node: WorkItemGraphNode): { width: number; height: number } {
  if (node.kind === "item") return { width: ITEM_W, height: ITEM_H + (node.ask ? ASK_H : 0) };
  return { width: SMALL_W, height: SMALL_H };
}

const EDGE_STROKE = {
  blocked_on: "var(--text-default-secondary)",
  ref: "var(--text-default-secondary)",
  person: "var(--color-feedback-alert-primary, var(--text-default-secondary))",
  program: "var(--color-accent, var(--text-default-secondary))",
} as const;

/** How a focus shows: the chain alone, or the whole graph with the rest dimmed (the fleet map's modes). */
const FOCUS_MODES: readonly AgentFlowFocusMode[] = ["dim", "isolate"];

/** A display choice of the graph kept per reader, like List | Graph (`useViewMode`). */
function useStoredChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      return allowed.includes(stored as T) ? (stored as T) : fallback;
    } catch {
      return fallback;
    }
  });
  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(key, next);
      } catch { /* quota exceeded or private browsing */ }
    },
    [key],
  );
  return [value, set];
}

/** The status of a prerequisite whose release is under way: its edges march toward what it releases. */
const IN_FLIGHT = new Set<WorkItemStatus>(["claimed", "in_progress"]);

export function WorkItemGraphView({
  graph,
  isLoading,
  onOpenItem,
  readerId,
}: {
  graph: WorkItemGraph | undefined;
  isLoading: boolean;
  onOpenItem: (id: string) => void;
  /** The signed-in reader: the nodes that wait for them carry the Waiting for you marker. */
  readerId?: string;
}) {
  const { t_i18n } = useFormatter();
  const colorMode = useColorMode();
  const { data: agents } = useAgents();
  const [layoutChoice, setLayoutChoice] = useStoredChoice<WorkItemGraphLayout>(
    "copilot-graph:work-items:layout",
    WORK_ITEM_GRAPH_LAYOUTS,
    DEFAULT_WORK_ITEM_GRAPH_LAYOUT,
  );
  const [groupBy, setGroupBy] = useStoredChoice<WorkItemGraphGroupBy>("copilot-graph:work-items:group-by", WORK_ITEM_GRAPH_GROUP_BYS, "none");
  const [focusMode, setFocusMode] = useState<AgentFlowFocusMode>("dim");
  const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(() => new Set());
  const [showDone, setShowDone] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  // A second select of the selected node clears the focus.
  const selectNode = (id: string) => setSelected((current) => (current === id ? null : id));
  const toggleGroup = useCallback(
    (key: string) =>
      setCollapsedKeys((current) => {
        const next = new Set(current);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
    [],
  );
  const vertical = layoutChoice === "hierarchy-tb";

  const shown = useMemo(() => (graph ? shownGraph(graph, { showDone }) : null), [graph, showDone]);
  const critical = useMemo(() => (shown ? criticalLeaves(shown) : new Set<string>()), [shown]);
  const chains = useMemo(
    () => (shown && selected && shown.nodes.some((n) => n.id === selected) ? focusChains(shown.edges, selected) : null),
    [shown, selected],
  );
  // Show only: the focused chain alone is laid out; Highlight keeps the whole graph and dims the rest.
  const visible = useMemo(() => {
    if (!shown || !chains || !selected || focusMode !== "isolate") return shown;
    const kept = new Set([selected, ...chains.upstream, ...chains.downstream]);
    return {
      ...shown,
      nodes: shown.nodes.filter((node) => kept.has(node.id)),
      edges: shown.edges.filter((edge) => kept.has(edge.source) && kept.has(edge.target)),
    };
  }, [shown, chains, selected, focusMode]);
  const fleetOfAgent = useMemo(
    () => new Map((agents ?? []).flatMap((a) => (a.fleet?.fleet_id ? [[a.id, { id: a.fleet.fleet_id, name: a.fleet.fleet_name }] as const] : []))),
    [agents],
  );
  const groups = useMemo(
    () => (visible ? graphGroups(visible.nodes, visible.edges, groupBy, { fleetOfAgent }) : []),
    [visible, groupBy, fleetOfAgent],
  );
  const laid = useMemo(
    () => (visible ? layoutWorkItemGraph(visible.nodes, visible.edges, { layout: layoutChoice, groups, collapsedKeys, sizeOf: nodeSize }) : null),
    [visible, layoutChoice, groups, collapsedKeys],
  );
  const groupName = useCallback((group: GraphGroup) => (group.translate ? t_i18n(group.label) : group.label), [t_i18n]);
  const itemCount = useCallback((group: GraphGroup) => groupItemCount(group, visible?.nodes ?? []), [visible]);

  const nodes = useMemo<Node[]>(() => {
    if (!visible || !laid) return [];
    const fixed = { draggable: false, connectable: false, selectable: false, focusable: false } as const;
    // Zones first: drawn under their members.
    const zones: Node[] = laid.zones.map((zone) => ({
      id: `zone:${zone.group.key}`,
      type: "zone",
      position: { x: zone.x, y: zone.y },
      width: zone.width,
      height: zone.height,
      data: { name: groupName(zone.group), groupKey: zone.group.key, count: itemCount(zone.group), onToggle: toggleGroup },
      zIndex: -1,
      ...fixed,
    }));
    const collapsed: Node[] = laid.collapsed.map((group) => ({
      id: groupNodeId(group.key),
      type: "collapsed",
      position: laid.positions.get(groupNodeId(group.key)) ?? { x: 0, y: 0 },
      width: GROUP_NODE_SIZE.width,
      height: GROUP_NODE_SIZE.height,
      data: { name: groupName(group), groupKey: group.key, count: itemCount(group), onToggle: toggleGroup, vertical },
      ...fixed,
    }));
    const drawn: Node[] = visible.nodes.flatMap((node) => {
      const position = laid.positions.get(node.id);
      if (!position) return [];
      let focus: FocusState = "none";
      if (chains && selected) {
        if (node.id === selected) focus = "selected";
        else if (chains.upstream.has(node.id) || chains.downstream.has(node.id)) focus = "lit";
        else focus = "dim";
      }
      const size = nodeSize(node);
      const data: GraphNodeData = {
        node,
        focus,
        vertical,
        critical: critical.has(node.id),
        waitingForYou: node.kind === "item" && !node.outside_filter && waitsForReader(node, readerId),
        onOpen: node.kind === "item" ? onOpenItem : undefined,
        onSelect: selectNode,
      };
      return [{ id: node.id, type: node.kind, position, width: size.width, height: size.height, data, ...fixed }];
    });
    return [...zones, ...collapsed, ...drawn];
  }, [visible, laid, chains, selected, critical, onOpenItem, readerId, vertical, groupName, itemCount, toggleGroup]);

  const edges = useMemo<Edge[]>(() => {
    if (!visible || !laid) return [];
    const byId = new Map(visible.nodes.map((node) => [node.id, node]));
    return laid.edges.map((edge) => {
      const dim = chains ? !chains.edges.has(edge.id) : false;
      const stroke = EDGE_STROKE[edge.kind];
      // Drawn from the prerequisite to what it releases (#1062): a prerequisite being worked on marches.
      const prerequisite = byId.get(edge.drawnSource);
      const animated = edge.kind !== "program" && prerequisite?.kind === "item" && IN_FLIGHT.has(prerequisite.status);
      return {
        id: edge.id,
        source: edge.drawnSource,
        target: edge.drawnTarget,
        type: "routed",
        animated,
        markerEnd: edge.kind === "program" ? undefined : { type: MarkerType.ArrowClosed, color: stroke, width: 16, height: 16 },
        style: {
          stroke,
          strokeWidth: chains && !dim ? 2 : 1.25,
          strokeOpacity: dim ? 0.2 : 0.9,
          strokeDasharray: edge.kind === "program" ? "6 4" : edge.kind === "ref" ? "2 3" : undefined,
        },
        selectable: false,
        focusable: false,
        data: { kind: edge.kind, points: edge.points },
      };
    });
  }, [visible, laid, chains]);

  const loading = isLoading || !graph || !shown || !visible;
  // The reader's choices alone refit the canvas (#1158): a live refresh that adds, drops or
  // moves items never does, so a reader zoomed into a cluster keeps their camera.
  const fitKey = graphFitKey({ layout: layoutChoice, groupBy, collapsedKeys, focusMode, selected, showDone });
  const groupByLabels: Record<WorkItemGraphGroupBy, string> = {
    none: t_i18n("None"),
    program: t_i18n("Program"),
    owner: t_i18n("Owner agent"),
    fleet: t_i18n("Fleet"),
    prefix: t_i18n("Key prefix"),
    status: t_i18n("Status"),
    priority: t_i18n("Priority"),
  };
  const focusLabels: Record<AgentFlowFocusMode, string> = { isolate: t_i18n("Show only"), dim: t_i18n("Highlight") };
  // The status line counts what is drawn, the zones' population: Show only included (UX r2 B2 on #1067).
  const counts = visible ? drawnCounts(visible) : null;
  const counters: AgentGraphCounter[] = counts && shown
    ? [
        { key: "items", label: t_i18n("{count, plural, one {item} other {items}}", { count: counts.items }), value: counts.items },
        { key: "links", label: t_i18n("{count, plural, one {link} other {links}}", { count: counts.links }), value: counts.links },
        { key: "person", label: t_i18n("waiting for a person"), value: counts.waitingForPerson, live: true },
        { key: "isolated", label: t_i18n("with no dependency, in the list view"), value: shown.isolated },
      ]
    : [];
  return (
    <GraphFrame
      testId={loading ? undefined : "work-item-graph"}
      label={
        shown && !loading
          ? t_i18n("Graph of {nodes, plural, one {# node} other {# nodes}} and {edges, plural, one {# link} other {# links}}", { nodes: shown.nodes.length, edges: shown.edges.length })
          : undefined
      }
    >
      {loading ? (
        <div className="flex h-full items-center justify-center" role="status" aria-label={t_i18n("Loading...")}>
          {/* A full-surface loader (design standards s.0bis(5), UX r1 A2 on #979). */}
          <Spinner size="xl" />
        </div>
      ) : (
        <>
          {/* The fleet graph's toolbar above the canvas (#1062): layout, grouping, focus, the legend. */}
          <AgentGraphToolbar
            layout={layoutChoice}
            layouts={WORK_ITEM_GRAPH_LAYOUTS}
            onLayoutChange={(next) => setLayoutChoice(next as WorkItemGraphLayout)}
            legend={
              <ul className="flex flex-col gap-2" data-testid="work-item-graph-legend">
                <li>{t_i18n("Arrow: unblocks")}</li>
                <li>{t_i18n("Moving dashes: its prerequisite is in progress")}</li>
                <li>{t_i18n("Dashed line: program and its items")}</li>
                <li>{t_i18n("Dashed border: outside the filters")}</li>
                <li>{t_i18n("Red border: only a person can release it")}</li>
              </ul>
            }
          >
            <div className="flex items-center gap-2">
              <span className="content-compact text-default-secondary" aria-hidden="true">
                {t_i18n("Group by")}
              </span>
              <Select value={groupBy} onValueChange={(next) => setGroupBy(next as WorkItemGraphGroupBy)}>
                <SelectTrigger className="w-40" aria-label={t_i18n("Group by")} data-testid="work-item-graph-group-by">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WORK_ITEM_GRAPH_GROUP_BYS.map((key) => (
                    <SelectItem key={key} value={key}>
                      {groupByLabels[key]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              variant="ghost"
              size="sm"
              aria-pressed={showDone}
              onClick={() => setShowDone((value) => !value)}
              data-testid="work-item-graph-show-done"
            >
              <Icon name={showDone ? "eye-off" : "eye"} size={16} aria-hidden />
              {showDone ? t_i18n("Hide done and cancelled") : t_i18n("Show done and cancelled")}
            </Button>
            {selected && (
              <div className="flex items-center gap-2">
                <SegmentedFilter
                  options={FOCUS_MODES.map((key) => ({ key, label: focusLabels[key] }))}
                  value={focusMode}
                  onChange={setFocusMode}
                  ariaLabel={t_i18n("Focus display")}
                  testId="work-item-graph-focus-mode"
                />
                <Button variant="outline" size="sm" onClick={() => setSelected(null)} data-testid="work-item-graph-clear-focus">
                  <Icon name="x" size={16} aria-hidden /> {t_i18n("Clear focus")}
                </Button>
              </div>
            )}
            {graph.truncated && (
              <span className="content-compact text-feedback-warning-tertiary" data-testid="work-item-graph-truncated">
                {t_i18n("Only the first {count, plural, one {# item is} other {# items are}} drawn: narrow the filters to see the rest.", { count: GRAPH_NODE_CAP })}
              </span>
            )}
          </AgentGraphToolbar>
          <div className="graph-canvas relative min-h-0 flex-1" data-testid="work-item-graph-canvas">
            {visible.nodes.length === 0 ? (
              <div className="flex h-full items-center justify-center">
                <EmptyState
                  variant="inset"
                  icon={NetworkIcon}
                  title={t_i18n("No dependency to draw")}
                  description={t_i18n("None of the items these filters match waits for another item, a pull request, a deployment or a person.")}
                />
              </div>
            ) : (
              <ReactFlow
                colorMode={colorMode}
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES}
                edgeTypes={EDGE_TYPES}
                fitView
                fitViewOptions={FIT_VIEW_OPTIONS}
                minZoom={0.1}
                maxZoom={2}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable={false}
                // The pointer side of a node (UX r1 B1 on #979): with a click handler React Flow
                // gives the node wrapper pointer events, so a click selects it (focus mode) and
                // its open button and cut-text tooltips are reachable with the mouse. A zone or a
                // collapsed group is no item: a click there clears the focus, as on the pane.
                onNodeClick={(_, node) => (node.type === "zone" || node.type === "collapsed" ? setSelected(null) : selectNode(node.id))}
                onPaneClick={() => setSelected(null)}
                proOptions={{ hideAttribution: true }}
              >
                <GraphCamera fitKey={fitKey} positions={laid?.positions} />
                <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--flow-grid)" />
                {/* No minimap: the fleet graph has none, and at fit view it covered cards (UX r1 B1 on #1067). */}
                <Controls showInteractive={false} position="bottom-left" />
              </ReactFlow>
            )}
          </div>
          {/* The counters under the canvas, the fleet graph's status line (#1062). */}
          <AgentGraphStatusLine counters={counters} />
        </>
      )}
    </GraphFrame>
  );
}

/**
 * The graph's card: toolbar, canvas and status line, the page's last block, as tall as
 * the window has room for (`useFitToViewport`, the bound the list and grid views take),
 * so the page never scrolls in Graph view (UX r1 B2 on #979). Before the first measure,
 * or outside the app's page, the former cap. The card is the fleet Graph tab's `Card`
 * (Paper at elevation 1), so both canvases resolve the same surface (UX r1 B3 on #1067).
 */
function GraphFrame({ testId, label, children }: { testId?: string; label?: string; children: React.ReactNode }) {
  const [frame, setFrame] = useState<HTMLDivElement | null>(null);
  const room = useFitToViewport(frame);
  return (
    <Card
      ref={setFrame}
      data-testid={testId}
      data-scroll-fit={room === undefined ? "capped" : room === null ? "page" : "viewport"}
      style={typeof room === "number" ? { height: room } : undefined}
      className={cn(
        "relative flex w-full flex-col overflow-hidden",
        typeof room !== "number" && "h-[calc(100dvh-16rem)] min-h-96",
      )}
      role={label ? "group" : undefined}
      aria-label={label}
    >
      {children}
    </Card>
  );
}

/**
 * The camera of the canvas. It fits the graph again when the canvas changes size (UX r1
 * A1 on #979) or the reader changes the drawing (a layout, a grouping, a group collapsed,
 * a focus, done shown, #1062): `fitView` alone runs on mount, and a change of filters
 * reads the graph again behind the loader, so the canvas mounts and fits. A live refresh
 * (#1158) never fits: it keeps the zoom and pans by what the layout moved the node
 * nearest the centre, so what the reader looks at stays where it is.
 */
function GraphCamera({ fitKey, positions }: { fitKey: string; positions: ReadonlyMap<string, GraphPoint> | undefined }) {
  const { fitView, getViewport, setViewport } = useReactFlow();
  const store = useStoreApi();
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  useEffect(() => {
    if (width > 0 && height > 0) void fitView(FIT_VIEW_OPTIONS);
  }, [width, height, fitKey, fitView]);
  const drawn = useRef<{ fitKey: string; positions: ReadonlyMap<string, GraphPoint> } | null>(null);
  useEffect(() => {
    if (!positions) return;
    const last = drawn.current;
    drawn.current = { fitKey, positions };
    // A first drawing or a choice of the reader: the fit above frames it.
    if (!last || last.fitKey !== fitKey || last.positions === positions) return;
    const { width: canvasWidth, height: canvasHeight } = store.getState();
    const next = anchoredViewport(last.positions, positions, getViewport(), { width: canvasWidth, height: canvasHeight });
    if (next) void setViewport(next);
  }, [fitKey, positions, store, getViewport, setViewport]);
  return null;
}
