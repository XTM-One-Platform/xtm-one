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

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  Coins,
  Copy,
  Image as ImageIcon,
  Layers,
  MessageSquare,
  Settings2,
  User,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { MaskedCost } from "@/components/ui/masked-cost";
import { Tip } from "@/components/ui/tooltip";
import { TraceFileChips } from "@/components/traces/trace-file-chips";
import {
  extractRoleBlocksFromSerializedText,
  maybeParseJsonString,
  normalizeMarkdownLikeString,
  normalizeRichText,
  type PromptRoleBlock,
} from "@/components/traces/prompt-text";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import {
  dedupeFileRefs,
  extractFileRefs,
  formatDuration,
  resolveProviderFromMetadata,
} from "@/lib/trace-utils";
import { TRACE_PROMPT_COLLAPSE_THRESHOLD } from "@/lib/constants";
import { formatFailureCause } from "@/lib/failure-causes";
import { cn, exactCompactCost, exactMetricValue, formatCompactCost, formatCompactNumber, formatShortDateTime, pluralize } from "@/lib/utils";
import { useSpanPayload, useSpanPayloadMedia } from "@/hooks/api/useTraceApi";
import type { UseQueryResult } from "@tanstack/react-query";
import { MEDIA_REFERENCE_KEY } from "@/types/traces";
import type {
  LLMSpan,
  LLMTrace,
  MediaReference,
  PayloadField,
  PayloadFieldMeta,
  TracePayloadResponse,
} from "@/types";

type InspectorTab = "preview" | "logview";

const TOOL_ARGUMENTS_PREVIEW_LIMIT = 4_000;

interface LLMMessage {
  role: string;
  content: unknown;
}

function extractMessages(input: unknown, output?: unknown): LLMMessage[] {
  // 1. Standard LLM call format: input.messages array
  if (input && typeof input === "object" && !Array.isArray(input)) {
    const msgs = (input as Record<string, unknown>).messages;
    if (Array.isArray(msgs) && msgs.length > 0) {
      return msgs.filter((m): m is LLMMessage => !!m && typeof m === "object" && "role" in m);
    }
  }

  // 2. Fallback: synthesise from raw input + output for non-LLM spans (e.g. ITERATION)
  const synthetic: LLMMessage[] = [];

  if (input && typeof input === "object" && !Array.isArray(input)) {
    const inp = input as Record<string, unknown>;
    const keys = Object.keys(inp).filter((k) => k !== "tools" && k !== "model");
    if (keys.length > 0) {
      const body = keys
        .map((k) => `${k}: ${typeof inp[k] === "string" ? inp[k] : JSON.stringify(inp[k])}`)
        .join("\n");
      synthetic.push({ role: "context", content: body });
    }
  }

  if (output && typeof output === "object" && !Array.isArray(output)) {
    const out = output as Record<string, unknown>;
    // Prefer a dedicated text field, fall back to full JSON
    const text =
      typeof out.content_preview === "string" ? out.content_preview
      : typeof out.content === "string" ? out.content
      : typeof out.text === "string" ? out.text
      : null;

    if (text) {
      synthetic.push({ role: "assistant", content: text });
    } else if (Object.keys(out).length > 0) {
      // Show remaining keys as an assistant block
      const body = Object.entries(out)
        .filter(([k]) => k !== "content_preview" && k !== "content" && k !== "text")
        .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join("\n");
      if (body) synthetic.push({ role: "assistant", content: body });
    }
  }

  return synthetic;
}

function messageContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part: unknown) => {
        if (!part || typeof part !== "object") return String(part);
        const p = part as Record<string, unknown>;
        if (p.type === "text" && typeof p.text === "string") return p.text;
        if (p.type === "tool_use") return `[tool_use: ${p.name ?? "?"}]`;
        if (p.type === "tool_result") return `[tool_result]`;
        // Media lives in object storage and the part holds a reference. Name it
        // here rather than dumping the reference's JSON into the prompt text —
        // the blob itself is rendered by the pane's attachment strip.
        const media = mediaReferenceInPart(p);
        if (media) {
          const size = formatBytes(media.size_bytes);
          return `[${media.content_type}${size ? `, ${size}` : ""} — see attachments]`;
        }
        return JSON.stringify(part);
      })
      .join("\n");
  }
  if (content == null) return "";
  return JSON.stringify(content);
}

/**
 * The media reference inside one content part, whatever shape carried it.
 *
 * OpenAI parts hold it under `image_url.url`, Anthropic blocks under
 * `source.data`, and a bare `data:` URI is replaced in place.
 */
function mediaReferenceInPart(part: Record<string, unknown>): MediaReference | null {
  const candidates: unknown[] = [
    part,
    part.image_url,
    asRecord(part.image_url)?.url,
    part.source,
    asRecord(part.source)?.data,
    part.url,
    part.data,
  ];
  for (const candidate of candidates) {
    const reference = readMediaReference(candidate);
    if (reference) return reference;
  }
  return null;
}

interface RoleStyle {
  label: string;
  icon: LucideIcon;
  labelColor: string;
  accent: string;
}

const ROLE_STYLES: Record<string, RoleStyle> = {
  system: {
    label: "System",
    icon: Settings2,
    labelColor: "text-filigran-ia-primary light:text-filigran-ia-primary",
    accent: "border-l-filigran-ia-primary/60 bg-filigran-ia-primary/[0.04] light:border-l-filigran-ia-primary/60 light:bg-filigran-ia-primary/[0.04]",
  },
  context: {
    label: "Context",
    icon: Layers,
    labelColor: "text-default-secondary",
    accent: "border-l-elevation-subtle-soft bg-elevation-hover",
  },
  user: {
    label: "User",
    icon: User,
    labelColor: "text-accent",
    accent: "border-l-accent/60 bg-accent/[0.04]",
  },
  assistant: {
    label: "Assistant",
    icon: Bot,
    labelColor: "text-feedback-success-primary light:text-feedback-success-primary",
    accent: "border-l-feedback-success-primary/60 bg-feedback-success-primary/[0.04] light:border-l-feedback-success-primary/60 light:bg-feedback-success-primary/[0.04]",
  },
  tool: {
    label: "Tool",
    icon: Wrench,
    labelColor: "text-feedback-warning-primary light:text-feedback-warning-primary",
    accent: "border-l-feedback-alert-primary/60 bg-feedback-alert-primary/[0.04] light:border-l-feedback-alert-primary/60 light:bg-feedback-alert-primary/[0.04]",
  },
};

function roleUi(role: string): RoleStyle {
  const normalized = role.trim().toLowerCase();
  const style = ROLE_STYLES[normalized];
  if (style) return style;
  const pretty = normalized ? `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}` : "Message";
  return {
    label: pretty,
    icon: MessageSquare,
    labelColor: "text-default-secondary",
    accent: "border-l-elevation-subtle-soft bg-elevation-hover",
  };
}

function RoleBlock({
  role,
  content,
  markdown,
}: {
  role: string;
  content: string;
  markdown: boolean;
}) {
  const ui = roleUi(role);
  const Icon = ui.icon;
  const isLong = content.length > TRACE_PROMPT_COLLAPSE_THRESHOLD;
  const [expanded, setExpanded] = useState(!isLong);

  useEffect(() => {
    setExpanded(!isLong);
  }, [content, isLong]);

  const header = (
    <>
      <Icon className={cn("h-3 w-3 shrink-0", ui.labelColor)} />
      <span className={cn("content-caption font-semibold uppercase tracking-wider", ui.labelColor)}>
        {ui.label}
      </span>
      {isLong ? (
        <>
          <span className="ml-auto content-caption text-default-secondary">
            {formatCompactNumber(content.length)} chars
          </span>
          {expanded
            ? <ChevronDown className="h-3 w-3 shrink-0 text-default-secondary" />
            : <ChevronRight className="h-3 w-3 shrink-0 text-default-secondary" />}
        </>
      ) : null}
    </>
  );

  return (
    <div className={cn("rounded-sm border border-elevation-subtle-soft border-l-2", ui.accent)}>
      {isLong ? (
        <button
          type="button"
          className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left"
          aria-expanded={expanded}
          aria-label={expanded ? `Collapse ${ui.label} block` : `Expand ${ui.label} block`}
          onClick={() => setExpanded((prev) => !prev)}
        >
          {header}
        </button>
      ) : (
        <div className="flex items-center gap-1.5 px-2 py-1.5">{header}</div>
      )}
      <div
        className={cn(
          "px-2 pb-2",
          !expanded && "relative max-h-24 overflow-hidden",
        )}
      >
        {markdown ? (
          <ChatMarkdown
            content={content}
            className="content-compact text-default-secondary [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-compact [&_h1]:content-compact [&_h2]:content-compact [&_h3]:content-compact"
          />
        ) : (
          <pre className={cn(
            "whitespace-pre-wrap break-words content-compact text-default-secondary",
            expanded && "max-h-96 overflow-auto",
          )}>
            {content || "—"}
          </pre>
        )}
        {!expanded ? (
          <button
            type="button"
            className="absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent"
            aria-label={`Expand ${ui.label} block`}
            onClick={() => setExpanded(true)}
          />
        ) : null}
      </div>
    </div>
  );
}

function RolePromptBlocks({ blocks }: { blocks: PromptRoleBlock[] }) {
  if (blocks.length === 0) {
    return <p className="content-compact text-default-secondary">—</p>;
  }

  return (
    <div className="space-y-2">
      {blocks.map((block, index) => (
        <RoleBlock
          key={`${block.role}-${index}`}
          role={block.role}
          content={block.content}
          markdown
        />
      ))}
    </div>
  );
}

function tagHue(tag: string): number {
  let h = 0;
  for (let i = 0; i < tag.length; i++) h = (h * 31 + tag.charCodeAt(i)) & 0xffffff;
  return h % 360;
}

function hasObjectContent(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length > 0;
}

function payloadToText(payload: unknown): string {
  if (payload == null) return "—";
  if (typeof payload === "string") return payload || "—";
  if (typeof payload === "number" || typeof payload === "boolean") return String(payload);
  if (Array.isArray(payload)) return payload.length > 0 ? JSON.stringify(payload, null, 2) : "—";
  if (!hasObjectContent(payload)) return "—";
  return JSON.stringify(payload, null, 2);
}

function normalizeToolKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function candidateToArray(candidate: unknown): unknown[] {
  if (Array.isArray(candidate)) return candidate;
  if (typeof candidate === "string") {
    const trimmed = candidate.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      return Array.isArray(parsed) ? parsed : [parsed];
    } catch {
      return [];
    }
  }
  if (!candidate || typeof candidate !== "object") return [];
  const record = candidate as Record<string, unknown>;
  if (Array.isArray(record.tool_calls)) return record.tool_calls;
  if (Array.isArray(record.tools)) return record.tools;
  if (Array.isArray(record.available_tools)) return record.available_tools;
  if (Array.isArray(record.AVAILABLE_TOOLS)) return record.AVAILABLE_TOOLS;
  if (Array.isArray(record.TOOLS_CALLS)) return record.TOOLS_CALLS;
  return [candidate];
}

function collectToolFieldCandidates(
  payload: unknown,
  expected: "defs" | "calls",
): unknown[] {
  const record = asRecord(payload);
  if (!record) return [];

  const keySets = {
    defs: new Set(["tools", "toolnames", "availabletools"]),
    calls: new Set(["toolcalls", "toolscalls", "toolcall"]),
  };

  const selectedKeys = keySets[expected];
  const out: unknown[] = [];
  for (const [key, value] of Object.entries(record)) {
    if (selectedKeys.has(normalizeToolKey(key))) {
      out.push(value);
    }
  }
  return out;
}

function extractRoleBlocksFromMessagesArray(value: unknown): PromptRoleBlock[] {
  if (!Array.isArray(value)) return [];

  const blocks: PromptRoleBlock[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== "object") continue;
    const message = entry as Record<string, unknown>;
    const role = typeof message.role === "string" ? message.role.trim().toLowerCase() : "";
    if (!role) continue;

    const content = message.content;
    // A messages array only exists once the payload was parsed: its strings are
    // what the model received, backslashes included.
    const text = typeof content === "string"
      ? normalizeRichText(content, { unescape: false })
      : messageContent(content).trim();

    if (!text) continue;
    blocks.push({ role, content: text });
  }

  return blocks;
}

function payloadValueToString(value: unknown): string {
  if (value == null) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

interface ToolDef {
  name: string;
  description?: string;
  parameters?: unknown;
}

function percentile(values: number[], quantile: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.min(sorted.length - 1, Math.max(0, Math.ceil(quantile * sorted.length) - 1));
  return sorted[rank] ?? null;
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function KpiTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2.5">
      <p className="content-caption uppercase tracking-wide text-default-secondary">{label}</p>
      <p className="mt-1 content-base font-semibold text-default-primary">{value}</p>
      {hint ? <p className="mt-0.5 text-[11px] text-default-secondary">{hint}</p> : null}
    </div>
  );
}

function parseToolDef(value: unknown, existing?: ToolDef): ToolDef | null {
  if (typeof value === "string") {
    const name = value.trim();
    return name ? { name, description: existing?.description, parameters: existing?.parameters } : null;
  }
  if (!value || typeof value !== "object") return null;

  const raw = value as Record<string, unknown>;
  const fn = raw.function && typeof raw.function === "object"
    ? (raw.function as Record<string, unknown>)
    : raw;

  const name =
    (typeof fn.name === "string" && fn.name) ||
    (typeof raw.name === "string" && raw.name) ||
    (typeof raw.tool_name === "string" && raw.tool_name) ||
    null;

  if (!name) return null;

  return {
    name,
    description:
      (typeof fn.description === "string" && fn.description) ||
      (typeof raw.description === "string" && raw.description) ||
      existing?.description,
    parameters:
      fn.parameters ??
      fn.input_schema ??
      raw.parameters ??
      raw.input_schema ??
      existing?.parameters,
  };
}

function extractToolDefsFromUnknown(payload: unknown): ToolDef[] {
  const seen = new Map<string, ToolDef>();

  const candidates = [
    ...collectToolFieldCandidates(payload, "defs"),
    payload,
  ];

  for (const candidate of candidates) {
    for (const item of candidateToArray(candidate)) {
      const existing = typeof item === "string" ? seen.get(item.trim()) : undefined;
      const parsed = parseToolDef(item, existing);
      if (!parsed) continue;
      seen.set(parsed.name, parsed);
    }
  }

  return Array.from(seen.values());
}

function parseToolCallName(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = maybeParseJsonString(trimmed);
    if (parsed !== trimmed) {
      return parseToolCallName(parsed);
    }
    return trimmed;
  }

  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const fn = raw.function && typeof raw.function === "object"
    ? (raw.function as Record<string, unknown>)
    : null;
  const tool = raw.tool && typeof raw.tool === "object"
    ? (raw.tool as Record<string, unknown>)
    : null;

  return (
    (typeof raw.name === "string" && raw.name) ||
    (typeof raw.tool_name === "string" && raw.tool_name) ||
    (typeof fn?.name === "string" && fn.name) ||
    (typeof tool?.name === "string" && tool.name) ||
    null
  );
}

function extractToolCallCountsFromUnknown(payload: unknown): Map<string, number> {
  const counts = new Map<string, number>();
  const candidates: unknown[] = [...collectToolFieldCandidates(payload, "calls")];

  // Some providers emit tool calls as a top-level array/string payload.
  if (Array.isArray(payload) || typeof payload === "string") {
    candidates.push(payload);
  }

  for (const candidate of candidates) {
    for (const item of candidateToArray(candidate)) {
      const name = parseToolCallName(item);
      if (!name) continue;
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
  }

  return counts;
}

function ToolRows({
  tools,
  callCounts,
}: {
  tools: ToolDef[];
  callCounts?: Map<string, number>;
}) {
  if (tools.length === 0) {
    return <p className="content-compact text-default-secondary">—</p>;
  }

  return (
    <div className="space-y-1">
      {tools.map((tool, index) => {
        const callCount = callCounts?.get(tool.name) ?? 0;
        return (
          <div
            key={`${tool.name}-${index}`}
            className="flex w-full items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1.5 content-base"
          >
            <span className="text-default-secondary">{index + 1}.</span>
            <span className="font-medium">{tool.name}</span>
            <Badge variant="outline" className="ml-auto text-[11px]">
              {callCount > 0 ? `${callCount} ${pluralize(callCount, "call")}` : "not called"}
            </Badge>
          </div>
        );
      })}
    </div>
  );
}

interface PayloadNoticeProps {
  field: PayloadField;
  /** Preview from the span response, or the full value once loaded. */
  value: unknown;
  meta: PayloadFieldMeta | undefined;
  query: UseQueryResult<TracePayloadResponse>;
  requested: boolean;
  onLoad: (field: PayloadField) => void;
}

/** What a payload is, and what is missing from what is about to be rendered. */
function derivePayloadState(
  value: unknown,
  meta: PayloadFieldMeta | undefined,
  query: UseQueryResult<TracePayloadResponse>,
) {
  const envelope = readOffloadEnvelope(value);
  // Deliberately keyed on the data being in hand, not on `requested`: a
  // disabled query still reads its cache, so revisiting a span whose payload
  // was loaded a moment ago shows it again without a second fetch. `value` is
  // the resolved payload in that case (the caller prefers the query's data), so
  // gating this on `requested` would render the complete payload while calling
  // it a preview and offering to load what is already there.
  const loaded = query.data !== undefined;
  const unavailable = Boolean(envelope?.offload_failed || query.data?.unavailable);
  const bounded = Boolean(meta?.truncated || envelope);
  const showLoadAction = !loaded && !unavailable && bounded;
  return {
    envelope,
    loaded,
    unavailable,
    // An offloaded payload's row value is a pointer: show its captured preview
    // instead of the pointer's own fields.
    renderable: envelope && !loaded ? (envelope.preview ?? null) : value,
    // That preview is a truncated serialisation of the payload (escapes and
    // all), not text the model received, so it is rendered as raw.
    renderableIsSerializedPreview: Boolean(envelope && !loaded),
    size: formatBytes(meta?.size_bytes ?? envelope?.size_bytes ?? query.data?.size_bytes),
    showLoadAction,
    // Whether `PayloadNotice` renders anything at all — one flag per branch it
    // takes, so a caller that has to reserve layout for the notice asks here
    // rather than restating the branches and drifting from them.
    hasNotice: unavailable || showLoadAction || (loaded && bounded),
  };
}

/**
 * States what is missing from a rendered payload, and loads the rest on demand.
 *
 * Trace payloads are kept whole in the durable store, so responses carry a
 * bounded preview and say so through `payload_meta`. Every view that renders a
 * payload — the Input/Output panes, and the Log view's message list — needs this
 * notice: a truncated payload shown without it reads as the whole record, which
 * is the one thing a trace must never do. Renders nothing when the value in hand
 * is already complete.
 */
function PayloadNotice({
  field,
  value,
  meta,
  query,
  requested,
  onLoad,
}: PayloadNoticeProps) {
  const { envelope, size, unavailable, showLoadAction, hasNotice } = derivePayloadState(
    value,
    meta,
    query,
  );

  // The value in hand is already complete: nothing to state.
  if (!hasNotice) return null;

  if (unavailable) {
    return (
      <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 p-2 content-compact text-default-secondary">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
        <span>
          This {field} payload was never stored — object storage was unavailable when the
          trace was written{size ? ` (${size})` : ""}. Only the excerpt below was captured.
          {envelope?.error || query.data?.error ? (
            <span className="block text-default-secondary">{envelope?.error ?? query.data?.error}</span>
          ) : null}
        </span>
      </div>
    );
  }

  if (showLoadAction) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary">
        <span>
          {envelope
            ? `The ${field} payload is stored outside the trace row`
            : `Showing a preview of ${field}`}
          {size ? ` — ${size} in full` : ""}
        </span>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-6 px-2 content-compact"
          disabled={query.isFetching}
          onClick={() => onLoad(field)}
        >
          {requested && query.isFetching ? "Loading…" : "Load full payload"}
        </Button>
        {query.isError ? (
          <span className="text-feedback-warning-primary">
            Could not load the payload. Retry, or check object storage.
          </span>
        ) : null}
      </div>
    );
  }

  // Reached only with the payload loaded and something having been held back
  // from the preview it replaced.
  return (
    <p className="content-compact text-default-secondary">
      Showing the complete {field} payload{size ? ` (${size})` : ""}.
    </p>
  );
}

interface PayloadPaneProps extends PayloadNoticeProps {
  toolCallCounts?: Map<string, number>;
  /** Needed to fetch the media this payload references. */
  traceId: string;
  spanId: string;
}

/**
 * One payload pane, with an explicit action to load the stored payload in full.
 */
function PayloadPane({
  field,
  value,
  meta,
  query,
  requested,
  onLoad,
  toolCallCounts,
  traceId,
  spanId,
}: PayloadPaneProps) {
  const { renderable, renderableIsSerializedPreview } = derivePayloadState(value, meta, query);
  const mediaReferences = useMemo(() => collectMediaReferences(renderable), [renderable]);

  return (
    <div className="space-y-2">
      <PayloadNotice
        field={field}
        value={value}
        meta={meta}
        query={query}
        requested={requested}
        onLoad={onLoad}
      />
      <MediaStrip
        traceId={traceId}
        spanId={spanId}
        field={field}
        references={mediaReferences}
      />
      <RenderPayloadReadable
        payload={renderable}
        serializedPreview={renderableIsSerializedPreview}
        toolCallCounts={toolCallCounts}
      />
    </div>
  );
}

interface MediaChipProps {
  traceId: string;
  spanId: string;
  field: PayloadField;
  reference: MediaReference;
}

/**
 * One media blob from a payload, loaded only when the user asks for it.
 *
 * A span can reference several megabytes of images; fetching them because a
 * pane was opened would undo the point of bounding payload responses. So the
 * chip shows what the reference knows — type, size — and fetches the bytes on
 * click, rendering an image inline or offering a download for anything else.
 */
function MediaChip({ traceId, spanId, field, reference }: MediaChipProps) {
  const [requested, setRequested] = useState(false);
  const query = useSpanPayloadMedia(traceId, spanId, field, reference.sha256, {
    enabled: requested,
  });
  const isImage = reference.content_type.startsWith("image/");
  const size = formatBytes(reference.size_bytes);
  const label = `${reference.content_type}${size ? ` · ${size}` : ""}`;

  // The query caches the Blob; the object URL is this component's to own. Each
  // mount derives its own and revokes only that one, so unmounting frees the URL
  // without invalidating the shared cache entry a later mount will reuse.
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!query.data) {
      setObjectUrl(null);
      return;
    }
    const url = URL.createObjectURL(query.data);
    setObjectUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [query.data]);

  if (reference.offload_failed) {
    return (
      <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 p-2 content-compact text-default-secondary">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
        <span>
          This {isImage ? "image" : "file"} was never stored — object storage was
          unavailable when the trace was written{size ? ` (${size})` : ""}.
          {reference.error ? (
            <span className="block text-default-secondary">{reference.error}</span>
          ) : null}
        </span>
      </div>
    );
  }

  if (objectUrl && isImage) {
    return (
      <figure className="space-y-1">
        <img
          src={objectUrl}
          alt={`${field} media (${label})`}
          className="max-h-64 max-w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover object-contain"
        />
        <figcaption className="content-caption text-default-secondary">{label}</figcaption>
      </figure>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary">
      <ImageIcon className="h-3.5 w-3.5 shrink-0" />
      <span>{label}</span>
      {objectUrl ? (
        <a
          href={objectUrl}
          download={`${reference.sha256.slice(0, 16)}`}
          className="text-accent underline"
        >
          Download
        </a>
      ) : (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-6 px-2 content-compact"
          disabled={query.isFetching}
          onClick={() => setRequested(true)}
        >
          {query.isFetching ? "Loading…" : isImage ? "Show image" : "Load file"}
        </Button>
      )}
      {query.isError ? (
        <span className="text-feedback-warning-primary">
          Could not load it. Retry, or check object storage.
        </span>
      ) : null}
    </div>
  );
}

/** The media a payload references, as a strip above the payload itself. */
function MediaStrip({
  traceId,
  spanId,
  field,
  references,
}: {
  traceId: string;
  spanId: string;
  field: PayloadField;
  references: MediaReference[];
}) {
  if (references.length === 0) return null;
  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2">
      <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">
        {pluralize(references.length, "attachment", "attachments")}
      </p>
      {references.map((reference) => (
        <MediaChip
          key={reference.sha256}
          traceId={traceId}
          spanId={spanId}
          field={field}
          reference={reference}
        />
      ))}
    </div>
  );
}

function RenderPayloadReadable({
  payload,
  serializedPreview = false,
  toolCallCounts,
}: {
  payload: unknown;
  /** The payload is an offload preview: a truncated serialisation, escapes still in it. */
  serializedPreview?: boolean;
  toolCallCounts?: Map<string, number>;
}) {
  if (payload == null) {
    return <p className="content-compact text-default-secondary">—</p>;
  }

  const normalized = typeof payload === "string" ? maybeParseJsonString(payload) : payload;

  if (typeof normalized === "string") {
    const roleBlocks = extractRoleBlocksFromSerializedText(normalized);
    if (roleBlocks.length > 0) {
      return <RolePromptBlocks blocks={roleBlocks} />;
    }

    // A plain string is decoded text (typically a model's answer) and keeps its
    // backslashes; only an offload preview is a known serialisation.
    const text = normalizeMarkdownLikeString(normalized, { decoded: !serializedPreview });
    if (!text) {
      return <p className="content-compact text-default-secondary">—</p>;
    }
    return (
      <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
        <ChatMarkdown
          content={text}
          className="content-compact text-default-secondary [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-compact [&_h1]:content-compact [&_h2]:content-compact [&_h3]:content-compact"
        />
      </div>
    );
  }

  if (typeof normalized === "number" || typeof normalized === "boolean") {
    return (
      <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary">
        {String(normalized)}
      </div>
    );
  }

  if (Array.isArray(normalized)) {
    if (normalized.length === 0) {
      return <p className="content-compact text-default-secondary">—</p>;
    }

    // Some spans expose AVAILABLE_TOOLS/TOOLS_CALLS as top-level arrays.
    // Render them with the same tool rows UI instead of raw JSON.
    const topLevelTools = extractToolDefsFromUnknown(normalized);
    if (topLevelTools.length > 0) {
      return <ToolRows tools={topLevelTools} callCounts={toolCallCounts} />;
    }

    return (
      <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary">
        {JSON.stringify(normalized, null, 2)}
      </pre>
    );
  }

  if (!hasObjectContent(normalized)) {
    return (
      <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary">
        {payloadValueToString(normalized)}
      </div>
    );
  }

  const entries = Object.entries(normalized);
  const visibleEntries = entries.slice(0, 20);

  return (
    <div className="space-y-2">
      {visibleEntries.map(([key, value]) => {
        const parsedValue = typeof value === "string" ? maybeParseJsonString(value) : value;
        const normalizedKey = normalizeToolKey(key);

        if (normalizedKey === "systemprompt" && typeof parsedValue === "string") {
          const text = normalizeMarkdownLikeString(parsedValue, { decoded: true });
          return (
            <RolePromptBlocks key={key} blocks={[{ role: "system", content: text }]} />
          );
        }

        if (normalizedKey === "messages") {
          const blocks = extractRoleBlocksFromMessagesArray(parsedValue);
          if (blocks.length > 0) {
            return <RolePromptBlocks key={key} blocks={blocks} />;
          }
        }

        if (normalizedKey === "files") {
          const files = extractFileRefs({ files: parsedValue });
          if (files.length > 0) {
            return (
              <div
                key={key}
                className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2"
              >
                <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-default-secondary">
                  {key}
                </p>
                <TraceFileChips files={files} />
              </div>
            );
          }
        }

        if (normalizedKey === "availabletools" || normalizedKey === "tools" || normalizedKey === "toolnames") {
          const tools = extractToolDefsFromUnknown(parsedValue);
          return (
            <div
              key={key}
              className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2"
            >
              <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-default-secondary">
                {key}
              </p>
              <ToolRows tools={tools} callCounts={toolCallCounts} />
            </div>
          );
        }

        if (normalizedKey === "toolcalls" || normalizedKey === "toolscalls" || normalizedKey === "toolcall") {
          const callCounts = extractToolCallCountsFromUnknown(parsedValue);
          const calledTools = Array.from(callCounts.keys()).map((name) => ({ name }));
          return (
            <div
              key={key}
              className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2"
            >
              <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-default-secondary">
                {key}
              </p>
              <ToolRows tools={calledTools} callCounts={callCounts} />
            </div>
          );
        }

        const isMarkdown = typeof parsedValue === "string";
        const isStructured = parsedValue != null && typeof parsedValue === "object";

        return (
          <div
            key={key}
            className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2"
          >
            <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-default-secondary">
              {key}
            </p>
            {isMarkdown ? (
              <ChatMarkdown
                content={normalizeMarkdownLikeString(String(parsedValue), { decoded: true })}
                className="content-compact text-default-secondary [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-compact [&_h1]:content-compact [&_h2]:content-compact [&_h3]:content-compact"
              />
            ) : isStructured ? (
              <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary">
                {JSON.stringify(parsedValue, null, 2)}
              </pre>
            ) : (
              <p className="content-compact text-default-secondary">{payloadValueToString(parsedValue)}</p>
            )}
          </div>
        );
      })}
      {entries.length > visibleEntries.length ? (
        <p className="content-compact text-default-secondary">
          Showing first {visibleEntries.length} properties ({entries.length - visibleEntries.length} hidden).
        </p>
      ) : null}
    </div>
  );
}

function extractToolCallCounts(span: LLMSpan): Map<string, number> {
  const merged = new Map<string, number>();
  const sources = [span.input, span.output, span.metadata];

  for (const source of sources) {
    const counts = extractToolCallCountsFromUnknown(source);
    for (const [name, count] of counts.entries()) {
      merged.set(name, (merged.get(name) ?? 0) + count);
    }
  }

  return merged;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Marker key of a payload stored in object storage instead of in the row. */
const OFFLOAD_ENVELOPE_KEY = "__xtm_offload__";

interface OffloadEnvelope {
  size_bytes?: number;
  preview?: string;
  offload_failed?: boolean;
  error?: string;
}

/**
 * Read an offload envelope, if that is what this payload is.
 *
 * A payload above the configured ceiling lives in object storage and the row
 * keeps a pointer plus a preview. Rendering the pointer verbatim would show the
 * user `__xtm_offload__`/`object_name` noise, so the panes detect it and show
 * the preview with a load action instead.
 */
function readOffloadEnvelope(value: unknown): OffloadEnvelope | null {
  const record = asRecord(value);
  if (!record || !(OFFLOAD_ENVELOPE_KEY in record)) return null;
  return {
    size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : undefined,
    preview: typeof record.preview === "string" ? record.preview : undefined,
    offload_failed: record.offload_failed === true,
    error: typeof record.error === "string" ? record.error : undefined,
  };
}

/**
 * Read a media reference, if that is what this value is.
 *
 * A payload's images and PDFs live in object storage; the payload keeps a
 * reference in their place. Rendering it verbatim would show the user
 * `__xtm_media__`/`object_name` noise instead of the picture.
 */
function readMediaReference(value: unknown): MediaReference | null {
  const record = asRecord(value);
  if (!record || !(MEDIA_REFERENCE_KEY in record)) return null;
  const sha256 = typeof record.sha256 === "string" ? record.sha256 : "";
  return {
    content_type:
      typeof record.content_type === "string" ? record.content_type : "application/octet-stream",
    size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : 0,
    sha256,
    object_name: typeof record.object_name === "string" ? record.object_name : undefined,
    offload_failed: record.offload_failed === true,
    error: typeof record.error === "string" ? record.error : undefined,
  };
}

/**
 * Every media reference anywhere in a payload, de-duplicated by digest.
 *
 * References sit wherever the image content part was — inside `messages`, inside
 * a tool result, under an Anthropic `source.data` — so the whole structure has
 * to be walked rather than a known key read.
 */
function collectMediaReferences(payload: unknown): MediaReference[] {
  const found = new Map<string, MediaReference>();

  const visit = (node: unknown, depth: number) => {
    if (depth > 12 || node == null || typeof node !== "object") return;
    const reference = readMediaReference(node);
    if (reference) {
      if (reference.sha256 && !found.has(reference.sha256)) {
        found.set(reference.sha256, reference);
      }
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((item) => visit(item, depth + 1));
      return;
    }
    Object.values(node as Record<string, unknown>).forEach((item) => visit(item, depth + 1));
  };

  visit(payload, 0);
  return [...found.values()];
}

function formatBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Recover `"<key>": "<value>"` from a JSON fragment that no longer parses.
 *
 * Legacy rows (written before trace payloads were stored whole) had their
 * *serialized* output truncated, so `span.output` came back as an unparseable
 * string and every field in it — reasoning included — became unreachable. The
 * key is usually intact inside the retained prefix, so pull it out by hand
 * rather than showing nothing. New rows never take this path.
 */
function salvageJsonStringField(fragment: string, key: string): string | null {
  const marker = `"${key}":`;
  const keyAt = fragment.indexOf(marker);
  if (keyAt < 0) return null;
  let i = keyAt + marker.length;
  while (i < fragment.length && /\s/.test(fragment[i])) i += 1;
  if (fragment[i] !== '"') return null;
  i += 1;
  let raw = "";
  while (i < fragment.length) {
    const ch = fragment[i];
    if (ch === "\\") {
      raw += fragment.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (ch === '"') break;
    raw += ch;
    i += 1;
  }
  if (!raw) return null;
  try {
    // Re-parse as a JSON string so escapes (\n, \uXXXX) render properly.
    return JSON.parse(`"${raw}"`) as string;
  } catch {
    return raw;
  }
}

/** Read a payload as an object, parsing a JSON-encoded string if needed. */
function asRecordLoose(value: unknown): Record<string, unknown> | null {
  const direct = asRecord(value);
  if (direct) return direct;
  if (typeof value === "string" && value.trimStart().startsWith("{")) {
    try {
      return asRecord(JSON.parse(value));
    } catch {
      return null;
    }
  }
  return null;
}

interface ReasoningText {
  text: string | null;
  /**
   * The payload fields the rendered text was actually drawn from, so the pane
   * can say when one of them is bounded. Reasoning lives under `output` for
   * most providers and under `metadata` for some, and only the field that
   * contributed clips what is on screen — naming the other one would report
   * truncation in prose that is whole.
   */
  fields: PayloadField[];
}

function extractReasoningText(output: unknown, metadata: unknown): ReasoningText {
  const outputRecord = asRecordLoose(output);
  const metadataRecord = asRecordLoose(metadata);
  const candidates: [unknown, PayloadField][] = [
    [outputRecord?.reasoning, "output"],
    [outputRecord?.thinking, "output"],
    [metadataRecord?.reasoning, "metadata"],
    [metadataRecord?.thinking, "metadata"],
  ];

  // Legacy fallback: an output string that never parsed still tends to carry
  // the reasoning key inside the prefix that survived truncation.
  for (const [value, key] of [
    [output, "reasoning"],
    [output, "thinking"],
  ] as const) {
    if (candidates.some(([c]) => typeof c === "string" && c.trim())) break;
    if (typeof value === "string" && !asRecord(value)) {
      candidates.push([salvageJsonStringField(value, key), "output"]);
    }
  }

  const parts: string[] = [];
  const fields: PayloadField[] = [];
  for (const [candidate, field] of candidates) {
    const part = typeof candidate === "string" ? candidate.trim() : "";
    // Same de-duplication as before — providers that mirror reasoning into
    // both payloads would otherwise render it twice. A field is credited only
    // when it contributed a part that survived, so a mirrored copy does not
    // pull in a notice for a payload the reader is not looking at.
    if (!part || parts.includes(part)) continue;
    parts.push(part);
    if (!fields.includes(field)) fields.push(field);
  }

  if (parts.length === 0) return { text: null, fields: [] };
  return { text: parts.join("\n\n"), fields };
}

function findFailingIteration(spans: LLMSpan[]): LLMSpan | null {
  const byId = new Map(spans.map((span) => [span.id, span]));

  const directIterationError = spans.find((span) => (
    span.type === "ITERATION" && (span.level === "ERROR" || Boolean(span.status_message?.trim()))
  ));
  if (directIterationError) return directIterationError;

  for (const span of spans) {
    if (span.level !== "ERROR" && !span.status_message?.trim()) continue;
    let parentId = span.parent_span_id;
    while (parentId) {
      const parent = byId.get(parentId);
      if (!parent) break;
      if (parent.type === "ITERATION") return parent;
      parentId = parent.parent_span_id;
    }
  }

  return [...spans]
    .filter((span) => span.type === "ITERATION")
    .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    .pop() ?? null;
}

const QUOTA_REASON_LABELS: Record<string, string> = {
  charged: "charged to quota",
  charged_no_account: "charged to the platform-wide quota (no account)",
  metered_by_parent: "metered by parent request",
  exempt_builtin: "exempt (built-in automation)",
  evaluation: "not metered (evaluation)",
  anonymous: "not metered (anonymous)",
  no_user: "not metered (no user)",
  unmetered_error: "unmetered (quota backend error)",
};

function quotaReasonLabel(reason: string | undefined): string | null {
  if (!reason || reason === "unknown") return null;
  return QUOTA_REASON_LABELS[reason] ?? reason;
}

interface SpanInspectorProps {
  span: LLMSpan | null;
  spans?: LLMSpan[];
  tags?: string[];
  trace?: LLMTrace | null;
  traces?: LLMTrace[];
  sessionSummary?: { totalTraces: number; totalSpans: number } | null;
}

export function SpanInspector({
  span,
  spans = [],
  tags,
  trace,
  traces = [],
  sessionSummary,
}: SpanInspectorProps) {
  const costDataAvailable = useCostDataAvailability();
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("preview");
  const [showFullToolArguments, setShowFullToolArguments] = useState(false);
  // Payload fields the user asked to see in full. Span responses carry bounded
  // previews (a stored payload can be megabytes), so the complete value is
  // fetched on demand.
  //
  // The request is tagged with the span it was made for and narrowed during
  // render, rather than cleared from an effect on span change: an effect runs
  // *after* the render that switched spans, and during that render the previous
  // selection's flags would still be set — firing an unrequested fetch of the
  // new span's full payload, which is exactly the multi-megabyte transfer this
  // whole mechanism exists to avoid.
  const [payloadRequest, setPayloadRequest] = useState<{
    spanId: string;
    fields: PayloadField[];
  }>({ spanId: "", fields: [] });
  const requestedFields =
    payloadRequest.spanId === (span?.id ?? "") ? payloadRequest.fields : [];
  const requestPayload = (field: PayloadField) => {
    const spanId = span?.id ?? "";
    setPayloadRequest((current) => {
      if (current.spanId !== spanId) return { spanId, fields: [field] };
      if (current.fields.includes(field)) return current;
      return { spanId, fields: [...current.fields, field] };
    });
  };
  const payloadQueries = {
    input: useSpanPayload(span?.trace_id ?? "", span?.id ?? "", "input", {
      enabled: requestedFields.includes("input"),
    }),
    output: useSpanPayload(span?.trace_id ?? "", span?.id ?? "", "output", {
      enabled: requestedFields.includes("output"),
    }),
    metadata: useSpanPayload(span?.trace_id ?? "", span?.id ?? "", "metadata", {
      enabled: requestedFields.includes("metadata"),
    }),
  } as const;
  // The loaded payload wins over the preview everywhere it is consumed — the
  // Output pane, and the Reasoning pane that reads `output.reasoning`.
  const effectiveInput = payloadQueries.input.data?.value ?? span?.input;
  const effectiveOutput = payloadQueries.output.data?.value ?? span?.output;
  const effectiveMetadata = payloadQueries.metadata.data?.value ?? span?.metadata;
  const isSyntheticSession = useMemo(() => {
    const metadata = asRecord(span?.metadata);
    return metadata?.synthetic === true && metadata?.synthetic_kind === "session";
  }, [span?.metadata]);
  const isAgentLevel = useMemo(() => {
    if (!span) return false;
    const metadata = span.metadata;
    if (!metadata || typeof metadata !== "object") return false;
    return metadata.synthetic_root === true || metadata.observation_kind === "trace_root";
  }, [span]);
  const isLoopLevel = useMemo(() => {
    if (!span?.name) return false;
    const normalized = span.name.trim().toLowerCase().replace(/[_\s]+/g, "");
    return normalized === "agentloop";
  }, [span]);
  const directChildren = useMemo(() => {
    if (!span) return [];
    return spans
      .filter((candidate) => candidate.parent_span_id === span.id)
      .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  }, [span, spans]);
  const descendantSummary = useMemo(() => {
    if (!span) {
      return { total: 0, iterations: 0, generations: 0, tools: 0 };
    }
    const byParent = new Map<string, LLMSpan[]>();
    for (const candidate of spans) {
      const parentId = candidate.parent_span_id;
      if (!parentId) continue;
      const siblings = byParent.get(parentId);
      if (siblings) {
        siblings.push(candidate);
      } else {
        byParent.set(parentId, [candidate]);
      }
    }
    const stack = [...(byParent.get(span.id) ?? [])];
    let total = 0;
    let iterations = 0;
    let generations = 0;
    let tools = 0;
    while (stack.length > 0) {
      const current = stack.pop();
      if (!current) continue;
      total += 1;
      if (current.type === "ITERATION") iterations += 1;
      if (current.type === "GENERATION") generations += 1;
      if (current.type === "TOOL") tools += 1;
      const children = byParent.get(current.id);
      if (children?.length) stack.push(...children);
    }
    return { total, iterations, generations, tools };
  }, [span, spans]);
  const loopIterations = useMemo(() => {
    return directChildren.filter((child) => (
      child.type === "ITERATION" || child.name.trim().toLowerCase().startsWith("iteration-")
    ));
  }, [directChildren]);
  const spanTraceSource = useMemo(() => {
    const metadata = asRecord(span?.metadata);
    const candidate = metadata?.trace_source;
    if (typeof candidate === "string" && candidate.trim().length > 0) {
      return candidate;
    }
    return "—";
  }, [span?.metadata]);
  const showLogViewTab = useMemo(() => {
    return isLoopLevel;
  }, [isLoopLevel]);
  const tracesForMetrics = useMemo(
    () => (traces.length > 0 ? traces : (trace ? [trace] : [])),
    [trace, traces],
  );
  const sessionKpis = useMemo(() => {
    if (!isSyntheticSession) return null;

    const loadedTraceCount = tracesForMetrics.length;
    const totalTraceCount = sessionSummary?.totalTraces ?? loadedTraceCount;
    const loadedSpanCount = spans.length;
    const totalSpanCount = sessionSummary?.totalSpans ?? loadedSpanCount;
    const partialData = totalTraceCount > loadedTraceCount || totalSpanCount > loadedSpanCount;

    const totalInputTokens = tracesForMetrics.reduce((sum, item) => sum + item.input_tokens, 0);
    const totalOutputTokens = tracesForMetrics.reduce((sum, item) => sum + item.output_tokens, 0);
    const totalTokens = tracesForMetrics.reduce(
      (sum, item) => sum + (item.total_tokens || (item.input_tokens + item.output_tokens)),
      0,
    );
    const totalCost = tracesForMetrics.reduce((sum, item) => sum + item.total_cost, 0);
    const errorTraceCount = tracesForMetrics.filter((item) => item.status === "error").length;
    const okTraceCount = tracesForMetrics.filter((item) => item.status === "ok").length;

    const durations = tracesForMetrics
      .map((item) => item.duration_ms)
      .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
    const avgDurationMs = durations.length > 0
      ? durations.reduce((sum, value) => sum + value, 0) / durations.length
      : null;
    const p95DurationMs = percentile(durations, 0.95);
    const cumulativeDurationMs = durations.reduce((sum, value) => sum + value, 0);

    const startedAtValues = tracesForMetrics
      .map((item) => Date.parse(item.started_at))
      .filter((value) => Number.isFinite(value));
    const endedAtValues = tracesForMetrics
      .map((item) => Date.parse(item.ended_at ?? item.started_at))
      .filter((value) => Number.isFinite(value));
    const firstStartedAtMs = startedAtValues.length > 0 ? Math.min(...startedAtValues) : null;
    const lastEndedAtMs = endedAtValues.length > 0 ? Math.max(...endedAtValues) : null;
    const wallClockDurationMs = firstStartedAtMs != null && lastEndedAtMs != null
      ? Math.max(0, lastEndedAtMs - firstStartedAtMs)
      : null;

    const modelCounts = new Map<string, number>();
    const providerCounts = new Map<string, number>();
    const uniqueAgentKeys = new Set<string>();

    tracesForMetrics.forEach((item) => {
      const modelLabel = item.model?.trim() || "default";
      const providerLabel = resolveProviderFromMetadata(item.metadata) ?? "unknown";
      modelCounts.set(modelLabel, (modelCounts.get(modelLabel) ?? 0) + 1);
      providerCounts.set(providerLabel, (providerCounts.get(providerLabel) ?? 0) + 1);

      const agentKey = item.agent_name?.trim() || item.agent_id || null;
      if (agentKey) uniqueAgentKeys.add(agentKey);
    });

    const topModels = Array.from(modelCounts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3);
    const topProviders = Array.from(providerCounts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3);

    const iterationCount = spans.filter((item) => item.type === "ITERATION").length;
    const generationCount = spans.filter((item) => item.type === "GENERATION").length;
    const toolSpanCount = spans.filter((item) => item.type === "TOOL").length;

    return {
      loadedTraceCount,
      totalTraceCount,
      loadedSpanCount,
      totalSpanCount,
      partialData,
      totalInputTokens,
      totalOutputTokens,
      totalTokens,
      totalCost,
      errorTraceCount,
      okTraceCount,
      avgDurationMs,
      p95DurationMs,
      cumulativeDurationMs,
      firstStartedAtMs,
      lastEndedAtMs,
      wallClockDurationMs,
      uniqueAgents: uniqueAgentKeys.size,
      uniqueModels: modelCounts.size,
      uniqueProviders: providerCounts.size,
      iterationCount,
      generationCount,
      toolSpanCount,
      topModels,
      topProviders,
    };
  }, [isSyntheticSession, sessionSummary?.totalSpans, sessionSummary?.totalTraces, spans, tracesForMetrics]);

  // Copy actions operate on whatever the pane currently shows: the preview
  // until the user loads the payload, the whole thing afterwards.
  const inputText = useMemo(() => payloadToText(effectiveInput), [effectiveInput]);
  const outputText = useMemo(() => payloadToText(effectiveOutput), [effectiveOutput]);
  const toolArgumentsText = useMemo(() => payloadToText(span?.tool_arguments), [span?.tool_arguments]);
  const hasToolArguments = !!span?.tool_arguments && Object.keys(span.tool_arguments).length > 0;
  const isToolArgumentsLong = toolArgumentsText.length > TOOL_ARGUMENTS_PREVIEW_LIMIT;
  const toolArgumentsPreviewText = useMemo(() => {
    if (!isToolArgumentsLong) return toolArgumentsText;
    const hiddenCount = toolArgumentsText.length - TOOL_ARGUMENTS_PREVIEW_LIMIT;
    return `${toolArgumentsText.slice(0, TOOL_ARGUMENTS_PREVIEW_LIMIT)}\n… [preview truncated, ${hiddenCount.toLocaleString()} chars hidden]`;
  }, [isToolArgumentsLong, toolArgumentsText]);
  const toolCallCounts = useMemo(() => (span ? extractToolCallCounts(span) : new Map<string, number>()), [span]);
  const filesUsed = useMemo(() => {
    if (!span) return [];
    const own = extractFileRefs(span.input);
    const fromTrace = isAgentLevel ? extractFileRefs(trace?.input) : [];
    return dedupeFileRefs([...own, ...fromTrace]);
  }, [span, isAgentLevel, trace]);
  const filesProduced = useMemo(() => {
    if (!span) return [];
    const own = extractFileRefs(span.output);
    // Agent / loop / session-level views aggregate the files produced by
    // descendant tool spans; leaf spans only show their own output files.
    const aggregated = (isAgentLevel || isLoopLevel || isSyntheticSession)
      ? spans.flatMap((candidate) => extractFileRefs(candidate.output))
      : [];
    const usedIds = new Set(filesUsed.map((file) => file.file_id));
    return dedupeFileRefs([...own, ...aggregated]).filter((file) => !usedIds.has(file.file_id));
  }, [span, spans, isAgentLevel, isLoopLevel, isSyntheticSession, filesUsed]);
  const iterationActionConsumed = useMemo(() => {
    const metadata = asRecord(span?.metadata);
    return metadata?.quota_action_consumed === true;
  }, [span?.metadata]);
  const traceQuota = useMemo(() => {
    const metadata = asRecord(trace?.metadata);
    const consumed = metadata?.quota_actions_consumed;
    if (typeof consumed !== "number") return null;
    const reason = typeof metadata?.quota_reason === "string" ? metadata.quota_reason : undefined;
    return { consumed, reason };
  }, [trace?.metadata]);
  const sessionActionsConsumed = useMemo(() => {
    if (!isSyntheticSession) return null;
    let found = false;
    let total = 0;
    for (const item of tracesForMetrics) {
      const metadata = asRecord(item.metadata);
      const consumed = metadata?.quota_actions_consumed;
      if (typeof consumed === "number") {
        found = true;
        total += consumed;
      }
    }
    return found ? total : null;
  }, [isSyntheticSession, tracesForMetrics]);
  const reasoning = useMemo(
    () => extractReasoningText(effectiveOutput, effectiveMetadata),
    [effectiveOutput, effectiveMetadata],
  );
  const failingIteration = useMemo(() => findFailingIteration(spans), [spans]);
  const resolvedTraceErrorMessage = trace?.error_message?.trim()
    || span?.status_message?.trim()
    || failingIteration?.status_message?.trim()
    || null;
  const intermediateSteps = useMemo(() => {
    if (!span) return [] as LLMSpan[];

    const byParent = new Map<string, LLMSpan[]>();
    for (const candidate of spans) {
      const parentId = candidate.parent_span_id;
      if (!parentId) continue;
      const siblings = byParent.get(parentId);
      if (siblings) {
        siblings.push(candidate);
      } else {
        byParent.set(parentId, [candidate]);
      }
    }

    const stack = [...(byParent.get(span.id) ?? [])];
    const ordered: LLMSpan[] = [];
    while (stack.length > 0) {
      const current = stack.shift();
      if (!current) continue;
      if (current.type === "ITERATION" || current.type === "GENERATION" || current.type === "TOOL") {
        ordered.push(current);
      }
      const children = (byParent.get(current.id) ?? []).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
      if (children.length > 0) {
        stack.unshift(...children);
      }
    }
    return ordered;
  }, [span, spans]);
  // Built from the *effective* payloads, not `span.input` / `span.output`: those
  // are the bounded previews, so reading them here left the Log view showing
  // clipped messages even after the user had loaded the full payload in the
  // Preview tab — the loaded value was in hand and ignored.
  const messages = useMemo(
    () => (span ? extractMessages(effectiveInput, effectiveOutput) : []),
    [span, effectiveInput, effectiveOutput],
  );
  const provider = useMemo(() => {
    if (!span) return null;
    return resolveProviderFromMetadata(span.metadata)
      ?? resolveProviderFromMetadata(trace?.metadata);
  }, [span, trace]);
  const effectiveDurationMs = sessionKpis?.wallClockDurationMs ?? span?.duration_ms ?? null;
  const providerBadge = sessionKpis
    ? (sessionKpis.uniqueProviders > 1
      ? `${sessionKpis.uniqueProviders} ${pluralize(sessionKpis.uniqueProviders, "provider")}`
      : (sessionKpis.topProviders[0]?.[0] ?? "unknown"))
    : (provider ?? "unknown");
  const modelBadge = sessionKpis
    ? `${sessionKpis.uniqueModels} ${pluralize(sessionKpis.uniqueModels, "model")}`
    : (span?.model ?? "default");
  const tokenBadge = sessionKpis
    ? `${formatCompactNumber(sessionKpis.totalInputTokens)} prompt → ${formatCompactNumber(sessionKpis.totalOutputTokens)} completion`
    : `${formatCompactNumber(span?.input_tokens ?? 0)} prompt → ${formatCompactNumber(span?.output_tokens ?? 0)} completion`;
  // Compact cost and tokens, the exact figures on hover (UX B3/B4 on #10529).
  const badgeInput = sessionKpis ? sessionKpis.totalInputTokens : (span?.input_tokens ?? 0);
  const badgeOutput = sessionKpis ? sessionKpis.totalOutputTokens : (span?.output_tokens ?? 0);
  const tokenBadgeExact = exactMetricValue(badgeInput) || exactMetricValue(badgeOutput)
    ? `${exactMetricValue(badgeInput) ?? badgeInput} prompt → ${exactMetricValue(badgeOutput) ?? badgeOutput} completion`
    : undefined;
  const badgeCost = sessionKpis ? sessionKpis.totalCost : (span?.total_cost ?? null);
  const costBadge = badgeCost != null ? formatCompactCost(badgeCost) : "No cost";
  const costBadgeExact = badgeCost != null ? exactCompactCost(badgeCost) : undefined;

  useEffect(() => {
    if (!showLogViewTab && inspectorTab === "logview") {
      setInspectorTab("preview");
    }
  }, [showLogViewTab, inspectorTab]);

  useEffect(() => {
    setShowFullToolArguments(false);
  }, [span?.id]);

  const copyToClipboard = async (text: string) => {
    if (!text || text === "—") return;
    await navigator.clipboard.writeText(text);
  };

  if (!span) {
    return (
      <Card>
        <CardContent className="py-8 text-center content-base text-default-secondary">
          Select a span to inspect its input/output and metadata.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="space-y-3 p-0">
        <div className="border-b border-elevation-subtle-soft px-3 py-2">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <p className="truncate text-xl font-semibold">{span.name}</p>
            <Badge variant="secondary">{span.type}</Badge>
            <Badge variant={span.level === "ERROR" ? "destructive" : "outline"}>{span.level}</Badge>
          </div>
          <div className="flex flex-wrap items-center gap-2 content-compact">
            <Badge variant="outline">Latency: {formatDuration(effectiveDurationMs)}</Badge>
            <Badge variant="outline">Model: {modelBadge}</Badge>
            <Badge variant="outline">Provider: {providerBadge}</Badge>
            <Badge variant="outline" title={costDataAvailable ? costBadgeExact : undefined}>{costDataAvailable ? costBadge : <MaskedCost className="inline-flex" />}</Badge>
            <Badge variant="outline" title={tokenBadgeExact}>{tokenBadge}</Badge>
            {iterationActionConsumed ? (
              <Badge
                variant="outline"
                className="border-feedback-alert-primary/40 text-feedback-warning-primary light:border-feedback-alert-primary/40 light:text-feedback-warning-primary"
              >
                <Coins className="mr-1 h-3 w-3" />
                1 platform action
              </Badge>
            ) : null}
            {isAgentLevel && traceQuota ? (
              <Tip content={quotaReasonLabel(traceQuota.reason)}>
                <Badge
                  variant="outline"
                  className="border-feedback-alert-primary/40 text-feedback-warning-primary light:border-feedback-alert-primary/40 light:text-feedback-warning-primary"
                >
                  <Coins className="mr-1 h-3 w-3" />
                  {traceQuota.consumed} {pluralize(traceQuota.consumed, "platform action")}
                </Badge>
              </Tip>
            ) : null}
          </div>
        </div>

        {/* Trace-level tags */}
        {tags && tags.length > 0 && (
          <div className="px-3">
            <p className="mb-1.5 content-base font-medium">Tags</p>
            <div className="flex flex-wrap gap-1.5">
              {tags.map((tag) => {
                const hue = tagHue(tag);
                return (
                  <span
                    key={tag}
                    className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
                    style={{
                      backgroundColor: `hsl(${hue} 60% 55% / 0.13)`,
                      color: `hsl(${hue} 60% 55%)`,
                      border: `1px solid hsl(${hue} 60% 55% / 0.27)`,
                    }}
                  >
                    {tag}
                  </span>
                );
              })}
            </div>
          </div>
        )}

        {sessionKpis ? (
          <div className="space-y-2 border-t border-elevation-subtle-soft px-3 pt-3">
            <div className="flex items-center justify-between gap-2">
              <p className="content-base font-medium">Conversation KPIs</p>
              <Badge variant="outline">
                {formatCompactNumber(sessionKpis.loadedTraceCount)} / {formatCompactNumber(sessionKpis.totalTraceCount)} traces
              </Badge>
            </div>
            {sessionKpis.partialData ? (
              <p className="content-compact text-default-secondary">
                Showing loaded traces/spans only ({formatCompactNumber(sessionKpis.loadedTraceCount)} traces, {formatCompactNumber(sessionKpis.loadedSpanCount)} spans) because this session exceeds the explorer page limit.
              </p>
            ) : null}
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              <KpiTile
                label="Trace Coverage"
                value={`${formatCompactNumber(sessionKpis.totalTraceCount)} traces`}
                hint={`${formatCompactNumber(sessionKpis.errorTraceCount)} errors · ${formatPercent(sessionKpis.totalTraceCount > 0 ? sessionKpis.errorTraceCount / sessionKpis.totalTraceCount : 0)} error rate`}
              />
              <KpiTile
                label="Token Usage"
                value={`${formatCompactNumber(sessionKpis.totalTokens)} total`}
                hint={`${formatCompactNumber(sessionKpis.totalInputTokens)} in · ${formatCompactNumber(sessionKpis.totalOutputTokens)} out`}
              />
              <KpiTile
                label="Cost"
                value={costDataAvailable ? <span title={exactCompactCost(sessionKpis.totalCost)}>{formatCompactCost(sessionKpis.totalCost)}</span> : <MaskedCost />}
                hint={`${formatCompactNumber(sessionKpis.okTraceCount)} ${pluralize(sessionKpis.okTraceCount, "trace")} ok`}
              />
              <KpiTile
                label="Wall Time"
                value={formatDuration(sessionKpis.wallClockDurationMs)}
                hint={sessionKpis.firstStartedAtMs != null ? `From ${formatShortDateTime(new Date(sessionKpis.firstStartedAtMs).toISOString())}` : undefined}
              />
              <KpiTile
                label="Latency"
                value={`Avg ${formatDuration(sessionKpis.avgDurationMs)}`}
                hint={`P95 ${formatDuration(sessionKpis.p95DurationMs)} · Σ ${formatDuration(sessionKpis.cumulativeDurationMs)}`}
              />
              <KpiTile
                label="Activity"
                value={`${formatCompactNumber(sessionKpis.totalSpanCount)} spans`}
                hint={`${formatCompactNumber(sessionKpis.iterationCount)} ITERATION · ${formatCompactNumber(sessionKpis.generationCount)} GENERATION · ${formatCompactNumber(sessionKpis.toolSpanCount)} TOOL`}
              />
              {sessionActionsConsumed != null ? (
                <KpiTile
                  label="Platform Actions"
                  value={`${formatCompactNumber(sessionActionsConsumed)} consumed`}
                  hint={`Across ${formatCompactNumber(sessionKpis.loadedTraceCount)} loaded ${pluralize(sessionKpis.loadedTraceCount, "trace")}`}
                />
              ) : null}
            </div>
            <div className="grid gap-2 content-compact text-default-secondary sm:grid-cols-2">
              <p><span className="text-default-secondary">Agents:</span> {sessionKpis.uniqueAgents}</p>
              <p><span className="text-default-secondary">Models:</span> {sessionKpis.uniqueModels}</p>
              <p><span className="text-default-secondary">Providers:</span> {sessionKpis.uniqueProviders}</p>
              <p><span className="text-default-secondary">Session end:</span> {sessionKpis.lastEndedAtMs != null ? formatShortDateTime(new Date(sessionKpis.lastEndedAtMs).toISOString()) : "—"}</p>
            </div>
            {sessionKpis.topModels.length > 0 ? (
              <div>
                <p className="mb-1 content-compact text-default-secondary">Top models</p>
                <div className="flex flex-wrap gap-1.5">
                  {sessionKpis.topModels.map(([name, count]) => (
                    <Badge key={`model-${name}`} variant="outline">{name} · {count}</Badge>
                  ))}
                </div>
              </div>
            ) : null}
            {sessionKpis.topProviders.length > 0 ? (
              <div>
                <p className="mb-1 content-compact text-default-secondary">Top providers</p>
                <div className="flex flex-wrap gap-1.5">
                  {sessionKpis.topProviders.map(([name, count]) => (
                    <Badge key={`provider-${name}`} variant="outline">{name} · {count}</Badge>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {isAgentLevel ? (
          <div className="space-y-2 border-t border-elevation-subtle-soft px-3 pt-3">
            <p className="content-base font-medium">Agent View</p>
            <div className="grid gap-2 content-compact text-default-secondary sm:grid-cols-2">
              <p><span className="text-default-secondary">Trace:</span> {trace?.name ?? span.name}</p>
              <p><span className="text-default-secondary">Source:</span> {trace?.source ?? spanTraceSource}</p>
              <p><span className="text-default-secondary">Direct children:</span> {directChildren.length}</p>
              <p><span className="text-default-secondary">Nested observations:</span> {descendantSummary.total}</p>
              <p><span className="text-default-secondary">Iterations:</span> {descendantSummary.iterations}</p>
              <p><span className="text-default-secondary">Generations:</span> {descendantSummary.generations}</p>
              <p><span className="text-default-secondary">Tools:</span> {descendantSummary.tools}</p>
              <p><span className="text-default-secondary">Status:</span> {trace?.status ?? span.level}</p>
              <p><span className="text-default-secondary">Failing iteration:</span> {failingIteration?.name ?? "—"}</p>
              {traceQuota ? (
                <p>
                  <span className="text-default-secondary">Platform actions:</span>{" "}
                  {traceQuota.consumed}
                  {quotaReasonLabel(traceQuota.reason)
                    ? ` — ${quotaReasonLabel(traceQuota.reason)}`
                    : ""}
                </p>
              ) : null}
            </div>
            {trace?.status === "error" && resolvedTraceErrorMessage ? (
              <div className="rounded-sm border border-feedback-error-primary/40 bg-feedback-error-primary/10 p-2.5 content-compact text-feedback-error-primary">
                <div className="mb-1 flex items-center gap-1.5 font-semibold text-feedback-error-primary">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  Trace Error
                </div>
                {/* The classified cause leads, because it is the part
                    a reader can act on; the raw text follows for the detail
                    the enum necessarily drops. `failure_cause` is absent on
                    traces recorded before the classifier existed, so the
                    heading falls back to the message alone rather than
                    labelling those "unattributed" — they were never
                    classified at all. */}
                {trace.failure_cause ? (
                  <p className="mb-1 font-medium text-feedback-error-primary">
                    {formatFailureCause(trace.failure_cause)}
                  </p>
                ) : null}
                <p className="whitespace-pre-wrap break-words">{resolvedTraceErrorMessage}</p>
              </div>
            ) : null}
          </div>
        ) : null}

        {isLoopLevel ? (
          <div className="space-y-2 border-t border-elevation-subtle-soft px-3 pt-3">
            <div className="flex items-center justify-between gap-2">
              <p className="content-base font-medium">Loop View</p>
              <Badge variant="outline">{loopIterations.length} iterations</Badge>
            </div>
            {loopIterations.length === 0 ? (
              <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary">
                No iteration spans are linked to this loop.
              </div>
            ) : (
              <div className="space-y-1.5">
                {loopIterations.map((iteration) => {
                  const iterationMetadata = asRecord(iteration.metadata);
                  const actionConsumed = iterationMetadata?.quota_action_consumed === true;
                  return (
                    <div
                      key={iteration.id}
                      className="grid gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary sm:grid-cols-5"
                    >
                      <p className="truncate text-default-secondary">{iteration.name}</p>
                      <p>{formatDuration(iteration.duration_ms)}</p>
                      <p>{formatCompactNumber(iteration.input_tokens ?? 0)} / {formatCompactNumber(iteration.output_tokens ?? 0)} tok</p>
                      <p title={costDataAvailable && iteration.total_cost != null ? exactCompactCost(iteration.total_cost) : undefined}>{costDataAvailable ? (iteration.total_cost != null ? formatCompactCost(iteration.total_cost) : "—") : <MaskedCost />}</p>
                      {actionConsumed ? (
                        <p className="flex items-center gap-1 text-feedback-warning-primary light:text-feedback-warning-primary">
                          <Coins className="h-3 w-3" />
                          1 action
                        </p>
                      ) : (
                        <p>—</p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ) : null}

        <div className="px-3">
          <div className="flex items-center justify-between gap-2 border-b border-elevation-subtle-soft">
            <div className="inline-flex">
              <button
                type="button"
                className={`border-b-2 px-3 py-1.5 content-base font-medium ${inspectorTab === "preview" ? "border-elevation-default text-default-primary" : "border-transparent text-default-secondary"}`}
                onClick={() => setInspectorTab("preview")}
              >
                Preview
              </button>
              {showLogViewTab ? (
                <button
                  type="button"
                  className={`border-b-2 px-3 py-1.5 content-base font-medium ${inspectorTab === "logview" ? "border-elevation-default text-default-primary" : "border-transparent text-default-secondary"}`}
                  onClick={() => setInspectorTab("logview")}
                >
                  Log View
                </button>
              ) : null}
            </div>
          </div>
        </div>

        {inspectorTab === "preview" ? (
          <>
            <div className="grid gap-2 px-3 content-compact text-default-secondary sm:grid-cols-2">
              <p><span className="text-default-secondary">Started:</span> {formatShortDateTime(span.started_at)}</p>
              <p><span className="text-default-secondary">Duration:</span> {formatDuration(span.duration_ms)}</p>
              <p><span className="text-default-secondary">Model:</span> {span.model ?? "—"}</p>
              <p><span className="text-default-secondary">Tool Call ID:</span> {span.tool_call_id ?? "—"}</p>
            </div>

            {filesUsed.length > 0 || filesProduced.length > 0 ? (
              <div className="px-3">
                <p className="mb-2 content-base font-medium">Files</p>
                {filesUsed.length > 0 ? (
                  <div className="mb-2">
                    <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-default-secondary">
                      Used as input ({filesUsed.length})
                    </p>
                    <TraceFileChips files={filesUsed} />
                  </div>
                ) : null}
                {filesProduced.length > 0 ? (
                  <div>
                    <p className="mb-1 content-caption font-semibold uppercase tracking-wider text-feedback-success-primary light:text-feedback-success-primary">
                      Produced ({filesProduced.length})
                    </p>
                    <TraceFileChips files={filesProduced} />
                  </div>
                ) : null}
              </div>
            ) : null}

            {hasToolArguments ? (
              <div className="px-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <p className="content-base font-medium">Tool Arguments</p>
                  <div className="flex items-center gap-2">
                    {isToolArgumentsLong ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => setShowFullToolArguments((prev) => !prev)}
                      >
                        {showFullToolArguments ? "Show preview" : "Show full"}
                      </Button>
                    ) : null}
                    <Button type="button" size="sm" variant="outline" onClick={() => copyToClipboard(toolArgumentsText)}>
                      <Copy className="mr-1 h-3.5 w-3.5" />
                      Copy
                    </Button>
                  </div>
                </div>
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary">
                  {showFullToolArguments ? toolArgumentsText : toolArgumentsPreviewText}
                </pre>
              </div>
            ) : null}

            {!sessionKpis ? (
              <>
                <div className="px-3">
                  <p className="mb-2 content-base font-medium">Input</p>
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <Button type="button" size="sm" variant="outline" onClick={() => copyToClipboard(inputText)}>
                      <Copy className="mr-1 h-3.5 w-3.5" />
                      Copy
                    </Button>
                  </div>
                  <PayloadPane
                    field="input"
                    value={effectiveInput}
                    meta={span.payload_meta?.input}
                    query={payloadQueries.input}
                    requested={requestedFields.includes("input")}
                    onLoad={requestPayload}
                    toolCallCounts={toolCallCounts}
                    traceId={span.trace_id}
                    spanId={span.id}
                  />
                </div>

                <div className="px-3 pb-3">
                  <p className="mb-2 content-base font-medium">Output</p>
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <Button type="button" size="sm" variant="outline" onClick={() => copyToClipboard(outputText)}>
                      <Copy className="mr-1 h-3.5 w-3.5" />
                      Copy
                    </Button>
                  </div>
                  <PayloadPane
                    field="output"
                    value={effectiveOutput}
                    meta={span.payload_meta?.output}
                    query={payloadQueries.output}
                    requested={requestedFields.includes("output")}
                    onLoad={requestPayload}
                    toolCallCounts={toolCallCounts}
                    traceId={span.trace_id}
                    spanId={span.id}
                  />
                </div>
              </>
            ) : null}

            {reasoning.text ? (
              <div className="px-3 pb-3">
                <p className="mb-2 content-base font-medium">Reasoning</p>
                {/* Reasoning is the one payload read as prose rather than as a
                    record, so an inline `… [truncated N chars]` at the end is
                    the easiest of them to skim straight past — it reads as the
                    model simply having stopped there. The notice goes above the
                    text, on the field the prose was actually drawn from, even
                    though the Output pane's own notice sits on this same tab.

                    `empty:hidden` on the wrapper because each notice renders
                    nothing when its payload is already whole, and the margin
                    would then show as a stray gap. Filtering by `hasNotice`
                    here instead would cost the component its React Compiler
                    memoization: `derivePayloadState` called in the body reads
                    as a possible mutation of `effectiveOutput`. */}
                <div className="mb-2 space-y-2 empty:hidden">
                  {reasoning.fields.map((field) => (
                    <PayloadNotice
                      key={field}
                      field={field}
                      value={field === "output" ? effectiveOutput : effectiveMetadata}
                      meta={span.payload_meta?.[field]}
                      query={payloadQueries[field]}
                      requested={requestedFields.includes(field)}
                      onLoad={requestPayload}
                    />
                  ))}
                </div>
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary">
                  {reasoning.text}
                </pre>
              </div>
            ) : null}

            {intermediateSteps.length > 0 ? (
              <div className="px-3 pb-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <p className="content-base font-medium">Intermediate Steps</p>
                  <CountBadge variant="outline">{intermediateSteps.length}</CountBadge>
                </div>
                <div className="space-y-1.5">
                  {intermediateSteps.slice(0, 30).map((step) => (
                    <div
                      key={step.id}
                      className="grid gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary sm:grid-cols-[auto,1fr,auto,auto]"
                    >
                      <Badge variant="secondary" className="h-fit">{step.type}</Badge>
                      <p className="truncate text-default-secondary">{step.name}</p>
                      <p>{formatDuration(step.duration_ms)}</p>
                      <p className={step.level === "ERROR" ? "text-feedback-error-primary" : ""}>{step.level}</p>
                    </div>
                  ))}
                  {intermediateSteps.length > 30 ? (
                    <p className="content-compact text-default-secondary">
                      Showing first 30 steps ({intermediateSteps.length - 30} hidden).
                    </p>
                  ) : null}
                </div>
              </div>
            ) : null}
          </>
        ) : (
          <div className="space-y-2 px-3 pb-3">
            <p className="content-base font-medium">
              Messages{" "}
              <span className="ml-1 content-compact text-default-secondary">({messages.length})</span>
            </p>
            {/* The messages are read out of the input and output payloads, so a
                bounded preview clips them — silently, without this. Both fields
                get a notice: either can be the truncated one, and each renders
                nothing when the value in hand is already complete. */}
            <PayloadNotice
              field="input"
              value={effectiveInput}
              meta={span.payload_meta?.input}
              query={payloadQueries.input}
              requested={requestedFields.includes("input")}
              onLoad={requestPayload}
            />
            <PayloadNotice
              field="output"
              value={effectiveOutput}
              meta={span.payload_meta?.output}
              query={payloadQueries.output}
              requested={requestedFields.includes("output")}
              onLoad={requestPayload}
            />
            {messages.length === 0 ? (
              <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-base text-default-secondary">
                No messages found in span input.
              </div>
            ) : (
              <div className="space-y-2">
                {messages.map((msg, i) => (
                  <RoleBlock
                    key={i}
                    role={msg.role}
                    content={messageContent(msg.content)}
                    markdown={false}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
