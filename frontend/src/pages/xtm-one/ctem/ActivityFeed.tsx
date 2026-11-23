// SPDX-License-Identifier: Apache-2.0

import { useFormatter } from "@/i18n";
import { formatDayIntl, formatTimeAgo } from "@/lib/relative-time";
import { eventStatus, type ActivityEvent } from "./activity-feed";
import { activityText } from "./activity-text";
import { ActivityRow } from "./ActivityRow";
import { SIGNAL_STATE_LABEL } from "./loop/DecisionReason";

/**
 * The deterministic activity feed of the Live activity card (D3-4, #9567):
 * the rows of `deterministicActivity` through the card's shared `ActivityRow`
 * (icon, title, one detail line, status dot, time). A signal row (#838) opens
 * its loop trace through `onOpenSignal` (the Signals page, `?signal=<id>`); the other
 * rows stay read-only: no row links to a page CE cannot open.
 */
export function ActivityFeed({ events, onOpenSignal }: { events: ActivityEvent[]; onOpenSignal?: (signalId: string) => void }) {
  const { t_i18n, intl } = useFormatter();

  return (
    <ul className="divide-y divide-[var(--border-elevation-subtle)]" aria-label={t_i18n("Recent platform activity")}>
      {events.map((event) => {
        const { icon, title, detail } = activityText(event, t_i18n, (iso) => formatDayIntl(intl, iso));
        const signalId = event.kind === "signal_received" ? event.signalId : null;
        const label = event.kind === "signal_received" ? event.label : "";
        const open = signalId !== null && onOpenSignal ? () => onOpenSignal(signalId) : undefined;
        return (
          <ActivityRow
            key={event.id}
            as="li"
            icon={icon}
            title={title}
            detail={detail}
            status={eventStatus(event)}
            statusLabel={event.kind === "signal_received" ? t_i18n(SIGNAL_STATE_LABEL[event.state] ?? event.state) : undefined}
            time={formatTimeAgo(intl, event.at)}
            at={event.at}
            onOpen={open}
            openLabel={open ? t_i18n("Show {subject} in the loop", { subject: label }) : undefined}
          />
        );
      })}
    </ul>
  );
}
