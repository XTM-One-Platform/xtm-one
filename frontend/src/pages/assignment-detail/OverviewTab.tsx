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

import { BarChart3, Activity, CheckCircle2 } from "lucide-react";
import { StatCard } from "@/components/ui/stat-card";
import { useAssignmentRuns } from "@/hooks/api/useAssignmentApi";
import { useProviderCatalog, useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { formatCompactNumber, formatShortDate, formatTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Assignment, Agent } from "@/types";
import { TriggerCard } from "./TriggerCard";
import { ParametersCard } from "./ParametersCard";
import { RunAsCard } from "./RunAsCard";
import { AssignmentStatusTile } from "./AssignmentStatus";

// ---------------------------------------------------------------------------
// Overview Tab — Stats + Trigger + Parameters
// ---------------------------------------------------------------------------

interface OverviewTabProps {
  assignment: Assignment;
  agent: Agent | undefined;
  canManage: boolean;
  /** See `RunAsCard`'s `canChooseRunAs`. */
  canChooseRunAs?: boolean;
  onUpdate: () => void;
}

export function OverviewTab({ assignment, agent, canManage, canChooseRunAs = false, onUpdate }: OverviewTabProps) {
  const { t_i18n } = useFormatter();
  const { data: runs } = useAssignmentRuns(assignment.id, 10);
  const { data: catalog } = useProviderCatalog("integration");
  // The agent's integrations and knowledge bases, whoever owns them: an admin
  // sees and edits a colleague's assignment as its owner does.
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const { data: allKnowledgeBases } = useKnowledgeBases({ scope: "all" });

  const agentKbs = allKnowledgeBases?.filter((kb) =>
    agent?.knowledge_base_ids?.includes(kb.id)
  ) ?? [];

  // Stats
  const lastRun = assignment.last_triggered_at
    ? new Date(assignment.last_triggered_at)
    : null;

  // Count by status
  const statusCounts = (runs ?? []).reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});
  const successCount = statusCounts["completed"] ?? 0;
  const failedCount = statusCounts["failed"] ?? 0;
  // A run still pending or running is a recent run too: alone, it read "no
  // recent runs" above "from the last run".
  const inProgressCount = (statusCounts["pending"] ?? 0) + (statusCounts["running"] ?? 0);
  // The value holds two segments at most: three ("3 completed, 2 failed, 2 in
  // progress") overflow the tile's 2-line clamp at 1024.
  // In progress is the value only when it is the only status, else the subtitle.
  const inProgressInValue = inProgressCount > 0 && successCount === 0 && failedCount === 0;
  const statusSummary = [
    successCount > 0 && t_i18n("{count, plural, one {# completed} other {# completed}}", { count: successCount }),
    failedCount > 0 && t_i18n("{count, plural, one {# failed} other {# failed}}", { count: failedCount }),
    inProgressInValue && t_i18n("{count, plural, one {# in progress} other {# in progress}}", { count: inProgressCount }),
  ].filter(Boolean).join(", ");
  const runCount = (runs ?? []).length;
  const runsShown = formatCompactNumber(runCount);
  // No run: a dash for the value and "no recent runs" on the subtitle line,
  // which wraps, like "Never triggered" beside it. At text-2xl the sentence
  // was clamped in fr at 1024, and a "from the last" line would
  // measure nothing (fr puts 0 in `one`: "sur la dernière exécution").
  const healthSubtitle = runCount === 0
    ? t_i18n("no recent runs")
    : inProgressCount > 0 && !inProgressInValue
      ? t_i18n("{pending, plural, one {# in progress} other {# in progress}}, from the last {count, plural, one {run} other {{shown} runs}}", {
        pending: inProgressCount,
        count: runCount,
        shown: runsShown,
      })
      : t_i18n("from the last {count, plural, one {run} other {{shown} runs}}", { count: runCount, shown: runsShown });

  return (
    <div className="space-y-6">
      {/* ── KPI Stats ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={BarChart3}
          label="Total Runs"
          value={assignment.execution_count}
          subtitle="all time"
        />
        <AssignmentStatusTile assignment={assignment} />
        <StatCard
          icon={Activity}
          label="Last Triggered"
          value={lastRun ? formatShortDate(lastRun) : "—"}
          subtitle={lastRun ? formatTime(lastRun) : "Never triggered"}
        />
        <StatCard
          icon={CheckCircle2}
          label="Recent Health"
          value={statusSummary || "—"}
          subtitle={healthSubtitle}
        />
      </div>

      {/* ── Run As (company-managed assignments only) ── */}
      <RunAsCard
        assignment={assignment}
        agentHideRuns={agent?.hide_runs ?? false}
        canManage={canManage}
        canChooseRunAs={canChooseRunAs}
        onUpdate={onUpdate}
      />

      {/* ── Trigger ── */}
      <TriggerCard
        assignment={assignment}
        agent={agent}
        catalog={catalog}
        allIntegrations={allIntegrations}
        canManage={canManage}
        onUpdate={onUpdate}
      />

      {/* ── Parameters ── */}
      <ParametersCard
        assignment={assignment}
        agent={agent}
        agentKbs={agentKbs}
        canManage={canManage}
        onUpdate={onUpdate}
      />
    </div>
  );
}
