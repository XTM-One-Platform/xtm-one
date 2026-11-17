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
 * Canvas editor state + undo/redo.
 *
 * No new state library: every mutation goes through a reducer, so history comes
 * for free — the previous state is pushed onto an undo stack before each
 * *committed* change. Server state (the loaded flow, the node catalog, the
 * version list) stays in TanStack Query, per the repo's "never `useState` for
 * API data" rule; this hook owns only what the user is editing right now.
 *
 * ## What does and does not enter history
 *
 * React Flow fires a change event for every animation frame of a drag and for
 * every click that toggles selection. Recording those would burn the 50-step
 * budget in one gesture and make Ctrl+Z rewind a selection instead of an edit.
 * So:
 *
 * - `applyNodeChanges` / `applyEdgeChanges` (drag frames, selection, measuring)
 *   update state **without** touching history;
 * - `commit` is called once the gesture ends (drag stop) or for a discrete edit
 *   (add, delete, connect, config change) and **is** recorded.
 */

import { useCallback, useMemo, useReducer } from "react";
import {
  applyEdgeChanges,
  applyNodeChanges,
  type EdgeChange,
  type NodeChange,
  type Viewport,
} from "@xyflow/react";
import type { FlowGraph } from "@/types";
import {
  fromCanvas,
  graphsEqual,
  graphsSemanticallyEqual,
  toCanvasEdges,
  toCanvasNodes,
  type CanvasEdge,
  type CanvasNode,
} from "./graph-adapter";

/** The issue's stated minimum. */
const HISTORY_LIMIT = 50;

interface Snapshot {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
}

interface EditorState extends Snapshot {
  viewport: Viewport;
  past: Snapshot[];
  future: Snapshot[];
  /** The graph as last persisted — the baseline for the dirty flag. */
  baseline: FlowGraph;
}

type Action =
  /** Replace everything, e.g. after a load, a save, or a version restore. */
  | { type: "reset"; graph: FlowGraph; baseline?: FlowGraph }
  /** Transient: drag frames, selection, measurement. Not recorded. */
  | { type: "nodeChanges"; changes: NodeChange<CanvasNode>[] }
  | { type: "edgeChanges"; changes: EdgeChange<CanvasEdge>[] }
  | { type: "viewport"; viewport: Viewport }
  /** Discrete edit — recorded in history. */
  | { type: "commit"; nodes?: CanvasNode[]; edges?: CanvasEdge[] }
  | { type: "undo" }
  | { type: "redo" }
  /** Mark the current graph as saved without disturbing history. */
  | { type: "markSaved"; graph: FlowGraph };

function pushHistory(state: EditorState): Snapshot[] {
  const past = [...state.past, { nodes: state.nodes, edges: state.edges }];
  // Drop the oldest entries rather than growing without bound; a 200-node graph
  // held 500 times over is real memory.
  return past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past;
}

function reducer(state: EditorState, action: Action): EditorState {
  switch (action.type) {
    case "reset": {
      const nodes = toCanvasNodes(action.graph);
      const edges = toCanvasEdges(action.graph);
      return {
        nodes,
        edges,
        viewport: action.graph.viewport,
        past: [],
        future: [],
        // The baseline is the graph ROUND-TRIPPED through the canvas, not the
        // raw server copy: `fromCanvas` rounds positions to whole pixels, so
        // comparing against un-rounded input (an imported flow carrying its
        // source tool's float coordinates) would mark a freshly opened flow
        // dirty before the user has touched anything.
        baseline: action.baseline ?? fromCanvas(nodes, edges, action.graph.viewport),
      };
    }
    case "nodeChanges":
      return { ...state, nodes: applyNodeChanges(action.changes, state.nodes) };
    case "edgeChanges":
      return { ...state, edges: applyEdgeChanges(action.changes, state.edges) };
    case "viewport":
      return { ...state, viewport: action.viewport };
    case "commit":
      return {
        ...state,
        nodes: action.nodes ?? state.nodes,
        edges: action.edges ?? state.edges,
        past: pushHistory(state),
        // Any new edit invalidates the redo branch, as in every editor.
        future: [],
      };
    case "undo": {
      const previous = state.past[state.past.length - 1];
      if (!previous) return state;
      return {
        ...state,
        nodes: previous.nodes,
        edges: previous.edges,
        past: state.past.slice(0, -1),
        future: [{ nodes: state.nodes, edges: state.edges }, ...state.future],
      };
    }
    case "redo": {
      const [next, ...rest] = state.future;
      if (!next) return state;
      return {
        ...state,
        nodes: next.nodes,
        edges: next.edges,
        past: pushHistory(state),
        future: rest,
      };
    }
    case "markSaved":
      return { ...state, baseline: action.graph };
    default:
      return state;
  }
}

function initialState(graph: FlowGraph): EditorState {
  return {
    nodes: toCanvasNodes(graph),
    edges: toCanvasEdges(graph),
    viewport: graph.viewport,
    past: [],
    future: [],
    baseline: graph,
  };
}

export function useFlowEditor(initialGraph: FlowGraph) {
  const [state, dispatch] = useReducer(reducer, initialGraph, initialState);

  const graph = useMemo(
    () => fromCanvas(state.nodes, state.edges, state.viewport),
    [state.nodes, state.edges, state.viewport],
  );

  // Two tiers of "changed":
  //  - isDirty — the flow DOES something different (kinds/configs/edges).
  //    Drives the Save button, the run gate and the leave-page warning.
  //  - isLayoutDirty — only where things sit changed (moves, pan, zoom).
  //    Never gates anything; the shell auto-saves it silently.
  const isDirty = useMemo(
    () => !graphsSemanticallyEqual(graph, state.baseline),
    [graph, state.baseline],
  );
  const isLayoutDirty = useMemo(
    () => !isDirty && !graphsEqual(graph, state.baseline),
    [isDirty, graph, state.baseline],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange<CanvasNode>[]) =>
      dispatch({ type: "nodeChanges", changes }),
    [],
  );
  const onEdgesChange = useCallback(
    (changes: EdgeChange<CanvasEdge>[]) =>
      dispatch({ type: "edgeChanges", changes }),
    [],
  );
  const onViewportChange = useCallback(
    (viewport: Viewport) => dispatch({ type: "viewport", viewport }),
    [],
  );

  /** Record a discrete edit. Pass only what changed. */
  const commit = useCallback(
    (next: { nodes?: CanvasNode[]; edges?: CanvasEdge[] }) =>
      dispatch({ type: "commit", ...next }),
    [],
  );

  /** Snapshot the current state so an in-progress gesture becomes undoable. */
  const commitCurrent = useCallback(() => dispatch({ type: "commit" }), []);

  const reset = useCallback(
    (nextGraph: FlowGraph, baseline?: FlowGraph) =>
      dispatch({ type: "reset", graph: nextGraph, baseline }),
    [],
  );
  const markSaved = useCallback(
    (savedGraph: FlowGraph) => dispatch({ type: "markSaved", graph: savedGraph }),
    [],
  );

  const undo = useCallback(() => dispatch({ type: "undo" }), []);
  const redo = useCallback(() => dispatch({ type: "redo" }), []);

  return {
    nodes: state.nodes,
    edges: state.edges,
    viewport: state.viewport,
    graph,
    isDirty,
    isLayoutDirty,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    onNodesChange,
    onEdgesChange,
    onViewportChange,
    commit,
    commitCurrent,
    reset,
    markSaved,
    undo,
    redo,
  };
}
