// SPDX-License-Identifier: Apache-2.0

import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { eventStatus, type ActivityEvent } from "./activity-feed";
import { activityText } from "./activity-text";
import { ActivityRow } from "./ActivityRow";

/**
 * The deterministic activity feed of the Live activity card (D3-4, #9567):
 * the rows of `deterministicActivity` through the card's shared `ActivityRow`
 * (icon, title, one detail line, status dot, time). Read-only: no row links
 * to a page CE cannot open.
 */
export function ActivityFeed({ events }: { events: ActivityEvent[] }) {
  const { t_i18n, intl } = useFormatter();
  const row = (event: ActivityEvent) => activityText(event, t_i18n);

  return (
    <ul className="divide-y divide-[var(--border-elevation-subtle)]" aria-label={t_i18n("Recent platform activity")}>
      {events.map((event) => {
        const { icon, title, detail } = row(event);
        return (
          <ActivityRow
            key={event.id}
            as="li"
            icon={icon}
            title={title}
            detail={detail}
            status={eventStatus(event)}
            time={formatTimeAgo(intl, event.at)}
            at={event.at}
          />
        );
      })}
    </ul>
  );
}
