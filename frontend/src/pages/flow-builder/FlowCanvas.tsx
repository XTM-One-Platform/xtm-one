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
 * The builder canvas.
 *
 * Owns the React Flow instance: rendering, connection validation, drop target,
 * minimap, zoom/pan controls and the selection box. It holds no state of its
 * own — everything comes from `useFlowEditor` in the shell above, which is what
 * makes undo/redo a single stack rather than one per surface.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ViewportPortal,
  useReactFlow,
  type Connection,
  type IsValidConnection,
  type NodeChange,
  type EdgeChange,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./flow-builder.css";
import { Copy, Plus, Settings2, Trash2, Waypoints, OctagonPause } from "lucide-react";
import { cn } from "@/lib/utils";
import { useColorMode } from "@/hooks/useColorMode";
import { HANDOVER_TYPES, HANDOVER_TYPE_CONFIG } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { FlowNodeKind, FlowValidationIssue } from "@/types";
import { FlowNodeCard, type FlowNodeExtras } from "./FlowNode";
import { TypedEdge } from "./TypedEdge";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { PointMenu } from "@/components/ui/point-menu";
import { checkConnection, defaultEdgeKind, findPort } from "./connection-rules";
import {
  EDGE_COMPONENT_TYPE,
  NODE_COMPONENT_TYPE,
  nextEdgeId,
  nextNodeId,
  type CanvasEdge,
  type CanvasNode,
} from "./graph-adapter";
import { categoryMinimapColor, LOOP_BODY_PORTS } from "./node-visuals";
import { getHelperLines, type HelperLinesResult } from "./helper-lines";

/** What the open context menu is anchored to. */
type ContextTarget =
  | { kind: "pane"; screen: { x: number; y: number } }
  | { kind: "node"; nodeId: string }
  | { kind: "edge"; edgeId: string };

/** The handle a drop-to-create connection was dragged from. */
export interface PendingConnection {
  nodeId: string;
  handleId: string | null;
  handleType: "source" | "target";
}

/** Screen coordinates of a mouse/touch event, or null if unavailable. */
function eventClientPos(
  e: MouseEvent | TouchEvent,
): { x: number; y: number } | null {
  if ("clientX" in e) return { x: e.clientX, y: e.clientY };
  const touch = e.changedTouches?.[0];
  return touch ? { x: touch.clientX, y: touch.clientY } : null;
}

const nodeTypes = { [NODE_COMPONENT_TYPE]: FlowNodeCard };
const edgeTypes = { [EDGE_COMPONENT_TYPE]: TypedEdge };

/** Payload a library-panel drag carries. */
export const NODE_DRAG_MIME = "application/x-xtm-flow-node";

interface FlowCanvasProps {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  viewport: Viewport;
  kindsById: Map<string, FlowNodeKind>;
  issues: FlowValidationIssue[];
  /** Short per-node config summaries, keyed by node id. */
  summaries: Map<string, string>;
  /**
   * Execution overlay: per-node status of the run being
   * inspected. Transient — injected into extras, never persisted.
   */
  executionByNode?: Map<string, FlowNodeExtras["execution"]>;
  readOnly?: boolean;
  /** The dotted guide grid — a toolbar toggle, not a permanent fixture. */
  showGrid?: boolean;
  /** Alignment guides while dragging (lines + magnetic snap) — a toolbar
   *  toggle: constraining every drag is not everyone's taste. */
  snapGuides?: boolean;
  onNodesChange: (changes: NodeChange<CanvasNode>[]) => void;
  onEdgesChange: (changes: EdgeChange<CanvasEdge>[]) => void;
  onViewportChange: (viewport: Viewport) => void;
  onCommit: (next: { nodes?: CanvasNode[]; edges?: CanvasEdge[] }) => void;
  onCommitCurrent: () => void;
  onSelectNode: (nodeId: string | null) => void;
  /** Click on an edge — opens the edge property drawer. */
  onSelectEdge?: (edgeId: string | null) => void;
  /** Write-through for inputs embedded on node cards (Chat test message). */
  onNodeConfigChange?: (nodeId: string, key: string, value: string) => void;
  /** Fired when any drag ends — group membership follows position. */
  onNodeDragStop?: () => void;
  /** Drop a node kind at a flow-space position (library drag, quick add). */
  onAddNode: (kind: string, position: { x: number; y: number }) => void;
  /** Double-click / Space on empty canvas — opens the quick-add search. */
  onQuickAdd: (screenPosition: { x: number; y: number }) => void;
  /**
   * A connection dropped into empty canvas — opens quick-add so the picked node
   * lands already wired to the handle it was dragged from.
   */
  onQuickAddFromConnection?: (
    screenPosition: { x: number; y: number },
    source: PendingConnection,
  ) => void;
}

export function FlowCanvas({
  nodes,
  edges,
  viewport,
  kindsById,
  issues,
  summaries,
  executionByNode,
  readOnly = false,
  showGrid = false,
  snapGuides = false,
  onNodesChange,
  onEdgesChange,
  onViewportChange,
  onCommit,
  onCommitCurrent,
  onSelectNode,
  onSelectEdge,
  onNodeConfigChange,
  onNodeDragStop,
  onAddNode,
  onQuickAdd,
  onQuickAddFromConnection,
}: FlowCanvasProps) {
  const { screenToFlowPosition } = useReactFlow();
  const { t_i18n } = useFormatter();
  // React Flow stamps `light`/`dark` on its own container (default `light`),
  // and the design system keys every elevation token on that class — so a
  // `light` canvas drags the light palette into every node whatever the app
  // theme, turning the cards white in dark mode. Tie it to the
  // app's mode so the canvas inherits the matching token set.
  const colorMode = useColorMode();
  const wrapperRef = useRef<HTMLDivElement>(null);
  // Why a connection was refused, shown next to the cursor. React Flow can only
  // say yes/no through `isValidConnection`, so the explanation is captured here
  // as the check runs.
  const [refusal, setRefusal] = useState<string | null>(null);
  // Alignment guides while a node drags (flow-space coordinates).
  const [helperLines, setHelperLines] = useState<HelperLinesResult>({});

  // The right-click menu: position (in wrapper space) + what it acts on.
  const [menu, setMenu] = useState<{
    x: number;
    y: number;
    target: ContextTarget;
  } | null>(null);

  // Issues are addressed to a node id; group them once instead of filtering the
  // whole list inside every node's render.
  const issuesByNode = useMemo(() => {
    const map = new Map<string, FlowValidationIssue[]>();
    for (const issue of issues) {
      if (!issue.node_id) continue;
      const list = map.get(issue.node_id);
      if (list) list.push(issue);
      else map.set(issue.node_id, [issue]);
    }
    return map;
  }, [issues]);

  // Which input handles of each node currently have an edge landing on them —
  // drives the "accessory socket shown green once connected" affordance.
  const connectedHandlesByNode = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const edge of edges) {
      if (!edge.target) continue;
      const handle = edge.targetHandle ?? "in";
      const set = map.get(edge.target);
      if (set) set.add(handle);
      else map.set(edge.target, new Set([handle]));
    }
    return map;
  }, [edges]);

  // Nodes inside a loop's body — everything reachable from a looping node's
  // body port (For each's `each`, While's `body`), following flow edges
  // (capability wiring is not control flow). These re-execute on every pass,
  // and the double outline is what makes that branch legible at a glance.
  // The walk never re-enters a loop origin: While bodies wire BACK into the
  // While, and traversing through it would bleed the marking past `done`.
  const loopBranchNodes = useMemo(() => {
    const starts: string[] = [];
    const origins = new Set<string>();
    for (const edge of edges) {
      const source = nodes.find((n) => n.id === edge.source);
      const kind = source?.data.kind;
      if (!kind || !edge.target) continue;
      if ((edge.sourceHandle ?? "") !== LOOP_BODY_PORTS[kind]) continue;
      origins.add(edge.source);
      starts.push(edge.target);
    }
    const inBranch = new Set<string>();
    const queue = starts.filter((id) => !origins.has(id));
    while (queue.length > 0) {
      const nodeId = queue.pop()!;
      if (inBranch.has(nodeId)) continue;
      inBranch.add(nodeId);
      for (const edge of edges) {
        if (edge.source !== nodeId || !edge.target) continue;
        if (edge.data?.kind === "capability") continue;
        if (origins.has(edge.target)) continue;
        if (!inBranch.has(edge.target)) queue.push(edge.target);
      }
    }
    return inBranch;
  }, [nodes, edges]);

  // Source-side counterpart: which output handles have an edge leaving them.
  // A Switch uses it to keep its legacy single `case` socket rendered only
  // while an old-style edge still hangs off it.
  const connectedSourceHandlesByNode = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const edge of edges) {
      if (!edge.source) continue;
      const handle = edge.sourceHandle ?? "out";
      const set = map.get(edge.source);
      if (set) set.add(handle);
      else map.set(edge.source, new Set([handle]));
    }
    return map;
  }, [edges]);

  const decoratedNodes = useMemo(
    () =>
      nodes.map((node) => {
        const extras: FlowNodeExtras = {
          kindDef: kindsById.get(node.data.kind),
          issues: issuesByNode.get(node.id) ?? [],
          summary: summaries.get(node.id),
          execution: executionByNode?.get(node.id),
          connectedHandles: connectedHandlesByNode.get(node.id),
          connectedSourceHandles: connectedSourceHandlesByNode.get(node.id),
          inLoopBranch: loopBranchNodes.has(node.id),
          onEmbeddedConfigChange: readOnly ? undefined : onNodeConfigChange,
        };
        return { ...node, data: { ...node.data, extras }, draggable: !readOnly };
      }),
    [
      nodes,
      kindsById,
      issuesByNode,
      summaries,
      executionByNode,
      connectedHandlesByNode,
      connectedSourceHandlesByNode,
      loopBranchNodes,
      onNodeConfigChange,
      readOnly,
    ],
  );

  // The same marking for the branch's EDGES: an edge leaving the loop's
  // `each` port, or leaving any node inside the body, draws an orange
  // under-stroke — the link version of the nodes' double outline. Display
  // only: the decoration never reaches the persisted graph.
  const decoratedEdges = useMemo(
    () =>
      edges.map((edge) => {
        if (edge.data?.kind === "capability") return edge;
        const source = nodes.find((n) => n.id === edge.source);
        const fromEach =
          source != null &&
          (edge.sourceHandle ?? "") === LOOP_BODY_PORTS[source.data.kind];
        const inBody = loopBranchNodes.has(edge.source);
        // Object-typed constants wire in green (the UE struct colour).
        const objectTyped =
          edge.data?.kind === "value" &&
          source?.data.kind === "util.primitive" &&
          (source.data.config as Record<string, unknown> | undefined)
            ?.value_type === "object";
        // Live pulse: the wire feeding the RUNNING node from a completed
        // source is the execution front — animate it (UE's exec-wire pulse).
        const live =
          executionByNode?.get(edge.target) === "running" &&
          executionByNode?.get(edge.source) === "completed";
        if (!fromEach && !inBody && !live && !objectTyped) return edge;
        return {
          ...edge,
          data: {
            ...(edge.data ?? { kind: "data" as const }),
            ...(fromEach || inBody ? { inLoopBranch: true } : {}),
            ...(live ? { live: true } : {}),
            ...(objectTyped ? { objectTyped: true } : {}),
          },
        };
      }),
    [edges, nodes, loopBranchNodes, executionByNode],
  );

  const isValidConnection = useCallback<IsValidConnection<CanvasEdge>>(
    (connection) => {
      const source = nodes.find((n) => n.id === connection.source);
      const target = nodes.find((n) => n.id === connection.target);
      const result = checkConnection({
        sourceKind: source && kindsById.get(source.data.kind),
        sourcePortId: connection.sourceHandle,
        targetKind: target && kindsById.get(target.data.kind),
        targetPortId: connection.targetHandle,
        sourceNodeId: connection.source ?? "",
        targetNodeId: connection.target ?? "",
        existing: edges.map((e) => ({
          target: e.target,
          target_handle: e.targetHandle ?? "in",
        })),
      });
      setRefusal(result.ok ? null : (result.reason ?? null));
      return result.ok;
    },
    [nodes, edges, kindsById],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const source = nodes.find((n) => n.id === connection.source);
      const target = nodes.find((n) => n.id === connection.target);
      const sourcePort = findPort(
        source && kindsById.get(source.data.kind),
        connection.sourceHandle,
        "output",
      );
      const targetPort = findPort(
        target && kindsById.get(target.data.kind),
        connection.targetHandle,
        "input",
      );
      const kind = defaultEdgeKind(sourcePort, targetPort);
      // `isValidConnection` already refused anything without a legal kind; this
      // is the belt that keeps a malformed edge out of the graph regardless.
      if (!kind) return;

      const edge: CanvasEdge = {
        id: nextEdgeId(new Set(edges.map((e) => e.id))),
        type: EDGE_COMPONENT_TYPE,
        source: connection.source,
        sourceHandle: connection.sourceHandle,
        target: connection.target,
        targetHandle: connection.targetHandle,
        data: { kind },
        // A handover is always one of the four types; default to delegate so
        // the edge is typed from the moment it is drawn (right-click to change).
        ...(kind === "handover" ? { label: "delegate" } : {}),
      };

      // Plugging a Chat start point into an agent IS making it conversational:
      // flip the node's "Enable chat" toggle in the same commit, so the wiring
      // and the setting can never disagree.
      let nextNodes: CanvasNode[] | undefined;
      // A `value` connection carries a constant into the node: auto-insert
      // the reference into the target's first EMPTY variable field, so the
      // wire and the template can never disagree (the UE data pin).
      if (kind === "value" && source && target) {
        const ref =
          source.data.kind === "util.primitive"
            ? `{{${source.id}.value}}`
            : connection.sourceHandle?.startsWith("param:")
              ? `{{${source.id}.payload.${connection.sourceHandle.slice("param:".length)}}}`
              : null;
        const targetDef = kindsById.get(target.data.kind);
        const firstEmpty = targetDef?.config_fields.find(
          (f) =>
            (f.type === "variable_text" || f.type === "variable_textarea") &&
            !String(target.data.config?.[f.name] ?? "").trim(),
        );
        if (ref && firstEmpty) {
          nextNodes = nodes.map((n) =>
            n.id === target.id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    config: { ...n.data.config, [firstEmpty.name]: ref },
                  },
                }
              : n,
          );
        }
      }
      if (
        source?.data.kind === "trigger.chat" &&
        target?.data.kind === "agent" &&
        !target.data.config?.chat_enabled
      ) {
        nextNodes = nodes.map((n) =>
          n.id === target.id
            ? {
                ...n,
                data: {
                  ...n.data,
                  config: { ...n.data.config, chat_enabled: true },
                },
              }
            : n,
        );
      }
      onCommit({ edges: [...edges, edge], nodes: nextNodes });
    },
    [nodes, edges, kindsById, onCommit],
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const kind = event.dataTransfer.getData(NODE_DRAG_MIME);
      if (!kind) return;
      onAddNode(
        kind,
        screenToFlowPosition({ x: event.clientX, y: event.clientY }),
      );
    },
    [onAddNode, screenToFlowPosition],
  );

  // ── Right-click context menu ────────────────────────────────────────────
  const menuTarget = menu?.target;
  const menuOnHandover =
    menuTarget?.kind === "edge" &&
    edges.find((e) => e.id === menuTarget.edgeId)?.data?.kind === "handover";

  const openMenu = useCallback(
    (event: React.MouseEvent, target: ContextTarget) => {
      event.preventDefault();
      const rect = wrapperRef.current?.getBoundingClientRect();
      setMenu({
        x: event.clientX - (rect?.left ?? 0),
        y: event.clientY - (rect?.top ?? 0),
        target,
      });
    },
    [],
  );

  const duplicateNode = useCallback(
    (nodeId: string) => {
      const node = nodes.find((n) => n.id === nodeId);
      if (!node) return;
      const id = nextNodeId(node.data.kind, new Set(nodes.map((n) => n.id)));
      onCommit({
        nodes: [
          ...nodes.map((n) => ({ ...n, selected: false })),
          {
            ...node,
            id,
            selected: true,
            position: { x: node.position.x + 40, y: node.position.y + 40 },
          },
        ],
      });
    },
    [nodes, onCommit],
  );

  const deleteNode = useCallback(
    (nodeId: string) => {
      onCommit({
        nodes: nodes.filter((n) => n.id !== nodeId),
        // Drop edges dangling off the removed node, or the graph is malformed.
        edges: edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      });
    },
    [nodes, edges, onCommit],
  );

  const deleteEdge = useCallback(
    (edgeId: string) => onCommit({ edges: edges.filter((e) => e.id !== edgeId) }),
    [edges, onCommit],
  );

  // A handover's type rides in the edge label (round-trips through the graph),
  // so setting the type is a one-key edit of that one edge.
  const setHandoverType = useCallback(
    (edgeId: string, value: string) => {
      onCommit({
        edges: edges.map((e) => (e.id === edgeId ? { ...e, label: value } : e)),
      });
    },
    [edges, onCommit],
  );

  // The rows of the right-click menu, on the design-system menu (`PointMenu`):
  // the same items, order and actions as the hand-rolled menu it
  // replaced. A group starts with `separatorBefore`; the edge menu's
  // "Handover type" heading goes in the menu's header.
  const menuItems = useMemo<OverflowMenuItem[]>(() => {
    if (!menu) return [];
    const target = menu.target;

    if (target.kind === "pane") {
      return [
        {
          id: "add",
          label: t_i18n("Add node here"),
          icon: Plus,
          disabled: readOnly,
          onSelect: () => onQuickAdd(target.screen),
        },
      ];
    }

    if (target.kind === "node") {
      return [
        {
          id: "configure",
          label: t_i18n("Configure"),
          icon: Settings2,
          onSelect: () => onSelectNode(target.nodeId),
        },
        {
          id: "duplicate",
          label: t_i18n("Duplicate"),
          icon: Copy,
          disabled: readOnly,
          onSelect: () => duplicateNode(target.nodeId),
        },
        {
          id: "breakpoint",
          label: nodes.find((n) => n.id === target.nodeId)?.data.config
            ?.breakpoint
            ? t_i18n("Remove breakpoint")
            : t_i18n("Add breakpoint"),
          icon: OctagonPause,
          disabled: readOnly,
          onSelect: () => {
            const node = nodes.find((n) => n.id === target.nodeId);
            if (!node) return;
            onCommit({
              nodes: nodes.map((n) =>
                n.id === target.nodeId
                  ? {
                      ...n,
                      data: {
                        ...n.data,
                        config: {
                          ...n.data.config,
                          breakpoint: !n.data.config?.breakpoint,
                        },
                      },
                    }
                  : n,
              ),
            });
          },
        },
        {
          id: "delete",
          label: t_i18n("Delete"),
          icon: Trash2,
          destructive: true,
          separatorBefore: true,
          disabled: readOnly,
          onSelect: () => deleteNode(target.nodeId),
        },
      ];
    }

    // Edge.
    const edge = edges.find((e) => e.id === target.edgeId);
    const items: OverflowMenuItem[] = [];
    const handover = edge?.data?.kind === "handover";
    if (handover) {
      const current =
        typeof edge.label === "string" && edge.label ? edge.label : "delegate";
      for (const value of HANDOVER_TYPES) {
        items.push({
          id: value,
          label: t_i18n(HANDOVER_TYPE_CONFIG[value].label),
          icon: Waypoints,
          checked: current === value,
          disabled: readOnly,
          onSelect: () => setHandoverType(target.edgeId, value),
        });
      }
    }
    items.push({
      id: "delete",
      label: t_i18n("Delete edge"),
      icon: Trash2,
      destructive: true,
      separatorBefore: handover,
      disabled: readOnly,
      onSelect: () => deleteEdge(target.edgeId),
    });
    return items;
  }, [
    menu,
    edges,
    nodes,
    onCommit,
    readOnly,
    t_i18n,
    onQuickAdd,
    onSelectNode,
    duplicateNode,
    deleteNode,
    deleteEdge,
    setHandoverType,
  ]);

  return (
    <div
      ref={wrapperRef}
      className="relative h-full w-full flow-canvas"
      // React Flow has no pane double-click event, and listening on a node
      // would open quick-add when the user meant to rename. Firing only when
      // the target IS the pane keeps it to empty canvas.
      onDoubleClick={(event) => {
        const target = event.target as HTMLElement;
        if (!target.classList.contains("react-flow__pane")) return;
        onQuickAdd({ x: event.clientX, y: event.clientY });
      }}
    >
      <ReactFlow
        colorMode={colorMode}
        nodes={decoratedNodes}
        edges={decoratedEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        defaultViewport={viewport}
        onNodesChange={(changes) => {
          // Helper lines: single-node drags snap to neighbours' edges/centers.
          setHelperLines({});
          if (
            snapGuides &&
            changes.length === 1 &&
            changes[0].type === "position" &&
            changes[0].dragging &&
            changes[0].position
          ) {
            const lines = getHelperLines(changes[0], nodes);
            if (lines.snapX !== undefined) changes[0].position.x = lines.snapX;
            if (lines.snapY !== undefined) changes[0].position.y = lines.snapY;
            setHelperLines(lines);
          }
          onNodesChange(changes);
        }}
        onEdgesChange={onEdgesChange}
        onMoveEnd={(_e, next) => onViewportChange(next)}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onConnectEnd={(event, connectionState) => {
          setRefusal(null);
          // Landing on a node/handle is a normal connect (or an already-shown
          // refusal). Only a drop into empty canvas offers to create a node.
          if (readOnly || connectionState.toNode) return;
          const from = connectionState.fromHandle;
          const pos = eventClientPos(event);
          if (!from?.nodeId || !pos) return;
          onQuickAddFromConnection?.(pos, {
            nodeId: from.nodeId,
            handleId: from.id ?? null,
            handleType: from.type ?? "source",
          });
        }}
        // A drag fires a change per animation frame; recording history once the
        // gesture ends is what keeps Ctrl+Z from rewinding a single pixel.
        onNodeDragStart={onCommitCurrent}
        onNodeDragStop={() => onNodeDragStop?.()}
        onNodeClick={(_e, node) => onSelectNode(node.id)}
        onEdgeClick={(_e, edge) => onSelectEdge?.(edge.id)}
        onPaneClick={() => {
          onSelectNode(null);
          onSelectEdge?.(null);
        }}
        onPaneContextMenu={(event) =>
          openMenu(event as React.MouseEvent, {
            kind: "pane",
            screen: {
              x: (event as React.MouseEvent).clientX,
              y: (event as React.MouseEvent).clientY,
            },
          })
        }
        onNodeContextMenu={(event, node) =>
          openMenu(event, { kind: "node", nodeId: node.id })
        }
        onEdgeContextMenu={(event, edge) =>
          openMenu(event, { kind: "edge", edgeId: edge.id })
        }
        // Double-click opens quick-add (see the wrapper's handler), so the
        // built-in double-click-to-zoom would fight it.
        zoomOnDoubleClick={false}
        onDrop={onDrop}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
        }}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        // Left-drag pans the canvas (the expected "click and drag"); hold Shift
        // and drag to box-select. The right button stays free for the context
        // menu, so it never fights panning.
        panOnDrag
        selectionOnDrag={false}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={["Meta", "Shift"]}
        // Deletion is handled by the shell's own shortcut so it lands in the
        // history stack; React Flow's built-in delete would bypass it.
        deleteKeyCode={null}
        proOptions={{ hideAttribution: true }}
        minZoom={0.1}
        maxZoom={2}
      >
        {showGrid && (
          <Background
            variant={BackgroundVariant.Dots}
            gap={20}
            size={1}
            color="var(--flow-grid)"
          />
        )}
        {(helperLines.vertical !== undefined ||
          helperLines.horizontal !== undefined) && (
          <ViewportPortal>
            {helperLines.vertical !== undefined && (
              <div
                className="pointer-events-none absolute"
                style={{
                  left: helperLines.vertical,
                  top: -100000,
                  width: 1,
                  height: 200000,
                  background: "var(--flow-helper-line)",
                }}
              />
            )}
            {helperLines.horizontal !== undefined && (
              <div
                className="pointer-events-none absolute"
                style={{
                  top: helperLines.horizontal,
                  left: -100000,
                  height: 1,
                  width: 200000,
                  background: "var(--flow-helper-line)",
                }}
              />
            )}
          </ViewportPortal>
        )}
        <Controls showInteractive={false} position="bottom-left" />
        <MiniMap
          pannable
          zoomable
          position="bottom-right"
          className="!bottom-4 !right-4"
          nodeColor={(node) => {
            const kind = String((node as CanvasNode).data?.kind ?? "");
            return categoryMinimapColor(kindsById.get(kind)?.category, kind);
          }}
        />
      </ReactFlow>

      {refusal && (
        <div
          className={cn(
            "pointer-events-none absolute left-1/2 top-4 z-10 -translate-x-1/2",
            "rounded-sm border border-feedback-error-primary/40 layer-1 bg-elevation-default px-3 py-1.5 content-compact text-feedback-error-primary shadow-overlay",
          )}
          role="status"
        >
          {refusal}
        </div>
      )}

      <PointMenu
        at={menu ? { x: menu.x, y: menu.y } : null}
        items={menuItems}
        onClose={() => setMenu(null)}
        label={
          menu?.target.kind === "node"
            ? t_i18n("Node actions")
            : menu?.target.kind === "edge"
              ? t_i18n("Edge actions")
              : t_i18n("Canvas actions")
        }
        header={menuOnHandover ? t_i18n("Handover type") : undefined}
      />
    </div>
  );
}
