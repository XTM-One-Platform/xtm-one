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
 * Icons and per-category colours for canvas nodes.
 *
 * The catalog names a Lucide icon per node kind. Those names are resolved here
 * rather than added to the shared `ICON_MAP` in `lib/constants.ts`: that map is
 * the picker list users choose *agent* icons from, and padding it with
 * `GitMerge` / `StickyNote` / `Split` would offer them as agent avatars.
 */

import {
  BookOpen,
  Bot,
  Boxes,
  Braces,
  Brain,
  CalendarClock,
  CircleStop,
  Database,
  GitBranch,
  GitMerge,
  GraduationCap,
  Globe,
  HelpCircle,
  Equal,
  FolderOpen,
  DoorOpen,
  ArrowLeftRight,
  CircleDot,
  IterationCw,
  Variable,
  Waypoints,
  Sigma,
  SquareCode,
  Terminal,
  SquarePen,
  Hourglass,
  MessageCircleQuestion,
  Inbox,
  Library,
  ListChecks,
  ListFilter,
  ListOrdered,
  MessagesSquare,
  MonitorSmartphone,
  Radio,
  RadioTower,
  Play,
  Plug,
  Repeat,
  Replace,
  Send,
  ShieldCheck,
  Sparkles,
  Split,
  StickyNote,
  Tags,
  Timer,
  Ungroup,
  Wallet,
  Webhook,
  Workflow,
  Wrench,
  Zap,
  Activity,
  ChartBar,
  ChartLine,
  Clapperboard,
  FileText,
  Image,
  PenLine,
  Presentation,
  ScanEye,
  Search,
  Telescope,
  FileDown,
  FolderPlus,
} from "lucide-react";

export const FLOW_NODE_ICONS: Record<string, React.ElementType> = {
  BookOpen,
  Bot,
  Braces,
  Brain,
  CalendarClock,
  CircleStop,
  Database,
  GitBranch,
  GitMerge,
  GraduationCap,
  Globe,
  HelpCircle,
  SquareCode,
  Terminal,
  Sigma,
  SquarePen,
  Waypoints,
  Variable,
  IterationCw,
  CircleDot,
  ArrowLeftRight,
  DoorOpen,
  Equal,
  FolderOpen,
  Hourglass,
  Inbox,
  Library,
  ListChecks,
  ListFilter,
  ListOrdered,
  MessagesSquare,
  MessageCircleQuestion,
  MonitorSmartphone,
  Radio,
  RadioTower,
  Play,
  Plug,
  Repeat,
  Replace,
  Send,
  ShieldCheck,
  Sparkles,
  Split,
  StickyNote,
  Tags,
  Timer,
  Ungroup,
  Wallet,
  Webhook,
  Workflow,
  Wrench,
  Zap,
  Activity,
  ChartBar,
  ChartLine,
  Clapperboard,
  FileText,
  Image,
  PenLine,
  Presentation,
  ScanEye,
  Search,
  Telescope,
  FileDown,
  FolderPlus,
};

/**
 * Fallback for an icon name the catalog knows and this map does not, so a new
 * server-side node kind renders a neutral glyph rather than nothing.
 *
 * Resolve with a direct lookup at the call site —
 * `FLOW_NODE_ICONS[name] ?? DEFAULT_NODE_ICON`, the same shape as every
 * `ICON_MAP[...]` in the app. A helper *function* returning a component trips
 * `react-hooks/static-components`, which cannot tell it apart from building a
 * component during render.
 */
export const DEFAULT_NODE_ICON = Boxes;

/**
 * Per-category accent. Tokens only — the canvas has to flip with the theme like
 * every other surface, so no `white/N` overlays and no hardcoded `hsl()`.
 */
interface CategoryStyle {
  /** Icon tint + selected border. */
  accent: string;
  /** Icon chip background. */
  chip: string;
  /** Left rail on the node card. */
  rail: string;
  /** UE-style header band: colour fading left → right. */
  header: string;
}

const DEFAULT_STYLE: CategoryStyle = {
  accent: "text-accent",
  chip: "bg-accent/10",
  rail: "bg-accent/60",
  header: "bg-gradient-to-r from-accent/35 to-transparent",
};

const CATEGORY_STYLES: Record<string, CategoryStyle> = {
  triggers: {
    accent: "text-feedback-success-primary light:text-feedback-success-primary",
    chip: "bg-feedback-success-primary/10",
    rail: "bg-feedback-success-primary/60",
    header: "bg-gradient-to-r from-feedback-success-primary/35 to-transparent",
  },
  agents: DEFAULT_STYLE,
  capabilities: {
    accent: "text-filigran-tonic-primary light:text-filigran-tonic-primary",
    chip: "bg-filigran-tonic-primary/10",
    rail: "bg-filigran-tonic-primary/60",
    header: "bg-gradient-to-r from-filigran-tonic-primary/35 to-transparent",
  },
  conditions: {
    accent: "text-feedback-warning-primary light:text-feedback-warning-primary",
    chip: "bg-feedback-alert-primary/10",
    rail: "bg-feedback-alert-primary/60",
    header: "bg-gradient-to-r from-feedback-alert-primary/35 to-transparent",
  },
  actions: {
    accent: "text-filigran-ia-primary light:text-filigran-ia-primary",
    chip: "bg-filigran-ia-primary/10",
    rail: "bg-filigran-ia-primary/60",
    header: "bg-gradient-to-r from-filigran-ia-primary/35 to-transparent",
  },
  integrations: {
    accent: "text-feedback-info-primary light:text-feedback-info-primary",
    chip: "bg-feedback-info-primary/10",
    rail: "bg-feedback-info-primary/60",
    header: "bg-gradient-to-r from-feedback-info-primary/35 to-transparent",
  },
  flows: {
    accent: "text-filigran-ia-primary light:text-filigran-ia-primary",
    chip: "bg-filigran-ia-primary/10",
    rail: "bg-filigran-ia-primary/60",
    header: "bg-gradient-to-r from-filigran-ia-primary/35 to-transparent",
  },
  utilities: {
    accent: "text-default-secondary",
    chip: "bg-elevation-hover",
    rail: "bg-elevation-hover",
    header: "bg-gradient-to-r from-[var(--text-default-primary)]/15 to-transparent",
  },
  other: {
    accent: "text-default-secondary",
    chip: "bg-elevation-hover",
    rail: "bg-elevation-hover",
    header: "bg-gradient-to-r from-[var(--text-default-primary)]/15 to-transparent",
  },
};

function categoryStyle(category: string | undefined): CategoryStyle {
  return (category && CATEGORY_STYLES[category]) || DEFAULT_STYLE;
}

/**
 * Per-KIND accents, overriding the category. Looping nodes (For each, While)
 * share their own colour — they are control flow like If/Switch (amber), so a
 * warm neighbour hue keeps the family readable while making "this repeats"
 * unmistakable; the loop-body double outline (`--flow-loop-outline`) uses the
 * same orange.
 */
const LOOP_STYLE: CategoryStyle = {
  accent: "text-feedback-warning-primary light:text-feedback-warning-primary",
  chip: "bg-feedback-warning-primary/10",
  rail: "bg-feedback-warning-primary/60",
  header: "bg-gradient-to-r from-feedback-warning-primary/35 to-transparent",
};

/** Kinds that repeat their body — every loop treatment keys off this set. */
const LOOP_KINDS = new Set(["util.loop", "util.while"]);

/**
 * The event family — every node that binds a declared event type, wherever
 * it lives in the category tree (a trigger, an action, a utility, an agent
 * attachment). One shared colour makes the declare → emit → listen → wait
 * cycle readable at a glance across a large canvas; rose nods to UE's red
 * event nodes without colliding with the error red.
 */
const EVENT_KINDS = new Set([
  "trigger.event",
  "action.emit_event",
  "util.wait_event",
  "capability.on_delegate",
]);

/**
 * Flow-call keeps its fuchsia identity now that it lives in the merged
 * "Agents & Flows" library section: same family (things that THINK or
 * delegate whole runs), two distinguishable members — agent stays cyan.
 */
const FLOW_CALL_STYLE: CategoryStyle = {
  accent: "text-filigran-ia-primary light:text-filigran-ia-primary",
  chip: "bg-filigran-ia-primary/10",
  rail: "bg-filigran-ia-primary/60",
  header: "bg-gradient-to-r from-filigran-ia-primary/35 to-transparent",
};

const EVENT_STYLE: CategoryStyle = {
  accent: "text-feedback-error-primary light:text-feedback-error-primary",
  chip: "bg-feedback-error-primary/10",
  rail: "bg-feedback-error-primary/60",
  header: "bg-gradient-to-r from-feedback-error-primary/35 to-transparent",
};

/** The output port whose edges enter the loop's body, per looping kind. */
export const LOOP_BODY_PORTS: Record<string, string> = {
  "util.loop": "each",
  "util.while": "body",
};

const KIND_STYLES: Record<string, CategoryStyle> = {
  "util.loop": LOOP_STYLE,
  "util.while": LOOP_STYLE,
  "flow.call": FLOW_CALL_STYLE,
  "trigger.event": EVENT_STYLE,
  "action.emit_event": EVENT_STYLE,
  "util.wait_event": EVENT_STYLE,
  "capability.on_delegate": EVENT_STYLE,
};

/** The style for one node: its kind's own accent when it has one, else its
 *  category's. Use this everywhere a node is drawn (card, drawer, library). */
export function nodeStyle(
  kind: string | undefined,
  category: string | undefined,
): CategoryStyle {
  return (kind && KIND_STYLES[kind]) || categoryStyle(category);
}

/**
 * Minimap swatch per category. The minimap draws raw SVG rects, which cannot
 * take a Tailwind class, so the accent has to exist a second time as a literal
 * colour. Values track `CATEGORY_STYLES` — update both together.
 */
const MINIMAP_COLORS: Record<string, string> = {
  triggers: "var(--color-feedback-success-primary)",
  agents: "hsl(197 100% 45%)",
  capabilities: "hsl(173 60% 40%)",
  conditions: "var(--color-feedback-alert-primary)",
  actions: "hsl(263 70% 60%)",
  integrations: "hsl(199 89% 48%)",
  flows: "hsl(292 84% 61%)",
  utilities: "hsl(220 12% 50%)",
  other: "hsl(220 12% 50%)",
};

export function categoryMinimapColor(
  category: string | undefined,
  kind?: string,
): string {
  if (kind && LOOP_KINDS.has(kind)) return "hsl(27 96% 61%)"; // orange-400
  if (kind && EVENT_KINDS.has(kind)) return "hsl(351 95% 71%)"; // rose-400
  if (kind === "flow.call") return "hsl(292 84% 61%)"; // fuchsia-500
  return (category && MINIMAP_COLORS[category]) || MINIMAP_COLORS.agents;
}

/**
 * Icon per capability socket on the agent node's bottom edge, keyed by the
 * port id the catalog declares. What makes each attachment point readable
 * at a glance — the port typing already makes it the only legal target.
 */
export const CAPABILITY_PORT_ICONS: Record<string, React.ElementType> = {
  tools: Wrench,
  integrations: Plug,
  skills: GraduationCap,
  knowledge_bases: BookOpen,
  memory: Brain,
  guardrails: ShieldCheck,
  output: Braces,
  llm: Sparkles,
  shared_state: Database,
  work_items: ListChecks,
  workset: Library,
  budget: Wallet,
  on_delegate: RadioTower,
  workspace: FolderOpen,
};

/** True for the typed capability channels (they render on the node's edge
 * facing their counterpart: agent sockets below, `provides` plugs on top). */
export function isCapabilityPortType(type: string): boolean {
  return type.startsWith("capability");
}

/** Per-edge-kind stroke. Read by `TypedEdge`, which draws raw SVG. */
export const EDGE_STROKE: Record<string, string> = {
  data: "var(--flow-edge-data)",
  handover: "var(--flow-edge-handover)",
  condition_branch: "var(--flow-edge-branch)",
  error: "var(--flow-edge-error)",
  capability: "var(--flow-edge-capability)",
  value: "var(--flow-param-edge)",
};

export const EDGE_DASH: Record<string, string | undefined> = {
  data: undefined,
  handover: "6 4",
  condition_branch: "2 3",
  error: "4 4",
  // Tight dots: reads as attachment, not as a path the run takes.
  capability: "1 4",
  // Data feed from a Value node / parameter pin — dotted, never executed.
  value: "2 4",
};
