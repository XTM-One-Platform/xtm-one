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
 * One entry of a schedule list: a recurring, event-driven or manual
 * assignment trigger, or a one-off scheduled work item.
 *
 * Every row is exactly two lines of one fixed height, whatever it carries:
 *
 * - line 1: the title (cut with an ellipsis, whole in a tooltip) and its chips
 *   on the left, the next run as a countdown in the accent colour on the right;
 * - line 2: compact metadata tokens on the left (the schedule, the owner agent,
 *   the last run with its outcome dot), the next run's absolute time in the
 *   viewer's zone with UTC muted beside it on the right.
 *
 * The Schedule view (Agentic Flow map, fleet page) and an agent's "Coming up"
 * card render these rows, so the same entry reads the same everywhere.
 */

import type { ElementType, KeyboardEvent, ReactNode } from "react";
import { Bot, CalendarClock, ChevronRight, Clock, History } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { ScheduleActions } from "@/components/work-items/ScheduleActions";
import { ScheduleStateBadge } from "@/components/work-items/ScheduledDetails";
import { useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { ICON_MAP, INTEGRATION_PROVIDER_NAMES, TRIGGER_ICONS, TRIGGER_LABELS } from "@/lib/constants";
import { humanizeCron } from "@/lib/cron";
import {
  formatCompactInZone,
  formatCompactTimeAgo,
  formatCountdown,
  formatInZone,
  formatUtcTime,
  isUtcZone,
  soonestRun,
  timezoneWarningZone,
  zonedNextRun,
} from "@/lib/scheduled-work-items";
import { cn } from "@/lib/utils";
import { WARNING_ON_HOVER_TEXT as WARNING_TEXT } from "@/lib/warning-text";
import type { AssignmentSchedule, ProviderCatalogItem, ScheduledWorkItem } from "@/types";

/** The row box: two fixed lines, one height for every row. */
const ROW =
  "group/hover-row flex h-14 w-full items-center gap-3 rounded-sm border layer-1 bg-elevation-default px-3 text-left transition-colors hover:bg-elevation-hover";
const LINE_1 = "flex h-5 min-w-0 items-center gap-2";
const LINE_2 = "flex h-4 min-w-0 items-center gap-3 content-caption text-default-secondary";
const CHIP = "h-4 shrink-0 whitespace-nowrap px-1.5 text-[9px]";
/** The right column: wide enough for `Mon 22 Nov, 13:20 · 12:20 UTC`. */
const WHEN_COLUMN = "flex w-48 shrink-0 flex-col items-end gap-0.5";

/** Read by `t_i18n(OUTCOME_LABELS[status])`. @i18n-keys */
const OUTCOME_LABELS = {
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
} as const;
type Outcome = keyof typeof OUTCOME_LABELS;

/** The last run's outcome as a dot colour; the label is the tooltip's. */
const OUTCOME_DOT: Record<Outcome, string> = {
  completed: "bg-feedback-success-primary",
  failed: "bg-feedback-error-primary",
  skipped: "bg-feedback-warning-primary",
  cancelled: "bg-current",
};

/** A status the row knows how to show, else null (a future status shows no dot). */
function knownOutcome(status: string | null | undefined): Outcome | null {
  return status && Object.prototype.hasOwnProperty.call(OUTCOME_LABELS, status) ? (status as Outcome) : null;
}

type Formatter = ReturnType<typeof useFormatter>;

/**
 * What starts the trigger, in the viewer's language: the provider by
 * its display name and the event by its catalog label, the raw
 * identifiers only for a provider or an event the catalog does not list.
 */
function scheduleTriggerDescription(
  s: AssignmentSchedule,
  { t_i18n, t_text, locale }: Formatter,
  catalog: ProviderCatalogItem[] | undefined,
): string {
  // humanizeCron returns "" for a missing / invalid cron; fall back to
  // "Scheduled" (matching AgentFlowDrawer) so the row never renders blank.
  if (s.trigger_type === "schedule") return humanizeCron(s.cron, locale) || t_i18n("Scheduled");
  if (s.trigger_type === "webhook") return t_i18n("On inbound webhook");
  if (s.trigger_type === "integration_event") {
    const entry = s.provider ? catalog?.find((c) => c.provider === s.provider) : undefined;
    const eventLabel = s.event ? entry?.available_events?.find((e) => e.event === s.event)?.label : undefined;
    const event = s.event ? (eventLabel ? t_text(eventLabel) : s.event.replace(/_/g, " ")) : null;
    const provider = s.provider ? (INTEGRATION_PROVIDER_NAMES[s.provider] ?? entry?.name ?? s.provider) : null;
    let base: string;
    if (event && provider) base = t_i18n("On {event} from {provider}", { event, provider });
    else if (event) base = t_i18n("On {event}", { event });
    else if (provider) base = t_i18n("On integration event from {provider}", { provider });
    else base = t_i18n("On integration event");
    if (!s.poll_interval_seconds) return base;
    return t_i18n("{trigger}, polled every {minutes}m", {
      trigger: base,
      minutes: Math.round(s.poll_interval_seconds / 60),
    });
  }
  return t_i18n("Manual trigger");
}

function KindTag({ oneOff }: { oneOff: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge
      variant="outline"
      className={cn(
        CHIP,
        oneOff ? cn("border-feedback-alert-primary/40", WARNING_TEXT) : "border-accent/40 text-accent",
      )}
    >
      {oneOff ? t_i18n("One-off") : t_i18n("Recurring")}
    </Badge>
  );
}

/** A title that gives way to its chips, whole in a tooltip. */
function Title({ text }: { text: string }) {
  return (
    <Tip content={text}>
      <span data-testid="schedule-row-title" className="min-w-0 truncate content-base font-medium">
        {text}
      </span>
    </Tip>
  );
}

/**
 * One metadata token of line 2: an icon and a value cut with an ellipsis, the
 * whole value in a tooltip. `keep` holds a short value whole (a relative
 * time); the others give way, the schedule phrase first.
 */
function Token({
  icon: Icon,
  tip,
  keep,
  grow,
  children,
  testId,
}: {
  icon: ElementType;
  tip?: ReactNode;
  keep?: boolean;
  grow?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  const token = (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex min-w-0 items-center gap-1",
        keep ? "shrink-0" : grow ? "shrink" : "max-w-[24rem] shrink-[2]",
      )}
    >
      <Icon className="h-3 w-3 shrink-0" />
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
  return tip ? <Tip content={tip}>{token}</Tip> : token;
}

/** The absolute time of a run: the viewer's zone, then UTC muted. */
function AbsoluteWhen({ iso, zone, extra }: { iso: string; zone: string; extra?: string | null }) {
  const { t_i18n, locale } = useFormatter();
  const tip = (
    <span className="flex flex-col gap-0.5">
      <span>{formatInZone(iso, zone, locale)}</span>
      <span className="text-default-secondary">{formatUtcTime(iso)}</span>
      {extra && <span>{t_i18n("In the trigger's time zone: {time}", { time: extra })}</span>}
    </span>
  );
  return (
    <Tip content={tip}>
      <span data-testid="schedule-row-absolute" className="h-4 whitespace-nowrap content-caption leading-4">
        <span className="text-default-primary">{formatCompactInZone(iso, zone, locale)}</span>
        {/* A viewer in UTC would read the same time twice. */}
        {!isUtcZone(zone) && <span className="text-default-disabled"> · {formatUtcTime(iso)}</span>}
      </span>
    </Tip>
  );
}

/** The next run as a countdown: accent, warning for a delayed run or a one-off. */
function Countdown({ iso, nowMs, tone, tip }: { iso: string; nowMs: number; tone: "accent" | "warning"; tip?: string }) {
  const { t_i18n } = useFormatter();
  const value = (
    <span
      data-testid="schedule-row-countdown"
      className={cn(
        "h-5 whitespace-nowrap content-compact font-medium leading-5",
        tone === "accent" ? "text-accent" : WARNING_TEXT,
      )}
    >
      {formatCountdown(iso, nowMs, t_i18n)}
    </span>
  );
  return tip ? <Tip content={tip}>{value}</Tip> : value;
}

/** Enter on the row itself opens it; Enter on one of its actions stays the action's. */
function onEnter(handler: () => void) {
  return (e: KeyboardEvent) => {
    if (e.key === "Enter" && e.target === e.currentTarget) handler();
  };
}

/** An assignment trigger: recurring (cron), event-driven or manual. */
export function ScheduleRow({
  s,
  nowMs,
  zone,
  onClick,
}: {
  s: AssignmentSchedule;
  nowMs: number;
  /** The viewer's time zone, the absolute times are read in. */
  zone: string;
  onClick: () => void;
}) {
  const formatter = useFormatter();
  const { t_i18n, locale } = formatter;
  // The provider catalog names an event-driven row's event; one cached read
  // shared by every row (and the trigger editor).
  const { data: catalog } = useProviderCatalog();
  const AgentIcon = ICON_MAP[s.agent_icon] ?? Bot;
  const TriggerIcon = TRIGGER_ICONS[s.trigger_type] ?? Clock;
  const unknownZone = s.trigger_type === "schedule" ? timezoneWarningZone(s.timezone_warning) : null;
  const soon = soonestRun(s);
  const description = scheduleTriggerDescription(s, formatter, catalog);
  // The time and the outcome shown together belong to one run: the latest
  // finished run the viewer can open when there is one, else the last trigger
  // without a dot (a run in flight, or another member's private run).
  const finishedOutcome = s.last_run_finished_at ? knownOutcome(s.last_run_status) : null;
  const lastRunAt = finishedOutcome ? (s.last_run_finished_at as string) : s.last_triggered_at;
  const outcome = finishedOutcome;
  const lastRunTip = lastRunAt ? (
    <span className="flex flex-col gap-0.5">
      <span>{t_i18n("Last run {when}", { when: formatInZone(lastRunAt, zone, locale) })}</span>
      {outcome && <span>{t_i18n(OUTCOME_LABELS[outcome])}</span>}
    </span>
  ) : (
    t_i18n("No run yet")
  );
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="schedule-row"
      className={cn(ROW, "border-elevation-subtle-soft hover:border-accent/30")}
    >
      <AgentIcon className="h-4 w-4 shrink-0 text-accent" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className={LINE_1}>
          <Title text={s.assignment_name} />
          {s.trigger_type === "schedule" && <KindTag oneOff={false} />}
          {s.has_active_run && (
            <Badge variant="cyan" className={CHIP}>
              {t_i18n("In flight")}
            </Badge>
          )}
          {/* A backlog of accepted-but-not-yet-started webhook deliveries:
              the platform used to discard these silently, so an operator had
              no way to see intake outpacing execution. */}
          {s.queued_run_count > 0 && (
            <Tip
              content={t_i18n(
                "{count, plural, one {# accepted delivery is} other {# accepted deliveries are}} waiting to run. They start one at a time as the assignment frees up.",
                { count: s.queued_run_count },
              )}
            >
              <Badge
                variant="outline"
                data-testid="schedule-row-queued"
                className={cn(CHIP, WARNING_TEXT)}
              >
                {t_i18n("{count, plural, one {# queued} other {# queued}}", { count: s.queued_run_count })}
              </Badge>
            </Tip>
          )}
          {/* The configured zone is not one the scheduler knows: the cron
              fires in UTC, which is rarely what was meant. */}
          {unknownZone !== null && (
            <Tip content={t_i18n("Time zone {zone} not recognised, UTC used", { zone: unknownZone })}>
              <Badge
                variant="outline"
                data-testid="schedule-timezone-warning"
                className={cn(CHIP, "border-feedback-alert-primary/40", WARNING_TEXT)}
              >
                {t_i18n("Unknown time zone")}
              </Badge>
            </Tip>
          )}
        </div>
        <div className={LINE_2}>
          <Token icon={TriggerIcon} tip={description} grow testId="schedule-row-trigger">
            {description}
          </Token>
          <Token icon={Bot} tip={t_i18n("Agent: {name}", { name: s.agent_name })} testId="schedule-row-owner">
            {s.agent_name}
          </Token>
          <Token icon={History} tip={lastRunTip} keep testId="schedule-row-last-run">
            <span className="inline-flex items-center gap-1">
              {lastRunAt ? formatCompactTimeAgo(lastRunAt, t_i18n) : t_i18n("No run yet")}
              {outcome && (
                <span
                  data-testid="schedule-row-outcome"
                  data-outcome={outcome}
                  role="img"
                  aria-label={t_i18n(OUTCOME_LABELS[outcome])}
                  className={cn("h-1.5 w-1.5 shrink-0 rounded-full", OUTCOME_DOT[outcome])}
                />
              )}
            </span>
          </Token>
        </div>
      </div>
      <div className={WHEN_COLUMN}>
        {soon ? (
          <Countdown
            iso={soon.iso}
            nowMs={nowMs}
            tone={soon.pending ? "warning" : "accent"}
            tip={soon.pending ? t_i18n("Delayed run at {when}", { when: formatInZone(soon.iso, zone, locale) }) : undefined}
          />
        ) : (
          <span className="h-5 whitespace-nowrap content-compact leading-5 text-default-secondary">
            {t_i18n(TRIGGER_LABELS[s.trigger_type] ?? s.trigger_type)}
          </span>
        )}
        {soon ? <AbsoluteWhen iso={soon.iso} zone={zone} extra={zonedNextRun(s)} /> : <span className="h-4" />}
      </div>
      {/* The width of a one-off row's actions, so the time column lines up across sections. */}
      <div className="flex w-16 shrink-0 justify-end">
        <ChevronRight className="h-4 w-4 text-default-disabled" />
      </div>
    </button>
  );
}

/** A one-off scheduled work item, with Reschedule and Cancel while it has not run. */
export function OneOffRow({
  item,
  nowMs,
  zone,
  onOpen,
}: {
  item: ScheduledWorkItem;
  nowMs: number;
  zone: string;
  onOpen: () => void;
}) {
  const { t_i18n } = useFormatter();
  const AgentIcon = (item.agent_icon && ICON_MAP[item.agent_icon]) || Bot;
  const waiting = item.scheduled.state === "scheduled" || item.scheduled.state === "due";
  const action =
    item.scheduled.mode === "exact" && item.scheduled.tool ? item.scheduled.tool : t_i18n("Instruction");
  const label = item.scheduled.target ? `${action} -> ${item.scheduled.target}` : action;
  // The server's preview says what will run; it leads the token, cut first.
  const what = item.scheduled.preview ? `${item.scheduled.preview} · ${label}` : label;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={onEnter(onOpen)}
      data-testid="one-off-row"
      className={cn(ROW, "cursor-pointer border-feedback-alert-primary/20 hover:border-feedback-alert-primary/40")}
    >
      <AgentIcon className="h-4 w-4 shrink-0 text-feedback-warning-primary" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className={LINE_1}>
          <Title text={item.title} />
          <KindTag oneOff />
          <ScheduleStateBadge scheduled={item.scheduled} className={CHIP} />
        </div>
        <div className={LINE_2}>
          <Token icon={CalendarClock} tip={what} grow testId="schedule-row-trigger">
            {what}
          </Token>
          {item.agent_name && (
            <Token icon={Bot} tip={t_i18n("Agent: {name}", { name: item.agent_name })} testId="schedule-row-owner">
              {item.agent_name}
            </Token>
          )}
        </div>
      </div>
      <div className={WHEN_COLUMN}>
        {item.due_at && waiting ? (
          <Countdown iso={item.due_at} nowMs={nowMs} tone="warning" />
        ) : (
          <span className="h-5" />
        )}
        {item.due_at ? <AbsoluteWhen iso={item.due_at} zone={zone} /> : <span className="h-4" />}
      </div>
      <div className="flex w-16 shrink-0 justify-end">
        <ScheduleActions item={item} iconOnly />
      </div>
    </div>
  );
}
