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
 * "Coming up" on an agent's Overview: what this agent will do next and
 * when - your scheduled work items on it (one-off) and its next assignment
 * triggers (recurring) - with Cancel / Reschedule, and a link to the fleet's
 * Schedule view narrowed to this agent. A card of the Overview, not a tab.
 * Its entries are the Schedule view's own rows, so an entry reads the
 * same here and there.
 */
import { workItemPath } from "@/lib/task-routes";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, CalendarClock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { OneOffRow, ScheduleRow } from "@/components/ui/schedule-row";
import { useAssignmentSchedule } from "@/hooks/api/useAssignmentApi";
import { useScheduledWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { REFETCH_INTERVAL_FAST } from "@/lib/constants";
import { viewerTimeZone } from "@/lib/scheduled-work-items";
import type { Agent, AssignmentSchedule, ScheduledWorkItem } from "@/types";

const MAX_ENTRIES = 6;

type Entry =
  | { kind: "one-off"; at: number; item: ScheduledWorkItem }
  | { kind: "recurring"; at: number; schedule: AssignmentSchedule };

export function ComingUpCard({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const navigate = useNavigate();
  const zone = viewerTimeZone(user?.timezone);
  const nowMs = useNow(REFETCH_INTERVAL_FAST);
  const { data: oneOffs } = useScheduledWorkItems(agent.id);
  const { data: schedules } = useAssignmentSchedule();

  const entries: Entry[] = [
    ...(oneOffs ?? [])
      .filter((item) => item.due_at && ["scheduled", "due", "running"].includes(item.scheduled.state))
      .map((item): Entry => ({ kind: "one-off", at: new Date(item.due_at as string).getTime(), item })),
    ...(schedules ?? [])
      .filter((s) => s.agent_id === agent.id && s.trigger_type === "schedule" && s.next_run_at)
      .map((schedule): Entry => ({
        kind: "recurring",
        at: new Date(schedule.next_run_at as string).getTime(),
        schedule,
      })),
  ]
    .sort((a, b) => a.at - b.at)
    .slice(0, MAX_ENTRIES);

  return (
    <Card data-testid="coming-up-card">
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <CalendarClock className="h-4 w-4 text-accent" /> {t_i18n("Coming up")}
        </CardTitle>
        <Link
          to={`/flows/map/schedule?agent=${agent.id}`}
          className="flex items-center gap-1 text-[11px] text-accent hover:underline"
        >
          {t_i18n("Open in Schedule")} <ArrowRight className="h-3 w-3" />
        </Link>
      </CardHeader>
      <CardContent className="space-y-1.5 px-4 pb-4 pt-0">
        {entries.length === 0 ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("Nothing coming up. Ask this agent in a chat to do something later, or give it a scheduled assignment.")}
          </p>
        ) : (
          entries.map((entry) =>
            entry.kind === "one-off" ? (
              <OneOffRow
                key={entry.item.id}
                item={entry.item}
                nowMs={nowMs}
                zone={zone}
                onOpen={() => navigate(workItemPath(entry.item.id))}
              />
            ) : (
              <ScheduleRow
                key={entry.schedule.assignment_id}
                s={entry.schedule}
                nowMs={nowMs}
                zone={zone}
                onClick={() => navigate(`/agents/assignments/${entry.schedule.assignment_id}`)}
              />
            ),
          )
        )}
      </CardContent>
    </Card>
  );
}
