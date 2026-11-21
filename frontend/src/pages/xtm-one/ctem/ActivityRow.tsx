// SPDX-License-Identifier: Apache-2.0

import type { LucideIcon } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

export type ActivityRowStatus = "running" | "completed" | "partial" | "failed";

const DOT: Record<ActivityRowStatus, string> = {
  running: "bg-accent",
  completed: "bg-feedback-success-primary",
  // Amber: some expectations met. Red stays for none met or a platform down (#9594 A3).
  partial: "bg-feedback-warning-primary",
  failed: "bg-feedback-error-primary",
};

/**
 * One row of the Live activity card (#9567, UX round 1 B2): icon tile, title,
 * one detail line, status dot and time. The deterministic feed and the greyed
 * demo rows both render through it, so the card has a single row layout.
 *
 * The dot carries its status as text for assistive technology (#9594 A2), and
 * a row given `at` shows the absolute time in a tooltip over the relative one,
 * as the run rows do (#9594 A1).
 */
export function ActivityRow({
  as: Tag = "div",
  icon: Icon,
  title,
  detail,
  status,
  time,
  at,
  onOpen,
  openLabel,
}: {
  as?: "div" | "li";
  icon: LucideIcon;
  title: string;
  detail: string;
  status: ActivityRowStatus;
  /** The time as read in the row, relative ("3 minutes ago"). */
  time: string;
  /** ISO timestamp of the row: its absolute time is shown in a tooltip. */
  at?: string;
  /** A row that opens something (a signal's loop trace, #838): the whole row is the button. */
  onOpen?: () => void;
  /** The accessible name of that button. */
  openLabel?: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const statusLabel =
    status === "running" ? t_i18n("Running") : status === "partial" ? t_i18n("Partial") : status === "failed" ? t_i18n("Failed") : t_i18n("Completed");
  const timeLabel = <span data-testid="activity-row-time" className="content-caption text-default-secondary whitespace-nowrap">{time}</span>;
  const body = (
    <>
      <div className="h-7 w-7 rounded-sm bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)] flex items-center justify-center shrink-0">
        <Icon className="h-3.5 w-3.5 text-default-secondary" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <p className="content-compact font-medium truncate">{title}</p>
        <p className="text-[11px] text-default-secondary truncate">{detail}</p>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        <span
          className={cn("h-1.5 w-1.5 rounded-full", DOT[status], status === "running" && "animate-pulse")}
          data-status={status}
          aria-hidden
        />
        <span className="sr-only">{statusLabel}</span>
        {at ? <Tip content={formatShortDateTimeIntl(intl, at)}>{timeLabel}</Tip> : timeLabel}
      </div>
    </>
  );
  if (!onOpen) return <Tag className="flex items-center gap-3 py-2.5">{body}</Tag>;
  return (
    <Tag>
      <button
        type="button"
        onClick={onOpen}
        aria-label={openLabel}
        className="flex w-full items-center gap-3 rounded-sm py-2.5 text-left hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {body}
      </button>
    </Tag>
  );
}
