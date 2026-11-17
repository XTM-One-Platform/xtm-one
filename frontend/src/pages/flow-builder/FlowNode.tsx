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
 * The canvas node.
 *
 * One component for every node kind rather than one per category: everything
 * that differs — icon, accent, ports, the fields shown as a summary — comes
 * from the server's catalog entry, so a new node kind needs no new component.
 *
 * Notes render as a plain sticky card with no ports, matching the fact that an
 * annotation has none server-side and can never be connected.
 */

import { memo, useEffect, useRef } from "react";
import {
  Handle,
  NodeResizer,
  Position,
  useUpdateNodeInternals,
  type NodeProps,
} from "@xyflow/react";
import { AlertTriangle, CircleAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useFormatter } from "@/i18n";
import { Tip } from "@/components/ui/tooltip";
import type { FlowNodeKind, FlowValidationIssue } from "@/types";
import type { CanvasNode } from "./graph-adapter";
import {
  CAPABILITY_PORT_ICONS,
  DEFAULT_NODE_ICON,
  FLOW_NODE_ICONS,
  isCapabilityPortType,
  nodeStyle,
} from "./node-visuals";
import {
  SHAPE_SIZES,
  resolveNodeShape,
  shapePath,
  type NodeShape,
} from "./node-shapes";
import { withCaseSockets } from "./case-sockets";
import { declaredParameters } from "./params";
import { SpeechMicButton } from "./SpeechMicButton";

export interface FlowNodeExtras {
  /** Catalog entry for this node's kind, or undefined when the kind is stale. */
  kindDef?: FlowNodeKind;
  /** Issues the server pinned on this node. */
  issues: FlowValidationIssue[];
  /** Short human summary of the node's config, e.g. the agent it runs. */
  summary?: string;
  /**
   * Execution overlay: this node's status in the run being
   * inspected. Transient view state injected by FlowCanvas — it must never
   * reach the persisted graph (graph-adapter strips extras by construction).
   */
  execution?: "running" | "completed" | "failed" | "skipped";
  /** Input handle ids that currently have an edge — accessory sockets show
   *  green when connected and stay hidden until hover otherwise. */
  connectedHandles?: Set<string>;
  /** Output handle ids that currently have an edge — a Switch keeps its
   *  legacy `case` socket only while an old-style edge still uses it. */
  connectedSourceHandles?: Set<string>;
  /** Inside a loop's body (downstream of a Loop's `each` port) — rendered
   *  with a double outline so the re-executed branch reads at a glance. */
  inLoopBranch?: boolean;
  /** Write-through for inputs embedded ON the node card (e.g. the Chat
   *  trigger's test message) — commits one config key on blur. */
  onEmbeddedConfigChange?: (nodeId: string, key: string, value: string) => void;
}

/**
 * Ports are laid out evenly down the node's side. React Flow positions handles
 * by percentage, so N ports at (i+1)/(N+1) spreads them without overlap
 * whatever the node's height turns out to be.
 */
function portOffset(index: number, total: number): string {
  return `${((index + 1) / (total + 1)) * 100}%`;
}

function FlowNodeComponent({ id, data, selected }: NodeProps<CanvasNode>) {
  const { t_i18n } = useFormatter();
  const extras = (data.extras ?? { issues: [] }) as FlowNodeExtras;
  const kindDef = extras.kindDef;
  const style = nodeStyle(data.kind, kindDef?.category);
  const Icon = FLOW_NODE_ICONS[kindDef?.icon ?? ""] ?? DEFAULT_NODE_ICON;
  // Flowchart glyph for utilities (config first, then the kind's default).
  const nodeShape: NodeShape = resolveNodeShape(
    data.kind,
    kindDef?.category,
    data.config?.shape,
  );
  const updateNodeInternals = useUpdateNodeInternals();

  // A node with a `case` output (a Switch, a Classify node) grows one output
  // socket per configured case or label. ONE cases list whatever the mode —
  // wiring an LLM changes how a case matches, never which sockets exist.
  const connectedSources = extras.connectedSourceHandles ?? new Set<string>();
  let outputs = withCaseSockets(kindDef?.outputs ?? [], data.config?.cases, connectedSources);
  if (data.kind === "trigger.manual") {
    // Declared input parameters surface as labelled pins on the right edge —
    // the UE function-signature look. They are data affordances: dragging
    // one out creates a node prefilled with {{trigger.payload.<name>}}, but
    // no port accepts a `param` channel so no execution edge can exist.
    const params = declaredParameters(data.config);
    if (params.length > 0) {
      outputs = [
        ...outputs,
        ...params.map((param) => ({
          id: `param:${param.name}`,
          label: param.name,
          types: ["param" as const],
          multiple: true,
        })),
      ];
    }
  }
  // Handles are derived from config, so React Flow must re-measure whenever
  // the set changes (adding a case, wiring an LLM) or edges anchor mid-air.
  const outputsSignature = outputs.map((p) => p.id).join("|") + nodeShape;
  useEffect(() => {
    updateNodeInternals(id);
  }, [id, outputsSignature, updateNodeInternals]);

  // The Chat trigger's embedded test-message box — the mic button appends
  // dictated text into it, so the uncommitted typed text is never lost.
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null);

  const errors = extras.issues.filter((i) => i.severity === "error");
  const warnings = extras.issues.filter((i) => i.severity === "warning");
  const label = data.label || kindDef?.label || data.kind;

  if (data.kind === "util.group") {
    // A visual container: children carry parentId and draw on top (the group
    // has zIndex -1). Resizable when selected; the size lives in config.
    const width = Number(data.config?.width) || 420;
    const height = Number(data.config?.height) || 280;
    const borderColor =
      typeof data.config?.border_color === "string"
        ? (data.config.border_color as string)
        : undefined;
    const bgColor =
      typeof data.config?.bg_color === "string"
        ? (data.config.bg_color as string)
        : undefined;
    return (
      <div
        className={cn(
          "rounded-sm border-2 border-dashed border-elevation-subtle-soft bg-elevation-hover",
          selected && !borderColor && "border-accent/60",
        )}
        style={{
          width,
          height,
          ...(borderColor ? { borderColor } : {}),
          // 10% alpha: the frame must tint, not mask, what it contains.
          ...(bgColor ? { background: `${bgColor}1A` } : {}),
        }}
      >
        <NodeResizer
          isVisible={!!selected}
          minWidth={160}
          minHeight={120}
          onResizeEnd={(_e, params) =>
            extras.onEmbeddedConfigChange?.(id, "__size", `${Math.round(params.width)}x${Math.round(params.height)}`)
          }
        />
        <span className="pointer-events-none absolute left-2 top-1.5 content-caption font-semibold uppercase tracking-wide text-default-secondary">
          {data.label || "Group"}
        </span>
      </div>
    );
  }

  if (kindDef?.is_annotation) {
    // Resizable like the Group frame: the size lives in config (width /
    // height), so it saves with the graph and survives reloads. Height
    // stays automatic until the author grabs a handle.
    const noteWidth = Number(data.config?.width) || 224;
    const noteHeight = Number(data.config?.height) || undefined;
    return (
      <div
        className={cn(
          "rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.07] p-3 content-compact",
          selected && "ring-2 ring-accent",
        )}
        style={{
          width: noteWidth,
          ...(noteHeight ? { height: noteHeight } : {}),
        }}
      >
        <NodeResizer
          isVisible={!!selected}
          minWidth={140}
          minHeight={48}
          onResizeEnd={(_e, params) =>
            extras.onEmbeddedConfigChange?.(
              id,
              "__size",
              `${Math.round(params.width)}x${Math.round(params.height)}`,
            )
          }
        />
        <p className="h-full overflow-hidden whitespace-pre-wrap break-words text-default-secondary">
          {String(data.config?.text || "") || "Empty note"}
        </p>
      </div>
    );
  }

  const execution = extras.execution;
  // UE colour code: an object-typed constant (declared type) pins in GREEN,
  // scalars keep the cyan param dot.
  const isObjectPrimitive =
    data.kind === "util.primitive" && data.config?.value_type === "object";

  // Where each capability socket sits, to keep the crowded bottom edge
  // legible: structured output on the LEFT beside the flow input (it shapes
  // the answer, so it reads as an input); memory + guardrails on the TOP
  // (behaviour settings, not attached resources); tools / integrations /
  // skills / knowledge bases along the BOTTOM.
  const LEFT_SOCKET_IDS = new Set(["output"]);
  // `llm` joins the behaviour sockets on top: a condition node's bottom edge is
  // where its branches read from, so an attachment there would crowd them.
  const TOP_SOCKET_IDS = new Set([
    "memory",
    "guardrails",
    "llm",
    "shared_state",
    "work_items",
    "workset",
    "budget",
    "workspace",
  ]);

  const inputs = kindDef?.inputs ?? [];
  const isCapabilityInput = (port: (typeof inputs)[number]) =>
    port.types.every(isCapabilityPortType);
  const sideInputs = inputs.filter(
    (port) => !isCapabilityInput(port) || LEFT_SOCKET_IDS.has(port.id),
  );
  const topInputs = inputs.filter(
    (port) => isCapabilityInput(port) && TOP_SOCKET_IDS.has(port.id),
  );
  const capabilityInputs = inputs.filter(
    (port) =>
      isCapabilityInput(port) &&
      !LEFT_SOCKET_IDS.has(port.id) &&
      !TOP_SOCKET_IDS.has(port.id),
  );
  const hasLeftCapabilityIcon = sideInputs.some(
    (port) => CAPABILITY_PORT_ICONS[port.id],
  );

  // A capability socket is shown green once an edge lands on it; otherwise it
  // stays hidden (revealed on hover / while connecting) via `flow-cap-socket`.
  const connected = extras.connectedHandles ?? new Set<string>();
  const capHandleClass = (id: string) =>
    cn("flow-cap-socket", connected.has(id) && "flow-cap-connected");
  const capIconClass = (id: string) =>
    cn(
      "h-3 w-3 transition-opacity",
      connected.has(id)
        ? "text-feedback-success-primary opacity-100"
        : "text-default-secondary opacity-0 group-hover:opacity-100",
    );

  const hasBreakpoint = !!data.config?.breakpoint;
  const breakpointDot = hasBreakpoint ? (
    <span
      className="absolute -left-1.5 -top-1.5 z-20 h-3 w-3 rounded-full border border-[var(--bg-elevation-default)] bg-feedback-error-primary"
      title="Breakpoint"
    />
  ) : null;

  if (data.kind === "util.reroute") {
    // A knot: as small as a handle, in on the left, out on the right.
    return (
      <div
        className={cn(
          "group relative h-4 w-4 rounded-full border layer-1 bg-elevation-default transition-colors",
          selected ? "border-accent ring-1 ring-accent" : "border-elevation-subtle",
          extras.inLoopBranch && "flow-loop-branch",
        )}
        title={label}
      >
        {breakpointDot}
        <Handle id="in" type="target" position={Position.Left} />
        <Handle id="out" type="source" position={Position.Right} />
      </div>
    );
  }

  if (nodeShape !== "rectangle") {
    const [sw, sh] = SHAPE_SIZES[nodeShape];
    return (
      <div
        className={cn(
          "group relative",
          extras.inLoopBranch && "flow-loop-branch",
          execution === "skipped" && "opacity-60",
        )}
        style={{ width: sw }}
      >
        {breakpointDot}
        <div className="relative" style={{ width: sw, height: sh }}>
          <svg
            className="absolute inset-0 overflow-visible"
            width={sw}
            height={sh}
            aria-hidden
          >
            <path
              d={shapePath(nodeShape, sw, sh)}
              strokeWidth={selected ? 2 : 1.5}
              className={cn(
                "fill-[var(--bg-elevation-default)] stroke-[var(--border-elevation-subtle)] transition-colors",
                selected && "stroke-accent",
                errors.length > 0 && "stroke-feedback-error-primary/70",
                execution === "running" && "stroke-accent/80",
                execution === "completed" && "stroke-feedback-success-primary/70",
                execution === "failed" && "stroke-feedback-error-primary/90",
              )}
            />
          </svg>
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center gap-1.5 px-4">
            {isObjectPrimitive ? (
              // An object constant shows its declared type, UE-green.
              <span className="max-w-full truncate font-mono-plex text-[11px] text-feedback-success-primary">
                {String(data.config?.object_type_name || "Object")}
              </span>
            ) : data.kind === "util.primitive" &&
            typeof data.config?.value === "string" &&
            data.config.value ? (
              // A literal IS its value — show it, not an icon.
              <span className="max-w-full truncate font-mono-plex text-[11px]">
                {String(data.config.value)}
              </span>
            ) : (
              <Icon className={cn("h-4 w-4", style.accent)} />
            )}
            {errors.length > 0 && (
              <Tip content={errors.map((i) => i.message).join(" · ")}>
                <CircleAlert className="pointer-events-auto h-3 w-3 text-feedback-error-primary" />
              </Tip>
            )}
            {errors.length === 0 && warnings.length > 0 && (
              <Tip content={warnings.map((i) => i.message).join(" · ")}>
                <AlertTriangle className="pointer-events-auto h-3 w-3 text-feedback-warning-primary" />
              </Tip>
            )}
          </div>
          {sideInputs.map((port, index) => (
            <Handle
              key={port.id}
              id={port.id}
              type="target"
              position={Position.Left}
              className={
                port.types.includes("param")
                  ? "flow-param-handle"
                  : "flow-exec-handle"
              }
              style={{ top: portOffset(index, sideInputs.length) }}
              title={port.label}
            />
          ))}
          {outputs.map((port, index) => (
            <Handle
              key={port.id}
              id={port.id}
              type="source"
              position={Position.Right}
              className={
                port.id.startsWith("param:") || port.types.includes("param")
                  ? "flow-param-handle"
                  : "flow-exec-handle"
              }
              style={{ top: portOffset(index, outputs.length) }}
              title={port.label}
            />
          ))}
          {outputs.length > 1 && (
            <div className="pointer-events-none absolute inset-y-0 left-full w-0">
              {outputs.map((port, index) => (
                <span
                  key={port.id}
                  className="absolute left-1.5 max-w-24 -translate-y-1/2 truncate text-[9px] text-default-secondary"
                  style={{ top: portOffset(index, outputs.length) }}
                >
                  {port.label}
                </span>
              ))}
            </div>
          )}
        </div>
        <p
          className="mt-1 w-full truncate text-center content-caption text-default-secondary"
          title={label}
        >
          {label}
        </p>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "group relative flex w-60 overflow-hidden rounded-sm border layer-1 bg-elevation-default transition-colors",
        selected ? "border-accent ring-1 ring-accent" : "border-elevation-subtle",
        errors.length > 0 && "border-feedback-error-primary/60",
        // Loop body: a second outline around the card — the double border that
        // marks "this branch re-executes every pass". Outline (not ring) so it
        // never fights the selection ring or the execution shadow.
        extras.inLoopBranch && "flow-loop-branch",
        // Execution overlay: louder than validation, quieter than selection.
        execution === "running" &&
          "border-accent/70 shadow-[0_0_0_2px_hsl(197_100%_45%/0.25)]",
        execution === "completed" && "border-feedback-success-primary/60",
        execution === "failed" && "border-feedback-error-primary/80",
        execution === "skipped" && "opacity-60",
      )}
    >
      {breakpointDot}
      <div className="min-w-0 flex-1">
        {/* UE-style header band: the category colour fading left → right,
            icon + title on it — the node's identity reads from the header,
            not from a side rail. */}
        <div
          className={cn(
            "flex items-center gap-2 px-2.5 py-1.5",
            topInputs.length > 0 && "pt-5",
            style.header,
          )}
          title={kindDef?.label ?? data.kind}
        >
          <Icon className={cn("h-3.5 w-3.5 shrink-0", style.accent)} />
          <p className="min-w-0 flex-1 truncate content-compact font-semibold">
            {label}
          </p>
          {errors.length > 0 && (
            <Tip content={errors.map((i) => i.message).join(" · ")}>
              <CircleAlert className="h-3.5 w-3.5 shrink-0 text-feedback-error-primary" />
            </Tip>
          )}
          {errors.length === 0 && warnings.length > 0 && (
            <Tip content={warnings.map((i) => i.message).join(" · ")}>
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
            </Tip>
          )}
        </div>

        {/* Body: the kind (when the header shows a custom name), summary and
            embedded inputs. Extra padding so socket icons never sit on top of
            the content. */}
        <div
          className={cn(
            // min-h keeps UE proportions on empty-bodied nodes (a bare
            // trigger): the header is the banner, the body is where the
            // pins visually live.
            "min-h-10 min-w-0 px-2.5 pb-2 pt-1",
            capabilityInputs.length > 0 && "pb-6",
            hasLeftCapabilityIcon && "pl-5",
          )}
        >
          {data.label && data.label !== (kindDef?.label ?? "") && (
            <p className="truncate content-caption text-default-secondary">
              {kindDef?.label ?? data.kind}
            </p>
          )}

        {extras.summary && (
          <p className="mt-2 truncate border-t border-elevation-subtle-soft pt-2 content-caption text-default-secondary">
            {extras.summary}
          </p>
        )}

        {/* LangFlow-style embedded input: the Chat start point carries its
            test message ON the node — `nodrag` keeps typing from dragging. */}
        {data.kind === "trigger.chat" && (
          <div className="relative mt-2">
            <textarea
              key={String(data.config?.default_message ?? "")}
              ref={chatInputRef}
              rows={2}
              defaultValue={String(data.config?.default_message ?? "")}
              placeholder={t_i18n("Test message…")}
              aria-label={t_i18n("Test message")}
              className={cn("nodrag nowheel w-full resize-none rounded-sm border border-elevation-subtle bg-elevation-default px-2 py-1 pr-6 text-[11px] placeholder:text-default-secondary", FIELD_FOCUS_RING)}
              onBlur={(e) => {
                const value = e.target.value;
                if (value !== String(data.config?.default_message ?? "")) {
                  extras.onEmbeddedConfigChange?.(id, "default_message", value);
                }
              }}
            />
            {/* Browser STT: dictated text appends to whatever is typed and
                commits immediately (blur may never come after a dictation). */}
            <SpeechMicButton
              className="absolute right-1 top-1"
              onTranscript={(text) => {
                const current = chatInputRef.current?.value ?? "";
                const next = current ? `${current.replace(/\s+$/, "")} ${text}` : text;
                if (chatInputRef.current) chatInputRef.current.value = next;
                extras.onEmbeddedConfigChange?.(id, "default_message", next);
              }}
            />
          </div>
        )}
        </div>
      </div>

      {/* Flow input (and the output-schema socket) on the LEFT; the other
          capability sockets along the BOTTOM edge, one identified attachment
          point per family. The port typing already makes each socket the only
          legal target for its family — the icons make that legible before you
          even drag. */}
      {sideInputs.map((port, index) => (
        <Handle
          key={port.id}
          id={port.id}
          type="target"
          position={Position.Left}
          className={
            isCapabilityInput(port)
              ? capHandleClass(port.id)
              : port.types.includes("param")
                ? cn("flow-param-handle", isObjectPrimitive && "flow-object-handle")
                : "flow-exec-handle"
          }
          style={{ top: portOffset(index, sideInputs.length) }}
          title={port.label}
        />
      ))}
      {/* Icon beside each left capability socket (the flow `in` needs none). */}
      {sideInputs.map((port, index) => {
        const PortIcon = CAPABILITY_PORT_ICONS[port.id];
        if (!PortIcon) return null;
        return (
          <span
            key={`icon-${port.id}`}
            className="pointer-events-none absolute left-1.5 -translate-y-1/2"
            style={{ top: portOffset(index, sideInputs.length) }}
            title={port.label}
          >
            <PortIcon className={capIconClass(port.id)} />
          </span>
        );
      })}
      {capabilityInputs.map((port, index) => (
        <Handle
          key={port.id}
          id={port.id}
          type="target"
          position={Position.Bottom}
          className={capHandleClass(port.id)}
          style={{ left: portOffset(index, capabilityInputs.length) }}
          title={port.label}
        />
      ))}
      {topInputs.map((port, index) => (
        <Handle
          key={port.id}
          id={port.id}
          type="target"
          position={Position.Top}
          className={capHandleClass(port.id)}
          style={{ left: portOffset(index, topInputs.length) }}
          title={port.label}
        />
      ))}
      {outputs.map((port, index) => {
        // A capability node's `provides` plug faces toward the agent socket it
        // feeds: memory/guardrails sit on the agent's TOP edge, so their plug
        // faces DOWN (node placed above); the rest sit on the BOTTOM edge, so
        // their plug faces UP (node placed below). Either way it reads as
        // attachment, not flow.
        const isPlug = port.types.every(isCapabilityPortType);
        const plugFacesDown =
          isPlug &&
          port.types.some(
            (t) =>
              t === "capability_memory" ||
              t === "capability_guardrails" ||
              t === "capability_llm" ||
              t === "capability_shared_state" ||
              t === "capability_work_items" ||
              t === "capability_workset" ||
              t === "capability_budget" ||
              t === "capability_workspace",
          );
        return (
          <Handle
            key={port.id}
            id={port.id}
            type="source"
            className={
              port.id.startsWith("param:") || port.types.includes("param")
                ? cn("flow-param-handle", isObjectPrimitive && "flow-object-handle")
                : isPlug
                  ? undefined
                  : "flow-exec-handle"
            }
            position={
              plugFacesDown
                ? Position.Bottom
                : isPlug
                  ? Position.Top
                  : Position.Right
            }
            style={
              isPlug
                ? { left: portOffset(index, outputs.length) }
                : { top: portOffset(index, outputs.length) }
            }
            title={port.label}
          />
        );
      })}

      {/* The socket strips: one icon under each bottom point, above each top. */}
      {capabilityInputs.length > 0 && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-0">
          {capabilityInputs.map((port, index) => {
            const PortIcon = CAPABILITY_PORT_ICONS[port.id];
            if (!PortIcon) return null;
            return (
              <span
                key={port.id}
                className="absolute bottom-1 -translate-x-1/2"
                style={{ left: portOffset(index, capabilityInputs.length) }}
                title={port.label}
              >
                <PortIcon className={capIconClass(port.id)} />
              </span>
            );
          })}
        </div>
      )}
      {topInputs.length > 0 && (
        <div className="pointer-events-none absolute inset-x-0 top-0 h-0">
          {topInputs.map((port, index) => {
            const PortIcon = CAPABILITY_PORT_ICONS[port.id];
            if (!PortIcon) return null;
            return (
              <span
                key={port.id}
                className="absolute top-1 -translate-x-1/2"
                style={{ left: portOffset(index, topInputs.length) }}
                title={port.label}
              >
                <PortIcon className={capIconClass(port.id)} />
              </span>
            );
          })}
        </div>
      )}

      {/* Output port labels — a Switch with `case`/`default` is unreadable
          without them, and guessing which handle is which is the single most
          common way to wire a branch backwards. */}
      {outputs.length > 1 && (
        <div className="pointer-events-none absolute inset-y-0 right-0 w-0">
          {outputs.map((port, index) => (
            <span
              key={port.id}
              className="absolute right-2 max-w-24 -translate-y-1/2 truncate text-[9px] text-default-secondary"
              style={{ top: portOffset(index, outputs.length) }}
            >
              {port.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export const FlowNodeCard = memo(FlowNodeComponent);
