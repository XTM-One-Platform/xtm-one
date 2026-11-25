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

import { useState } from "react";
import { AlertTriangle, Box, Brain, ChevronDown, ChevronRight, Cpu, Eye, EyeOff, FolderTree, RefreshCw, Scan, Shield, Wrench } from "lucide-react";
import { exactCompactCost, formatCompactCost, formatCompactNumber } from "@/lib/utils";
import { formatDuration } from "@/lib/trace-utils";
import { MaskedCost } from "@/components/ui/masked-cost";
import { Tip } from "@/components/ui/tooltip";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import type { LLMSpan } from "@/types";

export interface SpanTreeNode extends LLMSpan {
  children: SpanTreeNode[];
}

const SPAN_TYPE_ICONS: Record<string, React.ElementType> = {
  SESSION: FolderTree,
  TRACE: Scan,
  GENERATION: Cpu,
  TOOL: Wrench,
  ITERATION: RefreshCw,
  ACTIVATION_GATE: Shield,
  MEMORY: Brain,
  SPAN: Box,
};

const MAX_DEPTH = 50;
const INDENT_STEP = 24;
const BASE_PADDING = 12;

interface SpanNodeProps {
  node: SpanTreeNode;
  depth: number;
  compact?: boolean;
  selectedSpanId?: string | null;
  onSelectSpan?: (spanId: string) => void;
  errorIterationIds?: ReadonlySet<string>;
}

export function SpanNode({ node, depth, compact = false, selectedSpanId, onSelectSpan, errorIterationIds }: SpanNodeProps) {
  const costDataAvailable = useCostDataAvailability();
  const [expanded, setExpanded] = useState(true);
  const [showInput, setShowInput] = useState(false);
  const [showOutput, setShowOutput] = useState(false);
  const [showToolArguments, setShowToolArguments] = useState(false);
  if (depth >= MAX_DEPTH) return null;
  const hasChildren = node.children.length > 0;
  const Icon = SPAN_TYPE_ICONS[node.type] ?? Box;
  const paddingLeft = depth * INDENT_STEP + BASE_PADDING;
  const hasInput = Object.keys(node.input ?? {}).length > 0;
  const hasOutput = Object.keys(node.output ?? {}).length > 0;
  const hasToolArguments = Object.keys(node.tool_arguments ?? {}).length > 0;
  const hasIO = hasInput || hasOutput;
  const isIODetailsOpen = showInput || showOutput;
  const isGeneration = node.type === "GENERATION";
  const isTool = node.type === "TOOL";
  const isSelected = selectedSpanId === node.id;
  const isErrorIteration = node.type === "ITERATION" && (errorIterationIds?.has(node.id) ?? false);
  const hasErrorSignal = node.level === "ERROR" || Boolean(node.status_message?.trim()) || isErrorIteration;
  const showDetails = !compact && (isGeneration || isTool || hasInput || hasOutput || hasToolArguments);
  const canSelect = Boolean(onSelectSpan);
  const rowClass = isErrorIteration
    ? "bg-feedback-error-primary/[0.08] border-l-2 border-feedback-error-primary"
    : isSelected
      ? "bg-accent/10"
      : "";

  return (
    <div>
      <div
        className={`w-full text-left border-b border-elevation-subtle-soft transition-colors hover:bg-elevation-hover ${canSelect ? "cursor-pointer" : ""} ${rowClass}`}
        onClick={canSelect ? () => onSelectSpan?.(node.id) : undefined}
        role={canSelect ? "button" : undefined}
        tabIndex={canSelect ? 0 : undefined}
        onKeyDown={canSelect ? (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onSelectSpan?.(node.id);
          }
        } : undefined}
      >
        <div
          className={`flex items-center gap-2 px-3 ${compact ? "py-1.5 content-caption" : "py-2"}`}
          style={{ paddingLeft: `${paddingLeft}px` }}
        >
          {hasChildren ? (
            <button
              type="button"
              aria-label={expanded ? "Collapse span" : "Expand span"}
              className="inline-flex h-4 w-4 items-center justify-center rounded-sm text-default-secondary hover:bg-elevation-hover"
              onClick={(event) => {
                event.stopPropagation();
                setExpanded((prev) => !prev);
              }}
            >
              {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            </button>
          ) : (
            <span className="w-3.5" />
          )}
          <Icon className="h-3.5 w-3.5 text-accent shrink-0" />
          <span
            className={`font-medium min-w-0 flex-1 ${compact ? "whitespace-normal break-words leading-tight content-caption" : "truncate"}`}
          >
            {node.name}
          </span>
          <span className={`${compact ? "text-[9px]" : "content-compact"} text-default-secondary uppercase shrink-0`}>{node.type}</span>
          {isErrorIteration ? (
            <Tip content={node.status_message?.trim() || "Iteration linked to trace error"}>
              <span className="inline-flex items-center gap-1 rounded-sm border border-feedback-error-primary/40 bg-feedback-error-primary/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-feedback-error-primary shrink-0">
                <AlertTriangle className="h-3 w-3" />
                Error Iteration
              </span>
            </Tip>
          ) : null}
          {!isErrorIteration && hasErrorSignal ? (
            <Tip content={node.status_message?.trim() || "Error signal"}>
              <AlertTriangle className="h-3.5 w-3.5 text-feedback-error-primary shrink-0" />
            </Tip>
          ) : null}
          {!compact && hasIO ? (
            <Tip content={isIODetailsOpen ? "Hide input/output" : "Show input/output"}>
              <button
                type="button"
                className="rounded-sm border border-elevation-subtle-soft p-1 text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
                onClick={(event) => {
                  event.stopPropagation();
                  const shouldOpen = !isIODetailsOpen;
                  setShowInput(shouldOpen && hasInput);
                  setShowOutput(shouldOpen && hasOutput);
                }}
              >
                {isIODetailsOpen ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </Tip>
          ) : null}
          <span className={`ml-auto ${compact ? "text-[9px]" : "content-compact"} text-default-secondary whitespace-nowrap shrink-0`}>
            {formatDuration(node.duration_ms)}
          </span>
          {node.type === "GENERATION" ? (
            <>
              <span className={`${compact ? "text-[9px]" : "content-compact"} text-default-secondary whitespace-nowrap shrink-0`}>
                {formatCompactNumber((node.input_tokens ?? 0) + (node.output_tokens ?? 0))} tok
              </span>
              <span
                className={`${compact ? "text-[9px]" : "content-compact"} text-default-secondary whitespace-nowrap shrink-0`}
                title={costDataAvailable ? exactCompactCost(node.total_cost) : undefined}
              >
                {costDataAvailable ? (node.total_cost == null ? "—" : formatCompactCost(node.total_cost)) : <MaskedCost className="inline-flex" />}
              </span>
            </>
          ) : null}
        </div>
      </div>

      {showDetails ? (
        <div className="px-3 py-2 space-y-2 border-b border-elevation-subtle-soft bg-elevation-hover" style={{ paddingLeft: `${paddingLeft + 24}px` }}>
          {isGeneration ? (
            <div className="grid gap-2 content-compact text-default-secondary sm:grid-cols-3">
              <p><span className="text-default-secondary">Model:</span> {node.model ?? "—"}</p>
              <p><span className="text-default-secondary">Stop reason:</span> {node.stop_reason ?? "—"}</p>
              <p><span className="text-default-secondary">TTFT:</span> {node.time_to_first_token != null ? `${node.time_to_first_token.toFixed(1)}ms` : "—"}</p>
            </div>
          ) : null}

          {isTool ? (
            <div className="grid gap-2 content-compact text-default-secondary sm:grid-cols-2">
              <p><span className="text-default-secondary">Tool call ID:</span> {node.tool_call_id ?? "—"}</p>
              <p><span className="text-default-secondary">Result source:</span> output payload</p>
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2">
            {hasInput ? (
              <button
                type="button"
                className="rounded-sm border border-elevation-subtle-soft px-2 py-1 content-compact text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
                onClick={() => setShowInput((prev) => !prev)}
              >
                {showInput ? "Hide Input" : "Show Input"}
              </button>
            ) : null}
            {isTool && hasToolArguments ? (
              <button
                type="button"
                className="rounded-sm border border-elevation-subtle-soft px-2 py-1 content-compact text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
                onClick={() => setShowToolArguments((prev) => !prev)}
              >
                {showToolArguments ? "Hide Arguments" : "Show Arguments"}
              </button>
            ) : null}
            {hasOutput ? (
              <button
                type="button"
                className="rounded-sm border border-elevation-subtle-soft px-2 py-1 content-compact text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
                onClick={() => setShowOutput((prev) => !prev)}
              >
                {showOutput ? (isTool ? "Hide Result" : "Hide Output") : (isTool ? "Show Result" : "Show Output")}
              </button>
            ) : null}
          </div>

          {showInput && hasInput ? (
            <pre className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary whitespace-pre-wrap break-words">
              {JSON.stringify(node.input, null, 2)}
            </pre>
          ) : null}
          {showToolArguments && isTool && hasToolArguments ? (
            <pre className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary whitespace-pre-wrap break-words">
              {JSON.stringify(node.tool_arguments, null, 2)}
            </pre>
          ) : null}
          {showOutput && hasOutput ? (
            <pre className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact text-default-secondary whitespace-pre-wrap break-words">
              {JSON.stringify(node.output, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : null}

      {hasChildren && expanded ? (
        <div>
          {node.children.map((child) => (
            <SpanNode
              key={child.id}
              node={child}
              depth={depth + 1}
              compact={compact}
              selectedSpanId={selectedSpanId}
              onSelectSpan={onSelectSpan}
              errorIterationIds={errorIterationIds}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
