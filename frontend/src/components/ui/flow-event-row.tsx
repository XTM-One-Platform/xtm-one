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
 * FlowEventRow - one entry in the Live Map activity feed.
 *
 * A compact, vertically-centered run-style row (modeled on RunCard): a status
 * icon, an optional agent-icon chip, a two-line text block (title + metadata,
 * both single-line truncated), and a vertically-centered relative timestamp
 * with an absolute-time tooltip.
 *
 * Shared by the agent flow drawer (Recent runs) and the /flows/map activity
 * feed (MapTab): the status label goes through `t_i18n` and both times are
 * formatted in the viewer's locale here, once, so every surface follows.
 */

import { memo } from "react";
import {
  ArrowRight,
  ArrowRightLeft,
  Bot,
  Circle,
  ListChecks,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { ICON_MAP, STATUS_CONFIG, runStatusConfig, type StatusConfig } from "@/lib/constants";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { STATUS_LABELS as WORK_ITEM_STATUS_LABELS } from "@/lib/work-item-context";
import { cn, timeAgo } from "@/lib/utils";

type FlowEventKind = "run" | "delegation" | "work_item";

export interface FlowEvent {
  id: string;
  kind: FlowEventKind;
  status: string;
  /** `run_outcome.run_outcome` of a run or a delegation: `interrupted` reads "Interrupted". */
  outcome?: string | null;
  title: string;
  /** Source agent name (delegations). */
  sourceLabel?: string;
  /** Target agent name (delegations). */
  targetLabel?: string;
  /** Fallback context line when there is no source -> target pair. */
  context?: string;
  /** Agent Lucide icon key, rendered as a chip when it resolves. */
  iconKey?: string;
  /** Secondary truncated text (e.g. a run result summary). */
  summary?: string;
  timestamp: string | null;
  href?: string;
}

const KIND_ICON: Record<FlowEventKind, React.ElementType> = {
  run: Bot,
  delegation: ArrowRightLeft,
  work_item: ListChecks,
};

function resolveStatus(status: string, outcome?: string | null): StatusConfig {
  if (outcome === "interrupted") return runStatusConfig({ status, outcome });
  return (
    STATUS_CONFIG[status] ?? {
      icon: Circle,
      color: "text-default-secondary",
      badge: "outline",
      // A work item status reads its own catalog key ("open" is "Open (status)",
      // never the verb "Open"); anything else is humanised (translated
      // by the caller).
      label:
        (WORK_ITEM_STATUS_LABELS as Record<string, string>)[status] ??
        status.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()),
    }
  );
}

/**
 * Memoised: a row and its time tooltip render again only when
 * its event or its handler changes, not on every render of the page around it
 * (a layout switch of the agent map). Pass a stable `onOpen`.
 */
export const FlowEventRow = memo(function FlowEventRow({
  event,
  onOpen,
}: {
  event: FlowEvent;
  onOpen?: (href: string) => void;
}) {
  const { t_i18n, locale, intl } = useFormatter();
  const cfg = resolveStatus(event.status, event.outcome);
  const StatusIcon = cfg.icon;
  const KindIcon = KIND_ICON[event.kind];
  const AgentIcon = event.iconKey ? (ICON_MAP[event.iconKey] ?? Bot) : null;
  const isRunning = event.status === "running";
  const clickable = !!event.href;
  const hasHandover = !!event.sourceLabel && !!event.targetLabel;
  const hasPrimary = hasHandover || !!event.context;

  return (
    <div
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={() => clickable && onOpen?.(event.href!)}
      onKeyDown={(e) => {
        if (clickable && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onOpen?.(event.href!);
        }
      }}
      className={cn(
        // min-w-0 + overflow-hidden: a long work item title is cut inside the
        // row, never pushed past the card.
        "group flex min-w-0 items-center justify-between gap-3 overflow-hidden rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-2",
        clickable &&
          "cursor-pointer transition-colors hover:border-accent/30 hover:bg-elevation-hover",
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {/* Status icon */}
        <StatusIcon
          className={cn(
            "h-4 w-4 shrink-0",
            cfg.color,
            isRunning && "animate-spin",
          )}
        />

        {/* Agent icon chip (when it resolves) */}
        {AgentIcon && (
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
            <AgentIcon className="h-3.5 w-3.5 text-accent" />
          </div>
        )}

        {/* Text block */}
        <div className="min-w-0 flex-1">
          {/* Title row */}
          <div className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate content-compact font-medium" title={event.title}>{event.title}</span>
            <Badge
              variant={cfg.badge}
              className="h-4 shrink-0 px-1.5 text-[9px]"
            >
              {t_i18n(cfg.label)}
            </Badge>
          </div>

          {/* Metadata row */}
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11px] text-default-secondary">
            <KindIcon className="h-3 w-3 shrink-0" />
            {hasHandover ? (
              <span className="flex min-w-0 items-center gap-1 truncate">
                <span className="truncate">{event.sourceLabel}</span>
                <ArrowRight className="h-3 w-3 shrink-0" />
                <span className="truncate">{event.targetLabel}</span>
              </span>
            ) : event.context ? (
              <span className="min-w-0 truncate">{event.context}</span>
            ) : null}
            {event.summary && (
              <>
                {hasPrimary && (
                  <span className="shrink-0 text-default-disabled">·</span>
                )}
                <span className="min-w-0 flex-1 truncate text-default-secondary/80">
                  {event.summary}
                </span>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Vertically-centered relative time with absolute-time tooltip */}
      {event.timestamp && (
        <Tip content={formatShortDateTimeIntl(intl, event.timestamp)}>
          <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">
            {timeAgo(event.timestamp, locale)}
          </span>
        </Tip>
      )}
    </div>
  );
});
