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
 * Display-only React Flow canvas mirroring the wizard's agent draft.
 *
 * The user edits through the step panel; this canvas is the visual
 * construction support, arranged like the flow builder's "materialize agent"
 * action so the two surfaces read the same: capability nodes (tools,
 * integrations, knowledge bases, skills) on a bottom row, memory and
 * guardrails on a top row, handover targets to the side — with the model and
 * budget carried by the agent node itself, as in the builder. The nodes the
 * current step concerns carry the builder's selected-ring treatment.
 *
 * Deliberately not FlowCanvas: that component needs the flow node catalog,
 * issues, summaries and seven editor callbacks. A mirror needs none of it.
 */
import { useEffect, useMemo, type CSSProperties } from "react";
import {
  Background,
  BackgroundVariant,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ArrowLeftRight, BookOpen, Bot, Brain, Cog, Lightbulb, Shield, Sparkles, Wrench,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useColorMode } from "@/hooks/useColorMode";
import { Tip } from "@/components/ui/tooltip";
import { ProviderLogo } from "@/pages/new-agent-wizard/ProviderLogo";
import { useFormatter } from "@/i18n";
import { ICON_MAP } from "@/lib/constants";

export type WizardNodeKey =
  | "agent" | "tools" | "integrations" | "knowledge" | "skills"
  | "memory" | "guardrails" | "handovers";

export interface WizardDraftSummary {
  name: string;
  icon: string;
  persona: string;
  /** Display names of the selected tools (built-in + custom). */
  toolNames: string[];
  /** The bound integrations — shown as provider icons with the name as
   *  tooltip on the node. */
  integrations: { id: string; name: string; provider: string }[];
  /** Display names of the selected knowledge bases. */
  knowledgeBaseNames: string[];
  /** Display names of the attached skills. */
  skillNames: string[];
  /** Display names of the handover target agents. */
  handoverNames: string[];
  /** Labels of the ENABLED memory tiers (empty = all tiers off). */
  memoryTierLabels: string[];
  /** Whether guardrails text is set. */
  hasGuardrails: boolean;
  /** The pinned model's label, or null for the platform default. */
  modelLabel: string | null;
  /** Whether any custom run-budget limit is set. */
  hasBudget: boolean;
}

type WizardNodeData = {
  title: string;
  subtitle: string;
  lines: string[];
  /** When set, rendered as a row of provider-logo BUTTONS (name in a
   *  tooltip, shown on hover and on keyboard focus) instead of the text
   *  lines — the integrations node. Clicking one opens the node's step.
   *  Keyed by `id`, the integration's: two integrations can share a provider. */
  iconItems?: { id: string; label: string; provider: string }[];
  icon: keyof typeof NODE_ICONS | "agent";
  agentIcon?: string;
  highlighted: boolean;
  /** The wizard's assistant is changing what this node shows. */
  assisting?: boolean;
  empty: boolean;
  /** Opens this node's configuration step in the wizard's left panel. */
  onOpenStep?: () => void;
};

const NODE_ICONS = {
  tools: Wrench,
  integrations: Cog,
  knowledge: BookOpen,
  skills: Lightbulb,
  memory: Brain,
  guardrails: Shield,
  handovers: ArrowLeftRight,
};

function WizardNodeCard({ data }: NodeProps<Node<WizardNodeData>>) {
  const { t_i18n } = useFormatter();
  const Icon =
    data.icon === "agent"
      ? (ICON_MAP[data.agentIcon || "Bot"] ?? Bot)
      : NODE_ICONS[data.icon];
  return (
    <div
      className={cn(
        "relative w-[240px] rounded-sm border layer-1 bg-elevation-default px-4 py-3 transition-all",
        data.highlighted ? "border-accent ring-2 ring-accent/60" : "border-elevation-subtle",
        data.highlighted && data.assisting && "ring-4 ring-accent/40",
        data.empty && !data.highlighted && "opacity-50 border-dashed",
      )}
    >
      {data.highlighted && data.assisting && (
        <span className="absolute -top-2.5 right-3 flex items-center gap-1 rounded-sm border border-transparent bg-accent px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-negative-primary shadow">
          <Sparkles className="h-2.5 w-2.5 animate-pulse" /> {t_i18n("AI")}
        </span>
      )}
      {/* Ports are decorative — the mirror has no interactions — but React
          Flow needs them for the edges to anchor. */}
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <div className="flex items-center gap-2.5">
        <div className={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-sm", data.highlighted ? "bg-accent/15" : "bg-elevation-hover")}>
          <Icon className={cn("h-4 w-4", data.highlighted ? "text-accent" : "text-default-secondary")} />
        </div>
        <div className="min-w-0">
          <p className="truncate content-base font-semibold">{data.title}</p>
          <p className="truncate content-caption text-default-secondary">{data.subtitle}</p>
        </div>
      </div>
      {data.iconItems && data.iconItems.length > 0 ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {data.iconItems.slice(0, 8).map((item) => (
            <Tip key={item.id} content={item.label}>
              <button
                type="button"
                aria-label={item.label}
                className="flex h-6 w-6 items-center justify-center rounded-sm bg-elevation-hover transition-colors hover:bg-elevation-highlight focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                onClick={data.onOpenStep}
              >
                <ProviderLogo provider={item.provider} className="h-4 w-4" />
              </button>
            </Tip>
          ))}
          {data.iconItems.length > 8 && (
            <span className="text-[11px] text-default-secondary">+{data.iconItems.length - 8}</span>
          )}
        </div>
      ) : data.lines.length > 0 ? (
        <ul className="mt-2 space-y-0.5">
          {data.lines.slice(0, 4).map((line, i) => (
            <li key={i} className="text-[11px] text-default-secondary">
              <Tip content={line}>
                <span
                  tabIndex={0}
                  className="block truncate rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                >
                  • {line}
                </span>
              </Tip>
            </li>
          ))}
          {data.lines.length > 4 && (
            <li className="text-[11px] text-default-secondary">+{data.lines.length - 4}</li>
          )}
        </ul>
      ) : null}
    </div>
  );
}

const nodeTypes = { wizardNode: WizardNodeCard };

export function WizardCanvas(props: {
  draft: WizardDraftSummary;
  /** Canvas nodes the current step concerns — they carry the selected ring. */
  highlight: WizardNodeKey[];
  /** The wizard's assistant is playing its changes: the highlighted nodes
   *  say so. */
  assisting?: boolean;
  /** Opens a node's configuration step in the wizard's left panel. */
  onOpenStep?: (key: WizardNodeKey) => void;
}) {
  return (
    <ReactFlowProvider>
      <WizardCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function WizardCanvasInner({
  draft,
  highlight,
  assisting = false,
  onOpenStep,
}: {
  draft: WizardDraftSummary;
  highlight: WizardNodeKey[];
  assisting?: boolean;
  onOpenStep?: (key: WizardNodeKey) => void;
}) {
  const { t_i18n } = useFormatter();
  const reactFlow = useReactFlow();
  // Track the app's light/dark mode on the canvas so nodes inherit the matching
  // design-system tokens (see the long note in FlowCanvas.tsx).
  const colorMode = useColorMode();

  const { nodes, edges } = useMemo(() => {
    const countSubtitle = (count: number) =>
      t_i18n("{count, plural, =0 {Nothing selected} one {# selected} other {# selected}}", { count });

    // The agent node carries what the builder's agent node carries: identity,
    // and the pinned model / custom budget when they diverge from defaults.
    const agentLines: string[] = [];
    if (draft.modelLabel) agentLines.push(draft.modelLabel);
    if (draft.hasBudget) agentLines.push(t_i18n("Custom limits"));

    // Same arrangement as the builder's materialize action: capabilities on a
    // bottom row, memory + guardrails on a top row, handovers to the side.
    const bottomRow: {
      key: WizardNodeKey;
      title: string;
      lines: string[];
      iconItems?: { id: string; label: string; provider: string }[];
      count: number;
    }[] = [
      { key: "tools", title: t_i18n("Tools"), lines: draft.toolNames, count: draft.toolNames.length },
      {
        key: "integrations",
        title: t_i18n("Integrations"),
        lines: [],
        iconItems: draft.integrations.map((i) => ({ id: i.id, label: i.name, provider: i.provider })),
        count: draft.integrations.length,
      },
      { key: "knowledge", title: t_i18n("Knowledge bases"), lines: draft.knowledgeBaseNames, count: draft.knowledgeBaseNames.length },
      { key: "skills", title: t_i18n("Skills"), lines: draft.skillNames, count: draft.skillNames.length },
    ];

    // An OPTIONAL node exists only while its step is open (so the highlight
    // has something to point at) or once it actually carries content —
    // abandoning the step without setting anything removes it again, so no
    // orphan is left on the canvas. Note memory "content" means diverging
    // from the all-on default: a default configuration needs no node.
    const editing = (key: WizardNodeKey) => highlight.includes(key);
    const optionalVisible: Record<"skills" | "memory" | "guardrails" | "handovers", boolean> = {
      skills: editing("skills") || draft.skillNames.length > 0,
      memory: editing("memory") || draft.memoryTierLabels.length < 4,
      guardrails: editing("guardrails") || draft.hasGuardrails,
      handovers: editing("handovers") || draft.handoverNames.length > 0,
    };
    const visibleBottomRow = bottomRow.filter(
      (cap) => cap.key !== "skills" || optionalVisible.skills,
    );

    // With dragging/selection off, React Flow puts pointer-events: none on
    // node wrappers — which also kills hover/focus INSIDE the node (the
    // icon buttons and their tooltips). Re-enable them per node; dragging
    // stays off via the flags.
    const NODE_STYLE = { pointerEvents: "all" as const };

    const nodes: Node<WizardNodeData>[] = [
      {
        id: "agent",
        type: "wizardNode",
        style: NODE_STYLE,
        position: { x: 280, y: 0 },
        data: {
          title: draft.name.trim() || t_i18n("New agent"),
          subtitle: draft.persona.trim() ? t_i18n("Persona defined") : t_i18n("No persona yet"),
          lines: agentLines,
          icon: "agent",
          agentIcon: draft.icon,
          highlighted: highlight.includes("agent"),
          assisting,
          empty: !draft.name.trim(),
        },
      },
      ...visibleBottomRow.map((cap, i): Node<WizardNodeData> => ({
        id: cap.key,
        type: "wizardNode",
        style: NODE_STYLE,
        position: { x: -140 + i * 280, y: 260 },
        data: {
          title: cap.title,
          subtitle: countSubtitle(cap.count),
          lines: cap.lines,
          iconItems: cap.iconItems,
          icon: cap.key as keyof typeof NODE_ICONS,
          highlighted: highlight.includes(cap.key),
          assisting,
          empty: cap.count === 0,
          onOpenStep: onOpenStep ? () => onOpenStep(cap.key) : undefined,
        },
      })),
    ];

    if (optionalVisible.memory) {
      nodes.push({
        id: "memory",
        type: "wizardNode",
        style: NODE_STYLE,
        position: { x: 120, y: -220 },
        data: {
          title: t_i18n("Memory"),
          subtitle: `${draft.memoryTierLabels.length}/4`,
          lines: draft.memoryTierLabels,
          icon: "memory",
          highlighted: highlight.includes("memory"),
          assisting,
          // Memory always means something (defaults on) — never dashed.
          empty: false,
        },
      });
    }
    if (optionalVisible.guardrails) {
      nodes.push({
        id: "guardrails",
        type: "wizardNode",
        style: NODE_STYLE,
        position: { x: 440, y: -220 },
        data: {
          title: t_i18n("Guardrails"),
          subtitle: draft.hasGuardrails ? t_i18n("Defined") : t_i18n("Not set"),
          lines: [],
          icon: "guardrails",
          highlighted: highlight.includes("guardrails"),
          assisting,
          empty: !draft.hasGuardrails,
        },
      });
    }
    if (optionalVisible.handovers) {
      nodes.push({
        id: "handovers",
        type: "wizardNode",
        style: NODE_STYLE,
        position: { x: 700, y: 20 },
        data: {
          title: t_i18n("Handovers"),
          subtitle: countSubtitle(draft.handoverNames.length),
          lines: draft.handoverNames,
          icon: "handovers",
          highlighted: highlight.includes("handovers"),
          assisting,
          empty: draft.handoverNames.length === 0,
        },
      });
    }

    const present = new Set(nodes.map((n) => n.id));
    const edges: Edge[] = [
      ...visibleBottomRow.map((cap): Edge => ({
        id: `agent-${cap.key}`,
        source: cap.key,
        target: "agent",
        animated: highlight.includes(cap.key),
        style: { strokeWidth: 1.5 },
      })),
      ...(["memory", "guardrails"] as const)
        .filter((key) => present.has(key))
        .map((key): Edge => ({
          id: `agent-${key}`,
          source: key,
          target: "agent",
          animated: highlight.includes(key),
          style: { strokeWidth: 1.5 },
        })),
      ...(present.has("handovers")
        ? [{
            id: "agent-handovers",
            source: "agent",
            target: "handovers",
            animated: highlight.includes("handovers"),
            style: { strokeWidth: 1.5 },
          } satisfies Edge]
        : []),
    ];

    return { nodes, edges };
  }, [draft, highlight, assisting, onOpenStep, t_i18n]);

  // Optional nodes appear and disappear with their step — refit the view so a
  // node materializing off-screen (handovers, the top row) is actually seen.
  const nodeIdSignature = nodes.map((n) => n.id).join("|");
  useEffect(() => {
    const timer = setTimeout(
      () => reactFlow.fitView({ padding: 0.25, duration: 300 }),
      50,
    );
    return () => clearTimeout(timer);
  }, [nodeIdSignature, reactFlow]);

  return (
    <ReactFlow
      colorMode={colorMode}
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.25 }}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      panOnDrag
      zoomOnScroll
      proOptions={{ hideAttribution: true }}
      // React Flow's unlayered stylesheet paints `.react-flow` and wins over the
      // layered `bg-elevation-default` utility, so set its own background
      // variable to the platform surface — in `colorMode="dark"` the default
      // would be a flat `#141414` (see FlowCanvas' flow-builder.css note).
      style={{ "--xy-background-color": "var(--bg-elevation-default)" } as CSSProperties}
    >
      <Background variant={BackgroundVariant.Dots} gap={24} size={1} />
    </ReactFlow>
  );
}
