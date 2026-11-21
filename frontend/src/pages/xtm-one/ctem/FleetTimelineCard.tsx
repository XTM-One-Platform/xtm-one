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
 * Fleet events timeline of the CTEM command center (#9050 slice 3): the latest
 * runs of every fleet the reader can see, newest first, each naming its fleet,
 * its agent, what started it (an OpenCTI, OpenAEV or OpenCRQ event, a schedule,
 * a work item...) and its outcome (`lib/ctem-fleet-timeline.ts`). A row opens
 * the run. With no run yet the widget says what starts one. In Community
 * Edition (`licensed` false) it shows the same state as the Fleet activity
 * card: what fleets do and the one call to action of `AiLockedFooter`, no
 * "All" and never "Open fleets" (a locked page): dev-docs/ctem-open-source.md,
 * section 6 (B3).
 */
import { ArrowUpRight, History } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import type { TimelineEntry, TimelineOutcome, TimelineSource } from "@/lib/ctem-fleet-timeline";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CASE_STATE_LABEL } from "./cases-shared";

/**
 * The key of what started a run. The run-source keys are their own (en
 * "Run source: chat" reads "Chat") so each language says a noun, never the
 * verb "Chat" of a button. @i18n-keys
 */
const SOURCE_LABELS = {
  opencti: "OpenCTI event",
  openaev: "OpenAEV event",
  opencrq: "OpenCRQ event",
  integration: "Integration event",
  schedule: "Run source: schedule",
  work_item: "Run source: work item",
  chat: "Run source: chat",
  webhook: "Run source: webhook",
  platform_event: "Run source: platform event",
  delegation: "Run source: handover",
  other: "Manual or other",
} as const satisfies Record<TimelineSource, string>;

/**
 * The English label of one run's badge, keys of their own so each language
 * says it of one run (fr "Réussie", never the plural count "Réussies").
 * @i18n-keys
 */
const OUTCOME_LABELS = {
  succeeded: "Run outcome: succeeded",
  failed: "Run outcome: failed",
  running: "Run outcome: running",
  pending: "Run outcome: pending",
  interrupted: "Run outcome: interrupted",
  cancelled: "Run outcome: cancelled",
} as const satisfies Record<TimelineOutcome, string>;

const OUTCOME_VARIANTS: Partial<Record<TimelineOutcome, "success" | "warning" | "destructive" | "secondary">> = {
  succeeded: "success",
  failed: "destructive",
  interrupted: "warning",
};

export function FleetTimelineCard({
  timeline,
  isLoading,
  licensed = true,
  lockReason = null,
  casesById = {},
}: {
  timeline: TimelineEntry[];
  isLoading: boolean;
  /**
   * The cases the reader sees (`GET /ctem-cases`), by id (#915). A run row
   * whose case is not in it shows the run alone: a case the reader cannot
   * read is never named.
   */
  casesById?: Readonly<Record<string, CtemCaseSummary | undefined>>;
  /** False in Community Edition: the card shows what fleets do, with the footer. */
  licensed?: boolean;
  /** Why AI cannot run (`useAiLockReason`), for the footer of the Community Edition state. */
  lockReason?: AiLockReason | null;
}) {
  const communityEdition = !licensed;
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const open = (entry: TimelineEntry) =>
    navigate(`/operations/executions/${entry.runId}`, { state: { backTo: "/", backLabel: "Back to Command Center" } });

  return (
    <Card className="h-full overflow-hidden flex flex-col">
      <CardContent className="p-4 flex flex-col flex-1 min-h-0 gap-3">
        <CtemWidgetHeader
          icon={<History className="h-4 w-4" />}
          title={t_i18n("Fleet timeline")}
          actions={
            !communityEdition ? (
              <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/operations/executions")}>
                <ArrowUpRight className="h-3 w-3" /> {t_i18n("All")}
              </Button>
            ) : undefined
          }
        />
        {communityEdition ? (
          // No `requiresAi`: EmptyState would hide the footer with AI off.
          <EmptyState
            variant="inset"
            icon={History}
            title={t_i18n("Fleets run with Enterprise Edition")}
            description={t_i18n("Runs of your fleets show here as their triggers fire: OpenCTI, OpenAEV and OpenCRQ events, schedules and work items.")}
          >
            <AiLockedFooter reason={lockReason ?? "unlicensed"} className="justify-center" />
          </EmptyState>
        ) : timeline.length === 0 ? (
          isLoading ? null : (
            <EmptyState
              variant="inset"
              icon={History}
              title={t_i18n("No fleet run yet")}
              description={t_i18n("Runs of your fleets show here as their triggers fire: OpenCTI, OpenAEV and OpenCRQ events, schedules and work items.")}
              actionLabel={t_i18n("Open fleets")}
              onAction={() => navigate("/fleets")}
            />
          )
        ) : (
          <ul className="flex-1 min-h-0 overflow-y-auto divide-y divide-[var(--border-elevation-subtle)] p-1 -m-1">
            {timeline.map((entry) => {
              const key = entry.outcome;
              const source = t_i18n(SOURCE_LABELS[entry.source]);
              const kase = entry.caseId ? casesById[entry.caseId] : undefined;
              return (
                <li key={entry.runId} className="py-0.5">
                  <div
                    {...buttonRoleProps(() => open(entry))}
                    aria-label={t_i18n("Open run of {agent}", { agent: entry.agentName ?? entry.fleetName })}
                    className={cn(
                      "flex items-center gap-3 py-2 px-1 rounded-sm cursor-pointer hover:bg-elevation-hover transition-colors min-w-0",
                      BUTTON_ROLE_FOCUS_RING,
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="content-compact font-semibold truncate">
                        {entry.agentName ?? t_i18n("Agent not read yet")}
                      </p>
                      <p className="content-caption text-default-secondary truncate">
                        {entry.event ? (
                          <Tip content={entry.event}>
                            <span>{source}</span>
                          </Tip>
                        ) : (
                          source
                        )}
                        {" · "}
                        {entry.fleetName}
                      </p>
                    </div>
                    {key && (
                      <Badge variant={OUTCOME_VARIANTS[key] ?? "secondary"} className="shrink-0">
                        {t_i18n(OUTCOME_LABELS[key])}
                      </Badge>
                    )}
                    <span className="content-caption text-default-secondary tabular-nums shrink-0">
                      {formatTimeAgo(intl, entry.at)}
                    </span>
                  </div>
                  {kase && (
                    <Link
                      to={`/ctem/cases/${kase.id}`}
                      state={{ backTo: "/", backLabel: "Back to Command Center" }}
                      aria-label={t_i18n("Open case {title}", { title: kase.title })}
                      className="flex items-center gap-2 px-1 pb-1.5 -mt-1 min-w-0 content-caption text-default-secondary hover:text-default-primary hover:underline"
                    >
                      <span className="truncate">
                        {t_i18n("Case")}: {kase.title}
                      </span>
                      <span className="shrink-0">· {t_i18n(CASE_STATE_LABEL[kase.state])}</span>
                    </Link>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
