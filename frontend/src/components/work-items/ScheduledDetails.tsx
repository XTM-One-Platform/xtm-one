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
 * What a scheduled work item says, wherever it is listed: its time in
 * the viewer's zone and in UTC, the action (an exact tool call or an
 * instruction) and where it goes, a preview of the content, and once it ran
 * its outcome with a link to its run. The preview and the outcome are written
 * by agents in Markdown: a card shows them as plain text clamped to a
 * few lines, the detail dialog (`full`) renders them whole.
 */
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Badge } from "@/components/ui/badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  SCHEDULE_STATE_LABELS,
  SCHEDULE_STATE_VARIANT,
  formatInZone,
  formatUtcTime,
  isUtcZone,
  viewerTimeZone,
} from "@/lib/scheduled-work-items";
import { markdownSummary } from "@/lib/markdown-summary";
import { cn } from "@/lib/utils";
import type { WorkItem, WorkItemScheduled } from "@/types";

/** `withIcon` leads the state with the calendar icon: a list row marks the
 *  item as scheduled and names its state in one badge. */
export function ScheduleStateBadge({
  scheduled,
  className,
  withIcon = false,
}: {
  scheduled: WorkItemScheduled;
  className?: string;
  withIcon?: boolean;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Badge
      variant={SCHEDULE_STATE_VARIANT[scheduled.state]}
      className={cn("content-caption", withIcon && "gap-1", className)}
    >
      {withIcon && <Icon name="calendar" size={16} aria-hidden />}
      {/* The icon is silent: a later state still tells a screen reader the
          item is scheduled, and a waiting one says "Scheduled" once. */}
      {withIcon && scheduled.state !== "scheduled" && (
        <span className="sr-only">{t_i18n("Scheduled")}: </span>
      )}
      {t_i18n(SCHEDULE_STATE_LABELS[scheduled.state])}
    </Badge>
  );
}

/**
 * The one line a list row gives a scheduled item: its time in the
 * viewer's zone and in UTC (once when the viewer is in UTC), the
 * action and where it goes. The preview and the outcome are in the detail
 * dialog.
 */
export function ScheduledSummary({ item }: { item: WorkItem & { scheduled: WorkItemScheduled } }) {
  const { t_i18n, locale } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const s = item.scheduled;
  return (
    <span className="flex min-w-0 items-center gap-x-3 overflow-hidden whitespace-nowrap" data-testid="scheduled-summary">
      {item.due_at && (
        <span className="flex shrink-0 items-center gap-1">
          <Icon name="calendar" size={16} className="text-accent" aria-hidden />
          {formatInZone(item.due_at, zone, locale)}
          {/* A viewer in UTC would read the same time twice. */}
          {!isUtcZone(zone) && <span>({formatUtcTime(item.due_at)})</span>}
        </span>
      )}
      <span className="flex min-w-0 shrink items-center gap-1">
        <Icon name={s.mode === "exact" ? "wrench" : "bolt"} size={16} className="shrink-0" aria-hidden />
        <span className="truncate">
          {s.mode === "exact" ? t_i18n("Exact: {tool}", { tool: s.tool ?? "" }) : t_i18n("Instruction")}
        </span>
      </span>
      {s.target && (
        <span className="flex min-w-0 items-center gap-1">
          <Icon name="locate-fixed" size={16} className="shrink-0" aria-hidden /> <span className="truncate">{s.target}</span>
        </span>
      )}
    </span>
  );
}

export function ScheduledDetails({
  item,
  full = false,
}: {
  item: WorkItem & { scheduled: WorkItemScheduled };
  /** Render the preview and the outcome whole, as Markdown (the detail dialog). */
  full?: boolean;
}) {
  const { t_i18n, locale } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const s = item.scheduled;
  const settled = s.state === "done" || s.state === "blocked" || s.state === "missed";
  const outcomeTone = s.state === "done" ? "text-feedback-success-primary/90" : "text-feedback-warning-primary";
  // The tone is inherited text colour only: a link in the outcome keeps its own
  // link token (text-accent on the <a>, ChatMarkdown's MarkdownLink).
  // Platform restarts it went on from by itself: said, never asked.
  const restartCount = (s.restarts?.resumed ?? 0) + (s.restarts?.requeued ?? 0);
  return (
    <div
      className={cn("space-y-1.5 text-default-secondary", full ? "content-compact" : "content-caption")}
      data-testid="scheduled-details"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {item.due_at && (
          <span className="flex items-center gap-1 text-default-secondary">
            <Icon name="calendar" size={16} className="text-accent" aria-hidden />
            {formatInZone(item.due_at, zone, locale)}
            {!isUtcZone(zone) && <span className="text-default-secondary">({formatUtcTime(item.due_at)})</span>}
          </span>
        )}
        <span className="flex items-center gap-1">
          <Icon name={s.mode === "exact" ? "wrench" : "bolt"} size={16} aria-hidden />
          {s.mode === "exact"
            ? t_i18n("Exact: {tool}", { tool: s.tool ?? "" })
            : t_i18n("Instruction")}
        </span>
        {s.target && (
          <span className="flex items-center gap-1">
            <Icon name="locate-fixed" size={16} aria-hidden /> {s.target}
          </span>
        )}
      </div>
      {s.preview &&
        (full ? (
          <div className="flex items-start gap-1.5 text-default-primary">
            <Icon name="quote" size={16} className="shrink-0 text-default-secondary" aria-hidden />
            <ChatMarkdown content={s.preview} className="chat-markdown min-w-0 flex-1 content-base" remoteImages={false} />
          </div>
        ) : (
          <p className="flex items-start gap-1 text-default-secondary">
            <Icon name="quote" size={16} className="shrink-0" aria-hidden /> <TruncatedText lines={2} text={markdownSummary(s.preview)} className="flex-1" />
          </p>
        ))}
      {restartCount > 0 && (
        <p className="flex items-center gap-1 text-default-secondary" data-testid="scheduled-restarts">
          <Icon name="history" size={16} className="shrink-0" aria-hidden />
          {t_i18n("Resumed by itself {count, plural, one {# time after a platform restart} other {# times after platform restarts}}", { count: restartCount })}
        </p>
      )}
      {settled &&
        item.result &&
        (full ? (
          <ChatMarkdown content={item.result} className={cn("chat-markdown content-base", outcomeTone)} remoteImages={false} />
        ) : (
          <TruncatedText lines={3} text={markdownSummary(item.result)} className={outcomeTone} />
        ))}
      {s.run_id && (
        <Link
          to={`/operations/executions/${s.run_id}`}
          className="inline-flex items-center gap-1 text-accent hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          <Icon name="history" size={16} aria-hidden /> {t_i18n("View the run")}
        </Link>
      )}
    </div>
  );
}
