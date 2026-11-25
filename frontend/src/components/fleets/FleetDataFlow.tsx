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
 * The "Data flow" tab of a fleet page (#9829; a tab of its own since #9980):
 * where the work of the fleet starts, which agent reacts, and where it goes.
 *
 * Read from `GET /fleets/{id}/flow` (configuration only: opening it never
 * starts a run or uses quota). Three fixed lanes, laid out by
 * `lib/fleet-flow-layout` (deterministic, no force): the sources (one card per
 * integration with its events as chips, one per other trigger kind), the
 * agents, the outputs (integrations written to, then "Reads only").
 *
 * Hover an agent: its own sources, outputs and handover partners stay lit.
 * Click a source: its assignments, events and filters open inside its card (no
 * drawer, no panel). A source with more than four events shows three and
 * "+N more", then "Show less". A disconnected integration is dashed with its
 * reason and a link to Integrations; an agent it alone starts reads "Cannot
 * start". The approved mockups are UX round 5 (item 1cad5b91).
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, RotateCcw } from "lucide-react";

import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { FleetFlow, FleetFlowEdge, FleetFlowNode } from "@/hooks/api/useFleetApi";
import { useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { humanizeCron } from "@/lib/cron";
import {
  MORE_OUTPUTS,
  flowPath,
  foldedOutputsOf,
  hoverPath,
  layoutFlow,
  moreOutputsLit,
  sourcePath,
  withLetterSpacing,
  type FlowLaneModel,
} from "@/lib/fleet-flow-layout";
import { platformEventLabel } from "@/lib/platform-events";
import { cn } from "@/lib/utils";

/** Edge strokes of the approved mockups, per theme: SVG strokes cannot read the CSS tokens. */
const STROKES = {
  light: { start: "#7A8591", handover: "#6F55E8", write: "#3B4A5A", read: "#1F6FB2", disabled: "#8A8F99" },
  dark: { start: "#66717F", handover: "#9C8BFF", write: "#B8C2CE", read: "#5AB0F0", disabled: "#7D828C" },
} as const;
/**
 * Band colours per source family (#9981), per theme: a band from a source takes
 * its family, a band to an output the family that carries most of its agent's
 * assignments. Light values hold 3:1 on the light card, dark ones on the dark card.
 */
const FAMILY = {
  light: { integration: "#6F55E8", platform_event: "#1F6FB2", schedule: "#B25E09", work_item: "#0F7B6C", webhook: "#B0307A", manual: "#5F6B7A" },
  dark: { integration: "#9C8BFF", platform_event: "#5AB0F0", schedule: "#F2A65A", work_item: "#4FD1B5", webhook: "#F27FC0", manual: "#A3ADBA" },
} as const;
type Family = keyof (typeof FAMILY)["light"];
const FAMILY_OF: Record<string, Family> = {
  integration: "integration",
  platform_event: "platform_event",
  schedule: "schedule",
  work_item: "work_item",
  work_items: "work_item",
  webhook: "webhook",
  manual: "manual",
};
const familyOf = (kind: string | undefined): Family => FAMILY_OF[kind ?? ""] ?? "manual";
/** Band opacity at rest, lit by a hover, and dimmed (OpenCRQ: 0.3 / 0.68 / 0.05). */
const BAND = { rest: 0.42, lit: 0.78, dim: 0.06, read: 0.22 } as const;
/** The kind swatch of a source or output card, as design-system token classes (both themes). */
const KIND_SWATCH: Record<string, string> = {
  integration: "bg-accent",
  platform_event: "bg-feedback-info-primary",
  work_item: "bg-feedback-info-primary",
  schedule: "bg-feedback-warning-primary",
  webhook: "bg-accent",
  manual: "bg-feedback-neutral-primary",
  work_items: "bg-feedback-info-primary",
};
const OFF_SWATCH = "bg-feedback-neutral-secondary";
const DIM = 0.18;
const NOT_CONNECTED = "not connected";

let canvas: HTMLCanvasElement | null = null;
/** The font the view renders in, read from the page (UX #9882 r3 B3: measuring in another face cut "+3" labels). */
let renderedFont = "system-ui, sans-serif";
function measureText(text: string, px: number, weight?: number): number {
  canvas ??= document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  if (!ctx) return text.length * px * 0.6;
  ctx.font = `${weight ?? 400} ${px}px ${renderedFont}`;
  return ctx.measureText(text).width;
}
const WORK_ITEM_OTHER_QUEUE = " (queue of another agent)";

interface FlowModel {
  lanes: FlowLaneModel;
  nodes: Map<string, FleetFlowNode>;
  /** Trigger edges per source, in the order the API answers. */
  triggers: Map<string, FleetFlowEdge[]>;
  /** Assignments per (source, agent) trunk. */
  trunkWeight: Map<string, number>;
  /** Disabled or disconnected trunks, drawn dashed. */
  trunkDisabled: Set<string>;
  /** The (agent, output) pairs with a write edge: any other out pair is a read. */
  writePairs: Set<string>;
}

const trunkKey = (s: string, a: string) => `${s}\u0000${a}`;

/** Words for a key no catalog names: `new_email` -> "New email", `acme_cloud` -> "Acme cloud" (never the raw key). */
function humanizeKey(key: string): string {
  const words = key.replace(/[._-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

/** The flow as lanes: one start pair per (source, agent), one out pair per (agent, output). */
function toFlowModel(flow: FleetFlow): FlowModel {
  const nodes = new Map(flow.nodes.map((n) => [n.id, n]));
  const triggers = new Map<string, FleetFlowEdge[]>();
  const trunkWeight = new Map<string, number>();
  const trunkEnabled = new Map<string, boolean>();
  const starts: Array<[string, string]> = [];
  const outs: Array<[string, string]> = [];
  const handovers: Array<[string, string]> = [];
  const written = new Set<string>();
  const writePairs = new Set<string>();
  for (const edge of flow.edges) {
    if (edge.kind === "trigger") {
      triggers.set(edge.source, [...(triggers.get(edge.source) ?? []), edge]);
      const key = trunkKey(edge.source, edge.target);
      if (!trunkWeight.has(key)) starts.push([edge.source, edge.target]);
      trunkWeight.set(key, (trunkWeight.get(key) ?? 0) + 1);
      trunkEnabled.set(key, (trunkEnabled.get(key) ?? false) || edge.enabled);
    } else if (edge.kind === "handover") {
      handovers.push([edge.source, edge.target]);
    } else {
      outs.push([edge.source, edge.target]);
      if (edge.kind === "write") {
        written.add(edge.target);
        writePairs.add(trunkKey(edge.source, edge.target));
      }
    }
  }
  const lane = (l: FleetFlowNode["lane"]) => flow.nodes.filter((n) => n.lane === l).map((n) => n.id);
  const outputs = lane("output");
  const trunkDisabled = new Set<string>();
  for (const [s, a] of starts) {
    if (!trunkEnabled.get(trunkKey(s, a)) || nodes.get(s)?.reason === NOT_CONNECTED) trunkDisabled.add(trunkKey(s, a));
  }
  return {
    lanes: {
      sources: lane("source"),
      agents: lane("agent"),
      writes: outputs.filter((o) => written.has(o)),
      reads: outputs.filter((o) => !written.has(o)),
      starts,
      handovers,
      outs,
    },
    nodes,
    triggers,
    trunkWeight,
    trunkDisabled,
    writePairs,
  };
}

interface FleetDataFlowProps {
  flow: FleetFlow | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  onOpenAgent: (agentId: string) => void;
}

export function FleetDataFlow({ flow, loading, error, onRetry, onOpenAgent }: FleetDataFlowProps) {
  const { t_i18n, t_text, locale } = useFormatter();
  // Provider names and event labels, the same cached read the schedule rows use (UX #9882 r2 B1, B2).
  const { data: catalog } = useProviderCatalog();
  const mode = useColorMode();
  const stroke = STROKES[mode];
  const box = useRef<HTMLDivElement>(null);
  /** The summary line: a 12 px text of the view, read for the letter-spacing the chips render with. */
  const probe = useRef<HTMLParagraphElement>(null);
  const [width, setWidth] = useState(0);
  const [letterSpacing, setLetterSpacing] = useState(0);
  const [hovered, setHovered] = useState<string | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  /** The band under the pointer: its count shows at its middle (#9981). */
  const [band, setBand] = useState<number | null>(null);
  /** Read-only outputs hidden: they stay counted in the header (#9981, as before). */
  const [hideReads, setHideReads] = useState(false);
  /** Every output drawn: past twelve they fold into one "+N more" card by default (UX #9992 r1 B1). */
  const [allOutputs, setAllOutputs] = useState(false);
  /** Keyboard focus survives the fold toggles (UX #9992 r2 B3): the button pressed unmounts. */
  const showLessRef = useRef<HTMLButtonElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const focusAfterToggle = useRef<"less" | "more" | null>(null);
  const toggleAllOutputs = (all: boolean) => {
    focusAfterToggle.current = all ? "less" : "more";
    setAllOutputs(all);
  };

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    // The lanes are laid out on the content width: clientWidth includes the box's padding (UX #9882 r1 B1).
    const read = () => {
      const style = getComputedStyle(el);
      if (style.fontFamily) renderedFont = style.fontFamily;
      // The canvas ignores letter-spacing; the chips render with it (UX #9882 r4 B1).
      const spacing = probe.current ? parseFloat(getComputedStyle(probe.current).letterSpacing) : 0;
      setLetterSpacing(Number.isFinite(spacing) ? spacing : 0);
      setWidth(Math.floor(el.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0")));
    };
    read();
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [flow, loading, error]);

  // After "Show all" focus goes to "Show less", after "Show less" to the "+N more" card (UX #9992 r2 B3).
  useLayoutEffect(() => {
    const target = focusAfterToggle.current;
    if (!target) return;
    focusAfterToggle.current = null;
    (target === "less" ? showLessRef : moreRef).current?.focus();
  }, [allOutputs]);

  const model = useMemo(() => (flow ? toFlowModel(flow) : null), [flow]);

  const nodeName = useCallback(
    (node: FleetFlowNode | undefined): string => {
      if (!node) return "";
      if (node.name) return node.name;
      if (node.kind === "integration") {
        // The provider's display name, never its key (UX #9882 r1 B2, r2 B1).
        if (!node.provider) return t_i18n("Integration");
        return (
          INTEGRATION_PROVIDER_NAMES[node.provider] ??
          catalog?.find((c) => c.provider === node.provider)?.name ??
          humanizeKey(node.provider)
        );
      }
      const kinds: Record<string, string> = {
        platform_event: t_i18n("Platform events"),
        schedule: t_i18n("Schedule"),
        webhook: t_i18n("Webhook"),
        work_item: t_i18n("Work items"),
        work_items: t_i18n("Work items"),
        manual: t_i18n("Manual"),
        agent: t_i18n("Hidden agent"),
      };
      return kinds[node.kind] ?? humanizeKey(node.kind);
    },
    [t_i18n, catalog],
  );

  /** An event in words: the provider catalog's label, the platform event's label, else the key in words; the key stays the tooltip (UX #9882 r2 B2). */
  const eventName = useCallback(
    (event: string, provider?: string | null): string => {
      if (provider) {
        const label = catalog?.find((c) => c.provider === provider)?.available_events?.find((e) => e.event === event)?.label;
        if (label) return t_text(label);
      } else {
        const label = platformEventLabel(event);
        if (label) return t_i18n(label);
      }
      return humanizeKey(event);
    },
    [catalog, t_i18n, t_text],
  );

  /** The chips of a source: its events, else the schedules or prefixes its triggers hold. */
  const chipsOf = useCallback(
    (id: string) => {
      const node = model?.nodes.get(id);
      const chips = new Map<string, string>();
      const provider = node?.kind === "integration" ? node.provider : null;
      for (const event of node?.events ?? []) chips.set(eventName(event, provider), event);
      for (const edge of model?.triggers.get(id) ?? []) {
        // A schedule in the reader's language (UX #9882 r1 B3); the API's English line is the fallback.
        let label = edge.cron ? `${humanizeCron(edge.cron, locale)} (${edge.timezone || "UTC"})` : edge.detail;
        // A work item prefix is worded, never a bare internal key (UX #9882 r3 B5); the raw key stays the tooltip.
        if (label && node?.kind === "work_item" && !edge.cron) {
          const other = label.endsWith(WORK_ITEM_OTHER_QUEUE);
          const prefix = other ? label.slice(0, -WORK_ITEM_OTHER_QUEUE.length) : label;
          label = other
            ? t_i18n("Work items keyed {prefix}, in another agent's queue", { prefix })
            : t_i18n("Work items keyed {prefix}", { prefix });
        }
        if (label && !chips.has(label)) chips.set(label, edge.detail || label);
      }
      if (!chips.size && node?.kind === "manual") chips.set(t_i18n("Run now"), t_i18n("Run now"));
      return [...chips].map(([label, title]) => ({ label, title }));
    },
    [model, t_i18n, locale, eventName],
  );

  /** The agents that hand work to *agent*: how an agent with no assignment starts (UX #9882 r2 B4). */
  const startersOf = useCallback(
    (agent: string) => (model ? [...new Set(model.lanes.handovers.filter(([, b]) => b === agent).map(([a]) => a))] : []),
    [model],
  );

  /** The texts the layout wraps at the card width; the cards render the same strings (UX #9882 r4 B2, B3). */
  const assignmentLine = useCallback(
    (edge: FleetFlowEdge) =>
      `${edge.assignment_name || t_i18n("Hidden assignment")} → ${nodeName(model?.nodes.get(edge.target))}`,
    [model, nodeName, t_i18n],
  );
  const eventsLine = useCallback(
    (edge: FleetFlowEdge, node: FleetFlowNode | undefined) =>
      [
        edge.events.map((e) => eventName(e, node?.kind === "integration" ? node.provider : null)).join(", "),
        edge.filter || edge.gate,
        edge.enabled ? "" : t_i18n("Disabled"),
      ]
        .filter(Boolean)
        .join(" · ") || t_i18n("No filter"),
    [eventName, t_i18n],
  );
  const offReason = useCallback(
    (off: string[]) => t_i18n("{names} not connected", { names: off.map((s) => nodeName(model?.nodes.get(s))).join(", ") }),
    [model, nodeName, t_i18n],
  );
  const partlyOff = useCallback(
    (count: number, total: number) =>
      t_i18n("{count, plural, one {# of {total} sources not connected} other {# of {total} sources not connected}}", { count, total }),
    [t_i18n],
  );

  /** The lanes drawn: the read-only outputs and their bands left out when hidden. */
  const lanes = useMemo<FlowLaneModel | null>(() => {
    if (!model) return null;
    if (!hideReads || !model.lanes.reads.length) return model.lanes;
    const reads = new Set(model.lanes.reads);
    return { ...model.lanes, reads: [], outs: model.lanes.outs.filter(([, o]) => !reads.has(o)) };
  }, [model, hideReads]);

  /** Assignments an agent carries, and the family carrying most of them (the colour of its output bands). */
  const agentLoad = useMemo(() => {
    const load = new Map<string, { count: number; family: Family }>();
    if (!model) return load;
    const perFamily = new Map<string, Map<Family, number>>();
    for (const [s, a] of model.lanes.starts) {
      const n = model.trunkWeight.get(trunkKey(s, a)) ?? 0;
      const fam = familyOf(model.nodes.get(s)?.kind);
      const counts = perFamily.get(a) ?? new Map<Family, number>();
      counts.set(fam, (counts.get(fam) ?? 0) + n);
      perFamily.set(a, counts);
    }
    for (const a of model.lanes.agents) {
      const counts = [...(perFamily.get(a) ?? new Map<Family, number>())];
      const count = counts.reduce((n, [, c]) => n + c, 0);
      const family = counts.sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))[0]?.[0] ?? "manual";
      load.set(a, { count, family });
    }
    return load;
  }, [model]);

  /**
   * What an output is: "Written by 3 agents" (#9981: never a bare "Written to"). No
   * assignment count: the API's write and read edges carry no assignment, so the
   * share of each agent's load that reaches an output is not known (UX #9992 r1 B2).
   */
  const outputCaption = useCallback(
    (id: string) => {
      if (!model) return "";
      const agents = new Set(model.lanes.outs.filter(([, o]) => o === id).map(([a]) => a)).size;
      return model.lanes.reads.includes(id)
        ? t_i18n("{agents, plural, one {Read by # agent} other {Read by # agents}}", { agents })
        : t_i18n("{agents, plural, one {Written by # agent} other {Written by # agents}}", { agents });
    },
    [model, t_i18n],
  );

  const layout = useMemo(() => {
    if (!model || !lanes || width < 320) return null;
    const notConnected = (id: string) => model.nodes.get(id)?.reason === NOT_CONNECTED;
    return layoutFlow(
      lanes,
      {
        sources: lanes.sources.map((id) => ({
          id,
          name: nodeName(model.nodes.get(id)),
          chips: chipsOf(id),
          statusLines: notConnected(id) ? 2 : 0,
          // The status line as rendered, measured at the card width (UX #9882 r3 B1: 3 lines in fr at 1024).
          statusText: notConnected(id) ? `${t_i18n("Not connected: sign in again in")} ${t_i18n("Integrations")}` : undefined,
          // The open detail: each assignment and its events line wrapped at the card width (UX #9882 r4 B3).
          detailLines: opened === id ? 1 : 0,
          detailTexts:
            opened === id
              ? (model.triggers.get(id) ?? []).flatMap((edge) => [
                  { text: assignmentLine(edge), medium: true },
                  { text: eventsLine(edge, model.nodes.get(id)) },
                ])
              : undefined,
        })),
        agents: lanes.agents.map((id) => {
          const name = nodeName(model.nodes.get(id));
          const srcs = lanes.starts.filter(([, a]) => a === id).map(([s]) => s);
          const off = srcs.filter(notConnected);
          if (off.length > 0 && off.length === srcs.length) {
            // "Cannot start" and the link, plus the reason on its measured lines (UX #9882 r4 B2).
            return { id, name, statusLines: 2, statusText: offReason(off) };
          }
          if (off.length > 0) {
            // "N of M sources not connected" on its measured lines, then the link.
            return { id, name, statusLines: 1, statusText: partlyOff(off.length, srcs.length) };
          }
          // "Started by X, Y" may take two lines; a count or "Disabled" one.
          const lines = srcs.length === 0 && startersOf(id).length > 0 ? 2 : 1;
          return { id, name, statusLines: lines };
        }),
        outputs: [...lanes.writes, ...lanes.reads].map((id) => ({
          id,
          name: nodeName(model.nodes.get(id)),
          caption: outputCaption(id),
          statusLines: notConnected(id) ? 1 : 0,
        })),
      },
      {
        width,
        expandedSource: expanded,
        measure: withLetterSpacing(measureText, letterSpacing),
        startWeight: (s, a) => model.trunkWeight.get(trunkKey(s, a)) ?? 1,
        outputLimit: allOutputs ? Infinity : undefined,
      },
      undefined,
      { more: (n) => t_i18n("+{count} more", { count: n }), less: t_i18n("Show less") },
    );
  }, [model, lanes, width, letterSpacing, opened, expanded, allOutputs, chipsOf, startersOf, assignmentLine, eventsLine, offReason, partlyOff, nodeName, outputCaption, t_i18n]);

  if (loading) {
    return (
      <div className="space-y-3 p-4" aria-busy="true" aria-label={t_i18n("Loading the data flow")}>
        <Skeleton className="h-4 w-72" />
        <Skeleton className="h-[300px] w-full" />
      </div>
    );
  }
  if (error || !flow || !model) {
    return (
      <EmptyState
        variant="inset"
        icon={AlertTriangle}
        title="The data flow could not be loaded"
        description="Nothing was changed. Try again in a moment."
        actionLabel="Try again"
        actionIcon={RotateCcw}
        onAction={onRetry}
      />
    );
  }

  const drawn = lanes ?? model.lanes;
  const isAgent = (id: string) => model.nodes.get(id)?.lane === "agent";
  // Hover lights one full path (#9981): an agent its sources, outputs and handover partners;
  // a source or an output every source, agent and output on the paths through it.
  const focus = hovered ?? opened;
  const lit = focus ? (isAgent(focus) ? hoverPath(drawn, focus) : hovered ? flowPath(drawn, focus) : sourcePath(drawn, focus)) : null;
  const nodeOpacity = (id: string) => (!lit || lit.has(id) ? 1 : DIM);
  // The "+N more" card counts as lit when the path reaches one of its folded outputs (UX #9992 r2 B2).
  const endLit = (e: string) => (e === MORE_OUTPUTS ? moreOutputsLit(lit, layout?.hiddenOutputs ?? []) : !!lit?.has(e));
  const edgeLit = (ends: string[]) => !focus || (isAgent(focus) ? ends.includes(focus) : ends.every(endLit));
  const notConnected = (id: string) => model.nodes.get(id)?.reason === NOT_CONNECTED;
  const assignmentsOf = (agent: string) => agentLoad.get(agent)?.count ?? 0;
  const assignmentCount = [...model.trunkWeight.values()].reduce((a, b) => a + b, 0);
  const outputCount = model.lanes.writes.length + model.lanes.reads.length;
  const family = FAMILY[mode];
  const hoveredBand = band !== null ? layout?.edges[band] : undefined;
  /** A band's colour: its source's family, or for an output band the main family of its agent. */
  const bandColor = (kind: string, ends: string[], disabled: boolean) => {
    if (disabled) return stroke.disabled;
    if (kind === "start") return family[familyOf(model.nodes.get(ends[0])?.kind)];
    return family[agentLoad.get(ends[0])?.family ?? "manual"];
  };
  const hiddenCount = layout?.hiddenOutputs.length ?? 0;
  const moreLabel = t_i18n("+{count} more", { count: hiddenCount });
  const hidden = layout?.hiddenOutputs ?? [];
  const namesOf = (ids: string[]) => ids.map((id) => nodeName(model.nodes.get(id))).join(", ");
  const hiddenNames = namesOf(hidden);
  const bandLabel = (kind: string, ends: string[], weight: number) => {
    const from = nodeName(model.nodes.get(ends[0]));
    if (ends[1] === MORE_OUTPUTS) {
      // Only this agent's own folded outputs, writes and reads apart (UX #9992 r2 B1).
      const own = foldedOutputsOf(drawn, hidden, ends[0], (a, o) => !model.writePairs.has(trunkKey(a, o)));
      if (own.writes.length && own.reads.length) {
        return t_i18n("{from} writes to {writes}; reads from {reads}", { from, writes: namesOf(own.writes), reads: namesOf(own.reads) });
      }
      if (own.reads.length) return t_i18n("{from} reads from {to}", { from, to: namesOf(own.reads) });
      return t_i18n("{from} writes to {to}", { from, to: namesOf(own.writes) });
    }
    const values = { from, to: nodeName(model.nodes.get(ends[1])), count: weight };
    if (kind === "start") return t_i18n("{from} starts {to}: {count, plural, one {# assignment} other {# assignments}}", values);
    // An output band claims no count: its assignments are not known (UX #9992 r1 B2).
    if (kind === "read") return t_i18n("{from} reads from {to}", values);
    return t_i18n("{from} writes to {to}", values);
  };

  return (
    <div className="flex min-h-[300px] flex-col" data-testid="fleet-data-flow">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
        <p ref={probe} className="content-compact text-default-secondary" aria-live="polite">
          {t_i18n("{sources, plural, one {# source} other {# sources}} · {assignments, plural, one {# assignment} other {# assignments}} · {agents, plural, one {# agent} other {# agents}} · {outputs, plural, one {# output} other {# outputs}} ({writes} written, {reads} read)", {
            sources: model.lanes.sources.length,
            assignments: assignmentCount,
            agents: model.lanes.agents.length,
            outputs: outputCount,
            writes: model.lanes.writes.length,
            reads: model.lanes.reads.length,
          })}
        </p>
        {model.lanes.reads.length > 0 && (
          <button
            type="button"
            className="rounded-sm content-compact text-accent underline outline-none focus-visible:ring-2 focus-visible:ring-focus"
            aria-pressed={hideReads}
            onClick={() => setHideReads((v) => !v)}
          >
            {hideReads ? t_i18n("Show read-only outputs") : t_i18n("Hide read-only outputs")}
          </button>
        )}
        {allOutputs && (
          <button
            type="button"
            className="rounded-sm content-compact text-accent underline outline-none focus-visible:ring-2 focus-visible:ring-focus"
            ref={showLessRef}
            onClick={() => toggleAllOutputs(false)}
          >
            {t_i18n("Show less")}
          </button>
        )}
      </div>
      <div ref={box} className="relative px-4 pb-4 pt-2">
        {layout && (
          <div
            className="relative"
            style={{ height: layout.height }}
            onMouseLeave={() => {
              setHovered(null);
              setBand(null);
            }}
          >
            <svg className="absolute inset-0" width={width} height={layout.height} role="img" aria-label={t_i18n("Data flow from the sources through the agents to the outputs")} focusable="false">
              <defs>
                <filter id="fleet-flow-glow" x="-20%" y="-20%" width="140%" height="140%">
                  <feGaussianBlur stdDeviation="3" result="blur" />
                  <feMerge>
                    <feMergeNode in="blur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>
              {layout.edges.map((edge, i) => {
                const disabled =
                  (edge.kind === "start" && model.trunkDisabled.has(trunkKey(edge.ends[0], edge.ends[1]))) ||
                  ((edge.kind === "write" || edge.kind === "read") && notConnected(edge.ends[1]));
                const on = edgeLit(edge.ends);
                if (edge.kind === "handover") {
                  const color = stroke.handover;
                  return (
                    <g key={i} opacity={on ? 1 : DIM}>
                      <path d={edge.d} fill="none" stroke={color} strokeWidth={edge.width} />
                      {edge.head && (
                        <path
                          d={`M${edge.head.x - 7 * edge.head.dir},${edge.head.y - 4} L${edge.head.x + edge.head.dir},${edge.head.y} L${edge.head.x - 7 * edge.head.dir},${edge.head.y + 4} z`}
                          fill={color}
                        />
                      )}
                    </g>
                  );
                }
                const color = bandColor(edge.kind, edge.ends, disabled);
                const glow = focus !== null && on;
                const opacity = focus === null ? (edge.kind === "read" ? BAND.read : BAND.rest) : on ? BAND.lit : BAND.dim;
                return (
                  <path
                    key={i}
                    d={edge.d}
                    fill={color}
                    opacity={band === i ? BAND.lit : opacity}
                    stroke={disabled || edge.kind === "read" ? color : undefined}
                    strokeDasharray={disabled ? "6 4" : edge.kind === "read" ? "2 3" : undefined}
                    strokeWidth={disabled || edge.kind === "read" ? 1 : undefined}
                    filter={glow ? "url(#fleet-flow-glow)" : undefined}
                    className="cursor-pointer transition-opacity"
                    onMouseEnter={() => setBand(i)}
                    onMouseLeave={() => setBand((b) => (b === i ? null : b))}
                    onClick={() => {
                      // The per-assignment detail opens in place in the band's source card (no drawer).
                      if (edge.kind === "start") setOpened(opened === edge.ends[0] ? null : edge.ends[0]);
                    }}
                  >
                    <title>{bandLabel(edge.kind, edge.ends, edge.weight)}</title>
                  </path>
                );
              })}
            </svg>
            {hoveredBand?.mid && hoveredBand.kind === "start" && (
              // The band's count at its middle while the pointer is on it (#9981); the full sentence is its title.
              <span
                className="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 rounded-sm border border-elevation-subtle bg-elevation-default px-1.5 content-compact font-medium tabular-nums text-default-primary"
                style={{ left: hoveredBand.mid.x, top: hoveredBand.mid.y }}
                data-testid="fleet-flow-band-count"
              >
                {hoveredBand.weight}
              </span>
            )}
            <span className="absolute top-0 content-compact font-medium text-default-secondary" style={{ left: 0 }}>
              {t_i18n("Where it starts")}
            </span>
            <span className="absolute top-0 content-compact font-medium text-default-secondary" style={{ left: layout.lanes.agentX }}>
              {t_i18n("Agents")}
            </span>
            <span className="absolute top-0 content-compact font-medium text-default-secondary" style={{ left: layout.lanes.outputX }}>
              {t_i18n("Where it goes")}
            </span>
            {layout.lanes.readsLabelY !== null && (
              <span
                className="absolute content-compact text-default-secondary"
                style={{ left: layout.lanes.outputX, top: layout.lanes.readsLabelY - 16 }}
              >
                {t_i18n("Reads only")}
              </span>
            )}

            {layout.sources.map((card) => {
              const node = model.nodes.get(card.id);
              const off = notConnected(card.id);
              const open = opened === card.id;
              const count = (model.triggers.get(card.id) ?? []).length;
              return (
                <div
                  key={card.id}
                  className={cn(
                    "absolute overflow-hidden rounded-sm border bg-elevation-default transition-opacity",
                    off ? "border-dashed border-elevation-subtle" : "border-elevation-subtle-soft",
                    open && "ring-2 ring-focus",
                  )}
                  style={{ left: card.x, top: card.y, width: card.w, height: card.h, opacity: nodeOpacity(card.id) }}
                  onMouseEnter={() => setHovered(card.id)}
                >
                  {/* The family bar of the OpenCRQ flow chart: the colour of the bands leaving this source. */}
                  <span className="absolute inset-y-0 left-0 w-[3px]" style={{ backgroundColor: off ? stroke.disabled : family[familyOf(node?.kind)] }} aria-hidden />
                  <button
                    type="button"
                    className="flex w-full items-start gap-2 px-2 pt-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                    aria-expanded={open}
                    onClick={() => setOpened(open ? null : card.id)}
                  >
                    <span
                      className={cn("mt-0.5 h-4 w-4 shrink-0 rounded-sm", off ? OFF_SWATCH : KIND_SWATCH[node?.kind ?? ""] ?? KIND_SWATCH.manual)}
                      aria-hidden
                    />
                    {/* Up to two lines, measured by the layout; the title only past two (#9981). */}
                    <span className={cn("min-w-0 flex-1 line-clamp-2 break-words content-compact font-medium", off ? "text-default-secondary" : "text-default-primary")} title={nodeName(node)}>
                      {nodeName(node)}
                    </span>
                    <span className="content-compact tabular-nums text-default-secondary">{count}</span>
                  </button>
                  {off && (
                    <p className="px-2 pt-1 content-compact text-feedback-warning-primary">
                      {t_i18n("Not connected: sign in again in")}{" "}
                      <Link to="/integrations" className="underline text-accent">
                        {t_i18n("Integrations")}
                      </Link>
                    </p>
                  )}
                  <div className="relative" style={{ height: card.detailTop - card.chipsTop }}>
                    {card.chips.map((chip) =>
                      chip.toggle ? (
                        <button
                          key={chip.label}
                          type="button"
                          className="absolute flex items-center whitespace-nowrap rounded-sm border border-elevation-subtle px-2 content-compact text-accent outline-none focus-visible:ring-2 focus-visible:ring-focus"
                          style={{ left: 10 + chip.x, top: chip.y, width: chip.w, height: chip.h }}
                          aria-expanded={chip.toggle === "less"}
                          onClick={() => setExpanded(chip.toggle === "more" ? card.id : null)}
                        >
                          {chip.label}
                        </button>
                      ) : (
                        <span
                          key={chip.label}
                          className={cn(
                            // A long label (a schedule) wraps in the full card width, never cut (UX #9882 r2 B3);
                            // top-aligned so a wrapped label never spills over the row above (r4 B1).
                            "absolute flex rounded-sm bg-elevation-hover px-2 content-compact leading-4",
                            chip.h > 20 ? "items-start py-0.5" : "items-center whitespace-nowrap",
                            off ? "text-default-secondary" : "text-default-primary",
                          )}
                          style={{ left: 10 + chip.x, top: chip.y, width: chip.w, height: chip.h }}
                          title={chip.title}
                        >
                          {chip.label}
                        </span>
                      ),
                    )}
                  </div>
                  {open && (
                    <ul className="space-y-0 px-2 pt-2" aria-label={t_i18n("What it starts")}>
                      {(model.triggers.get(card.id) ?? []).map((edge, i) => (
                        <li key={edge.assignment_id ?? i} className="content-compact">
                          {/* Both lines wrap on the lines the layout measured: nothing cut (UX #9882 r4 B3). */}
                          <span className={cn("block break-words font-medium", edge.enabled ? "text-default-primary" : "text-default-secondary")}>
                            {assignmentLine(edge)}
                          </span>
                          <span className="block break-words text-default-secondary">{eventsLine(edge, node)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}

            {layout.agents.map((card) => {
              const node = model.nodes.get(card.id);
              const srcs = model.lanes.starts.filter(([, a]) => a === card.id).map(([s]) => s);
              const off = srcs.filter(notConnected);
              const starters = srcs.length === 0 ? startersOf(card.id) : [];
              const startersText = starters.map((s) => nodeName(model.nodes.get(s))).join(", ");
              const isHovered = hovered === card.id;
              return (
                <div
                  key={card.id}
                  className={cn(
                    "absolute rounded-sm border bg-elevation-default transition-opacity",
                    isHovered ? "border-accent" : "border-elevation-subtle-soft",
                  )}
                  style={{ left: card.x, top: card.y, width: card.w, height: card.h, opacity: nodeOpacity(card.id) }}
                  onMouseEnter={() => setHovered(card.id)}
                  onFocus={() => setHovered(card.id)}
                  onBlur={() => setHovered(null)}
                >
                  <button
                    type="button"
                    className="block w-full px-3 pt-2 text-left content-compact font-medium text-default-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                    title={nodeName(node)}
                    disabled={!node?.agent_id || !node.visible}
                    onClick={() => node?.agent_id && onOpenAgent(node.agent_id)}
                  >
                    {/* Up to two lines, measured by the layout (#9981: never cut at 1440 px). */}
                    <span className="line-clamp-2 break-words">{nodeName(node)}</span>
                  </button>
                  {off.length > 0 ? (
                    <div className="px-3 content-compact text-feedback-warning-primary">
                      {off.length === srcs.length ? (
                        <>
                          <p className="font-medium">{t_i18n("Cannot start")}</p>
                          <p className="break-words">{offReason(off)}</p>
                        </>
                      ) : (
                        <p className="break-words">{partlyOff(off.length, srcs.length)}</p>
                      )}
                      <Link to="/integrations" className="underline text-accent">
                        {t_i18n("Open Integrations")}
                      </Link>
                    </div>
                  ) : (
                    <p className={cn("px-3 content-compact text-default-secondary", starters.length > 0 && "line-clamp-2")} title={starters.length > 0 ? startersText : undefined}>
                      {node && !node.enabled
                        ? t_i18n("Disabled")
                        : srcs.length > 0
                          ? t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: assignmentsOf(card.id) })
                          : starters.length > 0
                            ? t_i18n("Started by {names}", { names: startersText })
                            : t_i18n("Started by hand only")}
                    </p>
                  )}
                </div>
              );
            })}

            {layout.outputs.map((card) => {
              if (card.id === MORE_OUTPUTS) {
                // The outputs past twelve, folded: their names as the tooltip, a click draws them all (UX #9992 r1 B1).
                return (
                  <button
                    key={card.id}
                    type="button"
                    className="absolute rounded-sm border border-dashed border-elevation-subtle bg-elevation-default px-3 py-2 text-left outline-none transition-opacity focus-visible:ring-2 focus-visible:ring-focus"
                    // Lit when the lit path reaches a folded output (UX #9992 r2 B2).
                    style={{ left: card.x, top: card.y, width: card.w, height: card.h, opacity: moreOutputsLit(lit, hidden) ? 1 : DIM }}
                    title={hiddenNames}
                    ref={moreRef}
                    data-testid="fleet-flow-more-outputs"
                    onClick={() => toggleAllOutputs(true)}
                  >
                    <span className="block content-compact font-medium text-default-primary">{moreLabel}</span>
                    <span className="block pt-1 content-compact text-accent underline">{t_i18n("Show all")}</span>
                  </button>
                );
              }
              const node = model.nodes.get(card.id);
              const off = notConnected(card.id);
              const isRead = model.lanes.reads.includes(card.id);
              return (
                <div
                  key={card.id}
                  className={cn(
                    "absolute overflow-hidden rounded-sm border bg-elevation-default px-2 pl-3 pt-2 transition-opacity",
                    off || isRead ? "border-dashed" : "",
                    off ? "border-elevation-subtle" : isRead ? "border-accent" : "border-elevation-subtle-soft",
                  )}
                  // A read-only output reads faint, as before (#9981 keeps it hideable from the header).
                  style={{ left: card.x, top: card.y, width: card.w, height: card.h, opacity: isRead && !lit ? 0.75 : nodeOpacity(card.id) }}
                  onMouseEnter={() => setHovered(card.id)}
                >
                  <div className="flex items-start gap-2">
                    <span
                      className={cn("mt-0.5 h-4 w-4 shrink-0 rounded-sm", off ? OFF_SWATCH : KIND_SWATCH[node?.kind ?? ""] ?? KIND_SWATCH.integration)}
                      aria-hidden
                    />
                    <span className={cn("min-w-0 line-clamp-2 break-words content-compact font-medium", off ? "text-default-secondary" : "text-default-primary")} title={nodeName(node)}>
                      {nodeName(node)}
                    </span>
                  </div>
                  <p className="pt-1 content-compact text-default-secondary">{outputCaption(card.id)}</p>
                  {off && (
                    <p className="content-compact text-feedback-warning-primary">
                      {t_i18n("Not connected:")}{" "}
                      <Link to="/integrations" className="underline text-accent">
                        {t_i18n("Integrations")}
                      </Link>
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {!layout && <Skeleton className="h-[300px] w-full" />}
        {model.lanes.sources.length === 0 && (
          <p className="pt-2 content-compact text-default-secondary">{t_i18n("Nothing starts this fleet's agents yet: give one an assignment.")}</p>
        )}
      </div>
    </div>
  );
}

/** The legend of the Data flow view: the band colours per source family and the strokes it draws (#9981). */
export function FleetDataFlowLegend() {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const stroke = STROKES[mode];
  const family = FAMILY[mode];
  const bands: Array<[string, string]> = [
    [family.integration, t_i18n("Integration events")],
    [family.platform_event, t_i18n("Platform events")],
    [family.schedule, t_i18n("Schedule")],
    [family.work_item, t_i18n("Work items")],
    [family.webhook, t_i18n("Webhook")],
    [family.manual, t_i18n("Manual")],
  ];
  const rows: Array<[string, string | undefined, string, number]> = [
    [stroke.handover, undefined, t_i18n("Hands work to"), 1.5],
    [stroke.read, "2 3", t_i18n("Reads from (dotted)"), 2],
    [stroke.disabled, "6 4", t_i18n("Not connected or disabled (dashed)"), 1.5],
  ];
  return (
    <div className="space-y-2">
      <p className="content-compact text-default-secondary">{t_i18n("A band is as wide as the assignments it carries, coloured by what starts them")}</p>
      <ul className="space-y-1.5">
        {bands.map(([color, label]) => (
          <li key={label} className="flex items-center gap-2 content-compact text-default-primary">
            <svg width="40" height="10" aria-hidden focusable="false">
              <rect x="0" y="1" width="40" height="8" rx="2" fill={color} opacity={0.7} />
            </svg>
            {label}
          </li>
        ))}
        {rows.map(([color, dash, label, w]) => (
          <li key={label} className="flex items-center gap-2 content-compact text-default-primary">
            <svg width="40" height="10" aria-hidden focusable="false">
              <line x1="0" y1="5" x2="40" y2="5" stroke={color} strokeWidth={w} strokeDasharray={dash} />
            </svg>
            {label}
          </li>
        ))}
      </ul>
    </div>
  );
}
