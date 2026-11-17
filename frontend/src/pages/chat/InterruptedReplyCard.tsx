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

import { useId, useState } from "react";
import { AlertTriangle, ChevronDown, Play, RotateCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { cutBreakdown, type CutGroupView } from "@/lib/cut-turn-group";
import { cn, formatTime } from "@/lib/utils";

interface InterruptedReplyCardProps {
  view: CutGroupView;
  /** Start the turn that picks the work up (`stopped` only); omitted where the reader cannot send. */
  onResume?: () => void;
  /** Stop offering the resume (`stopped` only); omitted where the reader is not the owner. */
  onDismiss?: () => void;
  /** A send would be refused right now (AI off, tools still loading). */
  resumeDisabled?: boolean;
}

/**
 * A turn the platform cut, drawn as ONE status line in place of its stored
 * interrupted replies (`interrupted_turn.interrupted_reply()`): what stopped
 * it, whether it is resuming, resumed by itself or by the person, and a chip
 * that unfolds the completed tool calls per tool. Every cut of the same turn
 * folds into this one card (`groupCutReplies`), and its state follows the
 * stream (`turn_resuming`) and the conversation (`interrupted_turn`) live.
 * Muted when the resume is the normal path; a warning, with Resume and
 * Dismiss on the card itself, only when it could not resume by itself.
 */
export function InterruptedReplyCard({ view, onResume, onDismiss, resumeDisabled = false }: InterruptedReplyCardProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const breakdown = cutBreakdown(view.replies);
  const newest = view.replies[view.replies.length - 1];
  const cause =
    view.cause === "recycle"
      ? t_i18n("a worker recycle")
      : view.cause === "stop"
        ? t_i18n("a platform restart")
        : t_i18n("a server shutdown");
  // No resume counter: which resume of how many is the platform's business.
  let text: string;
  switch (view.state) {
    case "resumed":
      text =
        view.by === "you"
          ? t_i18n("Interrupted by {cause}, resumed by you", { cause })
          : t_i18n("Interrupted by {cause}, resumed by itself", { cause });
      break;
    case "resuming":
    case "queued":
      text = t_i18n("Interrupted by {cause}, resuming by itself", { cause });
      break;
    case "dismissed":
      text = t_i18n("Interrupted by {cause}, not resumed", { cause });
      break;
    default:
      text = t_i18n("Interrupted by {cause}, could not resume by itself", { cause });
  }
  const warn = view.state === "stopped";
  const showActions = warn && !!(onResume || onDismiss);
  const Icon = warn ? AlertTriangle : RotateCw;
  const hasDetail = breakdown.tools.length > 0;

  return (
    <div
      role="status"
      data-testid="interrupted-reply-card"
      data-state={view.state}
      className={cn(
        "ml-11 max-w-2xl rounded-sm border px-3 py-1.5",
        warn
          ? "border-feedback-alert-primary/25 bg-feedback-alert-primary/[0.04]"
          : "border-elevation-subtle-soft bg-elevation-hover/40",
      )}
    >
      <div className="flex min-h-6 items-start gap-2">
        <span className="flex h-6 shrink-0 items-center">
          <Icon
            className={cn("h-3.5 w-3.5", warn ? "text-feedback-warning-primary" : "text-default-secondary")}
            aria-hidden
          />
        </span>
        <p
          data-testid="interrupted-reply-card-text"
          className={cn("min-w-0 flex-1 break-words py-1 content-caption", warn ? "text-default-primary" : "text-default-secondary")}
        >
          {text}
        </p>
        {hasDetail && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls={detailsId}
            data-testid="interrupted-reply-card-toggle"
            className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-0.5 content-caption text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {t_i18n("{count, plural, one {# tool call} other {# tool calls}}", { count: breakdown.completed })}
            <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} aria-hidden />
          </button>
        )}
        <span className="flex h-6 shrink-0 items-center content-caption text-default-disabled">
          {formatTime(new Date(newest.created_at))}
        </span>
      </div>
      {showActions && (
        <div data-testid="interrupted-reply-card-actions" className="flex flex-wrap items-center gap-2 pb-1 pl-5 pt-1">
          {onResume && (
            <Button size="sm" onClick={onResume} disabled={resumeDisabled}>
              <Play className="h-3.5 w-3.5" aria-hidden />
              {t_i18n("Resume")}
            </Button>
          )}
          {onDismiss && (
            <Button size="sm" variant="ghost" onClick={onDismiss}>
              <X className="h-3.5 w-3.5" aria-hidden />
              {t_i18n("Dismiss")}
            </Button>
          )}
        </div>
      )}
      {hasDetail && open && (
        <div id={detailsId} data-testid="interrupted-reply-card-details" className="flex flex-wrap items-center gap-1 pb-1 pl-5 pt-1.5">
          {breakdown.tools.map((tool) => {
            const isLast = breakdown.last?.name === tool.name;
            return (
              <span
                key={tool.name}
                data-testid="interrupted-reply-card-tool"
                data-last={isLast || undefined}
                className={cn(
                  "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 font-mono-plex text-[11px]",
                  isLast ? "bg-accent/10 text-accent ring-1 ring-accent/30" : "bg-elevation-hover text-default-secondary",
                )}
                title={isLast ? t_i18n("Last completed") : undefined}
              >
                {tool.name}
                {tool.calls > 1 && <span className="opacity-70">×{tool.calls}</span>}
                {tool.failed > 0 && (
                  <span className="text-feedback-warning-primary">
                    {t_i18n("{count} failed", { count: tool.failed })}
                  </span>
                )}
              </span>
            );
          })}
          {breakdown.running > 0 && (
            <span className="content-caption text-default-disabled">
              {t_i18n("{count} still running when it stopped", { count: breakdown.running })}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
