// SPDX-License-Identifier: Apache-2.0

import type { LucideIcon } from "lucide-react";
import { Icon as DsIcon, type IconName } from "@filigran/design-system";
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
 * One row of the Live activity card (#9567, UX round 1 B2): status column,
 * icon tile, title, one detail line and time. The deterministic feed, the
 * signal rows (#838) and the greyed demo rows render through it.
 *
 * Its anatomy is the one of the compact run rows drawn in the same card
 * (#851 UX round 1 B2): the same 12 px inset, a 16 px status column before a
 * 32 px icon tile, a semibold title over one detail line, and the time at the
 * same right edge, so signal rows and run rows line up when they share the card.
 *
 * The icon is a design system icon name (#851 UX round 1 B3); a lucide
 * component is still accepted for the rows drawn before DS 1.9.0.
 *
 * The dot carries its status as text for assistive technology (#9594 A2):
 * `statusLabel` overrides the run wording (a signal reads its state, #851 A3).
 * A row given `at` shows the absolute time in a tooltip over the relative one.
 */
export function ActivityRow({
  as: Tag = "div",
  icon,
  title,
  detail,
  status,
  statusLabel,
  time,
  at,
  onOpen,
  openLabel,
}: {
  as?: "div" | "li";
  icon: IconName | LucideIcon;
  title: string;
  detail: string;
  status: ActivityRowStatus;
  /** The words read for the status dot; defaults to the run wording. */
  statusLabel?: string;
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
  const spoken =
    statusLabel ??
    (status === "running" ? t_i18n("Running") : status === "partial" ? t_i18n("Partial") : status === "failed" ? t_i18n("Failed") : t_i18n("Completed"));
  const timeLabel = <span data-testid="activity-row-time" className="content-caption text-default-secondary whitespace-nowrap">{time}</span>;
  const Lucide = typeof icon === "string" ? null : icon;
  const glyph = Lucide ? (
    <Lucide className="h-4 w-4 text-default-secondary" aria-hidden />
  ) : (
    <DsIcon name={icon as IconName} size={16} className="text-default-secondary" aria-hidden />
  );
  const body = (
    <>
      <span className="flex h-4 w-4 shrink-0 items-center justify-center" data-testid="activity-row-status">
        <span
          className={cn("h-1.5 w-1.5 rounded-full", DOT[status], status === "running" && "animate-pulse")}
          data-status={status}
          aria-hidden
        />
        <span className="sr-only">{spoken}</span>
      </span>
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">{glyph}</div>
      <div className="min-w-0 flex-1">
        <p className="content-compact font-semibold truncate">{title}</p>
        <p className="mt-0.5 text-[11px] text-default-secondary truncate">{detail}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">{at ? <Tip content={formatShortDateTimeIntl(intl, at)}>{timeLabel}</Tip> : timeLabel}</div>
    </>
  );
  if (!onOpen) return <Tag className="flex items-center gap-3 p-3">{body}</Tag>;
  return (
    <Tag>
      <button
        type="button"
        onClick={onOpen}
        aria-label={openLabel}
        className="flex w-full items-center gap-3 p-3 text-left transition-all hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {body}
      </button>
    </Tag>
  );
}
