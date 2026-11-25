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
 * AgentFlowGraph — Force-directed handover relationship graph.
 *
 * Displays all user-accessible agents (including company-managed) and their
 * handover relationships as a directed graph.  Arrows flow from source agent
 * to each of its configured handover targets.  Bidirectional handovers are
 * rendered as two separate directed edges with offset curves.
 *
 * Visual style mirrors PlatformFlowGraph: grid background, animated particles,
 * glow effects, pulse rings — but focused solely on agents & handover topology.
 *
 * **Interactive editing:** When `editableAgentIds` and `onAddEdge` are provided,
 * users can click editable nodes to start creating edges, click target nodes to
 * open a type picker, and click edge labels to edit or delete edges.
 *
 * **Node drag:** When `onNodeDrop` and `draggableNodeIds` are provided, a
 * draggable node pressed and moved past a few pixels follows a line to the
 * pointer instead of panning; released on a node `canDropNode` accepts, the
 * caller gets both ids (the fleet graph re-parents a member). A plain
 * click keeps its meaning; Escape or a release elsewhere cancels. The node
 * dropped on is ringed for a moment once the drop holds: at once when
 * `onNodeDrop` returns nothing, only when its promise resolves `true` when it
 * returns one (a failed save rings nothing).
 *
 * **Node pin:** With a layout, any other node pressed and
 * moved past a few pixels follows the pointer and stays where it is dropped,
 * its arcs with it (agent-graph-pins.ts), until the layout changes or
 * "Unpin agents" releases every pin.
 *
 * **Permission model:** Read-only agents (not in `editableAgentIds`) appear with
 * muted styling and cannot be selected as edge sources. Edges from read-only
 * sources are non-interactive.
 *
 * **Animation stability:** The expensive force-directed layout and the edge
 * JSX layer are guarded by *content-based fingerprints* instead of raw prop
 * references. This means the layout and SVG SMIL animations survive parent
 * re-renders caused by polling (e.g. `useActiveAgentIds` every 5 s) — even
 * when the parent passes a fresh array/object reference with identical data.
 */

import { memo, useMemo, useRef, useState, useCallback, useEffect, useImperativeHandle, useLayoutEffect } from "react";
import { flushSync } from "react-dom";
import { idsKey, useByContent } from "@/hooks/useByContent";
import { graphTooltipPosition } from "@/components/ui/agent-flow-tooltip";
import { Bot, Sparkles, ZoomIn, ZoomOut, Maximize2, Scan, Lock, PowerOff, Cable, PinOff } from "lucide-react";
import { altArrowFocus, freeSpot, keyboardMove, paddedNameBox, pinDrawing, withPin, type NameBox } from "@/lib/agent-graph-pins";
import {
  ICON_MAP,
  ALL_HANDOVER_TYPES_CONFIG,
  GA_HANDOVER_TYPES,
  HANDOVER_TYPES,
  HANDOVER_TYPE_CONFIG,
  OUTPUT_FORMAT_CONFIG,
  TRIGGER_ICONS,
  handoverTypeConfig,
  type OutputFormat,
} from "@/lib/constants";
import { ALL_HANDOVER_TYPES, runsEveryHandoverType, shownHandoverType } from "@/lib/handover-types";
import { A2A_EDGE_CONFIG, a2aEdgeConfig } from "@/lib/agent-graph-a2a";
import { dashAnimation, dashKeyframes } from "@/lib/agent-graph-dash";
import { generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { focusNeighbourhood, edgeTouchesFocus, focusOnGraph, isolateFocus, type AgentFlowFocusMode } from "@/lib/agent-flow-focus";
import { cn, pluralize } from "@/lib/utils";
import type { AgentSummary } from "@/lib/agent-summary";
import { LIVE_HANDOVER_WINDOW_MS, liveEdgesKey, type LiveHandover } from "@/lib/live-handovers";
import type { Agent, GeneralAssistantConfig } from "@/types";
import {
  GRAPH_COLORS,
  BEAM_PARTICLE_KEYFRAMES,
} from "@/lib/graph-utils";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import dagre from "dagre";
import {
  arcControlPoint,
  arcRoutes,
  badgeAngles,
  clearCurvature,
  edgeCurvatures,
  fitToCanvas,
  GRAPH_LAYOUTS,
  glyphRadius,
  keepArcInside,
  layoutGraph,
  fitNameStacks,
  fleetNameRooms,
  nameRoomsPx,
  orgChartMetrics,
  orgChartRoutes,
  placeArcLabels,
  quadraticPoint,
  spacingFor,
  truncateLabel,
  wrapLabel,
  wrapLabelWhole,
  staticLabelEdgePx,
  staticNameLines,
  spreadNameBlocks,
  type GraphForces,
  type GraphLayout,
  type LabelBox,
} from "@/lib/agent-graph-layout";
import { type GraphCache, cachedGraph, warmGraphs } from "@/lib/agent-graph-cache";
import { agentLabel, agentLabelLines, badgeCentre, distinctLabels, ghostGroupLine, nodeLabels, topBelow, type AgentLabel } from "@/lib/agent-graph-labels";
import { AGENT_DRAWER_PX, ARROW_GAP, VIEW_ZOOM, arrowHead, framedPanY, revealShift, ringReach, viewHeight, viewOrigin, visibleViewHeight, wheelFactor, wheelZoom } from "@/lib/agent-graph-view";
import { EDGE_REST_OPACITY, edgeInk } from "@/lib/agent-graph-ink";
import { fitTransform } from "@/lib/agent-graph-tween";
import { BASE_OPACITY_ATTR, TWEEN_EDGE_ATTR, TWEEN_NODE_ATTR, useAnimatedLayout } from "@/hooks/useAnimatedLayout";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { BeamParticle } from "@/components/ui/beam-particle";
import { Tip } from "@/components/ui/tooltip";
import { buttonRoleProps } from "@/lib/button-role";
import { HandoverTypePopover, placePopover, type PopoverAvoidBox } from "@/components/ui/handover-type-popover";

// ─── SVG dimensions ──────────────────────────────────────────
const W = 1400;
const H = 600;
// A drawing taller than the canvas grows it, up to this height, and never past
// what keeps the canvas and the toolbar docked under it inside the first
// viewport. Names keep a fixed screen size whatever the scale
// (NAME_PX below).
const MAX_H = 720;
// Screen room kept under the svg for the toolbar docked there (px).
const TOOLBAR_RESERVE_PX = 72;
const PAD_X = 90;
const PAD_Y = 70;
// Screen width of the zoom controls docked at the canvas's top right (right-3
// plus a 28 px button plus a 4 px gap): the fitted drawing keeps clear of
// them, so a right column's name is never drawn under them.
const ZOOM_RAIL_PX = 44;
// Screen room kept across between two arc ends on one ring, beyond an
// arrowhead's width.
const ARC_END_GAP_PX = 3;
// Room the layout library guarantees between neighbours (its defaults).
const LAYOUT_MIN_DISTANCE = 90;
const LAYOUT_LABEL_WIDTH = 150;
// With a layout, names and edge labels are drawn at a fixed screen size (CSS
// px), however far the drawing is scaled into the canvas or zoomed.
const NAME_PX = 11;
// A static force view's names (the fleet About mini graph): read at rest,
// never zoomed in, so at the type floor of 12 px (#10147 UX r4 B2: 11 px drawn).
const STATIC_NAME_PX = 12;
const EDGE_LABEL_PX = 9;
// IBM Plex Sans: average glyph advance as a share of the font size, and the
// screen gap kept between two names.
const GLYPH_ADVANCE = 0.56;
// While a layout change runs: edge labels, condition
// marks and beam particles stay mounted but are not painted, and the dashed
// edge flow and every decorative CSS animation pause, so the change costs no
// mount or unmount and the frames spend nothing on decorative animation.
// `!important`: the dash flow and the beam particles set `animation` inline,
// and an inline shorthand resets the play state to running over any plain
// stylesheet rule. The glow halos (radial gradients) and
// the arrowheads (markers) are left out while the move runs too: the browser
// rasterises them again on every frame a node or an edge moves, about half
// the cost of a frame at 99 agents.
// The pause names the animated elements by class, never `*`: a universal
// descendant rule makes the flip of `data-animating` restyle every element of
// the drawing, 37-59 ms on the first frame of a move at 149 agents. Beam
// particles need no pause, they are inside the hidden group.
const HIDE_MOVING_CLASS = "af-hide-moving";
// A node's keyboard focus ring, drawn only while the node is :focus-visible.
const FOCUS_RING_CLASS = "af-focus-ring";
const FOCUS_RING_CSS =
  `.${FOCUS_RING_CLASS}{display:none}`
  + `\ng[data-node-id]:focus-visible>.${FOCUS_RING_CLASS}{display:inline}`;
const PAUSE_MOVING_CLASS = "af-pause-moving";
/** The keyboard focus of an editable handover edge: its 14-unit hit area, transparent otherwise, is drawn as a
 *  band in the focus colour while it holds the keyboard focus. The band keeps an
 *  opacity of 0.85 so it reads 3:1 or more on the canvas in both themes (0.35 painted 1.90:1 light and 1.76:1 dark). Its colour is
 *  the one the design system's `ring-focus` paints (BUTTON_ROLE_FOCUS_RING), so
 *  the edge follows any change of the focus token. */
const EDGE_HIT_ATTR = "data-edge-hit";
const FOCUS_TOKEN = "var(--color-filigran-brand-primary)";
const EDGE_FOCUS_CSS =
  `path[${EDGE_HIT_ATTR}]:focus{outline:none}`
  + `\npath[${EDGE_HIT_ATTR}]:focus-visible{stroke:${FOCUS_TOKEN};stroke-opacity:0.85}`;
const MOVING_CSS =
  `svg[data-animating="true"] .${HIDE_MOVING_CLASS}{display:none}`
  + `\nsvg[data-animating="true"] .${PAUSE_MOVING_CLASS}{animation-play-state:paused !important}`
  + `\nsvg[data-animating="true"] [fill^="url(#af-glow"]{display:none}`
  + `\nsvg[data-animating="true"] [marker-end]{marker-end:none}`;
// One marching keyframe per dash period of the edge types: a cycle moves its
// dash by one whole period, so the loop never snaps back (#12382).
const DASH_KEYFRAMES = dashKeyframes([
  ...Object.values(HANDOVER_TYPE_CONFIG).map((c) => c.dash),
  ALL_HANDOVER_TYPES_CONFIG.dash,
  ...Object.values(A2A_EDGE_CONFIG).map((c) => c.dash),
]);
const LABEL_GAP_PX = 8;
// Fewer characters than this fit the screen room: the name is left out (the
// hover or keyboard focus shows it whole, zooming in brings it back) rather
// than drawn as a stub or over its neighbour's.
const MIN_LABEL_CHARS = 8;
// Smallest screen diameter of a node glyph drawn by a layout, whatever the
// node count or the zoom.
const GLYPH_FLOOR_PX = 12;
// A ghost node's home-group line, relative to the name size.
const SUB_LABEL_SCALE = 0.85;
// Screen room of a name with no neighbour on its row (about 24 characters).
const LONE_NAME_ROOM_PX = 160;
// Screen height of the tallest stack a wrapped drawing (`wrapNames`) draws
// under a glyph, which never cuts a name: four name lines, a ghost's group
// line on three lines and a gap (px).
const WRAPPED_STACK_PX = Math.ceil(NAME_PX * (1.2 + 3 * 1.15) + NAME_PX * SUB_LABEL_SCALE * (1.2 + 2 * 1.15) + 8);
// Tallest canvas of a wrapped drawing (drawing units): the page scrolls, so a
// dense fleet grows down instead of packing its rows over its names.
const WRAPPED_MAX_H = 1800;
// Screen height of a static view (`staticView`, the fleet About mini graph).
export const STATIC_VIEW_PX = 260;
// A static view's ghost group line, relative to the name size: 10 px on
// screen, the smallest size read at rest (the page flow keeps SUB_LABEL_SCALE).
const STATIC_SUB_LABEL_SCALE = 10 / NAME_PX;
// Screen height under a glyph of a static view's names: two name lines, a
// ghost's group line on three lines (a long fleet name wraps in a name's
// room), the descender of its last line and a gap (px). Its rows are spaced
// by it, and the room under the last row is it, so no caption is cut by the
// card edge and no group line is left out for lack of room.
const STATIC_STACK_PX = Math.ceil(NAME_PX * (1.2 + 1.15) + NAME_PX * STATIC_SUB_LABEL_SCALE * (1.2 + 2 * 1.15) + NAME_PX * 0.3 + 8);
// The same stack in a static force view, its two name lines at STATIC_NAME_PX.
const STATIC_FORCE_STACK_PX = STATIC_STACK_PX + Math.ceil((STATIC_NAME_PX - NAME_PX) * (1.2 + 1.15 + 0.3));
// The same stack under a node with no group line (a member of the fleet):
// two name lines, the descender and a gap (px).
const STATIC_NAME_STACK_PX = Math.ceil(NAME_PX * (1.2 + 1.15) + NAME_PX * 0.3 + 8);
// Screen room across a static view gives each name of a row (px): a wider
// layer wraps onto the next row instead of cutting its names.
const STATIC_NAME_ROOM_PX = 130;
// Screen room a static force view keeps between its widest name and the frame.
const STATIC_EDGE_PX = 12;
// Screen height a glyph and its gap take above its name (px).
const WRAPPED_GLYPH_PX = 28;
// Screen room a name at rest keeps from another node's ring.
const NAME_CLEARANCE_PX = 4;
// A fleet grid's name: its letter spacing, relative to the
// name size, and the fewest characters drawn before it is left out.
const FLEET_NAME_TRACKING = 0.04;
const MIN_FLEET_NAME_CHARS = 4;
// How long the node a member was dropped on stays ringed.
const DROP_FLASH_MS = 1200;

// ─── Colors ──────────────────────────────────────────────────
// Accent hues (CYAN family), node fill, grid and label colors are resolved
// per theme inside the component via `useColorMode` — SVG presentation
// attributes can't reference Tailwind tokens, so the graph picks light/dark
// values explicitly. See `resolveGraphPalette`.
function resolveGraphPalette(isLight: boolean) {
  return {
    CYAN: isLight ? "var(--color-accent)" : GRAPH_COLORS.CYAN,
    CYAN_B: isLight ? "hsl(197,100%,44%)" : GRAPH_COLORS.CYAN_B,
    CYAN_DIM: isLight ? "hsl(205,22%,58%)" : GRAPH_COLORS.CYAN_DIM,
    NODE_BG: isLight ? "hsl(0,0%,100%)" : GRAPH_COLORS.NODE_BG,
    GRID_C: isLight ? "hsl(220,16%,85%)" : GRAPH_COLORS.GRID,
    // Node drag: the drop target ring and the line over a valid target
    // (DROP_OK), the line over a node it cannot drop on (DROP_NO). Non-text UI,
    // so each reaches 3:1 on the node fill: DROP_OK 5.2:1 light, 9.7:1 dark;
    // DROP_NO 4.9:1 light, 5.6:1 dark, a grey hue so valid and invalid differ.
    DROP_OK: isLight ? "hsl(197,100%,32%)" : GRAPH_COLORS.CYAN_B,
    DROP_NO: isLight ? "hsl(220,9%,46%)" : "hsl(220,10%,58%)",
    // Graph text and the halo behind edge labels follow the design-system
    // tokens, which flip with the same `.light` / `.dark` class as the canvas
    // under the map: a literal colour picked from `data-mode` could disagree
    // with the canvas and draw white names on the light page.
    LABEL: "var(--text-default-primary)",
    LABEL_HALO: "var(--bg-elevation-default)",
  };
}

// Above this many edges the map keeps its frame rate: at rest only
// the hot edges (live, touching the hovered node, in the focus) run their
// dash and particles, only the hovered or focused nodes pulse (a running
// node keeps its turning spinner), and the ambient motes are left out.
// 150 nodes with everything animated drew about 30 fps; still, about 55.
const ANIMATED_EDGE_LIMIT = 40;

// The base opacity of a live handover edge (still edges rest at
// `EDGE_REST_OPACITY`); the legend's live swatch uses the same value.
const LIVE_EDGE_OPACITY = 0.95;

// The status word a live edge's tooltip names (catalogued labels).
const LIVE_STATUS_LABEL: Record<LiveHandover["status"], string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
};

// ─── Ambient floating particles ──────────────────────────────
const AMBIENT = [
  { x: 140, y: 80, rx: 30, ry: 14, dur: 16, r: 1.0, op: 0.10 },
  { x: 1260, y: 90, rx: 24, ry: 16, dur: 13, r: 0.9, op: 0.08 },
  { x: 400, y: 540, rx: 26, ry: 12, dur: 18, r: 1.1, op: 0.07 },
  { x: 1000, y: 550, rx: 22, ry: 14, dur: 15, r: 1.0, op: 0.09 },
  { x: 280, y: 300, rx: 24, ry: 10, dur: 20, r: 0.8, op: 0.06 },
  { x: 1120, y: 280, rx: 18, ry: 12, dur: 17, r: 0.9, op: 0.07 },
  { x: 700, y: 60, rx: 32, ry: 8, dur: 22, r: 1.0, op: 0.05 },
  { x: 700, y: 560, rx: 30, ry: 10, dur: 19, r: 0.9, op: 0.05 },
  { x: 180, y: 420, rx: 18, ry: 18, dur: 14, r: 0.8, op: 0.08 },
  { x: 1220, y: 420, rx: 16, ry: 16, dur: 12, r: 0.8, op: 0.06 },
];

/** The General Assistant's node, drawn when no agent stands in for it. */
const GA_ID = "__general_assistant__";

// ─── Internal types ──────────────────────────────────────────
interface GNode {
  id: string;
  name: string;
  label: string;
  /** The drawn name's parts: office prefix and role. */
  labelParts: AgentLabel;
  /** The agent's fleet, said by its tooltip with the stored name. */
  fleetName?: string;
  /** Room across for the name, in drawing units (layouts only): the name is cut to it at render. */
  labelRoom?: number;
  r: number;
  x: number;
  y: number;
  enabled: boolean;
  isCompanyManaged: boolean;
  isEditable: boolean;
  iconKey: string;
  handoverCount: number;
  inboundCount: number;
  isGeneralAssistant: boolean;
  isLinkedGA: boolean;
  isRunning: boolean;
  outputFormat: string;
}

interface GEdge {
  id: string;
  sourceId: string;
  targetId: string;
  path: string;
  bidirectional: boolean;
  handoverType: string;
  condition: string;
  midX: number;
  midY: number;
  /** The quadratic arc of an edge drawn straight between its nodes (its label slides along it). */
  arc?: { a: { x: number; y: number }; control: { x: number; y: number }; b: { x: number; y: number } };
  /** Direction the edge leaves its source (radians, y down): badges keep off it. */
  sourceAngle: number;
  /** Direction from the target back along the edge's end (radians, y down). */
  targetAngle: number;
  /** A static view draws a two-way handover once, an arrowhead at each end. */
  twoWay?: boolean;
}

// ─── Handover type options for pickers ───────────────────────
/** The English label and description of each type the pickers offer, read through `t_i18n`. @i18n-keys */
const EDGE_TYPE_TEXT = {
  delegate: { label: "Delegate", desc: "Async background task" },
  consult: { label: "Consult", desc: "Sync Q&A, stays in control" },
  transfer: { label: "Transfer", desc: "Permanent handover" },
  mandatory_transfer: { label: "Mandatory transfer", desc: "Auto-transfer after response" },
  all: { label: "All types", desc: "Delegate, consult or transfer" },
} as const;
type EdgeTypeOption = { key: string; label: string; desc: string };
const EDGE_TYPE_OPTIONS: EdgeTypeOption[] = (["delegate", "consult", "transfer", "mandatory_transfer"] as const)
  .map(key => ({ key, ...EDGE_TYPE_TEXT[key] }));
/** Offered only for an agent whose targets run every type, where it keeps them so. */
const ALL_TYPES_EDGE_OPTION: EdgeTypeOption = { key: ALL_HANDOVER_TYPES, ...EDGE_TYPE_TEXT.all };
/**
 * One width for the type picker and the edge editor, wide enough for the
 * longest translated footer (fr: Supprimer, Annuler, Enregistrer) so no
 * button paints past the popover border.
 */
const HANDOVER_POPOVER_WIDTH = "w-72";

/** A viewport box moved into the graph container's coordinates, the box a keyboard-opened popover keeps clear. */
function keepClearBox(
  box: { top: number; bottom: number; left: number; right: number },
  container: { top: number; left: number },
): PopoverAvoidBox {
  return {
    top: box.top - container.top,
    bottom: box.bottom - container.top,
    left: box.left - container.left,
    right: box.right - container.left,
  };
}

// ─── Props ───────────────────────────────────────────────────
export interface AgentFlowGraphProps {
  agents: Agent[];
  generalAssistant?: GeneralAssistantConfig | null;
  /** Set of agent IDs that currently have running/pending assignment runs. */
  activeAgentIds?: Set<string>;
  /** Set of agent IDs the current user can edit. */
  editableAgentIds?: Set<string>;
  /** Called when the user creates a new edge via the interactive graph. */
  onAddEdge?: (sourceId: string, targetId: string, handoverType: string) => void;
  /** Called when the user edits or deletes an edge. Pass 'delete' to remove. */
  onEditEdge?: (sourceId: string, targetId: string, newTypeOrDelete: string) => void;
  /** Called when the user double-clicks a node to navigate to it. */
  onNavigateToAgent?: (agentId: string) => void;
  /** Called when the user wants to toggle focus on a node (double-click). */
  onToggleFocus?: (agentId: string) => void;
  /**
   * Called on a right click on a node (the fleet graph's node menu),
   * with the agent id and the mouse event; the page draws its own menu. The
   * browser menu is suppressed only when this is set.
   */
  onNodeContextMenu?: (agentId: string, event: React.MouseEvent) => void;
  /**
   * Nodes that may be dragged onto another node. Without
   * `onNodeDrop` nothing is draggable and a press on a node pans as before.
   */
  draggableNodeIds?: ReadonlySet<string>;
  /** Whether a dragged node may be dropped on a node; absent, any other node. */
  canDropNode?: (draggedId: string, targetId: string) => boolean;
  /**
   * Called when a dragged node is released on a node `canDropNode` accepts.
   * Return a promise resolving whether the drop was saved: the target is
   * ringed only when it resolves `true`.
   */
  onNodeDrop?: (draggedId: string, targetId: string) => void | Promise<boolean>;
  /** Added to the tooltip of a draggable node, saying what a drop does. */
  nodeDragHint?: string;
  /** Agent IDs to highlight in the graph (focus/filter mode). */
  focusedAgentIds?: Set<string> | null;
  /** `dim` greys out the agents outside the focus, `isolate` lays out the focus alone. Default `dim`. */
  focusMode?: AgentFlowFocusMode;
  /** Per-agent assignment/trigger summary, rendered as node chips (Map tab). */
  agentSummaries?: Record<string, AgentSummary>;
  /**
   * Live edges keyed "sourceId->targetId" (`liveHandoverEdges`): a handover
   * running, pending or ended in the last 15 minutes. Drawn bright and fast,
   * their tooltip names when the handover fired and its status.
   */
  liveEdges?: ReadonlyMap<string, LiveHandover>;
  /** Called when the user clicks a node's info affordance to open the detail drawer. */
  onShowDetails?: (agentId: string) => void;
  /**
   * The node whose detail drawer is open: drawn with a selection ring and its
   * handover labels shown, as on hover, while the drawer stays open.
   */
  selectedNodeId?: string | null;
  /**
   * Direction the handovers flow in: `horizontal` (left to right, the default)
   * or `vertical` (top to bottom). The hierarchy views of the playground and
   * of a fleet draw the same graph in either direction.
   */
  orientation?: AgentFlowOrientation;
  /**
   * Draw the graph with one of the deterministic layouts of
   * `lib/agent-graph-layout` (force, hierarchy top-to-bottom or left-to-right,
   * radial around the most connected agent) instead of the dagre flow: every
   * agent placed, orphans included, edges straight, the same positions on
   * every poll. `orientation` only applies when it is absent.
   */
  layout?: GraphLayout;
  /**
   * The force controls of the graph toolbar: repulsion multiplier,
   * ideal edge length and smallest distance between two nodes, in layout
   * units. Absent, the spacing grows with the number of nodes drawn.
   */
  layoutForces?: GraphForces;
  /**
   * Leave out the legend bar under the graph: a page that docks
   * `AgentGraphToolbar` under it shows `AgentFlowLegend` there instead.
   */
  hideLegend?: boolean;
  /**
   * Draw on the whole height of the parent, not only on the canvas the layout
   * asked for: a page whose card is taller than the canvas (stretched by a side
   * panel, a tall window) keeps the grid and the drawing down to its bottom
   * edge, and a pan never shows a clip line inside it. The parent is a flex
   * column with a height; the graph is never shorter than its canvas. The
   * layout itself is unchanged: the extra height only widens the view.
   */
  fillHeight?: boolean;
  /**
   * Wrap a name too long for its room on two lines instead of cutting it: a
   * read-only drawing where every name must stay readable (the fleet About
   * flow). The map and the editable graphs keep the one-line cut.
   */
  wrapNames?: boolean;
  /**
   * A fixed mini drawing embedded in a page (the fleet About flow, #9757):
   * laid out once and frozen, about `STATIC_VIEW_PX` high. No wheel zoom (the
   * wheel scrolls the page), no pan, no node move, no zoom rail. A click on a
   * node calls `onStaticOpen` with its id, a click on the canvas with null.
   */
  staticView?: boolean;
  /** The click of a static view (see `staticView`): the agent clicked, or null. */
  onStaticOpen?: (agentId: string | null) => void;
  /**
   * Nodes that stand for a group of agents (the collapsed fleet nodes of the
   * map's Fleets layer): never a handover source or target, their edges
   * never edited; a click opens them through `onNavigateToAgent`.
   */
  viewOnlyNodeIds?: ReadonlySet<string>;
  /**
   * Ghost nodes, by agent id, with the name of the group they come from (empty
   * when none): agents drawn for context, not held by the view, such as the
   * connected and external agents of a fleet page. Drawn with a dashed
   * outline and less emphasis, the group name under their own; view only like
   * `viewOnlyNodeIds`.
   */
  ghostNodes?: ReadonlyMap<string, string>;
  /**
   * More words for a ghost's tooltip, by node id (an A2A partner's integration
   * and card URLs): the full text a name cut to its room leaves out.
   */
  ghostDetails?: ReadonlyMap<string, string>;
  /**
   * The fleet the graph shows (a fleet page): its members draw their name
   * without the office every one of them repeats ("Assistant"), the agents of
   * other fleets keep their full name. Absent (the map), every node draws its
   * full name. See `nodeLabels`.
   */
  viewedFleetId?: string | null;
  /**
   * Edges (`"sourceId->targetId"`) drawn with a distinct dotted, still stroke,
   * such as the cross-fleet handovers a fleet does not govern, with
   * `dashedEdgeTooltip` as their tooltip.
   */
  dashedEdgeIds?: ReadonlySet<string>;
  /** The tooltip of a dashed edge: one text, or one per edge id. */
  dashedEdgeTooltip?: string | ((edgeId: string) => string | undefined);
  /**
   * When set, only the handovers whose `"sourceId->targetId"` is in it are
   * drawn: a caller with its own edge list (the fleet graph) keeps the drawing
   * to that list instead of every handover between the agents it passes.
   */
  edgeIds?: ReadonlySet<string>;
  /**
   * Edges (`"sourceId->targetId"`) that are not handovers, with the type they
   * are drawn as: the A2A liaisons (`lib/agent-graph-a2a.ts`), `a2a_inbound`
   * or `a2a_outbound`. Drawn in their own ink and dash, never edited; their
   * partner nodes come in `ghostNodes`. Always drawn, `edgeIds` or not.
   */
  edgeTypeOverrides?: ReadonlyMap<string, string>;
  /** Handle of the drawn graph (see AgentFlowGraphHandle). */
  ref?: React.Ref<AgentFlowGraphHandle>;
}

/**
 * Imperative handle of the graph.
 * `switchLayout(next, commit)` starts moving the drawing to `next` at once,
 * on the DOM on screen, and calls `commit` (which sets the `layout` prop to
 * `next`) when the move ends; it returns false when nothing is animated, and
 * the caller then sets the prop at once.
 */
export interface AgentFlowGraphHandle {
  switchLayout: (next: GraphLayout, commit: () => void) => boolean;
}

export type AgentFlowOrientation = "horizontal" | "vertical";

// ─── One drawn handover ──────────────────────────────────────
/** Where an edge editor opens: the pointer of a click, or the middle of the
 *  edge for Enter or Space on its focused hit area. A key
 *  also names the box to keep clear and the element the focus goes back to
 *  on close. */
type EdgeClickAt = Pick<React.MouseEvent, "clientX" | "clientY" | "stopPropagation"> & {
  keyboard?: { top: number; bottom: number; left: number; right: number; returnTo: Element | null };
};

interface EdgeRowProps {
  edge: GEdge & { tweenKey: string; leaving?: boolean; fade?: number };
  /** Rank of the edge, which staggers its particles. */
  index: number;
  /** A change is drawn: the frame loop owns the geometry, labels and particles are hidden. */
  moving: boolean;
  head: { length: number; halfWidth: number };
  edgeColor: string;
  /** The type's stroke pattern (`HANDOVER_TYPE_CONFIG.dash`), null for a solid line. */
  edgeDash: string | null;
  typeLabel: string;
  /** Accessible name of the edge: "Handover <source> to <target>, <type>". */
  edgeName: string;
  isDashed: boolean;
  dashedTip?: string;
  edgeOpacity: number;
  edgeBaseOpacity: number;
  isAnimated: boolean;
  isLive: boolean;
  /** "Handover fired at HH:MM, status" on a live edge. */
  liveTip?: string;
  isEditable: boolean;
  hasLayout: boolean;
  srcEditable: boolean;
  srcEnabled: boolean;
  edgeFocusDim: boolean;
  hoverDim: boolean;
  labelShown: boolean;
  labelX: number;
  labelY: number;
  edgeLabelSize: number;
  labelHalo: string;
  editTip: string;
  readOnlyTip: string;
  /** An A2A liaison's own tip: it is never a handover, so never the edit or read-only tip. */
  liaisonTip?: string;
  conditionTip: string;
  onEdgeClick: (sourceId: string, targetId: string, handoverType: string, sourceEditable: boolean, at: EdgeClickAt) => void;
  onHoverEdge: (id: string | null) => void;
  showTooltip: (e: React.MouseEvent, text: string, edgeId?: string) => void;
  moveTooltip: (e: React.MouseEvent) => void;
  hideTooltip: () => void;
}

/**
 * Whether a row draws the same as before. While a change is drawn
 * (on both sides) the frame loop writes each edge's `d` and its labels and
 * particles are hidden, so its geometry is left out: a layout click renders
 * only the rows whose look changed, never every row for its new path.
 */
function sameEdgeRow(a: EdgeRowProps, b: EdgeRowProps): boolean {
  for (const key of Object.keys(b) as Array<keyof EdgeRowProps>) {
    if (key === "edge" || key === "labelX" || key === "labelY" || key === "head") continue;
    if (a[key] !== b[key]) return false;
  }
  if (a.head.length !== b.head.length || a.head.halfWidth !== b.head.halfWidth) return false;
  const e = a.edge;
  const f = b.edge;
  if (
    e.id !== f.id || e.tweenKey !== f.tweenKey || e.sourceId !== f.sourceId || e.targetId !== f.targetId
    || e.handoverType !== f.handoverType || e.condition !== f.condition || e.leaving !== f.leaving
  ) return false;
  if (a.moving && b.moving) return true;
  return e.path === f.path && a.labelX === b.labelX && a.labelY === b.labelY;
}

const EdgeRow = memo(function EdgeRow({
  edge, index: ei, head, edgeColor, edgeDash, typeLabel, edgeName, isDashed, dashedTip, edgeOpacity, edgeBaseOpacity, isAnimated, isLive, liveTip,
  isEditable, hasLayout: layout, srcEditable, srcEnabled, edgeFocusDim, hoverDim, labelShown, labelX, labelY,
  edgeLabelSize, labelHalo, editTip, readOnlyTip, liaisonTip, conditionTip,
  onEdgeClick: handleEdgeClick, onHoverEdge: setHoveredEdgeId, showTooltip, moveTooltip, hideTooltip,
}: EdgeRowProps) {
  const markerId = `af-arrow-${edge.id}`;
  const labelAt = { x: labelX, y: labelY };
  // The edge is named once for assistive tech and scripted tests: on
  // its hit area as a button when a click edits it, else on the drawn line.
  const editableHit = isEditable && srcEditable && !edge.leaving;
  const hitRef = useRef<SVGPathElement>(null);
  // Enter or Space on the focused edge opens its editor at the edge's middle,
  // as a click does at the pointer (buttonRoleProps: role, tab stop, keys).
  // The editor then keeps clear of the edge's box and takes the focus.
  const keyboardEdit = buttonRoleProps(editableHit ? () => {
    const r = hitRef.current?.getBoundingClientRect();
    handleEdgeClick(edge.sourceId, edge.targetId, edge.handoverType, srcEditable, {
      clientX: r ? r.left + r.width / 2 : 0,
      clientY: r ? r.top + r.height / 2 : 0,
      stopPropagation: () => {},
      keyboard: { top: r?.top ?? 0, bottom: r?.bottom ?? 0, left: r?.left ?? 0, right: r?.right ?? 0, returnTo: hitRef.current },
    });
  } : undefined);
  // The hit path and the label under the pointer share one hover: the label
  // is drawn while the edge is hovered, so if it took the pointer away from
  // the hit path the edge would lose its hover, the label would go, and the
  // pointer would rest on the path with no new mouseenter (#9106).
  const hitTip = liaisonTip ?? (dashedTip ? dashedTip : isEditable ? (srcEditable ? editTip : readOnlyTip) : liveTip);
  const enterEdge = (e: React.MouseEvent) => {
    setHoveredEdgeId(edge.id);
    // On a live edge the parent already leads each action tip with the live line.
    // The tip keeps clear of this edge's own type label, drawn on hover.
    if (hitTip) showTooltip(e, hitTip, edge.id);
  };
  const labelRef = useRef<SVGTextElement>(null);
  const leaveEdge = (e: React.MouseEvent) => {
    // Moving between the hit path and the label stays on the edge.
    const to = e.relatedTarget;
    if (to && (to === hitRef.current || to === labelRef.current)) return;
    setHoveredEdgeId(null);
    hideTooltip();
  };
  const labelClickable = isEditable && srcEditable;
  return (
    <>
      <defs>
        <marker
          id={markerId}
          data-arrow-length={head.length.toFixed(2)}
          markerWidth={head.length} markerHeight={head.halfWidth * 2}
          refX={head.length} refY={head.halfWidth}
          orient="auto-start-reverse" markerUnits="userSpaceOnUse"
        >
          <path
            d={`M 0 0 L ${head.length} ${head.halfWidth} L 0 ${head.halfWidth * 2} L ${head.length * 0.25} ${head.halfWidth} Z`}
            fill={edgeColor} opacity="0.9"
          />
        </marker>
      </defs>
      <path
        d={edge.path} fill="none"
        role={editableHit || edge.leaving ? undefined : "img"}
        aria-label={editableHit || edge.leaving ? undefined : edgeName}
        aria-hidden={editableHit || edge.leaving ? true : undefined}
        data-edge-id={edge.id}
        stroke={edgeColor}
        strokeWidth={isDashed ? 1.2 : 0.8}
        opacity={edgeOpacity}
        {...{ [TWEEN_EDGE_ATTR]: edge.tweenKey, [BASE_OPACITY_ATTR]: edgeBaseOpacity }}
        strokeDasharray={isDashed ? "1.5 3" : edgeDash ?? undefined}
        strokeLinecap={isDashed ? "round" : undefined}
        data-edge-dashed={isDashed ? "true" : undefined}
        markerEnd={`url(#${markerId})`}
        markerStart={edge.twoWay ? `url(#${markerId})` : undefined}
        data-edge-two-way={edge.twoWay ? "true" : undefined}
        // A solid type has no dash to march: its particles carry the motion.
        className={isDashed || !edgeDash || !isAnimated ? undefined : PAUSE_MOVING_CLASS}
        style={{
          animation: isDashed || !isAnimated ? undefined : dashAnimation(edgeDash, isLive),
        }}
      />
      {isDashed && !isEditable && !layout && dashedTip && (
        <path
          {...{ [TWEEN_EDGE_ATTR]: edge.tweenKey }}
          d={edge.path}
          fill="none"
          stroke="transparent"
          strokeWidth={14}
          onMouseEnter={(e) => showTooltip(e, dashedTip)}
          onMouseMove={moveTooltip}
          onMouseLeave={hideTooltip}
        />
      )}
      {(isEditable || layout || liveTip) && !edge.leaving && (
        <path
          {...{ [TWEEN_EDGE_ATTR]: edge.tweenKey }}
          d={edge.path}
          fill="none"
          stroke="transparent"
          strokeWidth={14}
          ref={hitRef}
          {...keyboardEdit}
          {...{ [EDGE_HIT_ATTR]: editableHit ? "true" : undefined }}
          aria-label={editableHit ? edgeName : undefined}
          aria-hidden={editableHit ? undefined : true}
          style={{ cursor: isEditable && srcEditable ? "pointer" : "default" }}
          onClick={isEditable ? (e) => handleEdgeClick(edge.sourceId, edge.targetId, edge.handoverType, srcEditable, e) : undefined}
          onFocus={editableHit ? () => setHoveredEdgeId(edge.id) : undefined}
          onBlur={editableHit ? () => setHoveredEdgeId(null) : undefined}
          onMouseEnter={enterEdge}
          onMouseMove={isEditable || dashedTip || liveTip || liaisonTip ? moveTooltip : undefined}
          onMouseLeave={leaveEdge}
        />
      )}
      {srcEnabled && !edgeFocusDim && labelShown && (
        <text
          className={HIDE_MOVING_CLASS}
          x={labelAt.x} y={labelAt.y + edgeLabelSize * 0.35}
          textAnchor="middle"
          fill={edgeColor}
          stroke={layout ? labelHalo : undefined}
          strokeWidth={layout ? edgeLabelSize * 0.35 : undefined}
          paintOrder="stroke"
          fontSize={edgeLabelSize}
          fontWeight="500"
          fontFamily="'IBM Plex Sans', sans-serif"
          opacity="0.6"
          ref={labelRef}
          data-edge-label={edge.id}
          pointerEvents={labelClickable ? undefined : "none"}
          style={labelClickable ? { cursor: "pointer" } : undefined}
          onClick={labelClickable ? (e) => handleEdgeClick(edge.sourceId, edge.targetId, edge.handoverType, srcEditable, e) : undefined}
          onMouseEnter={labelClickable ? enterEdge : undefined}
          onMouseMove={labelClickable ? moveTooltip : undefined}
          onMouseLeave={labelClickable ? leaveEdge : undefined}
        >
          {typeLabel}
        </text>
      )}
      {/* Condition icon — small route/filter picto after the type label */}
      {srcEnabled && !edgeFocusDim && labelShown && edge.condition && (() => {
        const labelHalfW = typeLabel.length * edgeLabelSize * 0.31;
        const ix = labelAt.x + labelHalfW + 2;
        // The label's visual centre is labelAt.y (baseline 0.35 x the font size under it).
        const textCenterY = labelAt.y;
        return (
          <g
            className={HIDE_MOVING_CLASS}
            style={{ cursor: "default" }}
            onMouseEnter={(e) => showTooltip(e, conditionTip)}
            onMouseMove={moveTooltip}
            onMouseLeave={hideTooltip}
          >
            {/* Funnel / filter icon centered on textCenterY */}
            <path
              d={`M ${ix - 2.2} ${textCenterY - 2} L ${ix + 2.2} ${textCenterY - 2} L ${ix + 0.5} ${textCenterY + 0.3} L ${ix + 0.5} ${textCenterY + 2} L ${ix - 0.5} ${textCenterY + 2} L ${ix - 0.5} ${textCenterY + 0.3} Z`}
              fill={edgeColor}
              opacity="0.7"
            />
          </g>
        );
      })()}
      {srcEnabled && !edgeFocusDim && isAnimated && !hoverDim && !edge.leaving && (
        <g className={HIDE_MOVING_CLASS}>
          <BeamParticle
            path={edge.path}
            dur={3.0 + (ei * 0.5) % 2}
            delay={(ei * 1.2) % 5}
            color={edgeColor}
          />
          <BeamParticle
            path={edge.path}
            dur={3.5 + (ei * 0.7) % 2}
            delay={1.5 + (ei * 0.9) % 4}
            color={edgeColor}
            size={1.0}
          />
        </g>
      )}
    </>
  );
}, sameEdgeRow);

// ─── Main component ─────────────────────────────────────────
// ─── Zoom / Pan constants ────────────────────────────────────
const MIN_ZOOM = VIEW_ZOOM.min;
const MAX_ZOOM = VIEW_ZOOM.max;
const ZOOM_BTN_STEP = 0.10; // for +/- buttons
// A press on a draggable node becomes a drag past this many screen pixels; under it, a click.
const NODE_DRAG_THRESHOLD_PX = 4;

export function AgentFlowGraph({
  agents,
  generalAssistant,
  activeAgentIds: activeAgentIdsProp,
  editableAgentIds,
  onAddEdge,
  onEditEdge,
  onNavigateToAgent,
  onToggleFocus,
  onNodeContextMenu,
  draggableNodeIds,
  canDropNode,
  onNodeDrop,
  nodeDragHint,
  focusedAgentIds,
  focusMode = "dim",
  agentSummaries,
  liveEdges: liveEdgesProp,
  onShowDetails,
  selectedNodeId = null,
  orientation = "horizontal",
  layout,
  layoutForces,
  hideLegend = false,
  fillHeight = false,
  wrapNames = false,
  staticView = false,
  onStaticOpen,
  viewOnlyNodeIds,
  ghostNodes,
  ghostDetails,
  viewedFleetId = null,
  dashedEdgeIds,
  dashedEdgeTooltip,
  edgeIds,
  edgeTypeOverrides,
  ref,
}: AgentFlowGraphProps) {
  const { t_i18n, locale, intl } = useFormatter();
  // The polled inputs held by content: a poll tick that rebuilds the same
  // running agents or live edges as new objects hands the drawing the objects
  // it already drew, so nothing is recomputed or redrawn for it.
  const activeAgentIds = useByContent(activeAgentIdsProp, idsKey(activeAgentIdsProp));
  const liveEdges = useByContent(liveEdgesProp, liveEdgesKey(liveEdgesProp));
  // A group node or a ghost: opened on click, never a handover endpoint.
  const isViewOnlyId = useCallback(
    (id: string) => !!viewOnlyNodeIds?.has(id) || !!ghostNodes?.has(id),
    [viewOnlyNodeIds, ghostNodes],
  );
  // A ghost's group line under its name: 10 px on screen in a static view.
  const subScale = staticView ? STATIC_SUB_LABEL_SCALE : SUB_LABEL_SCALE;
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  // The drawn width of the svg, so text keeps a fixed screen size, and the
  // tallest canvas (drawing units) that keeps the canvas and the toolbar under
  // it inside the first viewport.
  const [svgWidth, setSvgWidth] = useState(W);
  const [viewportMaxH, setViewportMaxH] = useState(MAX_H);
  // The drawn height of the svg (px), read only when it fills its parent:
  // the view then shows as many drawing units down as the box holds.
  const [svgHeightPx, setSvgHeightPx] = useState(0);
  // The drawn width in 80 px steps: what the org chart's port spacing reads,
  // so a pixel of resize does not re-lay the graph.
  const svgWidthStep = Math.max(80, Math.round(svgWidth / 80) * 80);
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      // An environment that lays nothing out (no width, or none at all) keeps
      // the drawing's own sizes.
      const width = svg.clientWidth;
      if (!Number.isFinite(width) || width <= 0) return;
      setSvgWidth(width);
      const box = svg.getBoundingClientRect();
      if (fillHeight && Number.isFinite(box.height) && box.height > 0) setSvgHeightPx(box.height);
      const top = box.top + (window.scrollY || 0);
      const room = window.innerHeight - top - TOOLBAR_RESERVE_PX;
      if (!Number.isFinite(room)) return;
      // Rounded to 20 units so a scrollbar or a pixel does not re-lay the graph.
      setViewportMaxH(Math.max(H, Math.min(MAX_H, Math.floor((room * W) / width / 20) * 20)));
    };
    measure();
    // A resize of the box (a sibling panel or the toolbar growing a line, a
    // window resize) commits the new view before the browser paints: the svg
    // stretches its drawing to the box (preserveAspectRatio none), so a view
    // written one frame later painted the whole graph stretched for a frame,
    // a flash of the full drawing every time a polled caption beside it
    // changed height.
    const measureNow = () => flushSync(measure);
    const observer = new ResizeObserver(measureNow);
    observer.observe(svg);
    window.addEventListener("resize", measureNow);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measureNow);
    };
  }, [fillHeight]);
  // `edgeId`: the hovered edge, whose drawn type label the tooltip keeps clear of.
  const [tooltip, setTooltip] = useState<{ text: string; x: number; y: number; w: number; h: number; edgeId?: string } | null>(null);

  const colorMode = useColorMode();
  const { CYAN, CYAN_B, CYAN_DIM, NODE_BG, GRID_C, LABEL, LABEL_HALO, DROP_OK, DROP_NO } = resolveGraphPalette(colorMode === "light");

  const isEditable = !!onAddEdge;

  // ── Interactive edge creation state ──
  const [connectSource, setConnectSource] = useState<string | null>(null);
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null);
  const [typePicker, setTypePicker] = useState<{
    sourceId: string;
    targetId: string;
    selectedType: string;
    offerAllTypes: boolean;
    x: number;
    y: number;
    /** Opened from the keyboard: the box of the target node kept clear. */
    avoid?: PopoverAvoidBox;
  } | null>(null);

  // ── Edge edit popover state ──
  const [edgeEditor, setEdgeEditor] = useState<{
    sourceId: string;
    targetId: string;
    currentType: string;
    selectedType: string;
    x: number;
    y: number;
    /** Opened from the keyboard: the box of the edge kept clear. */
    avoid?: PopoverAvoidBox;
    /** Opened from the keyboard: the boxes of the edge's end nodes, kept clear when the canvas allows it. */
    alsoAvoid?: PopoverAvoidBox[];
  } | null>(null);
  // The element a popover opened from the keyboard gives the focus back to on close.
  const popoverReturnRef = useRef<Element | null>(null);
  useEffect(() => {
    if (typePicker || edgeEditor) return;
    const back = popoverReturnRef.current;
    popoverReturnRef.current = null;
    if (back instanceof HTMLElement || back instanceof SVGElement) {
      if (back.isConnected) back.focus();
    }
  }, [typePicker, edgeEditor]);

  // ── Zoom / Pan state ──────────────────────────────────────────
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const clickTimer = useRef<number | null>(null);
  const [ctrlHeld, setCtrlHeld] = useState(false);
  // The node under the pointer or the keyboard focus: its name is
  // drawn whole, its handover labels and its badges shown; with a layout
  // they are hidden otherwise, so 200 agents and 130 handovers stay legible.
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  // Track Ctrl/Cmd key state for cursor feedback
  useEffect(() => {
    const down = (e: KeyboardEvent) => { if (e.key === "Control" || e.key === "Meta") setCtrlHeld(true); };
    const up = (e: KeyboardEvent) => { if (e.key === "Control" || e.key === "Meta") setCtrlHeld(false); };
    const blur = () => setCtrlHeld(false);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); window.removeEventListener("blur", blur); };
  }, []);


  // ── Node drag: a draggable node dropped on another, the caller says what it means ──
  const nodeDragRef = useRef<{ id: string; x: number; y: number; moved: boolean; targetId: string | null; valid: boolean } | null>(null);
  const [nodeDrag, setNodeDrag] = useState<{ id: string; pos: { x: number; y: number } | null; targetId: string | null; valid: boolean } | null>(null);
  // A release after a drag must not also count as a click (start a handover, open a ghost).
  const suppressClickRef = useRef(false);
  // The node a handover is being dragged from by its connect handle:
  // a release on another agent ends the handover there.
  const connectDragRef = useRef<string | null>(null);
  // The drawing's viewBox, read by the pointer handlers to place the drag line.
  const viewBoxRef = useRef({ x: 0, y: 0, w: W, h: H });

  // ── Node pin: a node moved by hand stays where it is dropped ──
  // The pins hold for the drawing they were made on (`base`, the layout's
  // nodes): a new layout or new agents drop them, with no effect to run.
  const [pinState, setPinState] = useState<{ base: unknown; pins: Map<string, { x: number; y: number }> }>({ base: null, pins: new Map() });
  const pinBaseRef = useRef<unknown>(null);
  const nodeMoveRef = useRef<{ id: string; x: number; y: number; moved: boolean; at?: { x: number; y: number } } | null>(null);
  // The nodes as drawn and the room a glyph keeps, read when a move ends: a
  // drop never lands on another glyph.
  // With the names as drawn and the screen clearance a glyph keeps from them
  // (`pad`): a drop never covers a neighbour's name either.
  const dropRoomRef = useRef<{
    nodes: { id: string; x: number; y: number; r: number }[];
    gap: number;
    names: (NameBox & { id: string })[];
    pad: number;
  }>({ nodes: [], gap: 0, names: [], pad: 0 });
  // The node being moved: the drawing follows the pointer at once, untweened.
  const [movingId, setMovingId] = useState<string | null>(null);

  const startNodeMove = useCallback((nodeId: string, e: React.PointerEvent) => {
    suppressClickRef.current = false;
    if (e.button !== 0 || e.ctrlKey || e.metaKey) return;
    // The canvas does not pan under a node that may be moved.
    e.stopPropagation();
    nodeMoveRef.current = { id: nodeId, x: e.clientX, y: e.clientY, moved: false };
  }, []);

  // Pins `id` at `at` on the drawing shown now; pins made on another drawing are dropped.
  const movePin = useCallback((id: string, at: { x: number; y: number }) => {
    const base = pinBaseRef.current;
    setPinState((s) => ({ base, pins: withPin(s.base === base ? s.pins : new Map(), id, at) }));
  }, [setPinState]);

  const startNodeDrag = useCallback((nodeId: string, e: React.PointerEvent) => {
    suppressClickRef.current = false;
    if (e.button !== 0 || !onNodeDrop || !draggableNodeIds?.has(nodeId)) return;
    // The canvas does not pan under a node that may be dragged.
    e.stopPropagation();
    nodeDragRef.current = { id: nodeId, x: e.clientX, y: e.clientY, moved: false, targetId: null, valid: false };
  }, [onNodeDrop, draggableNodeIds]);

  const cancelNodeDrag = useCallback(() => {
    nodeDragRef.current = null;
    setNodeDrag(null);
  }, []);

  // After a drop: the node dropped on is ringed for a moment, so the
  // new parent is seen on the graph and not only in the toast.
  const [dropFlashId, setDropFlashId] = useState<string | null>(null);
  const dropFlashTimer = useRef<number | null>(null);
  const flashDropTarget = useCallback((id: string) => {
    if (dropFlashTimer.current) window.clearTimeout(dropFlashTimer.current);
    setDropFlashId(id);
    dropFlashTimer.current = window.setTimeout(() => {
      dropFlashTimer.current = null;
      setDropFlashId(null);
    }, DROP_FLASH_MS);
  }, []);
  useEffect(() => () => { if (dropFlashTimer.current) window.clearTimeout(dropFlashTimer.current); }, []);

  // Pan via mouse drag: the drawing follows the pointer one for one, whatever
  // the drawn width of the svg.
  const handlePointerDown = useCallback((e: React.PointerEvent) => {
    // A new press: a release that fired no click never swallows this one.
    suppressClickRef.current = false;
    // Only pan on left button, ignore when clicking on interactive elements;
    // a static view never pans.
    if (e.button !== 0 || staticView) return;
    setIsPanning(true);
    panStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  }, [pan, staticView]);

  const handlePointerMove = useCallback((e: React.PointerEvent) => {
    const move = nodeMoveRef.current;
    if (move) {
      if (!move.moved && Math.hypot(e.clientX - move.x, e.clientY - move.y) < NODE_DRAG_THRESHOLD_PX) return;
      if (!move.moved) setMovingId(move.id);
      move.moved = true;
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      const vb = viewBoxRef.current;
      const at = { x: vb.x + ((e.clientX - rect.left) / rect.width) * vb.w, y: vb.y + ((e.clientY - rect.top) / rect.height) * vb.h };
      setTooltip(null);
      move.at = at;
      movePin(move.id, at);
      return;
    }
    const drag = nodeDragRef.current;
    if (drag) {
      if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < NODE_DRAG_THRESHOLD_PX) return;
      drag.moved = true;
      const rect = svgRef.current?.getBoundingClientRect();
      const vb = viewBoxRef.current;
      const pos = rect && rect.width > 0 && rect.height > 0
        ? { x: vb.x + ((e.clientX - rect.left) / rect.width) * vb.w, y: vb.y + ((e.clientY - rect.top) / rect.height) * vb.h }
        : null;
      const overId = (e.target as Element | null)?.closest?.("[data-node-id]")?.getAttribute("data-node-id") ?? null;
      const targetId = overId && overId !== drag.id ? overId : null;
      drag.targetId = targetId;
      drag.valid = !!targetId && (canDropNode ? canDropNode(drag.id, targetId) : true);
      setTooltip(null);
      setNodeDrag({ id: drag.id, pos, targetId, valid: drag.valid });
      return;
    }
    if (!isPanning) return;
    const unitsPerPx = W / Math.max(1, svgRef.current?.clientWidth || W);
    const dx = (e.clientX - panStart.current.x) * unitsPerPx;
    const dy = (e.clientY - panStart.current.y) * unitsPerPx;
    setPan({ x: panStart.current.panX + dx, y: panStart.current.panY + dy });
  }, [isPanning, canDropNode, movePin]);

  const handlePointerUp = useCallback(() => {
    setIsPanning(false);
    connectDragRef.current = null;
    const move = nodeMoveRef.current;
    if (move) {
      nodeMoveRef.current = null;
      setTooltip(null);
      setMovingId(null);
      // The release of a move is not a click (start a handover, open a ghost).
      if (move.moved) suppressClickRef.current = true;
      // Dropped on or against another agent: moved to the nearest free spot.
      if (move.moved && move.at) {
        const { nodes: drawn, gap, names, pad } = dropRoomRef.current;
        const self = drawn.find((n) => n.id === move.id);
        const others = drawn.filter((n) => n.id !== move.id);
        const r = self?.r ?? 0;
        const otherNames = names.filter((b) => b.id !== move.id);
        const spot = freeSpot(move.at, others, 2 * r + gap, otherNames, r + pad);
        if (spot !== move.at) movePin(move.id, spot);
      }
      return;
    }
    const drag = nodeDragRef.current;
    if (!drag) return;
    cancelNodeDrag();
    if (!drag.moved) return;
    suppressClickRef.current = true;
    if (drag.valid && drag.targetId && onNodeDrop) {
      const targetId = drag.targetId;
      const saved = onNodeDrop(drag.id, targetId);
      // A drop saved later rings its target only once saved: a
      // failed save must not show a parent the member never got.
      if (saved instanceof Promise) {
        void saved.then((ok) => { if (ok) flashDropTarget(targetId); }, () => undefined);
      } else {
        flashDropTarget(targetId);
      }
    }
  }, [cancelNodeDrag, onNodeDrop, movePin, flashDropTarget]);

  // Leaving the canvas ends a pan and cancels a node drag: nothing is dropped.
  const handlePointerLeave = useCallback(() => {
    setIsPanning(false);
    connectDragRef.current = null;
    nodeMoveRef.current = null;
    setMovingId(null);
    cancelNodeDrag();
  }, [cancelNodeDrag]);

  // Zoom controls
  const zoomIn = useCallback(() => setZoom(z => Math.min(MAX_ZOOM, z * (1 + ZOOM_BTN_STEP))), []);
  const zoomOut = useCallback(() => setZoom(z => Math.max(MIN_ZOOM, z * (1 - ZOOM_BTN_STEP))), []);
  const resetView = useCallback(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, []);

  // The mouse wheel and a trackpad pinch zoom at the pointer, as
  // d3-zoom does on the OpenCTI graph: the page never scrolls under the
  // canvas, and the point under the pointer stays under it.
  const viewRef = useRef({ zoom: 1, pan: { x: 0, y: 0 }, canvasH: H, viewH: H });
  viewRef.current.zoom = zoom;
  viewRef.current.pan = pan;
  useEffect(() => {
    const el = containerRef.current;
    // A static view leaves the wheel to the page: it scrolls over the drawing.
    if (!el || staticView) return;
    const handleWheel = (e: WheelEvent) => {
      const svg = svgRef.current;
      if (!svg) return;
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const { zoom: z, pan: p, canvasH: ch, viewH: vh } = viewRef.current;
      const next = wheelZoom(
        { zoom: z, pan: p },
        { fx: (e.clientX - rect.left) / rect.width, fy: (e.clientY - rect.top) / rect.height },
        wheelFactor(e),
        W,
        ch,
        vh,
      );
      if (next.zoom === z) return;
      setZoom(next.zoom);
      setPan(next.pan);
    };
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, [staticView]);

  // ── Escape key cancels all interactive modes ──
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        connectDragRef.current = null;
        nodeMoveRef.current = null;
        setMovingId(null);
        nodeDragRef.current = null;
        setNodeDrag(null);
        setConnectSource(null);
        setMousePos(null);
        setTypePicker(null);
        setEdgeEditor(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const showTooltip = useCallback((e: React.MouseEvent, text: string, edgeId?: string) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setTooltip({ text, x: e.clientX - rect.left, y: e.clientY - rect.top, w: rect.width, h: rect.height, edgeId });
  }, []);

  const moveTooltip = useCallback((e: React.MouseEvent) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setTooltip(prev => prev ? { ...prev, x: e.clientX - rect.left, y: e.clientY - rect.top, w: rect.width, h: rect.height } : null);
  }, []);

  const hideTooltip = useCallback(() => setTooltip(null), []);

  // The hover tooltip is centred above the pointer, then kept inside the
  // canvas on both axes: near an edge it shifts back in, near the top
  // it flips below the pointer, and it never covers the zoom controls nor
  // the hovered edge's own type label (#12347 UX r3 B3).
  // Its size is only known once drawn, so the position is set before paint.
  const tooltipRef = useRef<HTMLDivElement>(null);
  const zoomRailRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = tooltipRef.current;
    if (!el || !tooltip) return;
    const rail = zoomRailRef.current;
    const container = containerRef.current;
    let keepClear: { left: number; top: number; width: number; height: number } | null = null;
    if (tooltip.edgeId && container) {
      const label = [...container.querySelectorAll<SVGTextElement>("[data-edge-label]")]
        .find((el) => el.getAttribute("data-edge-label") === tooltip.edgeId);
      const box = label?.getBoundingClientRect();
      const at = container.getBoundingClientRect();
      if (box && box.width > 0) keepClear = { left: box.left - at.left, top: box.top - at.top, width: box.width, height: box.height };
    }
    const { left, top } = graphTooltipPosition({
      pointerX: tooltip.x,
      pointerY: tooltip.y,
      tooltipWidth: el.offsetWidth,
      tooltipHeight: el.offsetHeight,
      containerWidth: tooltip.w,
      containerHeight: tooltip.h,
      avoid: rail ? { left: rail.offsetLeft, top: rail.offsetTop, width: rail.offsetWidth, height: rail.offsetHeight } : null,
      keepClear,
    });
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }, [tooltip]);

  // ── Node click handler ──
  const handleNodeClick = useCallback((nodeId: string, nodeIsEditable: boolean, nodeIsGeneralAssistant: boolean, e: EdgeClickAt) => {
    // A group node opens; it never starts or ends a handover.
    if (isViewOnlyId(nodeId)) {
      e.stopPropagation();
      setTypePicker(null);
      setEdgeEditor(null);
      setConnectSource(null);
      setMousePos(null);
      onNavigateToAgent?.(nodeId);
      return;
    }
    // A plain click selects the agent and opens its drawer; a
    // handover starts from the node's connect handle, or Space.
    if (!connectSource && onShowDetails && !nodeIsGeneralAssistant && !e.keyboard) {
      e.stopPropagation();
      if (typePicker) { setTypePicker(null); return; }
      if (edgeEditor) { setEdgeEditor(null); return; }
      onShowDetails(nodeId);
      return;
    }
    if (!isEditable) return;
    e.stopPropagation();

    // If type picker or edge editor is open, close it
    if (typePicker) { setTypePicker(null); return; }
    if (edgeEditor) { setEdgeEditor(null); return; }

    if (!connectSource) {
      // Start connection — only from editable nodes
      if (!nodeIsEditable) return;
      setConnectSource(nodeId);
    } else if (connectSource === nodeId) {
      // Cancel — clicked same node
      setConnectSource(null);
      setMousePos(null);
    } else {
      if (nodeIsGeneralAssistant) {
        setConnectSource(null);
        setMousePos(null);
        return;
      }
      // Complete connection — show type picker
      const containerRect = containerRef.current?.getBoundingClientRect();
      if (containerRect) {
        const source = agents.find((a) => a.id === connectSource);
        const offerAllTypes = !!source && runsEveryHandoverType(source.handover_agent_ids || [], source.handover_types);
        // The node's hover tooltip never stays drawn over the picker.
        setTooltip(null);
        popoverReturnRef.current = e.keyboard?.returnTo ?? null;
        setTypePicker({
          sourceId: connectSource,
          targetId: nodeId,
          selectedType: offerAllTypes ? ALL_HANDOVER_TYPES : "delegate",
          offerAllTypes,
          x: e.clientX - containerRect.left,
          y: e.clientY - containerRect.top,
          avoid: e.keyboard ? keepClearBox(e.keyboard, containerRect) : undefined,
        });
      }
      setConnectSource(null);
      setMousePos(null);
    }
  }, [isEditable, connectSource, typePicker, edgeEditor, agents, isViewOnlyId, onNavigateToAgent, onShowDetails]);

  // A press on a node's connect handle starts a handover from it,
  // the gesture a plain click made before; the canvas neither pans nor moves the node.
  const startConnectDrag = useCallback((nodeId: string, e: React.PointerEvent) => {
    suppressClickRef.current = false;
    if (e.button !== 0) return;
    e.stopPropagation();
    if (clickTimer.current) { clearTimeout(clickTimer.current); clickTimer.current = null; }
    setTypePicker(null);
    setEdgeEditor(null);
    setTooltip(null);
    connectDragRef.current = nodeId;
    setConnectSource(nodeId);
  }, []);

  // ── Edge click handler ──
  const handleEdgeClick = useCallback((sourceId: string, targetId: string, handoverType: string, sourceEditable: boolean, e: EdgeClickAt) => {
    if (!onEditEdge || !sourceEditable) return;
    e.stopPropagation();
    const containerRect = containerRef.current?.getBoundingClientRect();
    if (!containerRect) return;
    // The hover tooltip never stays drawn over the editor.
    setTooltip(null);
    popoverReturnRef.current = e.keyboard?.returnTo ?? null;
    // The end nodes are kept clear too, so the editor never hides whom the edge links.
    const endBoxes = e.keyboard
      ? [sourceId, targetId]
        .map((id) => svgRef.current?.querySelector(`g[data-node-id="${CSS.escape(id)}"]`)?.getBoundingClientRect())
        .filter((r): r is DOMRect => !!r && (r.width > 0 || r.height > 0))
        .map((r) => keepClearBox(r, containerRect))
      : [];
    setEdgeEditor({
      sourceId,
      targetId,
      currentType: handoverType,
      selectedType: handoverType,
      x: e.clientX - containerRect.left,
      y: e.clientY - containerRect.top,
      avoid: e.keyboard ? keepClearBox(e.keyboard, containerRect) : undefined,
      alsoAvoid: endBoxes.length > 0 ? endBoxes : undefined,
    });
  }, [onEditEdge]);

  // ── Clamp popover to container bounds ──
  const clampPopover = useCallback((el: HTMLDivElement | null) => {
    if (!el || !containerRef.current) return;
    const container = containerRef.current.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    // Opened from the keyboard: the box it was opened from is kept clear, below,
    // above or beside it; placePopover is the geometry.
    const box = ["avoidTop", "avoidBottom", "avoidLeft", "avoidRight"].map((k) => Number(el.dataset[k]));
    const avoid = el.dataset.avoidTop !== undefined && box.every(Number.isFinite)
      ? { top: box[0], bottom: box[1], left: box[2], right: box[3] }
      : undefined;
    let alsoAvoid: PopoverAvoidBox[] | undefined;
    try {
      alsoAvoid = el.dataset.avoidAlso ? (JSON.parse(el.dataset.avoidAlso) as PopoverAvoidBox[]) : undefined;
    } catch {
      alsoAvoid = undefined;
    }
    const { left, top } = placePopover({
      x: parseFloat(el.style.left) || 0,
      y: parseFloat(el.style.top) || 0,
      width: rect.width,
      height: rect.height,
      containerWidth: container.width,
      containerHeight: container.height,
      avoid,
      alsoAvoid,
    });
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.transform = "none";
  }, []);

  // ── Type picker confirm ──
  const handlePickType = useCallback(() => {
    if (typePicker && onAddEdge) {
      onAddEdge(typePicker.sourceId, typePicker.targetId, typePicker.selectedType);
    }
    setTypePicker(null);
  }, [typePicker, onAddEdge]);

  // ── Click on empty space closes popovers / cancels connection ──
  const handleContainerClick = useCallback((e: React.MouseEvent) => {
    if (suppressClickRef.current) { suppressClickRef.current = false; return; }
    if (isPanning) return;
    const target = e.target as HTMLElement;
    if (connectSource && !target.closest("[data-node-id]")) {
      setConnectSource(null);
      setMousePos(null);
    }
    if (typePicker && !target.closest("[data-type-picker]")) {
      setTypePicker(null);
    }
    if (edgeEditor && !target.closest("[data-edge-editor]")) {
      setEdgeEditor(null);
    }
  }, [connectSource, typePicker, edgeEditor, isPanning]);

  // ── Content-based fingerprints ───────────────────────────────
  const agentKey = useMemo(
    () => agents.map(a =>
      `${a.id}|${a.name}|${a.fleet?.fleet_id ?? ""}|${a.fleet?.fleet_name ?? ""}|${a.enabled}|${a.is_company_managed}|${a.icon}|${a.output_format}|${(a.handover_agent_ids || []).join(",")}|${JSON.stringify(a.handover_types || {})}|${JSON.stringify(a.handover_conditions || {})}`
    ).sort().join("\n"),
    [agents],
  );
  // The agent answering in the General Assistant's place replaces its node.
  const standInId = generalAssistantStandIn(generalAssistant?.linked_agent_id, agents)?.id ?? null;
  const gaKey = useMemo(
    () => generalAssistant
      ? `ga|${standInId ?? ""}|${(generalAssistant.handover_agent_ids || []).join(",")}|${JSON.stringify(generalAssistant.handover_types ?? {})}`
      : "",
    [generalAssistant, standInId],
  );
  const standInRef = useRef(standInId);
  standInRef.current = standInId;
  const editableKey = useMemo(
    () => [...(editableAgentIds ?? [])].sort().join(","),
    [editableAgentIds],
  );
  const agentsRef = useRef(agents);
  agentsRef.current = agents;
  const gaRef = useRef(generalAssistant);
  gaRef.current = generalAssistant;
  const editableRef = useRef(editableAgentIds);
  editableRef.current = editableAgentIds;
  const viewedFleetRef = useRef(viewedFleetId);
  viewedFleetRef.current = viewedFleetId;
  const edgeIdsKey = useMemo(() => (edgeIds ? [...edgeIds].sort().join(",") : null), [edgeIds]);
  const edgeIdsRef = useRef(edgeIds);
  edgeIdsRef.current = edgeIds;
  const overridesKey = useMemo(
    () => (edgeTypeOverrides ? [...edgeTypeOverrides].map(([k, v]) => `${k}=${v}`).sort().join(",") : ""),
    [edgeTypeOverrides],
  );
  const overridesRef = useRef(edgeTypeOverrides);
  overridesRef.current = edgeTypeOverrides;
  const ghostNodesRef = useRef(ghostNodes);
  ghostNodesRef.current = ghostNodes;
  const ghostKey = useMemo(() => (ghostNodes ? [...ghostNodes.keys()].sort().join(",") : ""), [ghostNodes]);
  // The nodes the graph can draw: a focused id that is none of them (a stale
  // `?focus=` link, a deleted agent) focuses nothing.
  const graphIds = useMemo(() => {
    const ids = new Set(agents.map(a => a.id));
    if (generalAssistant && !standInId) ids.add(GA_ID);
    return ids;
  }, [agents, generalAssistant, standInId]);
  const shownFocus = useMemo(
    () => focusOnGraph(focusedAgentIds, graphIds),
    [focusedAgentIds, graphIds],
  );
  // Isolating re-lays the map out, so the focus joins the layout fingerprints.
  const isolatedKey = focusMode === "isolate" && shownFocus ? [...shownFocus].sort().join(",") : "";

  // ── Layout computation ──
  // A function of the layout, so a layout click can compute its target before
  // React renders it (`previewLayout` below).
  const computeGraph = useCallback((layout: GraphLayout | undefined) => {
    const curAgents = agentsRef.current;
    const curGA = gaRef.current;
    const curStandInId = standInRef.current;
    const curEditable = editableRef.current;
    const agentMap = new Map(curAgents.map(a => [a.id, a]));

    const sortedAgents = [...curAgents].sort((a, b) => a.id.localeCompare(b.id));
    // The one rule for an agent's handover edge: a known agent, never itself,
    // and inside the edge scope when the caller passes one. The badge count and
    // the drawn edges both read it, so a node never counts an edge not drawn.
    const curEdgeIds = edgeIdsRef.current;
    const curOverrides = overridesRef.current;
    const isDrawnHandover = (srcId: string, tgtId: string) =>
      agentMap.has(tgtId) && tgtId !== srcId
      && (!curEdgeIds || curEdgeIds.has(`${srcId}->${tgtId}`) || !!curOverrides?.has(`${srcId}->${tgtId}`));

    const gNodes: Omit<GNode, "isRunning">[] = [];
    const idToIdx = new Map<string, number>();
    // Own-fleet members without their office, the rest with it folded in,
    // never two nodes with one name; the stored name stays the aria-label
    // and tooltip.
    const labels = nodeLabels(
      sortedAgents.map((a) => ({ id: a.id, name: a.name, fleet: a.fleet ? { id: a.fleet.fleet_id, name: a.fleet.fleet_name } : null })),
      viewedFleetRef.current,
    );

    for (const a of sortedAgents) {
      idToIdx.set(a.id, gNodes.length);
      const parts = labels.get(a.id) ?? agentLabel(a.name);
      gNodes.push({
        id: a.id,
        name: a.name,
        label: parts.label,
        labelParts: parts,
        fleetName: a.fleet?.fleet_name || undefined,
        r: 12,
        x: 0, y: 0,
        enabled: a.enabled,
        isCompanyManaged: a.is_company_managed,
        isEditable: curEditable?.has(a.id) ?? false,
        iconKey: a.icon,
        // Same rule as the edges drawn below.
        // Handover targets only: an A2A liaison is drawn, not counted.
        handoverCount: (a.handover_agent_ids || []).filter(id => isDrawnHandover(a.id, id) && !curOverrides?.has(`${a.id}->${id}`)).length,
        inboundCount: 0,
        isGeneralAssistant: false,
        isLinkedGA: curStandInId === a.id,
        outputFormat: a.output_format,
      });
    }

    // Add General Assistant node
    if (curGA && !curStandInId) {
      idToIdx.set(GA_ID, gNodes.length);
      const gaName = "General Assistant";
      gNodes.push({
        id: GA_ID,
        name: gaName,
        label: gaName,
        labelParts: agentLabel(gaName),
        r: 12,
        x: 0, y: 0,
        enabled: true,
        isCompanyManaged: false,
        isEditable: false, // GA not editable from graph
        iconKey: "",
        handoverCount: (curGA.handover_agent_ids || []).filter(id => agentMap.has(id)).length,
        inboundCount: 0,
        isGeneralAssistant: true,
        isLinkedGA: false,
        outputFormat: "markdown",
      });
    }

    // Build directed edges
    let rawEdges: Array<{ srcId: string; tgtId: string; handoverType: string; condition: string }> = [];

    for (const a of sortedAgents) {
      const types = a.handover_types || {};
      const conditions = a.handover_conditions || {};
      for (const hId of (a.handover_agent_ids || [])) {
        if (isDrawnHandover(a.id, hId)) {
          // An A2A liaison keeps its own type; every other edge is a handover.
          const override = curOverrides?.get(`${a.id}->${hId}`);
          rawEdges.push({ srcId: a.id, tgtId: hId, handoverType: override ?? shownHandoverType(types, hId, HANDOVER_TYPES), condition: override ? "" : conditions[hId] || "" });
          const tNode = gNodes[idToIdx.get(hId)!];
          if (tNode) tNode.inboundCount++;
        }
      }
    }

    // GA handover edges
    if (curGA && !curStandInId && idToIdx.has(GA_ID)) {
      const gaTypes = curGA.handover_types ?? {};
      for (const hId of (curGA.handover_agent_ids || [])) {
        if (agentMap.has(hId)) {
          rawEdges.push({ srcId: GA_ID, tgtId: hId, handoverType: shownHandoverType(gaTypes, hId, GA_HANDOVER_TYPES), condition: "" });
          const tNode = gNodes[idToIdx.get(hId)!];
          if (tNode) tNode.inboundCount++;
        }
      }
    }

    // ── Isolated focus: lay out the focused neighbourhood alone ──
    // The counts on the nodes stay the agents' own, whatever is shown.
    let shownNodes = gNodes;
    if (isolatedKey) {
      const isolated = isolateFocus(
        rawEdges.map(e => ({ ...e, sourceId: e.srcId, targetId: e.tgtId })),
        new Set(isolatedKey.split(",")),
      );
      rawEdges = isolated.edges;
      shownNodes = gNodes.filter(n => isolated.nodeIds.has(n.id));
    }

    // ── A static view draws a two-way handover once ──
    // One path with an arrowhead at each end instead of two arcs bowing to
    // either side, the second of which swept through a third node's name
    // (#9760 UX round 6, B1). The path kept leaves the member: a ghost's edge
    // back is the one dropped, as the layout below drops it. The Graph view
    // keeps both edges and their types.
    const twoWayKeys = new Set<string>();
    if (staticView) {
      const edgeKey = (e: { srcId: string; tgtId: string }) => `${e.srcId}->${e.tgtId}`;
      const pairOf = (e: { srcId: string; tgtId: string }) => (e.srcId < e.tgtId ? `${e.srcId}|${e.tgtId}` : `${e.tgtId}|${e.srcId}`);
      const keys = new Set(rawEdges.map(edgeKey));
      const ghostsNow = ghostNodesRef.current;
      const keptOfPair = new Map<string, string>();
      for (const e of rawEdges) {
        if (e.srcId === e.tgtId || !keys.has(`${e.tgtId}->${e.srcId}`)) continue;
        const pair = pairOf(e);
        const kept = keptOfPair.get(pair);
        if (kept === undefined || (ghostsNow?.has(kept.split("->")[0]) && !ghostsNow.has(e.srcId))) keptOfPair.set(pair, edgeKey(e));
      }
      for (const kept of keptOfPair.values()) twoWayKeys.add(kept);
      rawEdges = rawEdges.filter((e) => !keptOfPair.has(pairOf(e)) || keptOfPair.get(pairOf(e)) === edgeKey(e));
    }

    // ── Identify orphan nodes (no edges at all) ──
    const connectedIds = new Set<string>();
    for (const { srcId, tgtId } of rawEdges) {
      connectedIds.add(srcId);
      connectedIds.add(tgtId);
    }
    const orphanNodes = shownNodes.filter(n => !connectedIds.has(n.id));
    const connectedNodes = shownNodes.filter(n => connectedIds.has(n.id));

    // ── A layout of the layout library places every node itself ──
    // The radial layout centres on the most connected agent (ties by id).
    let libLayout: Map<string, { x: number; y: number }> | null = null;
    let libMinDistance = LAYOUT_MIN_DISTANCE;
    if (layout) {
      const degree = new Map<string, number>();
      for (const { srcId, tgtId } of rawEdges) {
        degree.set(srcId, (degree.get(srcId) ?? 0) + 1);
        degree.set(tgtId, (degree.get(tgtId) ?? 0) + 1);
      }
      const centerId = [...degree.entries()]
        .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))[0]?.[0] ?? null;
      // The spacing grows with the node count, the agents with no
      // handover form a cluster shaped for the canvas, not a long strip.
      libMinDistance = layoutForces?.collide ?? spacingFor(shownNodes.length);
      // A static view lays a two-way handover with a ghost out from the
      // member's side: the ghost's edge back would otherwise let the cycle
      // breaker put the ghost (a group of other fleets) above the
      // orchestrator (#9760 UX round 5, B2). Both edges are still drawn.
      const layoutGhosts = staticView ? ghostNodesRef.current : undefined;
      const edgeKeys = new Set(rawEdges.map(e => `${e.srcId}->${e.tgtId}`));
      const layoutEdges = rawEdges.filter(e => !(layoutGhosts?.has(e.srcId) && !layoutGhosts.has(e.tgtId) && edgeKeys.has(`${e.tgtId}->${e.srcId}`)));
      libLayout = layoutGraph(
        layout,
        shownNodes.map(n => n.id),
        layoutEdges.map(e => ({ sourceId: e.srcId, targetId: e.tgtId })),
        {
          centerId,
          minDistance: libMinDistance,
          labelWidth: Math.max(LAYOUT_LABEL_WIDTH, libMinDistance),
          // A static view packs its components for its own wide, short frame.
          aspect: staticView ? svgWidthStep / STATIC_VIEW_PX : W / H,
          // and wraps a layer wider than its frame holds names onto rows.
          layerWrap: staticView
            ? Math.max(2, Math.floor((svgWidthStep * (1 - (2 * PAD_X) / W)) / STATIC_NAME_ROOM_PX))
            : undefined,
          // and layers each agent at its shortest depth: a cross-link between
          // two members of one depth adds no row (#9760 UX round 5, B1).
          shallowLayers: staticView,
          linkDistance: layoutForces?.linkDistance,
          charge: layoutForces?.charge,
          // The agents with no handover sit with their fleet mates, a shelf
          // gap from the other fleets and the playground.
          groups: new Map(curAgents.flatMap((a) => (a.fleet?.fleet_id ? [[a.id, a.fleet.fleet_id] as [string, string]] : []))),
        },
      );
    }

    // ── Dagre hierarchical layout (Sugiyama algorithm) ──
    const g = new dagre.graphlib.Graph();
    const nc = connectedNodes.length;
    const dynNodeSep = nc > 8 ? 90 : 120;
    const dynRankSep = nc > 8 ? 140 : 180;
    const vertical = orientation === "vertical";
    g.setGraph({
      rankdir: vertical ? "TB" : "LR",
      nodesep: dynNodeSep,
      ranksep: dynRankSep,
      marginx: 0,
      marginy: 0,
      ranker: "network-simplex",
    });
    g.setDefaultEdgeLabel(() => ({}));

    // Add only connected nodes — dagre needs width/height, use diameter
    for (const node of libLayout ? [] : connectedNodes) {
      const d = node.r * 2 + 20; // padding around circle
      g.setNode(node.id, { width: d, height: d });
    }
    // Add edges (none with a library layout: its edges are drawn straight)
    for (const edge of libLayout ? [] : rawEdges) {
      g.setEdge(edge.srcId, edge.tgtId);
    }

    if (!libLayout && connectedNodes.length > 0) {
      dagre.layout(g);
    }

    // ── Separate disconnected subgraphs across the flow ──
    // Dagre can overlap disconnected components. Detect components via
    // BFS on handover edges, then shift each cluster so they stack across
    // the flow (vertically for a horizontal graph, horizontally for a
    // vertical one) with padding between them.
    {
      const across = vertical ? "x" : "y";
      const extent = vertical ? "width" : "height";
      const nodeIds = connectedNodes.map(n => n.id);
      const adjSet = new Map<string, Set<string>>();
      for (const id of nodeIds) adjSet.set(id, new Set());
      for (const { srcId, tgtId } of rawEdges) {
        adjSet.get(srcId)?.add(tgtId);
        adjSet.get(tgtId)?.add(srcId);
      }

      const visited = new Set<string>();
      const components: string[][] = [];
      for (const id of nodeIds) {
        if (visited.has(id)) continue;
        const comp: string[] = [];
        const queue = [id];
        visited.add(id);
        while (queue.length > 0) {
          const cur = queue.shift()!;
          comp.push(cur);
          for (const nb of adjSet.get(cur) ?? []) {
            if (!visited.has(nb)) { visited.add(nb); queue.push(nb); }
          }
        }
        components.push(comp);
      }

      if (!libLayout && components.length > 1) {
        const CLUSTER_PAD = 60; // gap between clusters in dagre coords
        // Compute bounding boxes per component (in dagre coordinates)
        const boxes = components.map(comp => {
          let cMinY = Infinity, cMaxY = -Infinity;
          for (const id of comp) {
            const dn = g.node(id);
            if (!dn) continue;
            const halfH = (dn[extent] ?? 0) / 2;
            cMinY = Math.min(cMinY, dn[across] - halfH);
            cMaxY = Math.max(cMaxY, dn[across] + halfH);
          }
          // Also include edge bend points for accurate bounding
          for (const { srcId, tgtId } of rawEdges) {
            if (!comp.includes(srcId) && !comp.includes(tgtId)) continue;
            const de = g.edge(srcId, tgtId);
            if (!de?.points) continue;
            for (const p of de.points) {
              cMinY = Math.min(cMinY, p[across]);
              cMaxY = Math.max(cMaxY, p[across]);
            }
          }
          return { comp, minY: cMinY, maxY: cMaxY, height: cMaxY - cMinY };
        });

        // Sort by size descending (largest cluster first)
        boxes.sort((a, b) => b.comp.length - a.comp.length);

        // Stack across the flow: shift each component's dagre positions
        let curY = boxes[0].minY; // start from the first cluster's position
        for (let ci = 0; ci < boxes.length; ci++) {
          const box = boxes[ci];
          const shiftY = curY - box.minY;
          if (Math.abs(shiftY) > 0.5) {
            for (const id of box.comp) {
              const dn = g.node(id);
              if (dn) dn[across] += shiftY;
            }
            // Also shift edge points
            for (const { srcId, tgtId } of rawEdges) {
              if (!box.comp.includes(srcId) && !box.comp.includes(tgtId)) continue;
              const de = g.edge(srcId, tgtId);
              if (de?.points) {
                for (const p of de.points) p[across] += shiftY;
              }
            }
          }
          curY += box.height + CLUSTER_PAD;
        }
      }
    }

    // Read positions back and map into our SVG coordinate space.
    // Compute bounding box from connected nodes AND edge bend points.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of connectedNodes) {
      const dn = g.node(node.id);
      if (!dn) continue;
      minX = Math.min(minX, dn.x); maxX = Math.max(maxX, dn.x);
      minY = Math.min(minY, dn.y); maxY = Math.max(maxY, dn.y);
    }
    for (const { srcId, tgtId } of rawEdges) {
      const de = g.edge(srcId, tgtId);
      if (!de?.points) continue;
      for (const p of de.points) {
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      }
    }

    const hasConnected = connectedNodes.length > 0;
    const hasOrphans = orphanNodes.length > 0;
    const ORPHAN_ROW_GAP = 60; // SVG-space gap between connected region and orphan row

    // Reserve vertical space: connected region + gap + orphan row
    const connectedBboxH = hasConnected ? (maxY - minY) : 0;
    const orphanRowH = hasOrphans ? 30 : 0; // approximate height for one row of orphan nodes
    const totalContentH = connectedBboxH + (hasConnected && hasOrphans ? ORPHAN_ROW_GAP + orphanRowH : orphanRowH);
    // A single column (a vertical chain) has no width: centre it, as a single
    // row is centred vertically below.
    const singleColumn = hasConnected && maxX - minX === 0;
    const bboxW = hasConnected ? ((maxX - minX) || 1) : 1;
    const bboxH = totalContentH;
    const availW = W - 2 * PAD_X;
    const availH = H - 2 * PAD_Y;
    const scaleX = hasConnected ? availW / bboxW : 1;
    // If all nodes share the same Y (single row), center vertically
    const scaleY = bboxH > 0 ? availH / bboxH : 1;
    const centerOffsetY = bboxH > 0 ? PAD_Y : H / 2;

    for (const node of connectedNodes) {
      const dn = g.node(node.id);
      if (dn) {
        node.x = singleColumn ? Math.round(W / 2) : Math.round((dn.x - minX) * scaleX + PAD_X);
        node.y = Math.round((dn.y - minY) * scaleY + centerOffsetY);
      }
    }

    // ── Place orphan nodes on bottom rows, left-aligned, max 15 per row ──
    // With nothing connected above them, the rows are centred on the canvas.
    if (hasOrphans && !libLayout) {
      const ORPHAN_COL_SPACING = 90;
      const ORPHAN_ROW_SPACING = 50;
      const MAX_PER_ROW = 15;
      const rowCount = Math.ceil(orphanNodes.length / MAX_PER_ROW);
      const rowWidth = (Math.min(orphanNodes.length, MAX_PER_ROW) - 1) * ORPHAN_COL_SPACING;
      const baseX = hasConnected ? PAD_X : Math.max(PAD_X, (W - rowWidth) / 2);
      const baseY = hasConnected
        ? Math.round(connectedBboxH * scaleY + centerOffsetY + ORPHAN_ROW_GAP)
        : Math.round(Math.max(PAD_Y, (H - (rowCount - 1) * ORPHAN_ROW_SPACING) / 2));
      // Sort orphans alphabetically for stable ordering
      orphanNodes.sort((a, b) => a.name.localeCompare(b.name));
      for (let oi = 0; oi < orphanNodes.length; oi++) {
        const col = oi % MAX_PER_ROW;
        const row = Math.floor(oi / MAX_PER_ROW);
        orphanNodes[oi].x = Math.round(baseX + col * ORPHAN_COL_SPACING);
        orphanNodes[oi].y = Math.round(baseY + row * ORPHAN_ROW_SPACING);
      }
    }

    // ── Library layout: one uniform scale into the canvas, centred ──
    // Never enlarged past 1.5x, so a fleet of three is not blown up.
    // A drawing too tall for the canvas grows the canvas (up to MAX_H) instead.
    let canvasH = H;
    let libScale = 1;
    if (libLayout && libLayout.size > 0) {
      let lx0 = Infinity, lx1 = -Infinity, ly0 = Infinity, ly1 = -Infinity;
      for (const p of libLayout.values()) {
        lx0 = Math.min(lx0, p.x); lx1 = Math.max(lx1, p.x);
        ly0 = Math.min(ly0, p.y); ly1 = Math.max(ly1, p.y);
      }
      // A static force view keeps, on each side, half its widest name line and
      // STATIC_EDGE_PX on screen (#10147 UX r2 B3: "corporate communications"
      // drew 2 to 7 px past the frame), not only PAD_X in drawing units.
      const padX = staticView && layout === "force"
        ? Math.max(PAD_X, staticLabelEdgePx(shownNodes.map((n) => n.label), { namePx: STATIC_NAME_PX, glyphAdvance: GLYPH_ADVANCE, roomPx: LONE_NAME_ROOM_PX, edgePx: STATIC_EDGE_PX }) / (svgWidthStep / W))
        : PAD_X;
      // A static view has no zoom rail over its right edge.
      const padRight = staticView ? padX : PAD_X + ZOOM_RAIL_PX / (svgWidthStep / W);
      // A drawing that wraps its names (the read-only About flow) keeps room
      // under its last row for the tallest stack a node draws there (three
      // name lines and a ghost's group line), so no caption is cut by the
      // card bottom, and grows past the first viewport instead of packing
      // its rows: a dense fleet keeps the vertical room its wrapped names need.
      const labelPad = staticView
        ? (layout === "force" ? STATIC_FORCE_STACK_PX : STATIC_STACK_PX) / (svgWidthStep / W)
        : wrapNames ? WRAPPED_STACK_PX / (svgWidthStep / W) : 0;
      // A static view is fitted whole into its fixed height (drawing units at
      // the drawn width): it never grows, the page around it scrolls.
      const staticH = Math.round(STATIC_VIEW_PX * (W / svgWidthStep));
      // A static view keeps a glyph's room above its first row and the name
      // stack under its last one, not the interactive canvas's PAD_Y on both
      // sides: in its short frame those pads left 70 px for the whole drawing.
      const staticTop = WRAPPED_GLYPH_PX / (svgWidthStep / W);
      const fit = staticView
        ? fitToCanvas(lx1 - lx0, ly1 - ly0, W, staticH, staticH, padX, (staticTop + labelPad) / 2, undefined, padRight)
        : fitToCanvas(lx1 - lx0, ly1 - ly0, W, H, wrapNames ? WRAPPED_MAX_H : viewportMaxH, PAD_X, PAD_Y + labelPad / 2, undefined, padRight);
      // The drawing is centred between the left pad and the right one.
      const midX = ((staticView ? padX : PAD_X) + W - padRight) / 2;
      const scale = fit.scale;
      libScale = scale;
      canvasH = fit.height;
      // A wrapped drawing spaces two nodes of one layer by at least a glyph and
      // its tallest name stack on screen: a dense layer grows the canvas down
      // instead of hiding the wrapped names it packed together.
      let scaleY = scale;
      // A static view spaces its layers by a glyph and a name stack on screen,
      // and its frame fits the drawing: no band under the last row, no layer
      // packed over the names above it. A fleet whose rows need more than
      // STATIC_VIEW_PX grows the frame down rather than leaving names out.
      let staticOriginY: number | null = null;
      // A static view's row offsets (drawing units from the first row): a row
      // holding a ghost keeps the whole stack under it (its group line), a
      // row of members the name lines only, so the frame stays compact
      // (#9760 UX round 3, B1) and no caption is cut.
      let staticRowY: Map<number, number> | null = null;
      // A force layout has no rows (every node its own y): stacking a row per
      // node grew the frame to 700 px beside a 220 px card (#10147 UX r1 B3).
      // It is fitted whole into the fixed STATIC_VIEW_PX frame instead.
      if (staticView && layout !== "force") {
        const pxPerUnitAt = svgWidthStep / W;
        const curGhosts = ghostNodesRef.current;
        const rowOf = (y: number) => Math.round(y);
        // Per row: the most name lines a node draws in its room, and whether
        // a ghost draws its group line under it.
        // (A row's room at render can be narrower than STATIC_NAME_ROOM_PX:
        // the estimate takes a fifth less, so a name never wraps past it.)
        const nameChars = Math.max(MIN_LABEL_CHARS, Math.floor((0.8 * STATIC_NAME_ROOM_PX - LABEL_GAP_PX) / (NAME_PX * GLYPH_ADVANCE)));
        const nodeName = new Map(shownNodes.map((n) => [n.id, n.label]));
        const ghostRows = new Set<number>();
        const rowLines = new Map<number, number>();
        // Per row holding a ghost: the most lines its group line wraps onto.
        const groupChars = Math.max(MIN_LABEL_CHARS, Math.floor((0.8 * STATIC_NAME_ROOM_PX - LABEL_GAP_PX) / (NAME_PX * STATIC_SUB_LABEL_SCALE * GLYPH_ADVANCE)));
        const rowGroupLines = new Map<number, number>();
        for (const [id, p] of libLayout) {
          const row = rowOf(p.y);
          const group = curGhosts?.get(id);
          if (group) {
            ghostRows.add(row);
            rowGroupLines.set(row, Math.max(rowGroupLines.get(row) ?? 1, wrapLabelWhole(group, groupChars).length));
          }
          const lines = Math.max(2, wrapLabelWhole(nodeName.get(id) ?? "", nameChars).length);
          rowLines.set(row, Math.max(rowLines.get(row) ?? 2, lines));
        }
        const rows = [...rowLines.keys()].sort((a, b) => a - b);
        // A ghost's group line on the lines it wraps onto, not three lines
        // whatever its length (#9760 UX round 6, B2: a one-line "Concierge"
        // kept two empty lines under its row).
        const groupLinePx = (row: number) =>
          Math.ceil(NAME_PX * STATIC_SUB_LABEL_SCALE * (1.2 + 1.15 * ((rowGroupLines.get(row) ?? 1) - 1)));
        const stackOf = (row: number) =>
          Math.min(STATIC_STACK_PX, STATIC_NAME_STACK_PX + (ghostRows.has(row) ? groupLinePx(row) : 0)) + Math.ceil(NAME_PX * 1.15 * ((rowLines.get(row) ?? 2) - 2));
        staticRowY = new Map();
        let offsetPx = 0;
        rows.forEach((row, i) => {
          if (i > 0) offsetPx += stackOf(rows[i - 1]) + WRAPPED_GLYPH_PX;
          staticRowY!.set(row, offsetPx / pxPerUnitAt);
        });
        // Under the last row, the stack that row draws: a row holding a ghost
        // keeps the whole stack (its group line); a row of members keeps its
        // name lines plus one, as a node at the frame's edge has less room
        // than the estimate and wraps once more, and the descender. No caption
        // touches the card edge, and no band of a ghost's stack under a row of
        // members (#9760 UX round 4, B1b).
        const lastRow = rows.length > 0 ? rows[rows.length - 1] : null;
        const fullStack = Math.max(STATIC_STACK_PX, lastRow === null ? 0 : stackOf(lastRow));
        // (The glyph's radius as drawn and its 3 px gap, the first name line,
        // each next one at 1.15 and the descender: measured from the drawn
        // glyph, not a fixed two-line allowance that left 36 px under the
        // text on AI security, #9760 UX round 6, B2.)
        const glyphPx = Math.max(glyphRadius(libMinDistance * scale) * pxPerUnitAt, GLYPH_FLOOR_PX / 2);
        const memberStack = (row: number) =>
          Math.ceil(glyphPx + 3 + NAME_PX * (1 + 1.15 * (rowLines.get(row) ?? 2)) + NAME_PX * 0.3) + 4;
        // A last row holding a ghost keeps the group line it draws, on the
        // lines it wraps onto, not the three lines and the row gap a row above
        // keeps (#9760 UX round 5, B3: 69 px under the text against 40 above).
        const ghostStack = (row: number) => memberStack(row) + groupLinePx(row);
        const lastStack = lastRow === null
          ? fullStack
          : Math.min(fullStack, ghostRows.has(lastRow) ? ghostStack(lastRow) : memberStack(lastRow));
        // Above the first row, the glyph as drawn and an 8 px pad, not the
        // 28 px row gap (#9760 UX round 6, B2): the frame is even top and bottom.
        const topUnits = Math.min(staticTop, (glyphPx + 8) / pxPerUnitAt);
        canvasH = Math.ceil(topUnits + (offsetPx + lastStack) / pxPerUnitAt);
        staticOriginY = topUnits;
      } else if (wrapNames && !staticView) {
        const pxPerUnitAt = svgWidthStep / W;
        scaleY = Math.max(scale, (WRAPPED_STACK_PX + WRAPPED_GLYPH_PX) / pxPerUnitAt / Math.max(1, libMinDistance));
        canvasH = Math.min(WRAPPED_MAX_H, Math.max(canvasH, Math.ceil((ly1 - ly0) * scaleY + 2 * PAD_Y + labelPad)));
      }
      const cx = (lx0 + lx1) / 2, cy = (ly0 + ly1) / 2;
      // Names keep a fixed screen size while the positions are scaled into the
      // canvas: the room its layout guarantees across (a label's width between
      // hierarchy neighbours, the node distance otherwise) is kept in drawing
      // units, and a longer name is cut to it at render, from the screen room,
      // so two names never overlap; the hover card and the accessible list
      // carry the full name.
      // A top-down drawing is narrow and tall (a layer per handover depth):
      // its fit is height-bound and left row neighbours a few characters
      // apart while the canvas width went unused. It spreads across with the
      // width-bound fit (capped as the uniform one is, the zoom-rail pad
      // kept), so siblings on a row read their names as in the other layouts.
      // A left-to-right drawing with few layers and a tall one (a hub and its
      // members) is height-bound the same way: its layers spread across, so
      // the names between two layers keep the canvas width it leaves unused.
      // A static force view spreads its nodes over the whole frame (#10147 UX
      // r3 B1: at 1440 the nodes sat in y 43-176 of a 271 px frame with a
      // 95 px band under them, names packed onto each other): across with the
      // width-bound fit, down from the glyph room at the top to the name stack
      // kept at the bottom, each axis on its own scale.
      const staticForce = staticView && layout === "force";
      const scaleX = layout === "hierarchy-tb" || layout === "hierarchy-lr" || staticForce ? fit.across : scale;
      const forceSpanY = staticH - staticTop - labelPad;
      const forceScaleY = staticForce && ly1 > ly0 ? Math.max(scale, forceSpanY / (ly1 - ly0)) : scale;
      const room = (layout === "hierarchy-tb" || layout === "hierarchy-lr" || layout === "orgchart" ? Math.max(LAYOUT_LABEL_WIDTH, libMinDistance) : libMinDistance) * scaleX;
      // The glyph shrinks with the room between nodes: a fifth of
      // it, capped, so a dense map is never a wall of rings edge to edge.
      const r = glyphRadius(libMinDistance * scale);
      for (const node of shownNodes) {
        const p = libLayout.get(node.id);
        if (!p) continue;
        node.x = Math.round(midX + (p.x - cx) * scaleX);
        node.y = staticForce
          ? Math.round(ly1 > ly0 ? staticTop + (p.y - ly0) * forceScaleY : staticTop + forceSpanY / 2)
          : staticOriginY !== null
            ? Math.round(staticOriginY + (staticRowY?.get(Math.round(p.y)) ?? (p.y - ly0) * scaleY))
            : Math.round(canvasH / 2 - labelPad / 2 + (p.y - cy) * scaleY);
        node.labelRoom = room;
        node.r = r;
      }
      // A static force view spaces its frozen nodes by the names it draws
      // (#10147 UX r5 B1: centres 46 to 53 px apart under 60 px names at
      // 1280, "assista…" cut): each node's name block (its glyph above, its
      // name lines and a ghost's group line below, the widest line across) is
      // kept clear of every other one by LABEL_GAP_PX, inside the frame, so a
      // name is drawn whole and never runs into its neighbour's.
      if (staticForce) {
        const pxPerUnitAt = svgWidthStep / W;
        const curGhosts = ghostNodesRef.current;
        const drawn = distinctLabels(new Map(shownNodes.map((n) => [n.id, n.label])));
        const charPx = STATIC_NAME_PX * GLYPH_ADVANCE;
        const subPx = NAME_PX * STATIC_SUB_LABEL_SCALE;
        const glyphPx = Math.max(r * pxPerUnitAt, GLYPH_FLOOR_PX / 2);
        const blocks = shownNodes.filter((n) => libLayout!.has(n.id)).map((n) => {
          const lines = staticNameLines(drawn.get(n.id) ?? n.label, charPx, LONE_NAME_ROOM_PX);
          const group = curGhosts?.get(n.id);
          const subText = group ? ghostGroupLine(n.labelParts, group) : "";
          const sub = subText ? staticNameLines(subText, subPx * GLYPH_ADVANCE, LONE_NAME_ROOM_PX) : [];
          const widest = Math.max(
            ...lines.map((l) => [...l].length * charPx),
            ...sub.map((l) => [...l].length * subPx * GLYPH_ADVANCE),
          );
          const below = glyphPx + 3 + STATIC_NAME_PX * (1.2 + 1.15 * (lines.length - 1) + 0.3)
            + (sub.length > 0 ? subPx * (1.2 + 1.15 * (sub.length - 1)) : 0);
          return { id: n.id, x: n.x * pxPerUnitAt, y: n.y * pxPerUnitAt, w: widest, above: glyphPx + 2, below };
        });
        const spread = spreadNameBlocks(
          blocks,
          { x0: STATIC_EDGE_PX, x1: W * pxPerUnitAt - STATIC_EDGE_PX, y0: 0, y1: staticH * pxPerUnitAt },
          LABEL_GAP_PX,
        );
        for (const node of shownNodes) {
          const p = spread.get(node.id);
          if (!p) continue;
          node.x = Math.round(p.x / pxPerUnitAt);
          node.y = Math.round(p.y / pxPerUnitAt);
        }
      }
    }

    // Build resolved edges using dagre's edge routing points
    const nodeById = new Map(shownNodes.map(n => [n.id, n]));
    const gEdges: GEdge[] = [];

    // One arc per edge: reciprocal edges and several edges of a pair
    // bow to alternate sides, so neither the arcs nor their labels overlap.
    const curvatures = edgeCurvatures(rawEdges.map(({ srcId, tgtId }) => ({ sourceId: srcId, targetId: tgtId })));
    // Layered and radial layouts route an arc around a third node in its way.
    const routeAround = layout === "hierarchy-tb" || layout === "hierarchy-lr" || layout === "radial";
    const obstacles = routeAround ? shownNodes.map((n) => ({ x: n.x, y: n.y })) : [];
    const clearance = (shownNodes[0]?.r ?? 10) + 4;
    // The org chart routes its edges orthogonally, with angled lanes.
    // Two ends on a card side stay an arrowhead's width plus a stroke apart,
    // the arrowhead as drawn on the fitted canvas (glyph floor included), so
    // no arrowhead lands on another edge. The ends
    // meet the ring as drawn at fit, the glyph floor included.
    const fitPxPerUnit = svgWidthStep / W;
    const glyphFloorAtFit = GLYPH_FLOOR_PX / 2 / fitPxPerUnit;
    const drawnR = Math.max(shownNodes[0]?.r ?? 10, glyphFloorAtFit);
    const orgPortGap = 2 * arrowHead(drawnR, fitPxPerUnit).halfWidth + 1.2;
    const orgRoutes = layout === "orgchart" && libLayout
      ? orgChartRoutes(
        shownNodes.map((n) => n.id),
        rawEdges.map(({ srcId, tgtId }) => ({ sourceId: srcId, targetId: tgtId })),
        new Map(shownNodes.map((n) => [n.id, { x: n.x, y: n.y, r: Math.max(n.r, glyphFloorAtFit) }])),
        orgChartMetrics({ minDistance: libMinDistance, labelWidth: Math.max(LAYOUT_LABEL_WIDTH, libMinDistance) }),
        libScale,
        ARROW_GAP,
        orgPortGap,
      )
      : null;
    // The other layouts draw each handover as an arc. Every arc keeps
    // an arrowhead's length clear of third nodes, force included, and the
    // ends on one ring are spread an arrowhead's width plus 3 px apart, so
    // an arrowhead does not land on another edge's end.
    const arcHead = arrowHead(drawnR, fitPxPerUnit);
    const arcs = layout && layout !== "orgchart" && libLayout
      ? arcRoutes(
        rawEdges.map(({ srcId, tgtId }) => ({ sourceId: srcId, targetId: tgtId })),
        new Map(shownNodes.map((n) => [n.id, { x: n.x, y: n.y, r: n.r }])),
        {
          curvatures,
          clearance: drawnR + ARROW_GAP + arcHead.length,
          box: { x0: PAD_X / 2, y0: PAD_Y / 2, x1: W - PAD_X / 2, y1: canvasH - PAD_Y / 2 },
          arrowGap: ARROW_GAP,
          head: { length: arcHead.length, room: 2 * arcHead.halfWidth + ARC_END_GAP_PX / fitPxPerUnit },
        },
      )
      : null;

    // Pre-count edges per normalised pair for bidirectional detection
    const pairEdgeCount = new Map<string, number>();
    const pairEdgeIdx = new Map<string, number>();
    for (const { srcId, tgtId } of rawEdges) {
      const key = srcId < tgtId ? `${srcId}|${tgtId}` : `${tgtId}|${srcId}`;
      pairEdgeCount.set(key, (pairEdgeCount.get(key) ?? 0) + 1);
    }

    // A static view's rows (drawing y of each), top to bottom.
    const staticRowYs = staticView && libLayout && layout !== "force" ? [...new Set(shownNodes.map((n) => n.y))].sort((a, b) => a - b) : [];

    rawEdges.forEach(({ srcId, tgtId, handoverType, condition }, i) => {
      const src = nodeById.get(srcId);
      const tgt = nodeById.get(tgtId);
      if (!src || !tgt) return;

      const pairKey = srcId < tgtId ? `${srcId}|${tgtId}` : `${tgtId}|${srcId}`;
      const pairIdx = pairEdgeIdx.get(pairKey) ?? 0;
      pairEdgeIdx.set(pairKey, pairIdx + 1);
      const pairTotal = pairEdgeCount.get(pairKey) ?? 1;

      // A static view routes every edge down to a lower row as a lane: down
      // from the source, across in the free band above the next row, then
      // straight down to the target (past a wrapped layer's first row through
      // the gap its staggered next row leaves between two names). An arc to a
      // far sibling bowed through the names of the row (#9760 UX round 7, B1).
      const below = staticRowYs.filter((y) => y > src.y + 1 && y <= tgt.y + 1);
      if (below.length > 0) {
        const laneY = below[0] - drawnR - 8 / fitPxPerUnit;
        const startY = src.y + drawnR;
        const endY = tgt.y - drawnR - ARROW_GAP;
        const dx = tgt.x - src.x;
        const turn = Math.min(6 / fitPxPerUnit, Math.abs(dx) / 2, (laneY - startY) / 2);
        const dir = Math.sign(dx);
        const path = Math.abs(dx) < 1
          ? `M ${src.x},${startY} L ${tgt.x},${endY}`
          : `M ${src.x},${startY} L ${src.x},${laneY - turn} Q ${src.x},${laneY} ${src.x + dir * turn},${laneY} L ${tgt.x - dir * turn},${laneY} Q ${tgt.x},${laneY} ${tgt.x},${laneY + turn} L ${tgt.x},${endY}`;
        gEdges.push({
          id: `e-${i}`,
          sourceId: srcId,
          targetId: tgtId,
          path,
          bidirectional: pairTotal >= 2,
          handoverType,
          condition: condition || "",
          midX: Math.round((src.x + tgt.x) / 2),
          midY: Math.round(laneY),
          sourceAngle: Math.PI / 2,
          targetAngle: -Math.PI / 2,
        });
        return;
      }

      const arc = arcs?.[i];
      if (arc) {
        const apex = quadraticPoint(arc.a, arc.control, arc.b, 0.5);
        gEdges.push({
          id: `e-${i}`,
          sourceId: srcId,
          targetId: tgtId,
          path: `M ${arc.a.x},${arc.a.y} Q ${arc.control.x},${arc.control.y} ${arc.b.x},${arc.b.y}`,
          bidirectional: pairTotal >= 2,
          handoverType,
          condition: condition || "",
          midX: Math.round(apex.x),
          midY: Math.round(apex.y),
          arc: { a: arc.a, control: arc.control, b: arc.b },
          sourceAngle: arc.sourceAngle,
          targetAngle: arc.targetAngle,
        });
        return;
      }

      const route = orgRoutes?.[i];
      if (route) {
        gEdges.push({
          id: `e-${i}`,
          sourceId: srcId,
          targetId: tgtId,
          path: route.path,
          bidirectional: pairTotal >= 2,
          handoverType,
          condition: condition || "",
          midX: Math.round(route.mid.x),
          midY: Math.round(route.mid.y),
          sourceAngle: route.sourceAngle,
          targetAngle: route.targetAngle,
        });
        return;
      }

      // Get dagre's edge routing points (includes bend points)
      const dagreEdge = g.edge(srcId, tgtId);
      let points: Array<{ x: number; y: number }>;

      if (dagreEdge?.points && dagreEdge.points.length >= 2) {
        // Scale dagre points to our SVG coordinate space
        points = dagreEdge.points.map((p: { x: number; y: number }) => ({
          x: (p.x - minX) * scaleX + PAD_X,
          y: (p.y - minY) * scaleY + centerOffsetY,
        }));
      } else {
        // Fallback: straight line
        points = [{ x: src.x, y: src.y }, { x: tgt.x, y: tgt.y }];
      }

      // Straight between the two nodes (every layout, and the flow page when
      // dagre gave no route): a quadratic arc with the pair's curvature.
      if (points.length === 2) {
        const a = { x: src.x, y: src.y };
        const b = { x: tgt.x, y: tgt.y };
        const curvature = routeAround ? clearCurvature(a, b, curvatures[i], obstacles, clearance) : curvatures[i];
        // With a layout an arc never bows out of the canvas, where it was cut:
        // its apex stays inside the padding.
        const bowed = arcControlPoint(a, b, curvature);
        const cp = libLayout ? keepArcInside(a, bowed, b, { x0: PAD_X / 2, y0: PAD_Y / 2, x1: W - PAD_X / 2, y1: canvasH - PAD_Y / 2 }) : bowed;
        const startDx = cp.x - src.x, startDy = cp.y - src.y;
        const startLen = Math.hypot(startDx, startDy) || 1;
        const endDx = tgt.x - cp.x, endDy = tgt.y - cp.y;
        const endLen = Math.hypot(endDx, endDy) || 1;
        const s = { x: src.x + (startDx / startLen) * src.r, y: src.y + (startDy / startLen) * src.r };
        // The arc ends where its arrow's tip does, ARROW_GAP off the ring.
        const en = { x: tgt.x - (endDx / endLen) * (tgt.r + ARROW_GAP), y: tgt.y - (endDy / endLen) * (tgt.r + ARROW_GAP) };
        // The label sits at the apex of its own arc.
        const apex = quadraticPoint(s, cp, en, 0.5);
        gEdges.push({
          id: `e-${i}`,
          sourceId: srcId,
          targetId: tgtId,
          path: `M ${s.x},${s.y} Q ${cp.x},${cp.y} ${en.x},${en.y}`,
          bidirectional: pairTotal >= 2,
          handoverType,
          condition: condition || "",
          midX: Math.round(apex.x),
          midY: Math.round(apex.y),
          arc: { a: s, control: cp, b: en },
          sourceAngle: Math.atan2(startDy, startDx),
          targetAngle: Math.atan2(-endDy, -endDx),
        });
        return;
      }

      // For bidirectional edges, offset perpendicular to separate them
      if (pairTotal >= 2) {
        const sign = pairIdx === 0 ? 1 : -1;
        const offsetAmt = 8 * sign;
        // Compute perpendicular direction from first to last point
        const fp = points[0], lp = points[points.length - 1];
        const edgeDx = lp.x - fp.x, edgeDy = lp.y - fp.y;
        const edgeLen = Math.sqrt(edgeDx * edgeDx + edgeDy * edgeDy) || 1;
        const perpX = -edgeDy / edgeLen, perpY = edgeDx / edgeLen;
        points = points.map(p => ({
          x: p.x + perpX * offsetAmt,
          y: p.y + perpY * offsetAmt,
        }));
      }

      // Compute endpoints anchored from actual node positions.
      // Direction from node center toward first/last bend (or other node).
      const nextPt = points.length > 2 ? points[1] : { x: tgt.x, y: tgt.y };
      const prevPt = points.length > 2 ? points[points.length - 2] : { x: src.x, y: src.y };

      const startDx = nextPt.x - src.x, startDy = nextPt.y - src.y;
      const startLen = Math.sqrt(startDx * startDx + startDy * startDy) || 1;
      const endDx = tgt.x - prevPt.x, endDy = tgt.y - prevPt.y;
      const endLen = Math.sqrt(endDx * endDx + endDy * endDy) || 1;

      const sx = src.x + (startDx / startLen) * src.r;
      const sy = src.y + (startDy / startLen) * src.r;
      const ex = tgt.x - (endDx / endLen) * (tgt.r + ARROW_GAP);
      const ey = tgt.y - (endDy / endLen) * (tgt.r + ARROW_GAP);

      // Bend points (between start node and end node)
      const bends = points.slice(1, -1);

      // Build SVG path
      let path: string;
      if (bends.length === 0) {
        // No bends — quadratic curve through midpoint
        const cmx = (sx + ex) / 2, cmy = (sy + ey) / 2;
        path = `M ${sx},${sy} Q ${cmx},${cmy} ${ex},${ey}`;
      } else if (bends.length === 1) {
        // Single bend → quadratic
        path = `M ${sx},${sy} Q ${bends[0].x},${bends[0].y} ${ex},${ey}`;
      } else {
        // Multiple bends → line segments through bends
        path = `M ${sx},${sy}`;
        for (const b of bends) {
          path += ` L ${b.x},${b.y}`;
        }
        path += ` L ${ex},${ey}`;
      }

      // Compute label midpoint ON the actual path (not chord midpoint)
      let mx: number, my: number;
      if (bends.length <= 1) {
        // Quadratic Bézier: midpoint at t=0.5 = 0.25*P0 + 0.5*CP + 0.25*P2
        const cpx = bends.length === 1 ? bends[0].x : (sx + ex) / 2;
        const cpy = bends.length === 1 ? bends[0].y : (sy + ey) / 2;
        mx = 0.25 * sx + 0.5 * cpx + 0.25 * ex;
        my = 0.25 * sy + 0.5 * cpy + 0.25 * ey;
      } else {
        // Polyline: walk segments to find true halfway point
        const allPts = [{ x: sx, y: sy }, ...bends, { x: ex, y: ey }];
        let totalLen = 0;
        const segLens: number[] = [];
        for (let j = 1; j < allPts.length; j++) {
          const dx = allPts[j].x - allPts[j - 1].x;
          const dy = allPts[j].y - allPts[j - 1].y;
          segLens.push(Math.sqrt(dx * dx + dy * dy));
          totalLen += segLens[segLens.length - 1];
        }
        const halfLen = totalLen / 2;
        let accum = 0;
        mx = (sx + ex) / 2; my = (sy + ey) / 2;
        for (let j = 0; j < segLens.length; j++) {
          if (accum + segLens[j] >= halfLen) {
            const t = segLens[j] > 0 ? (halfLen - accum) / segLens[j] : 0;
            mx = allPts[j].x + t * (allPts[j + 1].x - allPts[j].x);
            my = allPts[j].y + t * (allPts[j + 1].y - allPts[j].y);
            break;
          }
          accum += segLens[j];
        }
      }

      gEdges.push({
        id: `e-${i}`,
        sourceId: srcId,
        targetId: tgtId,
        path,
        bidirectional: pairTotal >= 2,
        handoverType,
        condition: condition || "",
        midX: Math.round(mx),
        midY: Math.round(my),
        sourceAngle: Math.atan2(startDy, startDx),
        targetAngle: Math.atan2(-endDy, -endDx),
      });
    });
    for (const edge of gEdges) if (twoWayKeys.has(`${edge.sourceId}->${edge.targetId}`)) edge.twoWay = true;

    return {
      layoutNodes: shownNodes,
      edges: gEdges,
      stats: {
        total: shownNodes.filter(n => !n.isGeneralAssistant).length,
        connections: rawEdges.length,
      },
      canvasH,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- guarded by content fingerprints
  }, [agentKey, gaKey, editableKey, edgeIdsKey, overridesKey, ghostKey, isolatedKey, viewedFleetId, orientation, viewportMaxH, svgWidthStep, wrapNames, staticView, layoutForces?.charge, layoutForces?.linkDistance, layoutForces?.collide]);
  // One computed graph per layout for the current inputs: the target a layout
  // click computed is reused by the render that draws it, and a switch back to
  // a layout already drawn costs nothing (the force layout alone takes about
  // 90 ms at 149 agents). New inputs start a new cache.
  const graphCache = useRef<GraphCache<ReturnType<typeof computeGraph>> | null>(null);
  const { layoutNodes, edges: layoutEdges, stats, canvasH } = useMemo(() => {
    graphCache.current = cachedGraph(graphCache.current, computeGraph, layout);
    return graphCache.current.byLayout.get(layout)!;
  }, [computeGraph, layout]);
  // The other layouts are computed while the browser is idle, one at a time,
  // so the first switch to each is as quick as a switch back.
  useEffect(() => {
    if (!layout) return;
    return warmGraphs(
      () => graphCache.current,
      (cache) => { graphCache.current = cache; },
      computeGraph,
      GRAPH_LAYOUTS,
    );
  }, [computeGraph, layout]);

  // Derive viewBox from zoom + pan, on the canvas the layout asked for. A
  // graph that fills a taller parent shows more drawing units down, at the
  // same scale, the canvas at the top of it and the extra height below
  // (`viewHeight`, `viewOrigin`): the first screen shows the drawing.
  const viewH = fillHeight ? viewHeight(canvasH, svgWidth, svgHeightPx, W) : canvasH;
  viewRef.current.canvasH = canvasH;
  viewRef.current.viewH = viewH;
  const vbW = W / zoom;
  const vbH = viewH / zoom;
  const { x: vbX, y: vbY } = viewOrigin({ zoom, pan }, W, canvasH);
  const viewBox = `${vbX} ${vbY} ${vbW} ${vbH}`;
  viewBoxRef.current = { x: vbX, y: vbY, w: vbW, h: vbH };

  // Text sizes in drawing units: fixed screen px with a layout, the drawing's
  // own sizes otherwise (the dagre flow of the standalone page).
  const pxPerUnit = svgWidth / vbW;
  const namePx = staticView && layout === "force" ? STATIC_NAME_PX : NAME_PX;
  const nameSize = layout ? namePx / pxPerUnit : 7;
  const edgeLabelSize = layout ? EDGE_LABEL_PX / pxPerUnit : 5.5;
  // The screen room across for each name: the distance to the nearest node
  // whose name sits on the same row (names are centred under their node, so
  // two neighbours share that distance), and the free span before a
  // neighbour's glyph lower down, with the boxes namesHidden uses below, so a
  // name is cut to fit rather than left out; a node
  // alone on its row gets the room its layout guarantees, at least a long
  // name's width.
  const nameRoomPx = useMemo(() => {
    if (!layout) return new Map<string, number>();
    const floor = GLYPH_FLOOR_PX / 2 / pxPerUnit;
    return nameRoomsPx(
      layoutNodes.map((n) => ({
        id: n.id,
        x: n.x,
        y: n.y,
        r: Math.max(n.r, floor),
        reach: ringReach({ ...n, isRunning: !!activeAgentIds?.has(n.id), isGhost: ghostNodes?.has(n.id) ?? false }),
        labelRoom: n.labelRoom,
      })),
      // The canvas edge cuts a name as a neighbour would: after
      // a layout switch the right-most name stays inside the canvas, and
      // clear of the zoom controls docked at its right edge.
      { pxPerUnit, namePx, lonePx: LONE_NAME_ROOM_PX, gapPx: LABEL_GAP_PX, clearancePx: NAME_CLEARANCE_PX, canvasWidth: W, rightReservePx: ZOOM_RAIL_PX },
    );
  }, [layout, layoutNodes, pxPerUnit, namePx, activeAgentIds, ghostNodes]);
  // A static force view's drawn names without the leading words two members
  // share (`distinctLabels`): the part that tells them apart is drawn.
  const staticLabels = useMemo(
    () => (staticView && layout === "force" ? distinctLabels(new Map(layoutNodes.map((n) => [n.id, n.label]))) : new Map<string, string>()),
    [staticView, layout, layoutNodes],
  );
  // A fleet node's name is its only identity on the map: wrapped on two lines
  // of its room (never under MIN_LABEL_CHARS) instead of cut. An
  // agent named with an office prefix wraps too, the office on the first
  // line and the role on the last, so a cut never hides what tells two
  // offices' agents apart; a member of the viewed fleet, drawn as its role
  // alone, wraps on two lines; any other agent name is cut on one line.
  const fittedNameLines = useCallback((node: GNode): string[] => {
    const room = nameRoomPx.get(node.id);
    if (room === undefined) return [node.label];
    const chars = Math.floor((room - LABEL_GAP_PX) / (namePx * GLYPH_ADVANCE));
    // A wrapped drawing (the read-only About flow) never cuts a name, at rest
    // or hovered and focused: it takes the lines it needs in its own room, so
    // the hovered name stays under its glyph and inside the canvas instead of
    // one long centred line past the card edge.
    if (wrapNames) {
      const shown = viewOnlyNodeIds?.has(node.id) || node.id === hoveredId ? node.name : node.label;
      // A static force view keeps every name on at most two lines of its
      // room, the last one ended by an ellipsis, the tooltip and the
      // accessible name carrying it whole (#10147 UX r3 B1: at 1280 four of
      // seven names were left out, the others took four or five lines).
      // The words every member's name repeats are left out there (#10147
      // UX r4 B1: five of seven read "Lab sample…"), so the part that tells
      // two members apart is the part drawn; the tooltip keeps the whole name.
      // A name is never cut there (#10147 UX r5 B1-B2): it takes two
      // lines, or three when two would be wider than a lone name's room,
      // and the frozen layout spaced the nodes by those lines.
      if (staticView && layout === "force") {
        const distinct = node.id === hoveredId ? shown : (staticLabels.get(node.id) ?? shown);
        return staticNameLines(distinct, namePx * GLYPH_ADVANCE, LONE_NAME_ROOM_PX);
      }
      return wrapLabelWhole(shown, Math.max(MIN_LABEL_CHARS, chars));
    }
    if (node.id === hoveredId) return [node.name];
    const maxLines = 2;
    if (viewOnlyNodeIds?.has(node.id)) return wrapLabel(node.name, Math.max(MIN_LABEL_CHARS, chars), maxLines);
    if (chars < MIN_LABEL_CHARS) return [""];
    if (node.labelParts.prefix || node.labelParts.own) return agentLabelLines(node.labelParts, chars, maxLines);
    return [truncateLabel(node.label, chars)];
  }, [nameRoomPx, namePx, viewOnlyNodeIds, hoveredId, wrapNames, staticView, layout, staticLabels]);
  // The widest line drawn, "" when the name does not fit at all.
  const fittedName = useCallback(
    (node: GNode) => fittedNameLines(node).reduce((a, b) => ([...b].length > [...a].length ? b : a), ""),
    [fittedNameLines],
  );
  // A ghost's home group under its name, cut to the same room at its smaller
  // size; dropped when too short (the aria-label and tooltip still carry it).
  // A wrapped drawing wraps the group line instead (joined by line breaks,
  // drawn as tspans), on the lines it needs: the home fleet reads whole.
  const fittedSubLabel = useCallback((node: GNode, group: string) => {
    // Left out when the drawn name already says the group.
    const text = ghostGroupLine(node.labelParts, group);
    if (!text) return "";
    if (staticView && layout === "force") return staticNameLines(text, namePx * subScale * GLYPH_ADVANCE, LONE_NAME_ROOM_PX).join("\n");
    const room = nameRoomPx.get(node.id) ?? LONE_NAME_ROOM_PX;
    const chars = Math.floor((room - LABEL_GAP_PX) / (namePx * subScale * GLYPH_ADVANCE));
    if (wrapNames) return wrapLabelWhole(text, Math.max(MIN_LABEL_CHARS, chars)).join("\n");
    if (chars < MIN_LABEL_CHARS) return "";
    return truncateLabel(text, chars);
  }, [nameRoomPx, namePx, wrapNames, subScale, staticView, layout]);

  // ── SVG mouse tracking for interactive edge drawing ──
  const handleSvgMouseMove = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (!connectSource) return;
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = vbX + ((e.clientX - rect.left) / rect.width) * vbW;
    const y = vbY + ((e.clientY - rect.top) / rect.height) * vbH;
    setMousePos({ x, y });
  }, [connectSource, vbX, vbY, vbW, vbH]);

  // Apply running state separately
  // With a layout a glyph is never drawn under GLYPH_FLOOR_PX across on
  // screen, however far the map is zoomed out.
  const glyphFloor = layout ? GLYPH_FLOOR_PX / 2 / pxPerUnit : 0;
  const restNodes: GNode[] = useMemo(
    () => layoutNodes.map(n => ({ ...n, r: Math.max(n.r, glyphFloor), isRunning: !!activeAgentIds?.has(n.id) })),
    [layoutNodes, activeAgentIds, glyphFloor],
  );
  // The nodes moved by hand at their pins, their arcs with them.
  useEffect(() => { pinBaseRef.current = layoutNodes; }, [layoutNodes]);
  const pins = pinState.base === layoutNodes ? pinState.pins : null;
  const { nodes, edges } = useMemo(
    () => (pins ? pinDrawing(restNodes, layoutEdges, pins) : { nodes: restNodes, edges: layoutEdges }),
    [restNodes, layoutEdges, pins],
  );
  const pinCount = pins?.size ?? 0;
  // What a drop keeps clear of (read by the pointer handlers): every glyph
  // and its outermost ring, plus the name clearance on screen.
  useEffect(() => {
    dropRoomRef.current = {
      ...dropRoomRef.current,
      nodes,
      gap: 8 + (2 * NAME_CLEARANCE_PX) / Math.max(pxPerUnit, 1e-6),
      pad: NAME_CLEARANCE_PX / Math.max(pxPerUnit, 1e-6),
    };
  }, [nodes, pxPerUnit]);
  // The fleets whose agents with no handover sit in a grid of their own,
  // each named over its grid: two fleets side by side read as two named groups.
  const fleetGrids = useMemo(() => {
    if (!layout) return [];
    const linked = new Set(edges.flatMap((edge) => [edge.sourceId, edge.targetId]));
    const fleetOf = new Map(agents.flatMap((a) => (a.fleet?.fleet_id ? [[a.id, a.fleet] as const] : [])));
    const byFleet = new Map<string, { name: string; nodes: GNode[] }>();
    for (const n of nodes) {
      const fleet = fleetOf.get(n.id);
      if (!fleet || linked.has(n.id)) continue;
      const entry = byFleet.get(fleet.fleet_id) ?? byFleet.set(fleet.fleet_id, { name: fleet.fleet_name ?? "", nodes: [] }).get(fleet.fleet_id)!;
      entry.nodes.push(n);
    }
    const grids = [...byFleet.entries()].flatMap(([id, { name, nodes: members }]) => {
      if (members.length < 2 || !name) return [];
      const x0 = Math.min(...members.map((n) => n.x - n.r));
      const x1 = Math.max(...members.map((n) => n.x + n.r));
      const y0 = Math.min(...members.map((n) => n.y - n.r));
      return [{ id, name, x: x0, y: y0, width: x1 - x0 }];
    });
    // One fleet alone needs no name: the page or the filter already says it.
    if (grids.length < 2) return [];
    // Each name runs past its own grid up to the next thing on its row (the
    // next grid's name, a glyph, the canvas edge), so a short grid with a
    // long name is not cut while room is free beside it.
    const rooms = fleetNameRooms(grids, nodes, W, nameSize * 1.6, nameSize);
    return grids.map((grid) => ({ ...grid, room: rooms.get(grid.id) ?? grid.width }));
  }, [layout, edges, agents, nodes, nameSize]);
  const nodeRadius = useMemo(() => new Map(nodes.map((n) => [n.id, n.r])), [nodes]);

  // ── Focus/filter: compute which nodes and edges are "connected" ──
  const focusedSet = useMemo(
    () => (shownFocus ? focusNeighbourhood(edges, shownFocus) : null),
    [shownFocus, edges],
  );
  // Directions the edges leave or reach each node, so its badges keep off them.
  const edgeAnglesByNode = useMemo(() => {
    const out = new Map<string, number[]>();
    const add = (id: string, angle: number) => {
      const list = out.get(id);
      if (list) list.push(angle);
      else out.set(id, [angle]);
    };
    for (const edge of edges) {
      add(edge.sourceId, edge.sourceAngle);
      add(edge.targetId, edge.targetAngle);
    }
    return out;
  }, [edges]);
  const focusedEdgeSet = useMemo(
    () => (shownFocus ? new Set(edges.filter(edge => edgeTouchesFocus(edge, shownFocus)).map(edge => edge.id)) : null),
    [shownFocus, edges],
  );
  // A large map animates its hot edges and nodes only (ANIMATED_EDGE_LIMIT).
  const calmMap = edges.length > ANIMATED_EDGE_LIMIT;
  // With a layout, hovering a node dims every node and handover not one
  // handover away from it; a focus or a connection being drawn keeps
  // its own emphasis.
  const hoverSet = useMemo(
    () => (layout && hoveredId && !shownFocus && !connectSource ? focusNeighbourhood(edges, new Set([hoveredId])) : null),
    [layout, hoveredId, shownFocus, connectSource, edges],
  );
  // Every layout change is drawn as a move (Sam's defect 4): the map
  // slides from what is on screen to the new layout, new agents fade in and
  // grow, removed ones fade out (agent-graph-tween.ts). Reduced motion and
  // the standalone flow page draw the change at once.
  const reducedMotion = usePrefersReducedMotion();
  const animated = useAnimatedLayout(nodes, edges, reducedMotion || !layout || movingId !== null, svgRef);
  // A layout click moves the drawing at once (the target layout costs a few
  // ms), and React draws the new layout once the move ends: the click waits
  // for no render of the graph. A target on a
  // canvas of another height is drawn the usual way.
  const previewTarget = animated.preview;
  useImperativeHandle(ref, () => ({
    switchLayout: (next, commit) => {
      // The current layout again, mid-move, moves the drawing back to it.
      if (!layout) return false;
      graphCache.current = cachedGraph(graphCache.current, computeGraph, next);
      const target = graphCache.current.byLayout.get(next)!;
      if (target.canvasH !== canvasH) return false;
      const targetNodes = target.layoutNodes.map((n) => ({ ...n, r: Math.max(n.r, glyphFloor), isRunning: !!activeAgentIds?.has(n.id) }));
      return previewTarget(targetNodes, target.edges, commit);
    },
  }), [layout, computeGraph, canvasH, glyphFloor, activeAgentIds, previewTarget]);
  const drawnNodes = animated.nodes;
  const drawnEdges = animated.edges;
  // Each edge row finds its source in one lookup, not a scan of the nodes.
  const layoutNodeById = useMemo(() => new Map(layoutNodes.map((n) => [n.id, n])), [layoutNodes]);
  // The drawn body of each node by its signature:
  // the same signature hands React the same element, which it skips, so a
  // layout change or a hover re-renders the node wrappers only.
  const bodyCache = useRef(new Map<string, { sig: string; body: React.ReactNode }>());
  const cachedBody = (id: string, sig: string | null, draw: () => React.ReactNode): React.ReactNode => {
    if (sig === null) {
      bodyCache.current.delete(id);
      return draw();
    }
    const hit = bodyCache.current.get(id);
    if (hit && hit.sig === sig) return hit.body;
    const body = draw();
    bodyCache.current.set(id, { sig, body });
    return body;
  };
  // Fit to view: zoom and pan so every agent, its name under it
  // included (width and lines), fills the canvas, centred on that box.
  const fitView = useCallback(() => {
    if (nodes.length === 0) return;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const n of nodes) {
      const lines = fittedNameLines(n).filter((line) => line.length > 0);
      const half = Math.max(n.r, (Math.max(0, ...lines.map((line) => [...line].length)) * nameSize * GLYPH_ADVANCE) / 2);
      x0 = Math.min(x0, n.x - half); x1 = Math.max(x1, n.x + half);
      y0 = Math.min(y0, n.y - n.r);
      y1 = Math.max(y1, n.y + n.r + 3 + nameSize * 1.2 * Math.max(1, lines.length));
    }
    // Framed in the part of the view the window shows, from the view's top:
    // a card taller than the screen keeps the drawing on the first screen.
    const rect = svgRef.current?.getBoundingClientRect();
    const frameH = fillHeight && rect
      ? visibleViewHeight(viewH, canvasH, rect.width, rect.top, window.innerHeight, W)
      : viewH;
    const fit = fitTransform({ x0, y0, x1, y1 }, W, frameH, 24, MIN_ZOOM, MAX_ZOOM);
    setZoom(fit.zoom);
    setPan({ x: fit.pan.x, y: framedPanY(fit.pan.y, fit.zoom, canvasH, frameH) });
  }, [nodes, canvasH, viewH, fillHeight, nameSize, fittedNameLines]);
  // The box of a ghost's group line (one or two lines, joined by a line
  // break) whose first baseline is at `baseline`.
  const subLabelBox = useCallback((x: number, baseline: number, sub: string): LabelBox => {
    const lines = sub.split("\n");
    const lineH = nameSize * subScale * 1.2;
    const h = lineH + (lines.length - 1) * nameSize * subScale * 1.15;
    const w = Math.max(...lines.map((line) => [...line].length)) * nameSize * subScale * GLYPH_ADVANCE;
    return { x, y: baseline - lineH / 2 + (h - lineH) / 2, w, h };
  }, [nameSize, subScale]);
  // A name at rest never covers another node's glyph:
  // it is left out, and the hovered or focused node always shows its own.
  // Computed once per target layout and hover, never per render: a layout
  // change renders the whole graph at its start and its end only.
  // A ghost's group line is part of what a node draws under it: a two-line
  // name and its group line make a taller stack than one line, and at a
  // narrow width the stack is cut (group line first, then the name) rather
  // than drawn over a neighbour's glyph or name.
  const { namesHidden, subLabelsHidden } = useMemo(() => {
    if (!layout) return { namesHidden: new Set<string>(), subLabelsHidden: new Set<string>() };
    const stacks = nodes.flatMap((n) => {
      if (n.id === hoveredId) return [];
      const lines = fittedNameLines(n).filter((line) => line.length > 0);
      const group = ghostNodes?.get(n.id);
      const sub = group ? fittedSubLabel(n, group) : "";
      if (lines.length === 0 && !sub) return [];
      const top = n.y + n.r + 3;
      const h = nameSize * (1.2 + Math.max(0, lines.length - 1) * 1.15);
      const name = lines.length > 0
        ? { x: n.x, y: top + h / 2, w: Math.max(...lines.map((line) => [...line].length)) * nameSize * GLYPH_ADVANCE, h }
        : null;
      const subBox = sub ? subLabelBox(n.x, top + nameSize * (2.1 + Math.max(0, lines.length - 1) * 1.15), sub) : null;
      return [{ id: n.id, name, sub: subBox }];
    });
    // A name that only touches a neighbour's ring is left out too: each glyph
    // keeps NAME_CLEARANCE_PX of screen room around it, counted from
    // its own outermost ring (running, company-managed or read-only, up to
    // r + 4), only its disc when it draws no ring.
    const clearance = (2 * NAME_CLEARANCE_PX) / pxPerUnit;
    const glyphs = nodes.map((n) => {
      const side = 2 * (n.r + ringReach({ ...n, isGhost: ghostNodes?.has(n.id) ?? false })) + clearance;
      return { id: n.id, box: { x: n.x, y: n.y, w: side, h: side } };
    });
    const fit = fitNameStacks(stacks, glyphs);
    // A static view never leaves a name out (#10147 UX r3 B1): the name is
    // cut to its room instead, and only a ghost's group line gives way.
    if (staticView) return { namesHidden: new Set<string>(), subLabelsHidden: new Set([...fit.subless, ...fit.hidden]) };
    return { namesHidden: fit.hidden, subLabelsHidden: fit.subless };
  }, [layout, nodes, hoveredId, fittedNameLines, fittedSubLabel, subLabelBox, nameSize, pxPerUnit, ghostNodes, staticView]);
  const shownSubLabel = (node: GNode, group: string) => (subLabelsHidden.has(node.id) ? "" : fittedSubLabel(node, group));
  // Nodes whose name does not fit its room at this zoom at all.
  const nameLessCount = useMemo(
    () => (layout ? nodes.filter((n) => !namesHidden.has(n.id) && fittedName(n) === "").length : 0),
    [layout, nodes, namesHidden, fittedName],
  );
  const shownName = useCallback(
    (node: GNode) => (namesHidden.has(node.id) ? "" : fittedName(node)),
    [namesHidden, fittedName],
  );
  const shownNameLines = (node: GNode): string[] => (namesHidden.has(node.id) ? [] : fittedNameLines(node));
  // The baseline of a ghost's group line, under the last line of its name.
  const subLabelY = (node: GNode) =>
    node.r + 3 + nameSize * (2.1 + Math.max(0, shownNameLines(node).filter((line) => line.length > 0).length - 1) * 1.15);
  // The names as drawn, for the drop room: a drop or a keyboard
  // move keeps the moved glyph off every other agent's name, and off the
  // small line drawn under a ghost's name (its home group).
  useEffect(() => {
    if (!layout) { dropRoomRef.current = { ...dropRoomRef.current, names: [] }; return; }
    const names = nodes.flatMap((n) => {
      const boxes: (NameBox & { id: string })[] = [];
      const lines = namesHidden.has(n.id) ? [] : fittedNameLines(n).filter((line) => line.length > 0);
      if (lines.length > 0) {
        const h = nameSize * (1.2 + (lines.length - 1) * 1.15);
        const w = Math.max(...lines.map((line) => [...line].length)) * nameSize * GLYPH_ADVANCE;
        boxes.push({ id: n.id, x: n.x, y: n.y + n.r + 3 + h / 2, w, h });
      }
      const group = ghostNodes?.get(n.id);
      const sub = group ? shownSubLabel(n, group) : "";
      if (sub) {
        const subY = n.y + n.r + 3 + nameSize * (2.1 + Math.max(0, lines.length - 1) * 1.15);
        // Padded by NAME_CLEARANCE_PX of screen room on every side, so the
        // moved glyph's outer ring keeps off the line too, not only its disc
        // (the ring stood 0.8 px from it at 1024).
        const line = { id: n.id, ...subLabelBox(n.x, subY, sub) };
        boxes.push(paddedNameBox(line, NAME_CLEARANCE_PX / Math.max(pxPerUnit, 1e-6)));
      }
      return boxes;
    });
    dropRoomRef.current = { ...dropRoomRef.current, names };
    // shownSubLabel reads fittedSubLabel and subLabelsHidden, both listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, nodes, namesHidden, subLabelsHidden, fittedNameLines, fittedSubLabel, subLabelBox, nameSize, ghostNodes, pxPerUnit]);
  // The boxes nothing drawn around a node (badges, trigger chips) may cover:
  // its own name and its ghost line, as drawn.
  const nodeLabelBoxes = (node: GNode, subLabel: string): LabelBox[] => {
    const boxes: LabelBox[] = [];
    const nameLines = shownNameLines(node).filter((line) => line.length > 0);
    if (nameLines.length > 0) {
      const h = nameSize * (1.2 + (nameLines.length - 1) * 1.15);
      const w = Math.max(...nameLines.map((line) => [...line].length)) * nameSize * GLYPH_ADVANCE;
      boxes.push({ x: node.x, y: node.y + node.r + 3 + h / 2, w, h });
    }
    if (subLabel) boxes.push(subLabelBox(node.x, node.y + subLabelY(node), subLabel));
    return boxes;
  };
  // Where each shown edge label sits: at its arc's apex, or slid along
  // the arc off another label, a node or a name; the dagre flow keeps its midpoint.
  const isLabelShown = useCallback((edge: GEdge) =>
    !layout
    || hoveredEdgeId === edge.id
    || hoveredId === edge.sourceId
    || hoveredId === edge.targetId
    || (selectedNodeId !== null && (selectedNodeId === edge.sourceId || selectedNodeId === edge.targetId))
    || !!focusedEdgeSet?.has(edge.id),
  [layout, hoveredEdgeId, hoveredId, selectedNodeId, focusedEdgeSet]);
  // Placed for the target layout (labels are hidden by CSS while a change
  // runs and stay mounted, so the end of a change does not redraw them).
  const labelPositions = useMemo(() => {
    const out = new Map<string, { x: number; y: number }>();
    if (!layout) return out;
    const shown = edges.filter((edge) => edge.arc && isLabelShown(edge));
    if (shown.length === 0) return out;
    const lineH = edgeLabelSize * 1.2;
    const obstacles = nodes.flatMap((n) => {
      const name = shownName(n);
      const nameW = [...name].length * nameSize * GLYPH_ADVANCE;
      // Every drawn line of the name, two for an office-prefixed agent.
      const lineCount = Math.max(1, shownNameLines(n).filter((line) => line.length > 0).length);
      const nameH = nameSize * (1.2 + (lineCount - 1) * 1.15);
      // A ghost's group line too: an edge label never covers it (#12347 UX r1 B2).
      const group = ghostNodes?.get(n.id);
      const sub = group ? shownSubLabel(n, group) : "";
      return [
        { x: n.x, y: n.y, w: 2 * n.r + 4, h: 2 * n.r + 4 },
        ...(nameW > 0 ? [{ x: n.x, y: n.y + n.r + 3 + nameSize * 0.05 + nameH / 2, w: nameW, h: nameH }] : []),
        ...(sub ? [subLabelBox(n.x, n.y + subLabelY(n), sub)] : []),
      ];
    });
    const places = placeArcLabels(
      shown.map((edge) => ({
        ...edge.arc!,
        w: (a2aEdgeConfig(edge.handoverType) ?? handoverTypeConfig(edge.handoverType)).label.length * edgeLabelSize * GLYPH_ADVANCE + 4,
        h: lineH,
        forced: edge.id === hoveredEdgeId,
      })),
      obstacles,
    );
    shown.forEach((edge, k) => {
      const p = places[k];
      if (p) out.set(edge.id, p);
    });
    return out;
    // shownSubLabel and subLabelY read subLabelsHidden, fittedSubLabel and the name lines, listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, edges, nodes, isLabelShown, shownName, nameSize, edgeLabelSize, hoveredEdgeId, ghostNodes, subLabelsHidden, fittedSubLabel, subLabelBox]);


  // The layout changes whenever an isolated focus starts, changes or ends:
  // show all of it, and drop a connection or popover that may name a node no
  // longer drawn.
  const lastIsolatedKey = useRef(isolatedKey);
  useEffect(() => {
    if (lastIsolatedKey.current === isolatedKey) return;
    lastIsolatedKey.current = isolatedKey;
    resetView();
    setConnectSource(null);
    setMousePos(null);
    setTypePicker(null);
    setEdgeEditor(null);
  }, [isolatedKey, resetView]);


  // The drawer opened on a node drawn under it: pan so the node
  // sits in the part of the canvas the drawer leaves visible. Once per
  // selection; a node already visible does not move.
  const lastRevealedId = useRef<string | null>(null);
  useEffect(() => {
    if (selectedNodeId === lastRevealedId.current) return;
    lastRevealedId.current = selectedNodeId;
    const svg = svgRef.current;
    const node = selectedNodeId ? nodes.find((n) => n.id === selectedNodeId) : undefined;
    if (!svg || !node) return;
    const rect = svg.getBoundingClientRect();
    const vb = viewBoxRef.current;
    const shift = revealShift(node.x, node.r, {
      svgLeft: rect.left,
      svgWidth: rect.width,
      coveredFrom: window.innerWidth - AGENT_DRAWER_PX,
      viewX: vb.x,
      viewW: vb.w,
    });
    if (shift !== 0) setPan((p) => ({ x: p.x - shift * viewRef.current.zoom, y: p.y }));
  }, [selectedNodeId, nodes]);

  // Find source node for rubber-band line
  const connectSourceNode = connectSource ? nodes.find(n => n.id === connectSource) : null;
  const nodeDragSource = nodeDrag?.pos ? nodes.find(n => n.id === nodeDrag.id) : null;

  return (
    <div
      className={fillHeight ? "relative flex min-h-0 flex-1 flex-col outline-none" : "relative outline-none"}
      ref={containerRef}
      tabIndex={-1}
      data-static-view={staticView ? "true" : undefined}
      style={
        staticView
          ? // The page scrolls over a static view, by wheel and by touch.
            { cursor: onStaticOpen ? "pointer" : "default" }
          : { cursor: isPanning || nodeDrag ? "grabbing" : ctrlHeld ? "zoom-in" : "grab", overscrollBehavior: "contain", touchAction: "none" }
      }
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerLeave={handlePointerLeave}
      onClick={
        staticView
          ? (e) => {
              // A static view opens the full graph: on the node clicked, or as a whole.
              const id = (e.target as Element).closest("[data-node-id]")?.getAttribute("data-node-id") ?? null;
              onStaticOpen?.(id);
            }
          : handleContainerClick
      }
      onDoubleClick={(e) => {
        if (staticView) return;
        // A double-click on the empty canvas fits the map back; a
        // node's own double-click toggles its focus and stops here.
        if ((e.target as Element).closest("[data-node-id], button, [data-type-picker], [data-edge-editor]")) return;
        resetView();
      }}
    >
      <svg
        ref={svgRef}
        viewBox={viewBox}
        className={cn("w-full rounded-sm overflow-hidden select-none", fillHeight && "grow shrink-0")}
        // Filling, the canvas's aspect is the floor and the parent's height the rest.
        style={{ aspectRatio: `${W} / ${canvasH}` }}
        preserveAspectRatio="none"
        data-animating={animated.animating ? "true" : undefined}
        data-fps={animated.fps > 0 ? animated.fps : undefined}
        data-worst-frame-ms={animated.worstFrameMs > 0 ? animated.worstFrameMs : undefined}
        data-first-frame-ms={animated.firstFrameMs > 0 ? animated.firstFrameMs : undefined}

        onMouseMove={handleSvgMouseMove}
      >
        {/* CSS keyframes */}
        <style>{`${DASH_KEYFRAMES}\n${BEAM_PARTICLE_KEYFRAMES}\n${MOVING_CSS}\n${FOCUS_RING_CSS}\n${EDGE_FOCUS_CSS}`}</style>
        <defs>
          <radialGradient id="af-glow-cyan">
            <stop offset="0%" stopColor={CYAN} stopOpacity="0.22" />
            <stop offset="60%" stopColor={CYAN} stopOpacity="0.05" />
            <stop offset="100%" stopColor={CYAN} stopOpacity="0" />
          </radialGradient>
          <radialGradient id="af-glow-dim">
            <stop offset="0%" stopColor={CYAN_DIM} stopOpacity="0.12" />
            <stop offset="60%" stopColor={CYAN_DIM} stopOpacity="0.03" />
            <stop offset="100%" stopColor={CYAN_DIM} stopOpacity="0" />
          </radialGradient>
          <radialGradient id="af-glow-gold">
            <stop offset="0%" stopColor={GRAPH_COLORS.AMBER} stopOpacity="0.3" />
            <stop offset="60%" stopColor={GRAPH_COLORS.AMBER} stopOpacity="0.08" />
            <stop offset="100%" stopColor={GRAPH_COLORS.AMBER} stopOpacity="0" />
          </radialGradient>

        </defs>

        {/* Grid background */}
        <pattern id="af-grid" width="50" height="50" patternUnits="userSpaceOnUse">
          <path d="M 50 0 L 0 0 0 50" fill="none" stroke={GRID_C} strokeWidth="0.5" />
        </pattern>
        {/* Filling, the grid covers the view wherever it is panned or zoomed. */}
        {fillHeight
          ? <rect data-grid x={vbX} y={vbY} width={vbW} height={vbH} fill="url(#af-grid)" opacity="0.3" />
          : <rect data-grid width={W} height={canvasH} fill="url(#af-grid)" opacity="0.3" />}

        {/* Ambient floating particles */}
        {!calmMap && AMBIENT.map((p, i) => (
          <circle key={`amb-${i}`} r={p.r} fill={CYAN} opacity={p.op}>
            <animate attributeName="cx" values={`${p.x};${p.x + p.rx};${p.x - p.rx * 0.5};${p.x}`} dur={`${p.dur}s`} repeatCount="indefinite" />
            <animate attributeName="cy" values={`${p.y};${p.y - p.ry};${p.y + p.ry * 0.7};${p.y}`} dur={`${p.dur * 1.3}s`} repeatCount="indefinite" />
          </circle>
        ))}

        {/* ── Directed edges ── */}
        <g opacity={focusedSet ? 1 : 1}>
          {drawnEdges.map((edge, ei) => {
            const src = layoutNodeById.get(edge.sourceId);
            // Off the hovered node's neighbourhood, or fading in or out with a layout change.
            const hoverDim = !!hoverSet && hoveredId !== edge.sourceId && hoveredId !== edge.targetId;
            const srcEnabled = src?.enabled ?? true;
            // A handover into a group node is drawn, never edited from it.
            const a2aCfg = a2aEdgeConfig(edge.handoverType);
            // A liaison is an integration binding: drawn, never edited here.
            const srcEditable = (src?.isEditable ?? false) && !a2aCfg && !isViewOnlyId(edge.targetId) && !isViewOnlyId(edge.sourceId);
            // A dashed edge (a cross-fleet handover not governed or not exposed): dotted and still.
            const isDashed = !!dashedEdgeIds?.has(`${edge.sourceId}->${edge.targetId}`);
            const dashedTip = !isDashed
              ? undefined
              : typeof dashedEdgeTooltip === "function"
                ? dashedEdgeTooltip(`${edge.sourceId}->${edge.targetId}`)
                : dashedEdgeTooltip;
            const typeCfg = a2aCfg ?? handoverTypeConfig(edge.handoverType);
            // A darker shade and a higher rest opacity on the light canvas,
            // so an edge keeps 3:1 against it.
            const edgeColor = edgeInk(typeCfg, colorMode === "light");
            const restOpacity = colorMode === "light" ? EDGE_REST_OPACITY.light : EDGE_REST_OPACITY.dark;
            // Sized from the node it points to, in drawing units, so it
            // follows the zoom and never outweighs the node.
            const head = arrowHead(nodeRadius.get(edge.targetId) ?? 8, layout ? pxPerUnit : undefined);
            // Translated before it names the edge;
            // lowercased by the locale's rules (a no-op where there is no case).
            const typeLabel = t_i18n(typeCfg.label).toLocaleLowerCase(locale);
            const edgeFocusDim = focusedEdgeSet && !focusedEdgeSet.has(edge.id);
            // With a layout the type label is drawn on hover only:
            // the edge itself, either end node, or an edge of the focus.
            // The flow page keeps its label 4 units over the midpoint; on a
            // layout, a label with no free room on its arc is left out.
            const placedAt = labelPositions.get(edge.id);
            const labelShown = !edge.leaving && isLabelShown(edge) && (!layout || !edge.arc || !!placedAt);
            const labelAt = placedAt ?? { x: edge.midX, y: edge.midY - 4 - edgeLabelSize * 0.35 };
            // A handover running, pending or ended in the last 15 minutes along
            // this edge: bright and fast, its tooltip says when it fired.
            const live = liveEdges?.get(`${edge.sourceId}->${edge.targetId}`);
            const isLive = !!live;
            const liveTip = live
              ? t_i18n("Handover fired at {time}, {status}", {
                time: intl.formatDate(live.firedAt, { timeStyle: "short" }),
                status: t_i18n(LIVE_STATUS_LABEL[live.status]).toLocaleLowerCase(locale),
              })
              : undefined;
            const withLive = (tip: string) => (liveTip ? `${liveTip}. ${tip}` : tip);
            const isAnimated =
              !calmMap || isLive || edge.sourceId === hoveredId || edge.targetId === hoveredId || !!focusedEdgeSet?.has(edge.id);
            const edgeBaseOpacity = (edgeFocusDim ? 0.05 : isLive ? LIVE_EDGE_OPACITY : restOpacity) * (hoverDim ? 0.15 : 1);
            const edgeOpacity = edgeBaseOpacity * (edge.fade ?? 1);
            const editTip = withLive(t_i18n("Click to edit this handover"));
            const readOnlyTip = withLive(t_i18n("Read-only: the source agent is not editable"));
            // A liaison is an integration binding, not a handover: its tip says what it is.
            const liaisonTip = !a2aCfg
              ? undefined
              : edge.handoverType === "a2a_inbound"
                ? t_i18n("A2A liaison: this partner tasks the agent through an A2A integration")
                : t_i18n("A2A liaison: the agent delegates to this partner agent through an A2A integration");
            const conditionTip = t_i18n("Condition: {condition}", { condition: edge.condition ?? "" });
            const edgeName = t_i18n("Handover {source} to {target}, {type}", {
              source: src?.name ?? edge.sourceId,
              target: layoutNodeById.get(edge.targetId)?.name ?? edge.targetId,
              type: typeLabel,
            });
            return (
              <EdgeRow
                key={edge.id}
                edge={edge}
                index={ei}
                moving={animated.animating}
                head={head}
                edgeColor={edgeColor}
                edgeDash={typeCfg.dash}
                typeLabel={typeLabel}
                edgeName={edgeName}
                isDashed={isDashed}
                dashedTip={dashedTip ? withLive(dashedTip) : undefined}
                edgeOpacity={edgeOpacity}
                edgeBaseOpacity={edgeBaseOpacity}
                isAnimated={isAnimated}
                isLive={isLive}
                liveTip={liveTip}
                isEditable={isEditable}
                hasLayout={!!layout}
                srcEditable={srcEditable}
                srcEnabled={srcEnabled}
                edgeFocusDim={!!edgeFocusDim}
                hoverDim={hoverDim}
                labelShown={labelShown}
                labelX={labelAt.x}
                labelY={labelAt.y}
                edgeLabelSize={edgeLabelSize}
                labelHalo={LABEL_HALO}
                editTip={editTip}
                readOnlyTip={readOnlyTip}
                liaisonTip={liaisonTip}
                conditionTip={conditionTip}
                onEdgeClick={handleEdgeClick}
                onHoverEdge={setHoveredEdgeId}
                showTooltip={showTooltip}
                moveTooltip={moveTooltip}
                hideTooltip={hideTooltip}
              />
            );
          })}
        </g>

        {/* ── Rubber-band line from source to cursor ── */}
        {connectSource && connectSourceNode && mousePos && (
          <line
            x1={connectSourceNode.x}
            y1={connectSourceNode.y}
            x2={mousePos.x}
            y2={mousePos.y}
            stroke={CYAN_B}
            strokeWidth={1.2}
            strokeDasharray="6 4"
            opacity={0.5}
          />
        )}

        {/* ── Node drag line: from the dragged node to the pointer.
             Over a valid target a solid, thicker cyan line; elsewhere a thin
             dashed grey one, at full opacity so both reach 3:1. ── */}
        {nodeDrag?.pos && nodeDragSource && (
          <line
            data-node-drag-line
            x1={nodeDragSource.x}
            y1={nodeDragSource.y}
            x2={nodeDrag.pos.x}
            y2={nodeDrag.pos.y}
            data-valid={nodeDrag.valid ? "true" : "false"}
            stroke={nodeDrag.valid ? DROP_OK : DROP_NO}
            strokeWidth={nodeDrag.valid ? 2 : 1.5}
            strokeDasharray={nodeDrag.valid ? undefined : "3 3"}
            strokeLinecap="round"
            pointerEvents="none"
          />
        )}

        {/* ── Fleet names over their grids ── */}
        {fleetGrids.length > 0 && movingId === null && (
          <g className={HIDE_MOVING_CLASS} aria-hidden="true" pointerEvents="none">
            {fleetGrids.map((grid) => {
              // Cut to its room, so it never runs into the next grid's name
              // or a glyph; a room too
              // narrow for a readable stub draws no name.
              const maxChars = Math.floor(grid.room / (nameSize * (GLYPH_ADVANCE + FLEET_NAME_TRACKING)));
              if (maxChars < MIN_FLEET_NAME_CHARS) return null;
              return (
              <text
                key={grid.id}
                data-fleet-grid-label={grid.id}
                x={grid.x} y={grid.y - nameSize * 0.8}
                fill={LABEL}
                opacity={0.6}
                fontSize={nameSize}
                fontWeight="600"
                letterSpacing={nameSize * FLEET_NAME_TRACKING}
                fontFamily="'IBM Plex Sans', sans-serif"
              >
                {truncateLabel(grid.name, maxChars)}
              </text>
              );
            })}
          </g>
        )}

        {/* ── Agent nodes ── */}
        {drawnNodes.map((node) => {
          const Icon = node.isGeneralAssistant ? Sparkles : (ICON_MAP[node.iconKey] ?? Bot);
          const summary = node.isGeneralAssistant ? undefined : agentSummaries?.[node.id];
          const isReadOnly = !node.isEditable && !node.isGeneralAssistant;
          const isFocused = !!shownFocus?.has(node.id);
          const isFocusConnected = focusedSet ? focusedSet.has(node.id) : true;
          const focusDim = focusedSet && !isFocusConnected;
          const isViewOnly = isViewOnlyId(node.id);
          const ghostGroup = ghostNodes?.get(node.id);
          const isGhost = ghostGroup !== undefined;
          // The stored name as it is, brackets included, and its fleet.
          // A fleet node is named after its fleet already: its name alone.
          const homeGroup = isGhost && ghostGroup ? ghostGroup : node.fleetName;
          const ghostDetail = ghostDetails?.get(node.id);
          const ghostName = `${homeGroup && homeGroup !== node.name ? `${node.name} · ${homeGroup}` : node.name}${ghostDetail ? ` · ${ghostDetail}` : ""}`;
          const isConnectTarget = connectSource !== null && connectSource !== node.id && !node.isGeneralAssistant && !isViewOnly;
          const isConnectHighlight = connectSource === node.id;
          const isSelected = selectedNodeId === node.id;
          const isDropTarget = !!nodeDrag?.valid && nodeDrag.targetId === node.id;
          const isDropRefused = !!nodeDrag && !nodeDrag.valid && nodeDrag.targetId === node.id;
          const isDropFlash = dropFlashId === node.id;
          const isDraggable = !!onNodeDrop && !!draggableNodeIds?.has(node.id);
          const glow = !node.enabled ? "url(#af-glow-dim)" : isFocused ? "url(#af-glow-gold)" : "url(#af-glow-cyan)";
          const strokeColor = isFocused ? GRAPH_COLORS.AMBER : CYAN;
          const textOpacity = focusDim ? 0.25 : node.enabled ? 0.9 : 0.4;
          // A ghost dims its shape, never its words: the name keeps a member's
          // opacity and the home group is drawn at a readable contrast.
          const hoverDim = !!hoverSet && !hoverSet.has(node.id);
          const nodeBaseOpacity = focusDim || hoverDim ? 0.15 : isGhost ? 1 : node.enabled ? 1 : 0.45;
          const nodeOpacity = nodeBaseOpacity * (node.fade ?? 1);
          const ghostShapeOpacity = isGhost ? (node.enabled ? 0.6 : 0.35) : undefined;
          const ghostSubLabel = isGhost && ghostGroup ? shownSubLabel(node, ghostGroup) : "";
          // Badges, output format and trigger chips: with a layout, on hover
          // or focus only; always on the standalone flow page.
          const detailShown = !layout || hoveredId === node.id;
          // Everything the body draws, in the node's own coordinates:
          // a move changes only the wrapper's transform, so
          // a layout change re-renders no body. A body with its details shown
          // (badges, chips, info) is drawn fresh.
          const bodySig = detailShown ? null : [
            node.r, node.enabled, node.isRunning, node.isEditable, node.isCompanyManaged, node.iconKey,
            node.handoverCount, node.inboundCount, node.isGeneralAssistant, node.isLinkedGA,
            ghostGroup ?? "", ghostSubLabel, isFocused, !!focusDim, isConnectHighlight, isSelected,
            isConnectTarget, hoveredId === node.id, shownNameLines(node).join("\n"),
            colorMode, nameSize, calmMap, isDropTarget, isDropFlash,
          ].join("|");
          // One line, its parts joined by a middle dot, every word translated.
          const runningLabel = node.isRunning ? ` · ${t_i18n("Running")}` : "";
          // A click opens the drawer where there is one; the connect handle starts a handover.
          const startHint = onShowDetails && !node.isGeneralAssistant
            ? ` · ${t_i18n("Click to open")}`
            : isEditable && node.isEditable ? ` · ${t_i18n("Click to start handover")}` : "";
          const nodeTooltip = isViewOnly
            ? `${ghostName} · ${t_i18n("Click to open")}`
            : isConnectTarget
            ? t_i18n("Click to connect from {name}", { name: nodes.find(n => n.id === connectSource)?.name ?? "" })
            : isConnectHighlight
            // The connection's own source: the next click cancels it.
            ? `${ghostName} · ${t_i18n("Click to cancel")}`
            : isReadOnly || !node.enabled
              ? `${ghostName} · ${!node.enabled ? t_i18n("Disabled") : t_i18n("Read-only")}`
              : node.handoverCount > 0
                ? `${ghostName}${runningLabel} · ${t_i18n("{count, plural, one {# handover target} other {# handover targets}}", { count: node.handoverCount })}${startHint}`
                : `${ghostName}${runningLabel}${startHint}`;
          const shownTooltip = isDraggable && nodeDragHint ? `${nodeTooltip} · ${nodeDragHint}` : nodeTooltip;

          return (
            <g
              key={node.id}
              data-node-id={node.id}
              data-selected={isSelected ? "true" : undefined}
              data-drop-target={isDropTarget ? "true" : undefined}
              data-drop-refused={isDropRefused ? "true" : undefined}
              data-drop-flash={isDropFlash ? "true" : undefined}
              role="group"
              aria-label={ghostName}
              data-ghost={isGhost ? "true" : undefined}
              data-dimmed={hoverDim ? "true" : undefined}
              data-leaving={node.leaving ? "true" : undefined}
              aria-hidden={node.leaving ? true : undefined}
              pointerEvents={node.leaving ? "none" : undefined}
              tabIndex={layout && !node.leaving ? 0 : undefined}
              transform={`translate(${node.x} ${node.y})`}
              opacity={nodeOpacity}
              {...{ [TWEEN_NODE_ATTR]: node.id, [BASE_OPACITY_ATTR]: nodeBaseOpacity }}
              style={{ cursor: nodeDrag ? (isDropRefused ? "not-allowed" : "grabbing") : ctrlHeld && !node.isGeneralAssistant ? "alias" : isConnectTarget ? "crosshair" : isEditable && !isViewOnly ? (isReadOnly ? "not-allowed" : "pointer") : "pointer", outline: "none" }}
              onPointerDown={staticView ? undefined : isDraggable ? (e) => startNodeDrag(node.id, e) : layout && !node.leaving ? (e) => startNodeMove(node.id, e) : undefined}
              data-pinned={pins?.has(node.id) ? "true" : undefined}
              onMouseEnter={(e) => { setHoveredId(node.id); if (nodeDragRef.current?.moved) return; showTooltip(e, ctrlHeld && !node.isGeneralAssistant ? t_i18n("Open {name}", { name: node.name }) : shownTooltip); }}
              onMouseMove={moveTooltip}
              onMouseLeave={() => { setHoveredId((id) => (id === node.id ? null : id)); hideTooltip(); }}
              onPointerUp={(e) => {
                // A handover dragged from another node's connect handle ends here.
                const from = connectDragRef.current;
                if (!from || from === node.id || node.leaving) return;
                connectDragRef.current = null;
                // The release is not also a click on the canvas, which would close the picker.
                suppressClickRef.current = true;
                handleNodeClick(node.id, node.isEditable, node.isGeneralAssistant, e);
              }}
              onFocus={(e) => {
                setHoveredId(node.id);
                // A keyboard focus says the full name and home fleet too,
                // as a hover does: a tooltip under the node's centre.
                const box = e.currentTarget.getBoundingClientRect();
                const rect = containerRef.current?.getBoundingClientRect();
                if (!rect) return;
                setTooltip({ text: shownTooltip, x: box.left + box.width / 2 - rect.left, y: box.top - rect.top, w: rect.width, h: rect.height });
              }}
              onBlur={() => { setHoveredId((id) => (id === node.id ? null : id)); hideTooltip(); }}
              onKeyDown={(e) => {
                // A static view: Enter and Space open the full graph on this
                // node, as a click does.
                if (staticView) {
                  if ((e.key === "Enter" || e.key === " ") && onStaticOpen && !node.leaving) {
                    e.preventDefault();
                    onStaticOpen(node.id);
                  }
                  return;
                }
                // Arrow keys move the node, Shift for a larger step, and pin it
                // like a drag.
                // The target goes through the free-spot rule of a mouse drop:
                // never onto another agent.
                const { nodes: drawn, gap, names, pad } = dropRoomRef.current;
                const others = drawn.filter((n) => n.id !== node.id);
                const otherNames = names.filter((b) => b.id !== node.id);
                // Alt+Arrow moves the focus to the nearest agent that way;
                // a plain arrow moves the agent.
                // Every Alt+Arrow is blocked, a target or not: Alt+Left/Right
                // is browser Back/Forward on Windows and Linux.
                if (e.altKey && layout && !node.leaving) {
                  const { handled, next } = altArrowFocus({ x: node.x, y: node.y }, e.key, others);
                  if (handled) e.preventDefault();
                  if (next) {
                    const el = svgRef.current?.querySelector<SVGGElement>(`g[data-node-id="${CSS.escape(next.id)}"]`);
                    el?.focus();
                  }
                  return;
                }
                const step = (e.shiftKey ? 10 : 2) * nameSize;
                const to = layout && !node.leaving
                  ? keyboardMove({ x: node.x, y: node.y }, e.key, step, others, 2 * node.r + gap, otherNames, node.r + pad)
                  : null;
                if (to) {
                  e.preventDefault();
                  movePin(node.id, to);
                  return;
                }
                // Space starts, cancels or ends a handover as a click does, and Enter too
                // while one is being drawn; Enter otherwise opens the drawer.
                const connectKey = e.key === " " || (e.key === "Enter" && (!!connectSource || !onShowDetails));
                if (connectKey && isEditable && !node.leaving) {
                  e.preventDefault();
                  const box = e.currentTarget.getBoundingClientRect();
                  handleNodeClick(node.id, node.isEditable, node.isGeneralAssistant, {
                    clientX: box.left + box.width / 2,
                    clientY: box.top + box.height / 2,
                    stopPropagation: () => {},
                    keyboard: { top: box.top, bottom: box.bottom, left: box.left, right: box.right, returnTo: e.currentTarget },
                  });
                  return;
                }
                if (e.key !== "Enter" || !onShowDetails || node.isGeneralAssistant) return;
                e.preventDefault();
                onShowDetails(node.id);
              }}
              onClick={(e) => {
                // A static view: the container's click opens the full graph on this node.
                if (staticView) return;
                // Ctrl/Cmd+click → navigate to agent detail
                if ((e.ctrlKey || e.metaKey) && !node.isGeneralAssistant && onNavigateToAgent) {
                  e.stopPropagation();
                  onNavigateToAgent(node.id);
                  return;
                }
                // The release of a node drag is not a click.
                if (suppressClickRef.current) { suppressClickRef.current = false; e.stopPropagation(); return; }
                // Skip the second click of a double-click
                if (e.detail >= 2) return;
                // Defer single-click to distinguish from double-click
                e.stopPropagation();
                // Cancel any pending timer first
                if (clickTimer.current) { clearTimeout(clickTimer.current); clickTimer.current = null; }
                const nodeId = node.id;
                const nodeIsEditable = node.isEditable;
                clickTimer.current = window.setTimeout(() => {
                  clickTimer.current = null;
                  handleNodeClick(nodeId, nodeIsEditable, node.isGeneralAssistant, e);
                }, 250);
              }}
              onDoubleClick={(e) => {
                e.stopPropagation();
                // Cancel the pending single-click
                if (clickTimer.current) { clearTimeout(clickTimer.current); clickTimer.current = null; }
                if (onToggleFocus) onToggleFocus(node.id);
              }}
              onContextMenu={onNodeContextMenu && !node.isGeneralAssistant ? (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (clickTimer.current) { clearTimeout(clickTimer.current); clickTimer.current = null; }
                hideTooltip();
                onNodeContextMenu(node.id, e);
              } : undefined}
            >
              {cachedBody(node.id, bodySig, () => (<>
              {/* Outer glow (none on a ghost) */}
              {!isGhost && <circle cx={0} cy={0} r={node.r + 8} fill={glow} />}

              {/* Connection source highlight ring */}
              {isConnectHighlight && (
                <circle
                  cx={0} cy={0} r={node.r + 6}
                  fill="none" stroke={CYAN_B} strokeWidth="1.5"
                  opacity="0.6"
                >
                  <animate attributeName="opacity" values="0.6;0.2;0.6" dur="1.5s" repeatCount="indefinite" />
                </circle>
              )}

              {/* Drop target ring: the node a dragged node would be dropped on */}
              {isDropTarget && (
                <circle
                  data-drop-ring
                  cx={0} cy={0} r={node.r + 7}
                  fill="none" stroke={DROP_OK} strokeWidth="2.5"
                />
              )}

              {/* New parent ring, for a moment after a drop */}
              {isDropFlash && !isDropTarget && (
                <circle
                  data-drop-flash-ring
                  cx={0} cy={0} r={node.r + 9}
                  fill="none" stroke={DROP_OK} strokeWidth="2.5"
                />
              )}

              {/* Selection ring: the node whose detail drawer is open */}
              {isSelected && (
                <circle
                  cx={0} cy={0} r={node.r + 6}
                  fill="none" stroke={CYAN_B} strokeWidth="2"
                />
              )}

              {/* Connection target highlight ring */}
              {isConnectTarget && (
                <circle
                  cx={0} cy={0} r={node.r + 4}
                  fill="none" stroke={CYAN} strokeWidth="0.8"
                  strokeDasharray="4 3"
                  opacity="0.4"
                />
              )}

              {/* Running spinner ring */}
              {node.isRunning && (
                <>
                  {(!calmMap || node.id === hoveredId || isFocused) && (
                    <circle cx={0} cy={0} r={node.r} fill="none" stroke={CYAN} strokeWidth="0.8" opacity="0">
                      <animate attributeName="r" values={`${node.r};${node.r + 10};${node.r}`} dur="2s" repeatCount="indefinite" />
                      <animate attributeName="opacity" values="0.5;0;0.5" dur="2s" repeatCount="indefinite" />
                    </circle>
                  )}
                  <circle
                    cx={0} cy={0} r={node.r + 4}
                    fill="none" stroke={CYAN_B} strokeWidth="1.2"
                    strokeDasharray={`${Math.PI * (node.r + 4) * 0.4} ${Math.PI * (node.r + 4) * 1.6}`}
                    strokeLinecap="round"
                    opacity="0.7"
                  >
                    <animateTransform attributeName="transform" type="rotate" from="0 0 0" to="360 0 0" dur="1.5s" repeatCount="indefinite" />
                  </circle>
                </>
              )}

              {/* Pulse ring for handover nodes (non-running) */}
              {node.enabled && !node.isRunning && !isReadOnly && (node.handoverCount > 0 || node.inboundCount > 0) && (!calmMap || node.id === hoveredId || isFocused) && (
                <circle cx={0} cy={0} r={node.r} fill="none" stroke={CYAN} strokeWidth="0.5" opacity="0">
                  <animate attributeName="r" values={`${node.r};${node.r + 12};${node.r}`} dur="4s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.2;0;0.2" dur="4s" repeatCount="indefinite" />
                </circle>
              )}

              {/* Read-only dashed ring (a ghost is view only already) */}
              {isReadOnly && !isGhost && (
                <circle
                  cx={0} cy={0} r={node.r + 3}
                  fill="none" stroke={CYAN_DIM} strokeWidth="0.5"
                  strokeDasharray="3 2"
                  opacity={0.5}
                />
              )}

              {/* Company-managed outer ring (for editable company-managed = admin) */}
              {node.isCompanyManaged && !isReadOnly && (
                <circle
                  cx={0} cy={0} r={node.r + 3}
                  fill="none" stroke={CYAN} strokeWidth="0.4"
                  strokeDasharray="3 2"
                  opacity={0.35}
                />
              )}

              {/* Main circle */}
              <circle
                cx={0} cy={0} r={node.r}
                fill={NODE_BG}
                stroke={node.isRunning ? CYAN_B : !node.enabled || isGhost ? CYAN_DIM : strokeColor}
                strokeWidth={node.isRunning ? 1.6 : 1.2}
                strokeDasharray={isGhost ? "3 2" : undefined}
                opacity={ghostShapeOpacity}
              />

              {/* Agent icon */}
              <foreignObject x={-5} y={-5} width={10} height={10} opacity={ghostShapeOpacity}>
                <Icon
                  width={10} height={10}
                  color={node.enabled ? CYAN_B : CYAN_DIM}
                  strokeWidth={1.5}
                />
              </foreignObject>

              {/* Agent name. Drawn after the drop and new parent rings, with the
                  canvas halo behind its glyphs while a ring is shown, so a ring
                  never paints over the name. */}
              <text
                data-node-name
                x={0} y={node.r + 3 + nameSize}
                textAnchor="middle"
                fill={LABEL}
                // A canvas-colour halo, so a branch trunk, a lane or a drop
                // ring passing under the name never runs through its letters:
                // the stroke
                // is painted under the fill.
                stroke={LABEL_HALO}
                strokeWidth={nameSize * 0.32}
                strokeLinejoin="round"
                paintOrder="stroke"
                fontSize={nameSize}
                fontWeight="500"
                fontFamily="'IBM Plex Sans', sans-serif"
                opacity={textOpacity}
                aria-hidden="true"
              >
                {shownNameLines(node).map((line, li) => (
                  <tspan key={li} x={0} dy={li === 0 ? 0 : nameSize * 1.15}>{line}</tspan>
                ))}
              </text>

              {/* Keyboard focus ring (the map's nodes take the focus) */}
              {layout && hoveredId === node.id && (
                <circle cx={0} cy={0} r={node.r + 3} fill="none" stroke={CYAN_B} strokeWidth={1} opacity={0.8} />
              )}

              {/* A ghost's home group, under its name */}
              {ghostSubLabel && (
                <text
                  x={0} y={subLabelY(node)}
                  textAnchor="middle"
                  fill={LABEL}
                  fontSize={nameSize * subScale}
                  fontStyle="italic"
                  fontFamily="'IBM Plex Sans', sans-serif"
                  opacity={focusDim ? 0.25 : 0.75}
                  aria-hidden="true"
                >
                  {ghostSubLabel.split("\n").map((line, li) => (
                    <tspan key={li} x={0} dy={li === 0 ? 0 : nameSize * subScale * 1.15}>{line}</tspan>
                  ))}
                </text>
              )}

              {/* Badges: outside the ring, each in a direction no edge of the
                  node takes, never over the ring or an edge start. */}
              {(() => {
                if (!detailShown) return null;
                const kinds: Array<"format" | "count" | "lock" | "ga" | "disabled"> = [];
                if (node.enabled) kinds.push("format");
                if (node.handoverCount > 0) kinds.push("count");
                if (isReadOnly) kinds.push("lock");
                if (node.isLinkedGA && !isReadOnly) kinds.push("ga");
                if (!node.enabled) kinds.push("disabled");
                const angles = badgeAngles(edgeAnglesByNode.get(node.id) ?? [], kinds.length);
                const fmt = OUTPUT_FORMAT_CONFIG[node.outputFormat as OutputFormat]?.label.toLowerCase() ?? node.outputFormat;
                const fmtW = fmt.length * 2.9 + 6;
                // The boxes a badge must leave free: the node's own name and its
                // ghost line, as drawn.
                const avoid = nodeLabelBoxes(node, ghostSubLabel);
                // Clear of the outermost ring drawn (the focus ring at r + 3 on
                // the map), then out of the name's way along the same angle.
                const ringRadius = layout && hoveredId === node.id ? node.r + 3.5 : node.r + 1;
                // The body is drawn about the node's own origin.
                const at = (a: number, hw: number, hh: number) => {
                  const c = badgeCentre({ x: node.x, y: node.y }, ringRadius, a, hw, hh, avoid);
                  return { x: c.x - node.x, y: c.y - node.y };
                };
                return kinds.map((kind, k) => {
                  const a = angles[k] ?? -Math.PI / 2;
                  if (kind === "format") {
                    const { x: bx, y: by } = at(a, fmtW / 2, 4);
                    const w = fmtW;
                    return (
                      <g key="format" opacity={0.6 * (ghostShapeOpacity ?? 1)}>
                        <rect
                          x={bx - w / 2} y={by - 4}
                          width={w} height={8}
                          rx={1.5}
                          fill={NODE_BG}
                          stroke={CYAN_DIM}
                          strokeWidth={0.4}
                        />
                        <text
                          x={bx} y={by + 1.5}
                          textAnchor="middle"
                          fill={CYAN_DIM}
                          fontSize="5"
                          fontWeight="500"
                          fontFamily="'IBM Plex Sans', sans-serif"
                        >
                          {fmt}
                        </text>
                      </g>
                    );
                  }
                  const { x: cx, y: cy } = at(a, 5.5, 5.5);
                  if (kind === "count") {
                    return (
                      <g key="count" opacity={ghostShapeOpacity} data-handover-count={node.handoverCount}>
                        <circle cx={cx} cy={cy} r="5.5" fill={NODE_BG} stroke={isReadOnly ? CYAN_DIM : CYAN} strokeWidth="0.6" />
                        <text
                          x={cx} y={cy + 2.5}
                          textAnchor="middle"
                          fill={isReadOnly ? CYAN_DIM : CYAN_B}
                          fontSize="6"
                          fontWeight="600"
                          fontFamily="'IBM Plex Sans', sans-serif"
                        >
                          {node.handoverCount}
                        </text>
                      </g>
                    );
                  }
                  const BadgeIcon = kind === "lock" ? Lock : kind === "ga" ? Sparkles : PowerOff;
                  const iconSize = kind === "ga" ? 8 : 7;
                  return (
                    <g key={kind} opacity={kind === "ga" ? undefined : ghostShapeOpacity}>
                      <circle cx={cx} cy={cy} r={kind === "ga" ? 5.5 : 5} fill={NODE_BG} stroke={kind === "ga" ? CYAN : CYAN_DIM} strokeWidth={kind === "ga" ? 0.6 : 0.5} />
                      <foreignObject x={cx - iconSize / 2} y={cy - iconSize / 2} width={iconSize} height={iconSize}>
                        <BadgeIcon width={iconSize} height={iconSize} color={kind === "ga" ? CYAN_B : CYAN_DIM} strokeWidth={2} />
                      </foreignObject>
                    </g>
                  );
                });
              })()}

              {/* Trigger-type chips (Map tab) — one mini icon per distinct trigger */}
              {summary && summary.triggerTypes.length > 0 && detailShown && (() => {
                const sz = 6;
                const gap = 2.5;
                const n = summary.triggerTypes.length;
                const totalW = n * sz + (n - 1) * gap;
                // In the node's own coordinates.
                const startX = -totalW / 2;
                // Under the node, and under its name and ghost line when they
                // reach lower, never over them.
                const cy = topBelow(node.y + node.r + 15, nodeLabelBoxes(node, ghostSubLabel), layout ? pxPerUnit : undefined) - node.y;
                return (
                  <g
                    opacity={focusDim ? 0.25 : 0.7}
                    onMouseEnter={(e) =>
                      showTooltip(
                        e,
                        t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: summary.assignmentCount }),
                      )
                    }
                    onMouseMove={moveTooltip}
                    onMouseLeave={hideTooltip}
                  >
                    {summary.triggerTypes.map((t, i) => {
                      const TIcon = TRIGGER_ICONS[t] ?? Bot;
                      return (
                        <foreignObject
                          key={t}
                          x={startX + i * (sz + gap)}
                          y={cy}
                          width={sz}
                          height={sz}
                        >
                          <TIcon width={sz} height={sz} color={CYAN_DIM} strokeWidth={2} />
                        </foreignObject>
                      );
                    })}
                  </g>
                );
              })()}

              {/* Connect handle: where the "i" was. A press on it starts a
                  handover, released on the target agent; a click on the node
                  itself opens the drawer. */}
              {isEditable && node.isEditable && !node.isGeneralAssistant && !isViewOnly && !node.leaving && detailShown && (
                <g
                  data-node-connect
                  style={{ cursor: "crosshair" }}
                  onPointerDown={(e) => startConnectDrag(node.id, e)}
                  // The release on the handle itself keeps the handover started: the next click picks the target.
                  onClick={(e) => e.stopPropagation()}
                  onMouseEnter={(e) => { e.stopPropagation(); showTooltip(e, t_i18n("Drag to an agent to start a handover")); }}
                  onMouseMove={moveTooltip}
                  onMouseLeave={hideTooltip}
                >
                  <circle cx={node.r - 2} cy={node.r - 2} r="5" fill={NODE_BG} stroke={CYAN} strokeWidth="0.6" />
                  <foreignObject x={node.r - 2 - 3.5} y={node.r - 2 - 3.5} width={7} height={7} pointerEvents="none">
                    <Cable width={7} height={7} color={CYAN_B} strokeWidth={2} />
                  </foreignObject>
                </g>
              )}
              </>))}
              {/* Keyboard focus ring: the design
                  system's ring, 2 screen px in the accent tone whatever the
                  zoom, shown by CSS on :focus-visible only (Tab, Alt+Arrow). */}
              {layout && (
                <circle
                  className={FOCUS_RING_CLASS}
                  data-focus-ring
                  cx={0} cy={0} r={node.r + 3 + 2 / pxPerUnit}
                  fill="none" stroke="var(--color-accent)" strokeWidth={2}
                  vectorEffect="non-scaling-stroke"
                  pointerEvents="none"
                />
              )}
              {/* A pinned agent carries a small filled dot at the top right of
                  its ring. */}
              {pins?.has(node.id) && (
                <circle
                  data-pin-mark
                  cx={(node.r + 2) * Math.SQRT1_2} cy={-(node.r + 2) * Math.SQRT1_2}
                  r={Math.max(1.5, nameSize * 0.3)}
                  fill={CYAN_B} stroke={NODE_BG} strokeWidth={0.6}
                  aria-hidden="true"
                />
              )}
            </g>
          );
        })}

      </svg>

      {/* Connection mode status bar */}
      {connectSource && (
        // One line in every language: as wide as its
        // text up to the graph's width less 2rem, else cut with an ellipsis
        // and the whole sentence in its title. Left at 50% alone would only
        // leave it half the graph's width, so its width is set, not shrunk.
        <div
          data-connect-hint
          title={t_i18n("Click a target agent to connect. Press Esc to cancel.")}
          style={{ width: "max-content", maxWidth: "calc(100% - 2rem)", whiteSpace: "nowrap" }}
          className="absolute top-2 left-1/2 -translate-x-1/2 z-40 rounded-sm border border-accent/30 layer-2 bg-elevation-default/90 px-4 py-1.5 content-compact text-accent animate-in fade-in-0 slide-in-from-top-2 flex items-center gap-2 shadow-overlay backdrop-blur-sm"
        >
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent animate-pulse" />
          <span className="min-w-0 truncate">{t_i18n("Click a target agent to connect. Press Esc to cancel.")}</span>
        </div>
      )}

      {/* Type picker popover (create edge) */}
      {typePicker && (
        <HandoverTypePopover
          kind="type-picker"
          title={t_i18n("Select handover type")}
          options={typePicker.offerAllTypes ? [ALL_TYPES_EDGE_OPTION, ...EDGE_TYPE_OPTIONS] : EDGE_TYPE_OPTIONS}
          selectedType={typePicker.selectedType}
          x={typePicker.x}
          y={typePicker.y}
          avoid={typePicker.avoid}
          autoFocus={!!typePicker.avoid}
          widthClass={HANDOVER_POPOVER_WIDTH}
          fallbackColor={CYAN}
          place={clampPopover}
          onSelect={(key) => setTypePicker(prev => prev ? { ...prev, selectedType: key } : null)}
          onCancel={() => setTypePicker(null)}
          onSave={handlePickType}
        />
      )}

      {/* Edge editor popover */}
      {edgeEditor && onEditEdge && (
        <HandoverTypePopover
          kind="edge-editor"
          title={t_i18n("Edit handover type")}
          options={edgeEditor.currentType === ALL_HANDOVER_TYPES ? [ALL_TYPES_EDGE_OPTION, ...EDGE_TYPE_OPTIONS] : EDGE_TYPE_OPTIONS}
          selectedType={edgeEditor.selectedType}
          x={edgeEditor.x}
          y={edgeEditor.y}
          avoid={edgeEditor.avoid}
          alsoAvoid={edgeEditor.alsoAvoid}
          autoFocus={!!edgeEditor.avoid}
          widthClass={HANDOVER_POPOVER_WIDTH}
          fallbackColor={CYAN}
          place={clampPopover}
          onSelect={(key) => setEdgeEditor(prev => prev ? { ...prev, selectedType: key } : null)}
          onCancel={() => setEdgeEditor(null)}
          onSave={() => {
            if (edgeEditor.selectedType !== edgeEditor.currentType) {
              onEditEdge(edgeEditor.sourceId, edgeEditor.targetId, edgeEditor.selectedType);
            }
            setEdgeEditor(null);
          }}
          onDelete={() => {
            // The deleted edge cannot take the focus back: its source node does, else the graph.
            const source = svgRef.current?.querySelector<SVGGElement>(`g[data-node-id="${CSS.escape(edgeEditor.sourceId)}"]`);
            popoverReturnRef.current = source && source.getAttribute("tabindex") === "0" ? source : containerRef.current;
            onEditEdge(edgeEditor.sourceId, edgeEditor.targetId, "delete");
            setEdgeEditor(null);
          }}
        />
      )}

      {/* Styled tooltip overlay: never over the type picker or the edge editor */}
      {tooltip && !typePicker && !edgeEditor && (
        <div
          ref={tooltipRef}
          data-testid="agent-flow-tooltip"
          className="absolute z-50 pointer-events-none rounded-sm border border-elevation-subtle layer-2 bg-elevation-default px-3 py-1.5 content-compact leading-relaxed text-default-primary shadow-overlay animate-in fade-in-0 zoom-in-95 whitespace-nowrap"
          style={{ top: tooltip.y - 8 }}
        >
          {tooltip.text}
        </div>
      )}

      {/* Every name is left out at this zoom (a dense map on a narrow screen):
          say how to read them. */}
      {layout && !staticView && nodes.length > 0 && namesHidden.size + nameLessCount >= nodes.length && (
        <div
          data-names-hint
          className="absolute bottom-3 left-3 z-10 pointer-events-none rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2 py-1 content-caption text-default-secondary"
        >
          {t_i18n("Zoom in or hover an agent to read its name")}
        </div>
      )}

      {/* Zoom controls (none on a static view: it never zooms) */}
      {!staticView && (
      <div ref={zoomRailRef} className="absolute top-3 right-3 flex flex-col gap-1 z-10" onPointerDown={e => e.stopPropagation()}>
        <Tip content={t_i18n("Zoom in")}>
          <button
            aria-label={t_i18n("Zoom in")}
            onClick={zoomIn}
            className="flex h-7 w-7 items-center justify-center rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft text-default-secondary hover:text-default-primary hover:layer-1 bg-elevation-default transition-colors"
          >
            <ZoomIn className="h-3.5 w-3.5" />
          </button>
        </Tip>
        <Tip content={t_i18n("Zoom out")}>
          <button
            aria-label={t_i18n("Zoom out")}
            onClick={zoomOut}
            className="flex h-7 w-7 items-center justify-center rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft text-default-secondary hover:text-default-primary hover:layer-1 bg-elevation-default transition-colors"
          >
            <ZoomOut className="h-3.5 w-3.5" />
          </button>
        </Tip>
        {layout && (
          <Tip content={t_i18n("Fit to view")}>
            <button
              aria-label={t_i18n("Fit to view")}
              onClick={fitView}
              className="flex h-7 w-7 items-center justify-center rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft text-default-secondary hover:text-default-primary transition-colors"
            >
              <Scan className="h-3.5 w-3.5" />
            </button>
          </Tip>
        )}
        {pinCount > 0 && (
          <Tip content={t_i18n("Unpin agents")}>
            <button
              aria-label={t_i18n("Unpin agents")}
              onClick={() => setPinState({ base: null, pins: new Map() })}
              className="flex h-7 w-7 items-center justify-center rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft text-default-secondary hover:text-default-primary transition-colors"
            >
              <PinOff className="h-3.5 w-3.5" />
            </button>
          </Tip>
        )}
        <Tip content={t_i18n("Reset view")}>
          <button
            aria-label={t_i18n("Reset view")}
            onClick={resetView}
            className="flex h-7 w-7 items-center justify-center rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft text-default-secondary hover:text-default-primary hover:layer-1 bg-elevation-default transition-colors"
          >
            <Maximize2 className="h-3.5 w-3.5" />
          </button>
        </Tip>
      </div>
      )}

      {/* Legend bar (pages without the graph toolbar) */}
      {!hideLegend && (
        <div className="flex items-center gap-6 px-5 py-2.5 border-t border-elevation-subtle-soft bg-elevation-hover text-[11px] text-default-secondary">
          <AgentFlowLegend
            showGeneralAssistant={!standInId}
            showAllTypes={edges.some((edge) => edge.handoverType === ALL_HANDOVER_TYPES)}
          />
          <div className="ml-auto flex items-center gap-4 content-caption tabular-nums">
            <span>{stats.total} {pluralize(stats.total, "agent")}</span>
            <span className="text-accent">{stats.connections} {pluralize(stats.connections, "handover")}</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────────────

export interface AgentFlowLegendProps {
  /** The General Assistant is drawn as its own node (no agent stands in for it). */
  showGeneralAssistant?: boolean;
  /** Some edge runs every handover type. */
  showAllTypes?: boolean;
  /**
   * `inline` for the legend bar, `list` for the toolbar's Legend popover,
   * `compact` for the key under a static mini graph (#9757): the three
   * handover types it draws and the other-fleet mark, one row.
   */
  variant?: "inline" | "list" | "compact";
  /** The graph draws ghost nodes and dotted cross-group handovers (the fleet page). */
  showFleetMarks?: boolean;
  /** The graph draws live handover edges: explain them and the still edges of a large map. */
  showLive?: boolean;
  /** The Fleets view draws entry ports on its hulls: explain them. */
  showEntryPort?: boolean;
  /** The graph draws A2A partners and their liaisons: explain them. */
  showA2A?: boolean;
}

/** A legend swatch drawn like the edge it explains: same ink, same dash (null: solid). */
function EdgeSwatch({ color, dash = "6 4", opacity }: { color: string; dash?: string | null; opacity?: number }) {
  return (
    <svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true" opacity={opacity} data-swatch-dash={dash ?? "solid"}>
      <line x1="0" y1="4" x2="14" y2="4" stroke={color} strokeWidth={1.2} strokeDasharray={dash ?? undefined} />
      <path d="M 13 1 L 19 4 L 13 7 L 14.5 4 Z" fill={color} opacity="0.7" />
    </svg>
  );
}

/**
 * What the marks of the agent graph mean: node states and handover types. The
 * standalone graph shows it in its legend bar; the Agentic Flow map shows it
 * behind the toolbar's Legend button.
 */
export function AgentFlowLegend({ showGeneralAssistant = false, showAllTypes = false, variant = "inline", showFleetMarks = false, showLive = false, showEntryPort = false, showA2A = false }: AgentFlowLegendProps) {
  const { t_i18n } = useFormatter();
  const isLight = useColorMode() === "light";
  const { CYAN, CYAN_B, CYAN_DIM, NODE_BG } = resolveGraphPalette(isLight);
  const items: Array<{ key: string; mark: React.ReactNode; label: string }> = [];
  if (variant === "compact") {
    const compact = [
      { key: "delegate", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.delegate, isLight)} dash={HANDOVER_TYPE_CONFIG.delegate.dash} />, label: t_i18n("Delegate") },
      { key: "consult", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.consult, isLight)} dash={HANDOVER_TYPE_CONFIG.consult.dash} />, label: t_i18n("Consult") },
      { key: "transfer", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.transfer, isLight)} dash={HANDOVER_TYPE_CONFIG.transfer.dash} />, label: t_i18n("Transfer") },
      {
        key: "ghost",
        mark: (
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <circle cx="7" cy="7" r="5" fill={NODE_BG} stroke={CYAN_DIM} strokeWidth="1.2" strokeDasharray="3 2" opacity="0.6" />
          </svg>
        ),
        label: t_i18n("Agent from another fleet"),
      },
    ];
    return (
      <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 content-caption text-default-secondary" aria-label={t_i18n("Legend")}>
        {compact.map((item) => (
          <li key={item.key} className="flex items-center gap-1.5">
            <span className="flex w-5 shrink-0 items-center justify-center">{item.mark}</span>
            <span>{item.label}</span>
          </li>
        ))}
      </ul>
    );
  }
  if (showGeneralAssistant) {
    items.push({ key: "ga", mark: <Sparkles className="h-2.5 w-2.5 text-accent" aria-hidden="true" />, label: t_i18n("General Assistant") });
  }
  items.push(
    {
      key: "running",
      mark: (
        <svg width="14" height="14" viewBox="0 0 14 14" className="animate-spin" aria-hidden="true">
          <circle cx="7" cy="7" r="5.5" fill="none" stroke={CYAN_B} strokeWidth="1.2" strokeDasharray="8 26" strokeLinecap="round" opacity="0.8" />
        </svg>
      ),
      label: t_i18n("Running"),
    },
    { key: "active", mark: <span className="h-2 w-2 rounded-full bg-accent" />, label: t_i18n("Active agent") },
    { key: "disabled", mark: <span className="h-2 w-2 rounded-full" style={{ background: CYAN_DIM }} />, label: t_i18n("Disabled agent") },
    {
      key: "read-only",
      mark: (
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <circle cx="7" cy="7" r="5" fill="none" stroke={CYAN_DIM} strokeWidth="0.6" strokeDasharray="2.5 1.5" />
        </svg>
      ),
      label: t_i18n("Read-only"),
    },
    // Each swatch takes the ink and the dash the type's edges take on this canvas.
    { key: "delegate", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.delegate, isLight)} dash={HANDOVER_TYPE_CONFIG.delegate.dash} />, label: t_i18n("Delegate") },
    { key: "consult", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.consult, isLight)} dash={HANDOVER_TYPE_CONFIG.consult.dash} />, label: t_i18n("Consult") },
    { key: "transfer", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.transfer, isLight)} dash={HANDOVER_TYPE_CONFIG.transfer.dash} />, label: t_i18n("Transfer") },
    { key: "mandatory", mark: <EdgeSwatch color={edgeInk(HANDOVER_TYPE_CONFIG.mandatory_transfer, isLight)} dash={HANDOVER_TYPE_CONFIG.mandatory_transfer.dash} />, label: t_i18n("Mandatory transfer") },
  );
  if (showLive) {
    items.push(
      {
        key: "live",
        // Live edges keep their type colour and dash and draw at full opacity
        // (`LIVE_EDGE_OPACITY`), so the swatch is a type-neutral dashed edge
        // in the text colour.
        mark: <EdgeSwatch color="currentColor" opacity={LIVE_EDGE_OPACITY} />,
        label: t_i18n("{minutes, plural, one {Live handover: running, or ended in the last # minute} other {Live handover: running, or ended in the last # minutes}}", {
          minutes: Math.round(LIVE_HANDOVER_WINDOW_MS / 60000),
        }),
      },
      {
        key: "calm",
        // The same dashed edge, dimmed like the still edges of a calm map.
        mark: <EdgeSwatch color="currentColor" opacity={isLight ? EDGE_REST_OPACITY.light : EDGE_REST_OPACITY.dark} />,
        label: t_i18n("{count, plural, one {Over # handover: only live, hovered, focused edges move} other {Over # handovers: only live, hovered, focused edges move}}", { count: ANIMATED_EDGE_LIMIT }),
      },
    );
  }
  if (showAllTypes) {
    items.push({ key: "all-types", mark: <EdgeSwatch color={edgeInk(ALL_HANDOVER_TYPES_CONFIG, isLight)} dash={ALL_HANDOVER_TYPES_CONFIG.dash} />, label: t_i18n("All types") });
  }
  if (showFleetMarks) {
    items.push(
      {
        key: "ghost",
        mark: (
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <circle cx="7" cy="7" r="5" fill={NODE_BG} stroke={CYAN_DIM} strokeWidth="1.2" strokeDasharray="3 2" opacity="0.6" />
          </svg>
        ),
        label: t_i18n("Agent from another fleet"),
      },
      {
        key: "ungoverned",
        mark: (
          <svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true">
            <line x1="1" y1="4" x2="14" y2="4" stroke={CYAN_DIM} strokeWidth="1.4" strokeDasharray="1.5 3" strokeLinecap="round" />
            <path d="M 13 1 L 19 4 L 13 7 L 14.5 4 Z" fill={CYAN_DIM} opacity="0.7" />
          </svg>
        ),
        label: t_i18n("Cross-fleet handover not governed or not exposed"),
      },
    );
  }
  if (showA2A) {
    const a2a = A2A_EDGE_CONFIG.a2a_inbound;
    items.push(
      {
        key: "a2a-partner",
        mark: (
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <circle cx="7" cy="7" r="5" fill={NODE_BG} stroke={edgeInk(a2a, isLight)} strokeWidth="1.2" strokeDasharray="3 2" />
          </svg>
        ),
        label: t_i18n("A2A partner outside the platform"),
      },
      { key: "a2a-edge", mark: <EdgeSwatch color={edgeInk(a2a, isLight)} dash={a2a.dash} />, label: t_i18n("A2A liaison: a partner tasks the agent, or the agent delegates to a partner agent") },
    );
  }
  if (showEntryPort) {
    items.push({
      key: "entry-port",
      // Drawn as on the Fleets map: a small ring on the hull's top border, where an entry handover arrives.
      mark: (
        <svg width="20" height="14" viewBox="0 0 20 14" aria-hidden="true">
          <path d="M 1 7 L 19 7" stroke={CYAN_DIM} strokeWidth="1" />
          <circle cx="10" cy="7" r="4" fill={NODE_BG} stroke={CYAN_DIM} strokeWidth="1.2" />
        </svg>
      ),
      label: t_i18n("Entry port: a handover from outside the fleet arrives at its orchestrator"),
    });
  }
  items.push({
    key: "targets",
    mark: (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <circle cx="7" cy="7" r="5" fill={NODE_BG} stroke={CYAN} strokeWidth="0.6" />
        <text x="7" y="9.5" textAnchor="middle" fill={CYAN_B} fontSize="7" fontWeight="600" fontFamily="'IBM Plex Sans', sans-serif">n</text>
      </svg>
    ),
    label: t_i18n("Handover targets"),
  });
  return (
    <ul className={variant === "list" ? "flex flex-col gap-2" : "contents"} aria-label={t_i18n("Legend")}>
      {items.map((item) => (
        <li key={item.key} className="flex items-center gap-1.5">
          <span className="flex w-5 shrink-0 items-center justify-center">{item.mark}</span>
          <span>{item.label}</span>
        </li>
      ))}
    </ul>
  );
}
