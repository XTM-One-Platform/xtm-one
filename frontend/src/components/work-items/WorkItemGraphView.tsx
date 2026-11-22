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
 * Tasks > Work Items, Graph view (#968): the items the list's filters match
 * and what they wait for, laid out left to right with dagre (a waiter left of
 * what it waits for). Nodes are items (status colour of the list badges, owner
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
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "dagre";
import { useEffect, useMemo, useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";
import { WaitingForYouBadge } from "@/components/work-items/WaitingForYouBadge";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { InfoTip } from "@/components/ui/info-tip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useColorMode } from "@/hooks/useColorMode";
import { useFitToViewport } from "@/hooks/useFitToViewport";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { PRIORITY_LABELS, STATUS_LABELS, waitsForReader } from "@/lib/work-item-context";
import { criticalLeaves, focusChains, shownGraph } from "@/lib/work-item-graph";
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
  critical: boolean;
  /** The item waits for the reader (the Waiting for me population, #1001): its marker. */
  waitingForYou: boolean;
  onOpen?: (id: string) => void;
  onSelect: (id: string) => void;
};

const HIDDEN_HANDLE = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0 } as const;

/**
 * The canvas's own fit padding, on mount and after a resize. The top side reserves the
 * controls bar inside the frame (top-2 plus its height, room for one wrap) so no node is
 * fitted under it (UX r2 B1 on #979); the other sides keep the 12% margin.
 */
const GRAPH_BAR_INSET_PX = 64;
const FIT_VIEW_OPTIONS = {
  padding: { top: `${GRAPH_BAR_INSET_PX}px`, right: 0.12, bottom: 0.12, left: 0.12 },
} as const;

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

function Handles() {
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} style={HIDDEN_HANDLE} />
      <Handle type="source" position={Position.Right} isConnectable={false} style={HIDDEN_HANDLE} />
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
      <Handles />
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
      <Handles />
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
      <Handles />
      <Icon name="users" size={16} className="shrink-0 text-feedback-warning-tertiary" aria-hidden />
      <TruncatedText text={name} className="content-compact text-default-primary" />
    </div>
  );
}

const NODE_TYPES = { item: ItemNode, ref: RefNode, person: PersonNode };

function NetworkIcon({ className }: { className?: string }) {
  return <Icon name="network" size={16} className={className} aria-hidden />;
}

function nodeSize(node: WorkItemGraphNode): { width: number; height: number } {
  if (node.kind === "item") return { width: ITEM_W, height: ITEM_H + (node.ask ? ASK_H : 0) };
  return { width: SMALL_W, height: SMALL_H };
}

/** Dagre, left to right: a waiter left of what it waits for, a program root left of its children. */
function layout(nodes: WorkItemGraphNode[], edges: { source: string; target: string }[]) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", ranksep: 90, nodesep: 28, edgesep: 16 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) g.setNode(node.id, nodeSize(node));
  for (const edge of edges) g.setEdge(edge.source, edge.target);
  dagre.layout(g);
  return new Map(
    nodes.map((node) => {
      const { x, y } = g.node(node.id);
      const { width, height } = nodeSize(node);
      return [node.id, { x: Math.round(x - width / 2), y: Math.round(y - height / 2) }] as const;
    }),
  );
}

const EDGE_STROKE = {
  blocked_on: "var(--text-default-secondary)",
  ref: "var(--text-default-secondary)",
  person: "var(--color-feedback-alert-primary, var(--text-default-secondary))",
  program: "var(--color-accent, var(--text-default-secondary))",
} as const;

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
  const [showDone, setShowDone] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  // A second select of the selected node clears the focus.
  const selectNode = (id: string) => setSelected((current) => (current === id ? null : id));

  const shown = useMemo(() => (graph ? shownGraph(graph, { showDone }) : null), [graph, showDone]);
  const positions = useMemo(() => (shown ? layout(shown.nodes, shown.edges) : new Map()), [shown]);
  const critical = useMemo(() => (shown ? criticalLeaves(shown) : new Set<string>()), [shown]);
  const chains = useMemo(
    () => (shown && selected && shown.nodes.some((n) => n.id === selected) ? focusChains(shown.edges, selected) : null),
    [shown, selected],
  );

  const nodes = useMemo<Node<GraphNodeData>[]>(() => {
    if (!shown) return [];
    return shown.nodes.map((node) => {
      let focus: FocusState = "none";
      if (chains && selected) {
        if (node.id === selected) focus = "selected";
        else if (chains.upstream.has(node.id) || chains.downstream.has(node.id)) focus = "lit";
        else focus = "dim";
      }
      const size = nodeSize(node);
      return {
        id: node.id,
        type: node.kind,
        position: positions.get(node.id) ?? { x: 0, y: 0 },
        width: size.width,
        height: size.height,
        data: {
          node,
          focus,
          critical: critical.has(node.id),
          waitingForYou: node.kind === "item" && !node.outside_filter && waitsForReader(node, readerId),
          onOpen: node.kind === "item" ? onOpenItem : undefined,
          onSelect: selectNode,
        },
        draggable: false,
        connectable: false,
        selectable: false,
        focusable: false,
      };
    });
  }, [shown, positions, chains, selected, critical, onOpenItem, readerId]);

  const edges = useMemo<Edge[]>(() => {
    if (!shown) return [];
    return shown.edges.map((edge) => {
      const dim = chains ? !chains.edges.has(edge.id) : false;
      const stroke = EDGE_STROKE[edge.kind];
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: "smoothstep",
        markerEnd: edge.kind === "program" ? undefined : { type: MarkerType.ArrowClosed, color: stroke, width: 16, height: 16 },
        style: {
          stroke,
          strokeWidth: chains && !dim ? 2 : 1.25,
          strokeOpacity: dim ? 0.2 : 0.9,
          strokeDasharray: edge.kind === "program" ? "6 4" : edge.kind === "ref" ? "2 3" : undefined,
        },
        selectable: false,
        focusable: false,
        data: { kind: edge.kind },
      };
    });
  }, [shown, chains]);

  const loading = isLoading || !graph || !shown;
  return (
    <GraphFrame
      testId={loading ? undefined : "work-item-graph"}
      label={
        shown && !loading
          ? t_i18n("Graph of {nodes, plural, one {# node} other {# nodes}} and {edges, plural, one {# link} other {# links}}", { nodes: shown.nodes.length, edges: shown.edges.length })
          : undefined
      }
    >
      {isLoading || !graph || !shown ? (
        <div className="flex h-full items-center justify-center" role="status" aria-label={t_i18n("Loading...")}>
          {/* A full-surface loader (design standards s.0bis(5), UX r1 A2 on #979). */}
          <Spinner size="xl" />
        </div>
      ) : (
        <>
          {/* The canvas controls sit in a bar inside the canvas frame, never in a row between
              the toolbar and the canvas or under it (UX r1 B2 and B3 on #979). */}
          <div className="pointer-events-none absolute inset-x-2 top-2 z-10 flex items-start gap-2" data-testid="work-item-graph-bar">
            <div className="pointer-events-auto flex min-w-0 flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-1.5 py-1 content-compact text-default-secondary">
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
              {shown.isolated > 0 && (
                <span data-testid="work-item-graph-isolated">
                  {t_i18n("{count, plural, one {# item with no dependency, in the list view} other {# items with no dependency, in the list view}}", { count: shown.isolated })}
                </span>
              )}
              {graph.truncated && (
                <span className="text-feedback-warning-tertiary" data-testid="work-item-graph-truncated">
                  {t_i18n("Only the first {count, plural, one {# item is} other {# items are}} drawn: narrow the filters to see the rest.", { count: GRAPH_NODE_CAP })}
                </span>
              )}
              <InfoTip
                label={t_i18n("Legend")}
                content={
                  <ul className="space-y-1" data-testid="work-item-graph-legend">
                    <li>{t_i18n("Arrow: waits for")}</li>
                    <li>{t_i18n("Dashed line: program and its items")}</li>
                    <li>{t_i18n("Dashed border: outside the filters")}</li>
                    <li>{t_i18n("Red border: only a person can release it")}</li>
                  </ul>
                }
              />
            </div>
            {selected && (
              <Button
                variant="outline"
                size="sm"
                className="pointer-events-auto ms-auto"
                onClick={() => setSelected(null)}
                data-testid="work-item-graph-clear-focus"
              >
                <Icon name="x" size={16} aria-hidden /> {t_i18n("Clear focus")}
              </Button>
            )}
          </div>
          {shown.nodes.length === 0 ? (
            <div className="flex h-full items-center justify-center pt-12">
              <EmptyState
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
              fitView
              fitViewOptions={FIT_VIEW_OPTIONS}
              minZoom={0.1}
              maxZoom={2}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              // The pointer side of a node (UX r1 B1 on #979): with a click handler React Flow
              // gives the node wrapper pointer events, so a click selects it (focus mode) and
              // its open button and cut-text tooltips are reachable with the mouse.
              onNodeClick={(_, node) => selectNode(node.id)}
              onPaneClick={() => setSelected(null)}
              proOptions={{ hideAttribution: true }}
              style={{ background: "transparent" }}
            >
              <FitOnResize />
              <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--flow-grid)" />
              <Controls showInteractive={false} position="bottom-left" />
              <MiniMap pannable zoomable position="bottom-right" />
            </ReactFlow>
          )}
        </>
      )}
    </GraphFrame>
  );
}

/**
 * The canvas frame: the page's last block, as tall as the window has room for
 * (`useFitToViewport`, the bound the list and grid views take), so the page never
 * scrolls in Graph view (UX r1 B2 on #979). Before the first measure, or outside the
 * app's page, the former cap.
 */
function GraphFrame({ testId, label, children }: { testId?: string; label?: string; children: React.ReactNode }) {
  const [frame, setFrame] = useState<HTMLDivElement | null>(null);
  const room = useFitToViewport(frame);
  return (
    <div
      ref={setFrame}
      data-testid={testId}
      data-scroll-fit={room === undefined ? "capped" : room === null ? "page" : "viewport"}
      style={typeof room === "number" ? { height: room } : undefined}
      className={cn(
        "relative w-full overflow-hidden rounded-md border border-elevation-subtle",
        typeof room !== "number" && "h-[calc(100dvh-16rem)] min-h-96",
      )}
      role={label ? "group" : undefined}
      aria-label={label}
    >
      {children}
    </div>
  );
}

/** Fits the graph again when the canvas changes size (UX r1 A1 on #979): `fitView` alone runs on mount. */
function FitOnResize() {
  const { fitView } = useReactFlow();
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  useEffect(() => {
    if (width > 0 && height > 0) void fitView(FIT_VIEW_OPTIONS);
  }, [width, height, fitView]);
  return null;
}
