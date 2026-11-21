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
 * FlowBuilder — the visual flow composition surface.
 *
 * Full-height three-pane layout: node library, canvas, config drawer. Lives on
 * `/flows/library/:id` rather than as a tab of `/flows` so the canvas gets the
 * whole viewport — a builder squeezed under a page title and a tab bar is the
 * thing this feature exists to stop being.
 *
 * All editor state lives in `useFlowEditor` (one reducer, one undo stack).
 * Server state — the flow, the node catalog, the version list — stays in
 * TanStack Query.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Sparkles,
} from "lucide-react";
import { ReactFlowProvider, useNodesInitialized, useReactFlow } from "@xyflow/react";
import { PageSkeleton } from "@/components/ui/skeleton";
import RouteNotFound from "@/pages/RouteNotFound";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { OwnerDeactivatedNotice } from "@/components/ui/owner-deactivated-notice";
import { SharedBanner } from "@/components/ui/shared-banner";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useToast } from "@/components/ui/toast";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useCommandHandler } from "@/lib/commands/CommandsProvider";
import { useAuth } from "@/hooks/useAuth";
import {
  useActivateFlow,
  useDeactivateFlow,
  useFlow,
  useFlowNodeCatalog,
  useFlowRunDetail,
  useDetachFlow,
  useImportFlow,
  useRestoreFlowVersion,
  uploadFlowRunFile,
  useRunFlow,
  useUpdateFlow,
} from "@/hooks/api/useFlowApi";
import { canManageEntity } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type {
  FlowActivateResult,
  FlowNodeKind,
  FlowValidationIssue,
} from "@/types";
import { BuilderToolbar } from "./BuilderToolbar";
import { CatalogRefreshNotice } from "./CatalogRefreshNotice";
import { FlowCanvas, type PendingConnection } from "./FlowCanvas";
import { declaredParameters } from "./params";
import {
  loadSnippets,
  removeSnippet,
  saveSnippet,
  type FlowSnippet,
} from "./snippets";
import type { FlowNodeExtras } from "./FlowNode";
import { EdgeConfigPanel } from "./EdgeConfigPanel";
import { NodeConfigPanel } from "./NodeConfigPanel";
import { NodeLibraryPanel } from "./NodeLibraryPanel";
import { QuickAddMenu } from "./QuickAddMenu";
import { defaultEdgeKind, findPort } from "./connection-rules";
import { ActivationInfoDialog, RunParamsDialog } from "./RunDialogs";
import { HealthPanel } from "./HealthPanel";
import { RunsPanel } from "./RunsPanel";
import { SelectionToolbar } from "./SelectionToolbar";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import { FlowOwnershipPanel } from "./FlowOwnershipPanel";
import { FLOW_PANEL_PARAM, ownerAccessUrl, wantsOwnerAccessPanel } from "./panel-param";
import { autoLayout } from "./auto-layout";
import {
  EDGE_COMPONENT_TYPE,
  EMPTY_GRAPH,
  NODE_COMPONENT_TYPE,
  fromCanvas,
  nextEdgeId,
  nextNodeId,
  toCanvasEdges,
  toCanvasNodes,
  type CanvasEdge,
  type CanvasNode,
} from "./graph-adapter";
import type { Agent } from "@/types";
import { summaryValue } from "./node-summary";
import { useFlowEditor } from "./useFlowEditor";
import { useOutputShapes } from "./useOutputShapes";

/** True when the event came from a text field — shortcuts must not fire there. */
function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  const tag = element.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    element.isContentEditable
  );
}

function BuilderInner({ flowId }: { flowId: string }) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const { screenToFlowPosition, getViewport, setViewport, fitView, zoomIn, zoomOut } =
    useReactFlow();
  // A flow with no stored viewport is framed once React Flow has measured its
  // nodes: `fitView` on the next frame saw only the first ones measured and
  // framed a corner of the graph (an imported flow opened zoomed on one node).
  const nodesInitialized = useNodesInitialized();
  const pendingFitRef = useRef(false);

  const { data: flow, isLoading } = useFlow(flowId, { notFoundIsOrdinary: true });
  const {
    data: catalog, isRefetchError: catalogStale, isFetching: catalogFetching, refetch: refetchCatalog,
  } = useFlowNodeCatalog();
  const updateFlow = useUpdateFlow();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();
  const restoreVersion = useRestoreFlowVersion();
  const importFlow = useImportFlow();
  const detachFlow = useDetachFlow();
  const runFlow = useRunFlow();
  const activateFlow = useActivateFlow();
  const deactivateFlow = useDeactivateFlow();

  const editor = useFlowEditor(flow?.graph ?? EMPTY_GRAPH);
  const { reset } = editor;

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [libraryCollapsed, setLibraryCollapsed] = useState(false);
  const [showVersions, setShowVersions] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const [showOwnership, setShowOwnership] = useState(false);
  // The Platform Health panel (#8261), shown while no other side panel is.
  const [showHealth, setShowHealth] = useState(false);
  // The dotted guide grid: a per-user preference toggled from the toolbar
  // (off by default — the canvas reads cleaner without it).
  const [showGrid, setShowGrid] = useState<boolean>(
    () => localStorage.getItem("xtm-flow-builder-grid") === "1",
  );
  const toggleGrid = useCallback(() => {
    setShowGrid((v) => {
      localStorage.setItem("xtm-flow-builder-grid", v ? "0" : "1");
      return !v;
    });
  }, []);
  // Alignment guides (lines + magnetic snap while dragging): also a per-user
  // toolbar toggle — constraining every drag is not everyone's taste.
  const [snapGuides, setSnapGuides] = useState<boolean>(
    () => localStorage.getItem("xtm-flow-builder-snap-guides") === "1",
  );
  const toggleSnapGuides = useCallback(() => {
    setSnapGuides((v) => {
      localStorage.setItem("xtm-flow-builder-snap-guides", v ? "0" : "1");
      return !v;
    });
  }, []);
  /** The run whose trace paints the canvas overlay. */
  const [inspectedRunId, setInspectedRunId] = useState<string | null>(null);
  const [runParamsOpen, setRunParamsOpen] = useState(false);
  // Deep link from a bell notification: `?run=<id>` opens the run panel on
  // that very run (an Ask-user question answers right there). Consumed once —
  // the param is dropped so refreshes and later navigation stay clean.
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkRunId = searchParams.get("run");
  useEffect(() => {
    if (!deepLinkRunId) return;
    setShowRuns(true);
    setInspectedRunId(deepLinkRunId);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("run");
      return next;
    }, { replace: true });
  }, [deepLinkRunId, setSearchParams]);
  const [activationInfo, setActivationInfo] =
    useState<FlowActivateResult | null>(null);
  const [quickAddAt, setQuickAddAt] = useState<{ x: number; y: number } | null>(
    null,
  );
  // When quick-add was opened by dropping a connection into empty canvas, the
  // handle it was dragged from — so the new node lands already wired.
  const [quickAddSource, setQuickAddSource] =
    useState<PendingConnection | null>(null);
  // Restricts the quick-add list to the kinds that can wire to that handle.
  const [quickAddAllowed, setQuickAddAllowed] = useState<Set<string> | null>(
    null,
  );
  const [issues, setIssues] = useState<FlowValidationIssue[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Clipboard for copy/paste. Deliberately module-local rather than the system
  // clipboard: nodes carry ids and config, not text, and reading the OS
  // clipboard would need a permission prompt for no gain.
  const clipboardRef = useRef<{ nodes: CanvasNode[]; edges: CanvasEdge[] }>({
    nodes: [],
    edges: [],
  });

  // Load the flow into the editor once it arrives, and again after any save or
  // restore replaces the server's copy.
  const loadedRef = useRef<string | null>(null);
  // Optimistic lock: the version this editor's graph is based on.
  // Sent with every graph save; a 409 means someone else saved meanwhile —
  // we stop overwriting and tell the user to reload.
  const baseVersionRef = useRef<number | null>(null);
  const [versionConflict, setVersionConflict] = useState<number | null>(null);
  useEffect(() => {
    if (!flow) return;
    const stamp = `${flow.id}:${flow.version_number}`;
    if (loadedRef.current === stamp) return;
    loadedRef.current = stamp;
    baseVersionRef.current = flow.version_number;
    setVersionConflict(null);
    reset(flow.graph);
    setIssues(flow.issues ?? []);

    // Position the canvas explicitly. React Flow reads `defaultViewport` only
    // at mount, and at mount this component has no flow yet — so a stored
    // viewport has to be applied imperatively or the graph renders at the
    // identity transform, i.e. off-screen for any flow whose nodes do not
    // start near the origin (every LangFlow import).
    const viewport = flow.graph.viewport ?? { x: 0, y: 0, zoom: 1 };
    const untouched = viewport.x === 0 && viewport.y === 0 && viewport.zoom === 1;
    if (flow.graph.nodes.length > 0) {
      if (untouched) pendingFitRef.current = true;
      else requestAnimationFrame(() => setViewport(viewport));
    }
  }, [flow, reset, setViewport]);

  useEffect(() => {
    if (!nodesInitialized || !pendingFitRef.current) return;
    pendingFitRef.current = false;
    // `fitView` needs every node's dimensions: wait for them to be measured.
    requestAnimationFrame(() => void fitView({ padding: 0.2 }));
  }, [nodesInitialized, fitView]);

  const kindsById = useMemo(() => {
    const map = new Map<string, FlowNodeKind>();
    for (const kind of catalog?.node_kinds ?? []) map.set(kind.kind, kind);
    return map;
  }, [catalog]);

  const canManage = canManageEntity(flow, user?.id, isAdmin);
  const readOnly = !canManage;

  /* Why this flow will not run (FR-016). Mirrors the backend resolver:
     any node whose kind requires AI holds the whole flow back, annotations
     excepted, and an unrecognised kind counts as requiring AI. `flow.call` is
     left out on purpose — the backend follows it into the called flow, which
     the builder cannot see, so claiming here that a call is safe would be a
     promise the trigger path may not keep. */
  const aiOff = useAiActionDisabled();
  const heldBackByAi = useMemo(() => {
    if (!aiOff) return false;
    return editor.nodes.some((node) => {
      const kindDef = kindsById.get(node.data.kind);
      if (kindDef?.is_annotation) return false;
      if (node.data.kind === "flow.call") return false;
      return kindDef ? (kindDef.requires_ai ?? true) : true;
    });
  }, [aiOff, editor.nodes, kindsById]);
  // The owner's account is deactivated: nothing starts the flow, and a
  // run is refused server-side for everyone, so the Run control says so.
  const pausedByOwner = !!flow?.paused_by_owner_deactivation;

  // A one-line summary per node, e.g. the agent it runs or its cron. Resolved
  // against the catalog's live option lists so an id shows as a name.
  const summaries = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of editor.nodes) {
      const kindDef = kindsById.get(node.data.kind);
      if (!kindDef) continue;
      const first = kindDef.config_fields.find((f) => f.required);
      if (!first) continue;
      const options = first.source
        ? (catalog?.sources[first.source] ?? [])
        : first.options;
      const value = summaryValue(node.data.config?.[first.name], options);
      if (value === null) continue;
      map.set(node.id, `${first.label}: ${value}`);
    }
    return map;
  }, [editor.nodes, kindsById, catalog]);

  const selectedNode =
    editor.nodes.find((n) => n.id === selectedNodeId) ?? null;
  const selectedEdge =
    editor.edges.find((e) => e.id === selectedEdgeId) ?? null;
  const selectedIssues = useMemo(
    () => issues.filter((i) => i.node_id === selectedNodeId),
    [issues, selectedNodeId],
  );

  // ── Mutations ─────────────────────────────────────────────────────────────

  const addNode = useCallback(
    (kind: string, position: { x: number; y: number }) => {
      const id = nextNodeId(kind, new Set(editor.nodes.map((n) => n.id)));
      const node: CanvasNode = {
        id,
        type: NODE_COMPONENT_TYPE,
        position,
        selected: true,
        data: { kind, config: {} },
      };
      editor.commit({
        nodes: [...editor.nodes.map((n) => ({ ...n, selected: false })), node],
      });
      setSelectedNodeId(id);
    },
    [editor],
  );

  const reconcileRef = useRef<(() => void) | null>(null);

  /** Inputs embedded on node cards (Chat test message, group resize). */
  const embeddedConfigChange = useCallback(
    (nodeId: string, key: string, value: string) => {
      // Group resizes re-evaluate which nodes the frame now contains — after
      // the size lands in state (next tick, so the commit below is visible).
      if (key === "__size") {
        setTimeout(() => reconcileRef.current?.(), 0);
      }
      editor.commit({
        nodes: editor.nodes.map((n) => {
          if (n.id !== nodeId) return n;
          if (key === "__size") {
            const [w, h] = value.split("x").map(Number);
            return {
              ...n,
              data: {
                ...n.data,
                config: { ...n.data.config, width: w, height: h },
              },
            };
          }
          return {
            ...n,
            data: { ...n.data, config: { ...n.data.config, [key]: value } },
          };
        }),
      });
    },
    [editor],
  );

  // ── Selection grouping ────────────────────────────────────────────────
  /**
   * Position IS membership: after any drag or group resize, a node whose
   * center sits inside a group's frame belongs to it, and one dragged out
   * leaves it. This is what lets a group capture nodes placed into it —
   * whether the node moved, the group moved, or the group grew.
   */
  const reconcileGroupMembership = useCallback(() => {
    const groups = editor.nodes.filter((n) => n.data.kind === "util.group");
    if (groups.length === 0) return;
    const groupById = new Map(groups.map((g) => [g.id, g]));
    const rectOf = (g: CanvasNode) => ({
      x: g.position.x,
      y: g.position.y,
      w: Number(g.data.config?.width) || 420,
      h: Number(g.data.config?.height) || 280,
    });

    let changed = false;
    const reassigned = editor.nodes.map((n) => {
      if (n.data.kind === "util.group") return n; // no nesting
      const parent = n.parentId ? groupById.get(n.parentId) : undefined;
      const absX = n.position.x + (parent?.position.x ?? 0);
      const absY = n.position.y + (parent?.position.y ?? 0);
      const cx = absX + (n.measured?.width ?? 240) / 2;
      const cy = absY + (n.measured?.height ?? 80) / 2;
      const target = groups.find((g) => {
        const r = rectOf(g);
        return cx >= r.x && cx <= r.x + r.w && cy >= r.y && cy <= r.y + r.h;
      });
      if ((target?.id ?? undefined) === n.parentId) return n;
      changed = true;
      if (!target) {
        return { ...n, parentId: undefined, position: { x: absX, y: absY } };
      }
      return {
        ...n,
        parentId: target.id,
        position: { x: absX - target.position.x, y: absY - target.position.y },
      };
    });
    if (!changed) return;
    // React Flow requires parents before children.
    editor.commit({
      nodes: [
        ...reassigned.filter((n) => n.data.kind === "util.group"),
        ...reassigned.filter((n) => n.data.kind !== "util.group"),
      ],
    });
  }, [editor]);
  reconcileRef.current = reconcileGroupMembership;

  const GROUP_PADDING = 32;
  const groupSelection = useCallback(() => {
    // Group the selected top-level nodes (existing groups and their children
    // stay put — nesting is not supported).
    const members = editor.nodes.filter(
      (n) => n.selected && n.data.kind !== "util.group" && !n.parentId,
    );
    if (members.length < 2) return;
    const minX = Math.min(...members.map((n) => n.position.x)) - GROUP_PADDING;
    const minY =
      Math.min(...members.map((n) => n.position.y)) - GROUP_PADDING - 12;
    const maxX = Math.max(
      ...members.map((n) => n.position.x + (n.measured?.width ?? 240)),
    );
    const maxY = Math.max(
      ...members.map((n) => n.position.y + (n.measured?.height ?? 80)),
    );
    const groupId = nextNodeId(
      "util.group",
      new Set(editor.nodes.map((n) => n.id)),
    );
    const group: CanvasNode = {
      id: groupId,
      type: NODE_COMPONENT_TYPE,
      position: { x: minX, y: minY },
      zIndex: -1,
      selected: false,
      data: {
        kind: "util.group",
        config: {
          width: maxX - minX + GROUP_PADDING,
          height: maxY - minY + GROUP_PADDING,
        },
      },
    };
    const memberIds = new Set(members.map((n) => n.id));
    // Parent BEFORE children, children switched to group-relative positions.
    editor.commit({
      nodes: [
        ...editor.nodes.filter((n) => !memberIds.has(n.id)),
        group,
        ...members.map((n) => ({
          ...n,
          parentId: groupId,
          position: { x: n.position.x - minX, y: n.position.y - minY },
        })),
      ],
    });
  }, [editor]);

  const ungroupSelection = useCallback(() => {
    const groups = editor.nodes.filter(
      (n) => n.selected && n.data.kind === "util.group",
    );
    if (groups.length === 0) return;
    const groupIds = new Set(groups.map((g) => g.id));
    const positionOf = new Map(groups.map((g) => [g.id, g.position]));
    editor.commit({
      nodes: editor.nodes
        .filter((n) => !groupIds.has(n.id))
        .map((n) =>
          n.parentId && groupIds.has(n.parentId)
            ? {
                ...n,
                parentId: undefined,
                position: {
                  x: n.position.x + (positionOf.get(n.parentId)?.x ?? 0),
                  y: n.position.y + (positionOf.get(n.parentId)?.y ?? 0),
                },
              }
            : n,
        ),
    });
  }, [editor]);

  // Drop-to-create: a connection dragged from a handle into empty canvas picks
  // a node kind, then lands it already wired. The dragged handle decides the
  // direction — from an output the new node is downstream, from an input it is
  // upstream — and the port matrix decides the edge kind (skipped, node still
  // created, if nothing compatible lines up).
  const addNodeConnected = useCallback(
    (
      kind: string,
      position: { x: number; y: number },
      source: PendingConnection,
    ) => {
      const id = nextNodeId(kind, new Set(editor.nodes.map((n) => n.id)));
      const node: CanvasNode = {
        id,
        type: NODE_COMPONENT_TYPE,
        position,
        selected: true,
        data: { kind, config: {} },
      };

      const sourceNode = editor.nodes.find((n) => n.id === source.nodeId);
      const newDef = kindsById.get(kind);
      const otherDef = sourceNode
        ? kindsById.get(sourceNode.data.kind)
        : undefined;
      const fromOutput = source.handleType === "source";

      // UE-style ergonomics: a data-shaping node created off an output drag
      // starts already consuming that output — like Break Struct picking up
      // the struct it was pulled from. Only conditions/utilities: their first
      // variable field IS "the value to work on"; an action's inputs are
      // authored, not inferred.
      if (
        fromOutput &&
        newDef &&
        otherDef &&
        (newDef.category === "conditions" || newDef.category === "utilities")
      ) {
        const primary = otherDef.output_variables[0]?.name;
        const firstVar = newDef.config_fields.find(
          (f) => f.required && f.type === "variable_text",
        );
        if (primary && firstVar) {
          node.data.config = {
            [firstVar.name]: `{{${source.nodeId}.${primary}}}`,
          };
        }
      }

      // An agent minted off a Chat start point is conversational by
      // construction — its "Enable chat" toggle starts on.
      if (fromOutput && otherDef?.kind === "trigger.chat" && kind === "agent") {
        node.data.config = { ...node.data.config, chat_enabled: true };
      }

      // A node pulled off a parameter pin consumes THAT parameter — the UE
      // data-pin drag. Overrides the generic prefill: the pin names the exact
      // value, not just "the node's primary output".
      if (
        fromOutput &&
        source.handleId?.startsWith("param:") &&
        newDef
      ) {
        const paramName = source.handleId.slice("param:".length);
        const firstVar = newDef.config_fields.find(
          (f) => f.type === "variable_text" || f.type === "variable_textarea",
        );
        if (firstVar) {
          node.data.config = {
            ...node.data.config,
            [firstVar.name]: `{{${source.nodeId}.payload.${paramName}}}`,
          };
        }
      }

      let edge: CanvasEdge | null = null;
      if (newDef && otherDef) {
        const existingPort = findPort(
          otherDef,
          source.handleId,
          fromOutput ? "output" : "input",
        );
        const newPort = (fromOutput ? newDef.inputs : newDef.outputs).find((p) =>
          fromOutput
            ? defaultEdgeKind(existingPort, p)
            : defaultEdgeKind(p, existingPort),
        );
        const edgeKind =
          existingPort && newPort
            ? fromOutput
              ? defaultEdgeKind(existingPort, newPort)
              : defaultEdgeKind(newPort, existingPort)
            : null;
        if (existingPort && newPort && edgeKind) {
          edge = {
            id: nextEdgeId(new Set(editor.edges.map((e) => e.id))),
            type: EDGE_COMPONENT_TYPE,
            source: fromOutput ? source.nodeId : id,
            sourceHandle: fromOutput ? source.handleId : newPort.id,
            target: fromOutput ? id : source.nodeId,
            targetHandle: fromOutput ? newPort.id : source.handleId,
            data: { kind: edgeKind },
            ...(edgeKind === "handover" ? { label: "delegate" } : {}),
          };
        }
      }

      editor.commit({
        nodes: [...editor.nodes.map((n) => ({ ...n, selected: false })), node],
        edges: edge ? [...editor.edges, edge] : editor.edges,
      });
      setSelectedNodeId(id);
    },
    [editor, kindsById],
  );

  // The node kinds that could legally wire to a dragged handle — used to skip
  // the picker when the choice is obvious and to filter it when it is not.
  const compatibleKinds = useCallback(
    (source: PendingConnection): string[] => {
      const sourceNode = editor.nodes.find((n) => n.id === source.nodeId);
      const otherDef = sourceNode
        ? kindsById.get(sourceNode.data.kind)
        : undefined;
      const fromOutput = source.handleType === "source";
      const existingPort = otherDef
        ? findPort(otherDef, source.handleId, fromOutput ? "output" : "input")
        : undefined;
      if (!existingPort) return [];
      const out: string[] = [];
      for (const def of kindsById.values()) {
        if (def.kind === "unresolved") continue;
        const ports = fromOutput ? def.inputs : def.outputs;
        const fits = ports.some((p) =>
          fromOutput
            ? defaultEdgeKind(existingPort, p)
            : defaultEdgeKind(p, existingPort),
        );
        if (fits) out.push(def.kind);
      }
      return out;
    },
    [editor.nodes, kindsById],
  );

  const openQuickAddFromConnection = useCallback(
    (screenPos: { x: number; y: number }, source: PendingConnection) => {
      const kinds = compatibleKinds(source);
      // Obvious: exactly one kind fits (e.g. a guardrails socket only accepts a
      // Guardrails node) — create it straight away, no picker.
      if (kinds.length === 1) {
        addNodeConnected(kinds[0], screenToFlowPosition(screenPos), source);
        return;
      }
      setQuickAddSource(source);
      setQuickAddAllowed(kinds.length > 0 ? new Set(kinds) : null);
      setQuickAddAt(screenPos);
    },
    [compatibleKinds, addNodeConnected, screenToFlowPosition],
  );

  /**
   * Expand an agent onto the canvas as its accessory nodes + edges.
   *
   * An agent already carries tools, integrations, skills, KBs, memory tiers,
   * guardrails, a structured-output schema and handovers. This drops the
   * matching capability node for each thing the agent actually has, wired to
   * the right agent socket, plus an agent node + typed `handover` edge per
   * handover target — the visual form of what the agent already does at
   * runtime. Everything lands in ONE commit.
   *
   * Idempotent on two axes: a capability socket that is already wired is left
   * alone (so a second press adds no capability node), and a handover target
   * already on the canvas is reused with its existing edge kept. One level
   * only for handovers (the selected agent's direct targets), so a handover
   * cycle can't fan out forever.
   */
  const materializeAgentGraph = useCallback(
    (sourceNodeId: string, agent: Agent) => {
      const source = editor.nodes.find((n) => n.id === sourceNodeId);
      if (!source) return;

      const takenNodes = new Set(editor.nodes.map((n) => n.id));
      const takenEdges = new Set(editor.edges.map((e) => e.id));
      const newNodes: CanvasNode[] = [];
      const newEdges: CanvasEdge[] = [];

      const wireCapability = (
        kind: string,
        socket: string,
        config: Record<string, unknown>,
        offset: { x: number; y: number },
      ) => {
        // A socket already fed by an edge means the author has wired this
        // capability themselves — never duplicate it.
        const alreadyWired = editor.edges.some(
          (e) => e.target === sourceNodeId && e.targetHandle === socket,
        );
        if (alreadyWired) return;
        const nodeId = nextNodeId(kind, takenNodes);
        takenNodes.add(nodeId);
        newNodes.push({
          id: nodeId,
          type: NODE_COMPONENT_TYPE,
          position: {
            x: source.position.x + offset.x,
            y: source.position.y + offset.y,
          },
          data: { kind, config },
        });
        const edgeId = nextEdgeId(takenEdges);
        takenEdges.add(edgeId);
        newEdges.push({
          id: edgeId,
          type: EDGE_COMPONENT_TYPE,
          source: nodeId,
          sourceHandle: "provides",
          target: sourceNodeId,
          targetHandle: socket,
          data: { kind: "capability" },
        });
      };

      // ── Capabilities the agent already carries ──────────────────────────
      // Built-in tool ids are prefixed so the runner can split them back out
      // (same convention the walker uses); the two id sets share one Tool node.
      const toolIds = [
        ...(agent.builtin_tool_ids ?? []).map((id) => `builtin:${id}`),
        ...(agent.custom_tool_ids ?? []),
      ];
      const integrationIds = Object.values(
        agent.integration_bindings ?? {},
      )
        .flat()
        .filter((v): v is string => typeof v === "string" && v.length > 0);
      const skillIds = agent.skill_ids ?? [];
      const kbIds = agent.knowledge_base_ids ?? [];
      const memory = {
        short_term: !!agent.memory_short_term_enabled,
        medium_term: !!agent.memory_medium_term_enabled,
        persistent: !!agent.memory_persistent_enabled,
        user: !!agent.memory_user_enabled,
      };

      // Bottom edge: the id-family capabilities, spread left-to-right.
      let bottomCol = 0;
      const bottom = () => ({ x: -180 + bottomCol++ * 200, y: 260 });
      if (toolIds.length > 0)
        wireCapability("capability.tool", "tools", { tool_ids: toolIds }, bottom());
      if (integrationIds.length > 0)
        wireCapability(
          "capability.integration",
          "integrations",
          { integration_ids: integrationIds },
          bottom(),
        );
      if (skillIds.length > 0)
        wireCapability(
          "capability.skill",
          "skills",
          { skill_ids: skillIds },
          bottom(),
        );
      if (kbIds.length > 0)
        wireCapability(
          "capability.knowledge_base",
          "knowledge_bases",
          { knowledge_base_ids: kbIds },
          bottom(),
        );

      // Top edge: memory + guardrails.
      let topCol = 0;
      const top = () => ({ x: topCol++ * 220, y: -220 });
      if (Object.values(memory).some(Boolean))
        wireCapability("capability.memory", "memory", memory, top());
      if (agent.guardrails && agent.guardrails.trim())
        wireCapability(
          "capability.guardrails",
          "guardrails",
          { guardrails: agent.guardrails },
          top(),
        );

      // Left edge: the structured-output schema (only when the agent emits JSON).
      if (agent.output_format === "json" && agent.output_schema)
        wireCapability(
          "capability.structured_output",
          "output",
          { schema: agent.output_schema },
          { x: -320, y: 0 },
        );

      // ── Handovers ───────────────────────────────────────────────────────
      const byAgentId = new Map<string, string>();
      for (const n of editor.nodes) {
        const aid = n.data.kind === "agent" ? n.data.config?.agent_id : null;
        if (typeof aid === "string" && aid) byAgentId.set(aid, n.id);
      }
      // Reuse agent nodes we are about to create in this same pass too, so a
      // handover target that is also (say) freshly placed isn't doubled.
      for (const n of newNodes) {
        const aid = n.data.kind === "agent" ? n.data.config?.agent_id : null;
        if (typeof aid === "string" && aid && !byAgentId.has(aid))
          byAgentId.set(aid, n.id);
      }
      const types = (agent.handover_types ?? {}) as Record<string, string>;
      (agent.handover_agent_ids ?? []).forEach((targetAgentId, i) => {
        let targetNodeId = byAgentId.get(targetAgentId);
        if (!targetNodeId) {
          targetNodeId = nextNodeId("agent", takenNodes);
          takenNodes.add(targetNodeId);
          byAgentId.set(targetAgentId, targetNodeId);
          newNodes.push({
            id: targetNodeId,
            type: NODE_COMPONENT_TYPE,
            position: {
              x: source.position.x + 360,
              y: source.position.y + i * 160,
            },
            data: { kind: "agent", config: { agent_id: targetAgentId } },
          });
        }
        const already = editor.edges.some(
          (e) =>
            e.source === sourceNodeId &&
            e.sourceHandle === "handover" &&
            e.target === targetNodeId,
        );
        if (!already) {
          const edgeId = nextEdgeId(takenEdges);
          takenEdges.add(edgeId);
          newEdges.push({
            id: edgeId,
            type: EDGE_COMPONENT_TYPE,
            source: sourceNodeId,
            sourceHandle: "handover",
            target: targetNodeId,
            targetHandle: "in",
            // The edge label names the handover type, so the graph reads the
            // way the agent's own config does.
            label: types[targetAgentId] ?? "delegate",
            data: { kind: "handover" },
          });
        }
      });

      if (newNodes.length === 0 && newEdges.length === 0) return;
      editor.commit({
        nodes: [...editor.nodes, ...newNodes],
        edges: [...editor.edges, ...newEdges],
      });
    },
    [editor],
  );

  /** Library click: drop in the middle of what the user is looking at. */
  const placeInView = useCallback(
    (kind: string) => {
      const { x, y, zoom } = getViewport();
      addNode(kind, {
        x: (-x + window.innerWidth / 2 - 130) / zoom,
        y: (-y + window.innerHeight / 2 - 48) / zoom,
      });
    },
    [addNode, getViewport],
  );

  const updateSelectedNode = useCallback(
    (mutate: (node: CanvasNode) => CanvasNode) => {
      editor.commit({
        nodes: editor.nodes.map((n) => (n.id === selectedNodeId ? mutate(n) : n)),
      });
    },
    [editor, selectedNodeId],
  );

  /** Merge several config keys in ONE commit — sequential single-key writes
   *  each start from a stale `editor.nodes` and clobber one another. An
   *  optional `label` renames the node in the same commit (agent auto-map
   *  adopts the agent's name so the node stops reading "Agent"). */
  const applyConfigPatch = useCallback(
    (nodeId: string, patch: Record<string, unknown>, label?: string) => {
      editor.commit({
        nodes: editor.nodes.map((n) =>
          n.id === nodeId
            ? {
                ...n,
                data: {
                  ...n.data,
                  ...(label !== undefined ? { label } : {}),
                  config: { ...n.data.config, ...patch },
                },
              }
            : n,
        ),
      });
    },
    [editor],
  );

  const deleteSelection = useCallback(() => {
    const doomed = new Set(
      editor.nodes.filter((n) => n.selected).map((n) => n.id),
    );
    const doomedEdges = new Set(
      editor.edges.filter((e) => e.selected).map((e) => e.id),
    );
    if (doomed.size === 0 && doomedEdges.size === 0) return;
    // Deleting a group ungroups its children (lifted back to absolute
    // positions) — a container's death must not take its content with it.
    const groupPositions = new Map(
      editor.nodes
        .filter((n) => doomed.has(n.id) && n.data.kind === "util.group")
        .map((g) => [g.id, g.position]),
    );
    editor.commit({
      nodes: editor.nodes
        .filter((n) => !doomed.has(n.id))
        .map((n) =>
          n.parentId && groupPositions.has(n.parentId)
            ? {
                ...n,
                parentId: undefined,
                position: {
                  x: n.position.x + (groupPositions.get(n.parentId)?.x ?? 0),
                  y: n.position.y + (groupPositions.get(n.parentId)?.y ?? 0),
                },
              }
            : n,
        ),
      // An edge whose endpoint is gone must go too, or the graph is dangling.
      edges: editor.edges.filter(
        (e) =>
          !doomedEdges.has(e.id) &&
          !doomed.has(e.source) &&
          !doomed.has(e.target),
      ),
    });
    if (selectedNodeId && doomed.has(selectedNodeId)) setSelectedNodeId(null);
  }, [editor, selectedNodeId]);

  const copySelection = useCallback(() => {
    const nodes = editor.nodes.filter((n) => n.selected);
    if (nodes.length === 0) return;
    const ids = new Set(nodes.map((n) => n.id));
    clipboardRef.current = {
      nodes,
      // Only edges wholly inside the selection: a half-edge would paste as a
      // dangling connection.
      edges: editor.edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    };
  }, [editor.nodes, editor.edges]);

  // ── Snippets: saved selections (localStorage) ─────────────────────────
  const [snippets, setSnippets] = useState<FlowSnippet[]>(() => loadSnippets());

  const handleSaveSnippet = useCallback(() => {
    const selection = editor.nodes.filter((n) => n.selected);
    if (selection.length === 0) return;
    const selectedIds = new Set(selection.map((n) => n.id));
    const name = window.prompt(
      t_i18n("Snippet name"),
      `${selection[0].data.label || selection[0].data.kind} ×${selection.length}`,
    );
    if (name === null) return;
    const graph = fromCanvas(
      selection,
      editor.edges.filter(
        (e) => selectedIds.has(e.source) && selectedIds.has(e.target),
      ),
      { x: 0, y: 0, zoom: 1 },
    );
    saveSnippet(name, graph.nodes, graph.edges);
    setSnippets(loadSnippets());
    toast({ title: t_i18n("Snippet saved"), variant: "success" });
  }, [editor.nodes, editor.edges, toast, t_i18n]);

  const handleInsertSnippet = useCallback(
    (snippet: FlowSnippet) => {
      const takenNodeIds = new Set(editor.nodes.map((n) => n.id));
      const takenEdgeIds = new Set(editor.edges.map((e) => e.id));
      const idMap = new Map<string, string>();
      const centre = screenToFlowPosition({
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      });
      const inserted = toCanvasNodes({
        nodes: snippet.nodes,
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
      }).map((node) => {
        const id = nextNodeId(node.data.kind, takenNodeIds);
        takenNodeIds.add(id);
        idMap.set(node.id, id);
        return {
          ...node,
          id,
          selected: true,
          position: {
            x: node.position.x + centre.x,
            y: node.position.y + centre.y,
          },
        };
      });
      for (const node of inserted) {
        if (node.parentId) node.parentId = idMap.get(node.parentId);
      }
      const insertedEdges = toCanvasEdges({
        nodes: [],
        edges: snippet.edges,
        viewport: { x: 0, y: 0, zoom: 1 },
      }).map((edge) => {
        const id = nextEdgeId(takenEdgeIds);
        takenEdgeIds.add(id);
        return {
          ...edge,
          id,
          source: idMap.get(edge.source)!,
          target: idMap.get(edge.target)!,
        };
      });
      editor.commit({
        nodes: [
          ...editor.nodes.map((n) => ({ ...n, selected: false })),
          ...inserted,
        ],
        edges: [...editor.edges, ...insertedEdges],
      });
    },
    [editor, screenToFlowPosition],
  );

  const pasteClipboard = useCallback(() => {
    const { nodes: copiedNodes, edges: copiedEdges } = clipboardRef.current;
    if (copiedNodes.length === 0) return;

    const takenNodeIds = new Set(editor.nodes.map((n) => n.id));
    const takenEdgeIds = new Set(editor.edges.map((e) => e.id));
    const idMap = new Map<string, string>();

    const pasted = copiedNodes.map((node) => {
      const id = nextNodeId(node.data.kind, takenNodeIds);
      takenNodeIds.add(id);
      idMap.set(node.id, id);
      return {
        ...node,
        id,
        selected: true,
        // Offset so the copy is visibly distinct from its source.
        position: { x: node.position.x + 40, y: node.position.y + 40 },
      };
    });
    // Group membership survives only when the group came along in the copy.
    for (const node of pasted) {
      if (node.parentId) {
        node.parentId = idMap.get(node.parentId);
      }
    }

    const pastedEdges = copiedEdges.map((edge) => {
      const id = nextEdgeId(takenEdgeIds);
      takenEdgeIds.add(id);
      return {
        ...edge,
        id,
        source: idMap.get(edge.source)!,
        target: idMap.get(edge.target)!,
      };
    });

    editor.commit({
      nodes: [...editor.nodes.map((n) => ({ ...n, selected: false })), ...pasted],
      edges: [...editor.edges, ...pastedEdges],
    });
  }, [editor]);

  const selectedNodes = useMemo(
    () => editor.nodes.filter((n) => n.selected),
    [editor.nodes],
  );
  const selectedEdgeCount = useMemo(
    () => editor.edges.filter((e) => e.selected).length,
    [editor.edges],
  );

  const handleDetach = useCallback(async (mode: "detach" | "collapse" = "detach") => {
    if (!flow || readOnly || selectedNodes.length === 0) return;
    try {
      const { flow: child, parent, report } = await detachFlow.mutateAsync({
        flowId: flow.id,
        graph: editor.graph,
        nodeIds: selectedNodes.map((n) => n.id),
        mode,
      });

      // The server returns the parent it just wrote, so adopt it rather than
      // re-fetching — and reset the baseline with it so the canvas is clean.
      loadedRef.current = `${parent.id}:${parent.version_number}`;
      baseVersionRef.current = parent.version_number;
      reset(parent.graph);
      setIssues(parent.issues ?? []);
      setSelectedNodeId(null);

      // Never let a detach be silent about what it broke.
      const notes: string[] = [];
      if (report.edges_dropped > 0) {
        notes.push(
          t_i18n("{count} connections crossed the selection and were removed", {
            count: report.edges_dropped,
          }),
        );
      }
      const refs = [
        ...report.references_removed,
        ...report.parent_references_removed,
      ];
      if (refs.length > 0) {
        notes.push(
          t_i18n("references that could no longer resolve were removed: {refs}", {
            refs: refs.map((r) => `{{${r}}}`).join(", "),
          }),
        );
      }
      toast({
        title:
          mode === "collapse"
            ? t_i18n("Collapsed into “{name}”", { name: child.name })
            : t_i18n("Detached into “{name}”", { name: child.name }),
        description:
          notes.join(" · ") ||
          t_i18n("Nothing was lost: the selection moved across intact."),
        variant: notes.length ? "info" : "success",
      });
    } catch (err) {
      toast({
        title: t_i18n("Could not detach the selection"),
        description: (err as Error).message,
        variant: "error",
      });
    }
  }, [flow, readOnly, selectedNodes, editor.graph, detachFlow, reset, toast, t_i18n]);

  const handleAutoLayout = useCallback(() => {
    editor.commit({
      nodes: autoLayout(
        editor.nodes,
        editor.edges,
        (kind) => kindsById.get(kind)?.is_annotation ?? false,
      ),
    });
    // Wait for the positions to render before framing them.
    requestAnimationFrame(() => fitView({ duration: 300, padding: 0.2 }));
  }, [editor, kindsById, fitView]);

  const save = useCallback(async () => {
    if (!flow || readOnly) return;
    const graph = editor.graph;
    if (!(await confirmSharing({
      entity_type: "flow",
      entity_id: flow.id,
      graph: graph as unknown as Record<string, unknown>,
    }))) {
      return;
    }
    try {
      const saved = await updateFlow.mutateAsync({
        id: flow.id,
        graph,
        base_version: baseVersionRef.current ?? undefined,
      });
      editor.markSaved(saved.graph);
      setIssues(saved.issues ?? []);
      loadedRef.current = `${saved.id}:${saved.version_number}`;
      baseVersionRef.current = saved.version_number;
      toast({
        title: t_i18n("Flow saved"),
        description: t_i18n("Saved as version {version}", {
          version: saved.version_number,
        }),
        variant: "success",
      });
    } catch (err) {
      // A 422 carries the node-addressed issues; pin them on the canvas rather
      // than showing a toast the user cannot act on.
      const failure = err as {
        status?: number;
        detail?: { issues?: FlowValidationIssue[]; current_version?: number };
      };
      if (failure.status === 409) {
        setVersionConflict(failure.detail?.current_version ?? null);
        toast({
          title: t_i18n("Flow changed elsewhere"),
          description: t_i18n(
            "Someone else saved this flow (now v{version}). Reload the page to continue from the latest version: saving now would overwrite their change.",
            { version: failure.detail?.current_version ?? "?" },
          ),
          variant: "error",
        });
        return;
      }
      if (failure.detail?.issues) setIssues(failure.detail.issues);
      toast({
        title: t_i18n("Could not save this flow"),
        description: (err as Error).message,
        variant: "error",
      });
    }
  }, [flow, readOnly, editor, updateFlow, toast, t_i18n, confirmSharing]);

  // Layout-only changes (moving nodes, pan, zoom) auto-save silently after a
  // short pause — no toast, and the server mints no version for them. Only a
  // SEMANTIC change (kinds/configs/edges) keeps the explicit Save gesture.
  useEffect(() => {
    if (!flow || readOnly || !editor.isLayoutDirty || editor.isDirty) return;
    if (updateFlow.isPending || versionConflict !== null) return;
    const handle = setTimeout(() => {
      const graph = editor.graph;
      updateFlow
        .mutateAsync({
          id: flow.id,
          graph,
          base_version: baseVersionRef.current ?? undefined,
        })
        .then((saved) => {
          editor.markSaved(saved.graph);
          loadedRef.current = `${saved.id}:${saved.version_number}`;
          baseVersionRef.current = saved.version_number;
        })
        // Best-effort: a failed layout save must never nag — the layout is
        // retried on the next change and included in the next real save.
        // EXCEPT a version conflict: keep retrying and this tab would
        // clobber the other writer the moment it wins — latch instead.
        .catch((err: { status?: number; detail?: { current_version?: number } }) => {
          if (err?.status === 409) {
            setVersionConflict(err.detail?.current_version ?? null);
          }
        });
    }, 1200);
    return () => clearTimeout(handle);
  }, [flow, readOnly, editor, updateFlow, versionConflict]);

  // What each node publishes — declared structures merged with the last run:
  // typed paths in the autocomplete and the drawer's Input data.
  const outputShapes = useOutputShapes(
    flow?.id,
    editor.nodes,
    editor.edges,
    !!selectedNodeId,
  );

  // ── Execution ──────────────────────────────────────────
  // The inspected run's steps paint the canvas overlay; the detail hook
  // polls while the run is active, so the overlay advances live.
  const { data: inspectedRun } = useFlowRunDetail(
    showRuns ? inspectedRunId : null,
  );
  const executionByNode = useMemo(() => {
    if (!showRuns || !inspectedRun) return undefined;
    const map = new Map<string, FlowNodeExtras["execution"]>();
    // Journal order: a node's LAST step wins (loop passes overwrite).
    for (const step of inspectedRun.steps) map.set(step.node_id, step.status);
    return map;
  }, [showRuns, inspectedRun]);

  /** The manual trigger's run-time inputs: default prompt + declared params. */
  const manualInputs = useMemo(() => {
    const trigger = editor.nodes.find(
      (n) => n.data.kind === "trigger.manual",
    );
    if (!trigger) return null;
    const prompt = trigger.data.config?.prompt;
    return {
      prompt: typeof prompt === "string" ? prompt : "",
      // Normalised declaration (typed list or legacy KEY_VALUE dict).
      parameters: declaredParameters(trigger.data.config),
    };
  }, [editor.nodes]);

  const [isStartingRun, setIsStartingRun] = useState(false);
  const startRun = useCallback(
    async (
      parameters?: Record<string, string>,
      prompt?: string,
      files?: File[],
    ) => {
      if (!flow) return;
      setIsStartingRun(true);
      try {
        // Attachments first — their ids ride on the run request and the
        // walker hands the content to every agent node.
        const fileIds: string[] = [];
        for (const file of files ?? []) {
          const uploaded = await uploadFlowRunFile(flow.id, file);
          fileIds.push(uploaded.file_id);
        }
        const run = await runFlow.mutateAsync({
          id: flow.id,
          parameters,
          prompt,
          fileIds,
        });
        setRunParamsOpen(false);
        // Open the trace immediately: watching the overlay advance IS the
        // feedback for "the run started".
        setShowRuns(true);
        setShowVersions(false);
        setShowOwnership(false);
        setInspectedRunId(run.id);
      } catch (err) {
        const detail = (err as { detail?: { issues?: FlowValidationIssue[] } })
          .detail;
        if (detail?.issues) setIssues(detail.issues);
        toast({
          title: t_i18n("Could not run this flow"),
          description: (err as Error).message,
          variant: "error",
        });
      } finally {
        setIsStartingRun(false);
      }
    },
    [flow, runFlow, toast, t_i18n],
  );

  const handleRun = useCallback(() => {
    // Guarded here, not only on the toolbar button: the same action is bound to
    // a command (`flowBuilder.run`), so gating the control alone would leave the
    // shortcut starting a run that the backend refuses (FR-018, T072).
    if (heldBackByAi || pausedByOwner) return;
    // Any manual trigger opens the run dialog — it is where the prompt,
    // the declared parameters and the run attachments live.
    // Manual means "press the button": the dialog only opens when the
    // trigger DECLARED parameters to fill — a bare manual trigger runs
    // immediately (its configured default prompt still applies server-side).
    const hasInputs = !!manualInputs && manualInputs.parameters.length > 0;
    if (hasInputs) setRunParamsOpen(true);
    else void startRun();
  }, [heldBackByAi, pausedByOwner, manualInputs, startRun]);

  const handleToggleActive = useCallback(async () => {
    if (!flow || readOnly) return;
    try {
      if (flow.status === "active") {
        await deactivateFlow.mutateAsync(flow.id);
        toast({
          title: t_i18n("Flow deactivated"),
          description: t_i18n("Its triggers no longer fire."),
        });
        return;
      }
      if (!(await confirmSharing(
        { entity_type: "flow", entity_id: flow.id, change: { status: "active" } },
        { confirmLabel: t_i18n("Activate") },
      ))) {
        return;
      }
      const result = await activateFlow.mutateAsync(flow.id);
      if (
        result.webhook_url ||
        result.webhook_secret ||
        result.warnings.length > 0
      ) {
        // The webhook secret is shown on activation ONLY — a dialog, so it
        // cannot vanish before the user copies it.
        setActivationInfo(result);
      } else {
        toast({
          title: t_i18n("Flow activated"),
          description: t_i18n("Its triggers are live."),
          variant: "success",
        });
      }
    } catch (err) {
      const detail = (err as { detail?: { issues?: FlowValidationIssue[] } })
        .detail;
      if (detail?.issues) setIssues(detail.issues);
      toast({
        title: t_i18n("Could not change the flow's status"),
        description: (err as Error).message,
        variant: "error",
      });
    }
  }, [flow, readOnly, activateFlow, deactivateFlow, toast, t_i18n, confirmSharing]);

  // Owner and access: metadata-only saves - no graph, so the version
  // does not move and the editor keeps any unsaved canvas edit.
  const saveOwnership = useCallback(
    async (patch: { user_id: string } | { is_company_managed: boolean }) => {
      if (!flow) return;
      const change = "user_id" in patch ? { new_owner_id: patch.user_id } : patch;
      const widens = "user_id" in patch || patch.is_company_managed;
      if (!(await confirmSharing(
        { entity_type: "flow", entity_id: flow.id, change },
        widens ? { confirmLabel: "user_id" in patch ? t_i18n("Transfer") : t_i18n("Make company-managed") } : { mode: "unshare" },
      ))) {
        return;
      }
      try {
        await updateFlow.mutateAsync({ id: flow.id, ...patch });
        toast({
          title:
            "user_id" in patch
              ? t_i18n("Flow transferred")
              : patch.is_company_managed
                ? t_i18n("Flow is now company-managed")
                : t_i18n("Flow is no longer company-managed"),
          variant: "success",
        });
      } catch (err) {
        toast({
          title: t_i18n("Could not change the flow's owner or access"),
          description: (err as Error).message,
          variant: "error",
        });
      }
    },
    [flow, updateFlow, toast, t_i18n, confirmSharing],
  );

  const handleExport = useCallback(() => {
    if (!flow) return;
    const document = {
      format: "native",
      version: 1,
      name: flow.name,
      description: flow.description,
      tags: flow.tags,
      graph: editor.graph,
    };
    const blob = new Blob([JSON.stringify(document, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `${flow.name.replace(/[^\w.-]+/g, "-")}.flow.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }, [flow, editor.graph]);

  const handleImportFile = useCallback(
    async (file: File) => {
      let document: Record<string, unknown>;
      try {
        document = JSON.parse(await file.text());
      } catch {
        toast({
          title: t_i18n("Import failed"),
          description: t_i18n("That file is not valid JSON."),
          variant: "error",
        });
        return;
      }
      try {
        // Imports land in a NEW flow rather than overwriting this one — an
        // import that silently replaced the open canvas would be unrecoverable.
        const { flow: imported, report } = await importFlow.mutateAsync({
          document,
        });
        const partial =
          report.nodes_unresolved > 0 ||
          report.nodes_to_review > 0 ||
          report.edges_dropped > 0;
        toast({
          title: t_i18n("Flow imported"),
          description: partial
            ? t_i18n(
                "{unresolved} nodes need remapping, {review} need a review, {dropped} connections were dropped.",
                {
                  unresolved: report.nodes_unresolved,
                  review: report.nodes_to_review,
                  dropped: report.edges_dropped,
                },
              )
            : t_i18n("Everything in the file was imported cleanly."),
          variant: partial ? "info" : "success",
        });
        navigate(`/flows/library/${imported.id}`);
      } catch (err) {
        toast({
          title: t_i18n("Import failed"),
          description: (err as Error).message,
          variant: "error",
        });
      }
    },
    [importFlow, navigate, toast, t_i18n],
  );

  const handleRestore = useCallback(
    async (versionNumber: number) => {
      if (!flow) return;
      const restored = await restoreVersion.mutateAsync({
        flowId: flow.id,
        versionNumber,
      });
      loadedRef.current = `${restored.id}:${restored.version_number}`;
      reset(restored.graph);
      setIssues(restored.issues ?? []);
      toast({
        title: t_i18n("Version restored"),
        description: t_i18n(
          "Version {version} is now current; the previous state is still in the history.",
          { version: versionNumber },
        ),
        variant: "success",
      });
    },
    [flow, restoreVersion, reset, toast, t_i18n],
  );

  // ── Keyboard shortcuts ────────────────────────────────────────────────────

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey;
      const typing = isTypingTarget(event.target);

      // Save is the one shortcut that must work from anywhere: a user editing a
      // node's prompt and pressing ⌘S means "save the flow", exactly as in any
      // editor. Every other binding yields to a focused field — Backspace there
      // must delete a character rather than the node being configured, and
      // ⌘A / ⌘C / ⌘V / ⌘Z belong to the text, not the canvas.
      if (mod && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void save();
        return;
      }
      if (typing) return;

      if (mod && event.shiftKey && event.key.toLowerCase() === "z") {
        event.preventDefault();
        editor.redo();
      } else if (mod && event.key.toLowerCase() === "z") {
        event.preventDefault();
        editor.undo();
      } else if (mod && event.key.toLowerCase() === "c") {
        copySelection();
      } else if (mod && event.key.toLowerCase() === "v") {
        if (!readOnly) pasteClipboard();
      } else if (mod && event.key.toLowerCase() === "a") {
        event.preventDefault();
        editor.commit({
          nodes: editor.nodes.map((n) => ({ ...n, selected: true })),
          edges: editor.edges.map((e) => ({ ...e, selected: true })),
        });
      } else if (mod && event.shiftKey && event.key.toLowerCase() === "f") {
        event.preventDefault();
        fitView({ duration: 200, padding: 0.2 });
      } else if (mod && (event.key === "=" || event.key === "+")) {
        event.preventDefault();
        zoomIn({ duration: 150 });
      } else if (mod && event.key === "-") {
        event.preventDefault();
        zoomOut({ duration: 150 });
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        if (!readOnly) deleteSelection();
      } else if (event.key === " " && !quickAddAt) {
        event.preventDefault();
        if (!readOnly) {
          setQuickAddAt({ x: window.innerWidth / 2, y: window.innerHeight / 3 });
        }
      } else if (event.key === "Escape") {
        setQuickAddAt(null);
        setSelectedNodeId(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    editor,
    save,
    copySelection,
    pasteClipboard,
    deleteSelection,
    fitView,
    zoomIn,
    zoomOut,
    quickAddAt,
    readOnly,
  ]);

  // Warn before leaving with unsaved work. Undo history is client-side, so a
  // reload loses it entirely.
  useEffect(() => {
    if (!editor.isDirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [editor.isDirty]);

  // ⌘K commands
  useCommandHandler("flowBuilder.save", () => void save());
  useCommandHandler("flowBuilder.autoLayout", handleAutoLayout);
  useCommandHandler("flowBuilder.export", handleExport);
  useCommandHandler("flowBuilder.versions", () => {
    setShowVersions((v) => !v);
    setShowOwnership(false);
  });
  useCommandHandler("flowBuilder.run", handleRun);
  useCommandHandler("flowBuilder.toggleActive", () => void handleToggleActive());
  useCommandHandler("flowBuilder.runs", () => {
    setShowRuns((v) => !v);
    setShowVersions(false);
    setShowOwnership(false);
  });

  // Owner transfer and the company-managed flag are admin acts (the API
  // refuses both otherwise), so the drawer is offered to admins only.
  const canEditOwnership = isAdmin && canManage;

  // `?panel=owner-access` (the paused-flow notice links here) opens the
  // drawer once the flow is loaded - for an admin only - and is consumed
  // either way, like `?run=`.
  const deepLinkOwnerAccess = wantsOwnerAccessPanel(searchParams);
  useEffect(() => {
    if (!deepLinkOwnerAccess || !flow) return;
    if (canEditOwnership) {
      setShowOwnership(true);
      setShowVersions(false);
      setShowRuns(false);
    }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete(FLOW_PANEL_PARAM);
      return next;
    }, { replace: true });
  }, [deepLinkOwnerAccess, flow, canEditOwnership, setSearchParams]);

  if (isLoading) return <PageSkeleton cards={2} />;
  // An unknown id (or one you cannot read) is an address no page answers (#797 S4).
  if (!flow) return <RouteNotFound />;

  return (
    // Fills the content area below the app header. `min-h-0` lets the canvas
    // and both drawers scroll internally instead of growing the page.
    <div className="flex h-[calc(100vh-8rem)] min-h-0 flex-col overflow-hidden rounded-sm border border-elevation-subtle">
      {confirmSharingElement}
      <BuilderToolbar
        name={flow.name}
        status={flow.status}
        versionNumber={flow.version_number}
        isDirty={editor.isDirty}
        isSaving={updateFlow.isPending}
        canUndo={editor.canUndo}
        canRedo={editor.canRedo}
        issues={issues}
        readOnly={readOnly}
        onNameChange={(name) => void updateFlow.mutateAsync({ id: flow.id, name })}
        onSave={() => void save()}
        onUndo={editor.undo}
        onRedo={editor.redo}
        onAutoLayout={handleAutoLayout}
        showGrid={showGrid}
        onToggleGrid={toggleGrid}
        snapGuides={snapGuides}
        onToggleSnapGuides={toggleSnapGuides}
        onExport={handleExport}
        onImport={() => fileInputRef.current?.click()}
        onToggleVersions={() => {
          setShowVersions((v) => !v);
          setShowRuns(false);
          setShowOwnership(false);
        }}
        onToggleOwnership={
          canEditOwnership
            ? () => {
                setShowOwnership((v) => !v);
                setShowVersions(false);
                setShowRuns(false);
              }
            : undefined
        }
        isRunning={runFlow.isPending}
        heldBackByAi={heldBackByAi}
        pausedByOwner={pausedByOwner}
        isActivating={activateFlow.isPending || deactivateFlow.isPending}
        onRun={handleRun}
        onToggleActive={() => void handleToggleActive()}
        onToggleRuns={() => {
          setShowRuns((v) => !v);
          setShowVersions(false);
          setShowOwnership(false);
        }}
        onToggleHealth={
          canManage
            ? () => {
                setShowHealth((v) => !v);
                setShowVersions(false);
                setShowRuns(false);
                setShowOwnership(false);
              }
            : undefined
        }
      />

      {readOnly && (
        <div className="px-3 pt-3">
          {flow.is_company_managed ? (
            <CompanyManagedBanner entityLabel="flow" />
          ) : (
            <SharedBanner entityLabel="flow" ownerName={flow.owner_name} readOnly />
          )}
        </div>
      )}

      {pausedByOwner && (
        <div className="px-3 pt-3">
          <OwnerDeactivatedNotice
            entity="flow"
            ownerId={flow.user_id}
            description={t_i18n("Nothing starts this flow while its owner's account is deactivated: schedules, webhooks, integration events, work items, flow events, sub-flow calls and manual runs are all held. It resumes as it was once the account is reactivated.")}
            reactivateHint={t_i18n("Reactivate the account from the user's page to resume this flow.")}
            ownerAccessHref={ownerAccessUrl(flow.id)}
          />
        </div>
      )}

      {heldBackByAi && (
        <div className="px-3 pt-3">
          <div
            role="status"
            className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.07] px-3 py-2 content-compact text-feedback-warning-tertiary"
          >
            <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
            <span>
              {t_i18n(
                "This flow contains a step that needs AI, so it will not run while AI is disabled. Its triggers are recorded as skipped. Editing it still works, and removing the AI step lets it run again.",
              )}
            </span>
          </div>
        </div>
      )}

      {catalogStale && (
        <div className="px-3 pt-3">
          <CatalogRefreshNotice retrying={catalogFetching} onRetry={() => void refetchCatalog()} />
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <NodeLibraryPanel
          snippets={snippets}
          onInsertSnippet={handleInsertSnippet}
          onRemoveSnippet={(id) => setSnippets(removeSnippet(id))}
          catalog={catalog}
          collapsed={libraryCollapsed}
          onCollapsedChange={setLibraryCollapsed}
          onPlace={placeInView}
          disabled={readOnly}
        />

        {/* A size container: the selection toolbar sizes its buttons to the
            canvas, not to the viewport. */}
        <div className="@container relative min-w-0 flex-1" data-testid="flow-canvas-area">
          <FlowCanvas
            nodes={editor.nodes}
            edges={editor.edges}
            viewport={editor.viewport}
            kindsById={kindsById}
            issues={issues}
            summaries={summaries}
            executionByNode={executionByNode}
            showGrid={showGrid}
            snapGuides={snapGuides}
            readOnly={readOnly}
            onNodesChange={editor.onNodesChange}
            onEdgesChange={editor.onEdgesChange}
            onViewportChange={editor.onViewportChange}
            onCommit={editor.commit}
            onCommitCurrent={editor.commitCurrent}
            onSelectNode={(id) => {
              setSelectedNodeId(id);
              if (id) setSelectedEdgeId(null);
            }}
            onSelectEdge={(id) => {
              setSelectedEdgeId(id);
              if (id) setSelectedNodeId(null);
            }}
            onNodeConfigChange={embeddedConfigChange}
            onNodeDragStop={reconcileGroupMembership}
            onAddNode={addNode}
            onQuickAdd={setQuickAddAt}
            onQuickAddFromConnection={openQuickAddFromConnection}
          />

          <SelectionToolbar
            nodeCount={selectedNodes.length}
            edgeCount={selectedEdgeCount}
            canGroup={
              selectedNodes.filter(
                (n) => n.data.kind !== "util.group" && !n.parentId,
              ).length >= 2
            }
            canUngroup={selectedNodes.some(
              (n) => n.data.kind === "util.group",
            )}
            onGroup={groupSelection}
            onUngroup={ungroupSelection}
            isDetaching={detachFlow.isPending}
            onCollapse={() => void handleDetach("collapse")}
            onSaveSnippet={handleSaveSnippet}
            readOnly={readOnly}
            onDetach={() => void handleDetach()}
            onDuplicate={() => {
              copySelection();
              pasteClipboard();
            }}
            onDelete={deleteSelection}
            onClear={() =>
              editor.commit({
                nodes: editor.nodes.map((n) => ({ ...n, selected: false })),
                edges: editor.edges.map((e) => ({ ...e, selected: false })),
              })
            }
          />
        </div>

        {selectedEdge && !selectedNode && !showVersions && !showRuns && !showOwnership && !showHealth && (
          <EdgeConfigPanel
            edge={selectedEdge}
            sourceLabel={
              (() => {
                const n = editor.nodes.find((x) => x.id === selectedEdge.source);
                return n?.data.label || kindsById.get(n?.data.kind ?? "")?.label || selectedEdge.source;
              })()
            }
            targetLabel={
              (() => {
                const n = editor.nodes.find((x) => x.id === selectedEdge.target);
                return n?.data.label || kindsById.get(n?.data.kind ?? "")?.label || selectedEdge.target;
              })()
            }
            readOnly={readOnly}
            onClose={() => setSelectedEdgeId(null)}
            onChange={(patch) =>
              editor.commit({
                edges: editor.edges.map((e) =>
                  e.id === selectedEdge.id ? { ...e, ...patch } : e,
                ),
              })
            }
            onDelete={() => {
              editor.commit({
                edges: editor.edges.filter((e) => e.id !== selectedEdge.id),
              });
              setSelectedEdgeId(null);
            }}
          />
        )}

        {selectedNode && !showVersions && !showRuns && !showOwnership && !showHealth && (
          <NodeConfigPanel
            node={selectedNode}
            kindDef={kindsById.get(selectedNode.data.kind)}
            catalog={catalog}
            nodes={editor.nodes}
            edges={editor.edges}
            kindsById={kindsById}
            outputShapes={outputShapes}
            issues={selectedIssues}
            readOnly={readOnly}
            onClose={() => setSelectedNodeId(null)}
            onChangeLabel={(label) =>
              updateSelectedNode((n) => ({ ...n, data: { ...n.data, label } }))
            }
            onMarkReviewed={() =>
              updateSelectedNode((n) => {
                const { review: _review, ...sourcePayload } = n.data.sourcePayload ?? {};
                return { ...n, data: { ...n.data, sourcePayload } };
              })
            }
            onChangeConfig={(name, value) => {
              // Rename-safe parameters: renaming a declared parameter (same
              // list, same position, one name changed) rewrites every
              // {{trigger.payload.<old>}} reference in the SAME commit.
              if (
                selectedNode.data.kind === "trigger.manual" &&
                name === "parameters" &&
                Array.isArray(value)
              ) {
                const before = declaredParameters(selectedNode.data.config);
                const after = value as { name?: unknown }[];
                let rename: { from: string; to: string } | null = null;
                if (before.length === after.length) {
                  const diffs = before
                    .map((p, i) => ({
                      from: p.name,
                      to: String(after[i]?.name ?? "").trim(),
                    }))
                    .filter((d) => d.from !== d.to);
                  if (
                    diffs.length === 1 &&
                    diffs[0].from &&
                    diffs[0].to &&
                    /^[A-Za-z_][A-Za-z0-9_]*$/.test(diffs[0].to)
                  ) {
                    rename = diffs[0];
                  }
                }
                if (rename) {
                  const needle = `{{${selectedNode.id}.payload.${rename.from}`;
                  const swap = `{{${selectedNode.id}.payload.${rename.to}`;
                  const rewrite = (v: unknown): unknown => {
                    if (typeof v === "string") return v.split(needle).join(swap);
                    if (Array.isArray(v)) return v.map(rewrite);
                    if (v && typeof v === "object") {
                      return Object.fromEntries(
                        Object.entries(v as Record<string, unknown>).map(
                          ([k, val]) => [k, rewrite(val)],
                        ),
                      );
                    }
                    return v;
                  };
                  editor.commit({
                    nodes: editor.nodes.map((n) => {
                      const config =
                        n.id === selectedNode.id
                          ? { ...n.data.config, parameters: value }
                          : (rewrite(n.data.config ?? {}) as Record<
                              string,
                              unknown
                            >);
                      return { ...n, data: { ...n.data, config } };
                    }),
                  });
                  return;
                }
              }
              updateSelectedNode((n) => ({
                ...n,
                data: { ...n.data, config: { ...n.data.config, [name]: value } },
              }));
            }}
            onApplyConfigPatch={applyConfigPatch}
            onMaterializeAgentGraph={materializeAgentGraph}
            onSelectChild={setSelectedNodeId}
            onDelete={() => {
              editor.commit({
                nodes: editor.nodes.filter((n) => n.id !== selectedNode.id),
                edges: editor.edges.filter(
                  (e) =>
                    e.source !== selectedNode.id && e.target !== selectedNode.id,
                ),
              });
              setSelectedNodeId(null);
            }}
          />
        )}

        {showVersions && (
          <VersionHistoryPanel
            flowId={flow.id}
            isRestoring={restoreVersion.isPending}
            readOnly={readOnly}
            onRestore={(version) => void handleRestore(version)}
            onClose={() => setShowVersions(false)}
          />
        )}

        {showOwnership && canEditOwnership && (
          <FlowOwnershipPanel
            flow={flow}
            isSaving={updateFlow.isPending}
            onTransferOwner={(userId) => void saveOwnership({ user_id: userId })}
            onCompanyManagedChange={(value) =>
              void saveOwnership({ is_company_managed: value })
            }
            onClose={() => setShowOwnership(false)}
          />
        )}

        {showHealth && !showVersions && !showRuns && !showOwnership && (
          <HealthPanel flowId={flow.id} canManage={canManage} onClose={() => setShowHealth(false)} />
        )}

        {showRuns && (
          <RunsPanel
            flowId={flow.id}
            inspectedRunId={inspectedRunId}
            onInspectRun={setInspectedRunId}
            onClose={() => {
              setShowRuns(false);
              setInspectedRunId(null);
            }}
          />
        )}
      </div>

      {quickAddAt && (
        <QuickAddMenu
          catalog={catalog}
          at={quickAddAt}
          allowedKinds={quickAddAllowed ?? undefined}
          onPick={(kind) => {
            const pos = screenToFlowPosition(quickAddAt);
            if (quickAddSource) addNodeConnected(kind, pos, quickAddSource);
            else addNode(kind, pos);
            setQuickAddAt(null);
            setQuickAddSource(null);
            setQuickAddAllowed(null);
          }}
          onClose={() => {
            setQuickAddAt(null);
            setQuickAddSource(null);
            setQuickAddAllowed(null);
          }}
        />
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (file) void handleImportFile(file);
        }}
      />

      {runParamsOpen && manualInputs && (
        <RunParamsDialog
          defaultPrompt={manualInputs.prompt}
          parameters={manualInputs.parameters}
          isStarting={isStartingRun}
          onRun={({ prompt, parameters, files }) => {
            // The dialog stays open while attachments upload; startRun closes
            // it on success so a failed upload never loses the inputs.
            void startRun(parameters, prompt, files);
          }}
          onClose={() => setRunParamsOpen(false)}
        />
      )}

      {activationInfo && (
        <ActivationInfoDialog
          result={activationInfo}
          onClose={() => setActivationInfo(null)}
        />
      )}
    </div>
  );
}

export default function FlowBuilder() {
  const { id } = useParams<{ id: string }>();

  if (!id) return null;

  return (
    <ReactFlowProvider>
      <BuilderInner flowId={id} />
    </ReactFlowProvider>
  );
}
