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

import {
  FileText, Bot, BarChart3, Activity, Tag,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { AgentsUsingCard } from "@/components/ui/agents-using-card";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useBoundAgentRunStats } from "@/hooks/api/useAssignmentApi";
import type { Prompt } from "@/types";
import { formatShortDate, formatTime, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";

// ---------------------------------------------------------------------------
// Overview Card
// ---------------------------------------------------------------------------

function OverviewCard({ prompt }: { prompt: Prompt }) {
  const { t_i18n } = useFormatter();
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <FileText className="h-4 w-4 text-accent" />
          {t_i18n("Overview")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Description */}
        {prompt.description && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Description")}</div>
            <p className="content-base text-default-secondary">{prompt.description}</p>
          </div>
        )}

        {/* Content preview */}
        {prompt.content && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Content Preview")}</div>
            <pre className="content-base text-default-secondary whitespace-pre-wrap max-h-[200px] overflow-y-auto">
              {prompt.content.length > 500 ? prompt.content.slice(0, 500) + "…" : prompt.content}
            </pre>
          </div>
        )}

        {/* Tags */}
        {prompt.tags && prompt.tags.length > 0 && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5"><Tag className="h-3 w-3" />{t_i18n("Tags")}</div>
            <div className="flex flex-wrap gap-1.5">
              {prompt.tags.map((tag) => (
                <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
              ))}
            </div>
          </div>
        )}

        {/* Timestamps */}
        <TimestampGrid createdAt={prompt.created_at} updatedAt={prompt.updated_at} />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Stats Card
// ---------------------------------------------------------------------------

function StatsCard({ prompt }: { prompt: Prompt }) {
  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.prompt_ids?.includes(prompt.id)) || [];
  const boundAgentIds = new Set(boundAgents.map((a) => a.id));
  const { totalRuns, lastRun } = useBoundAgentRunStats(boundAgentIds);

  const contentLength = prompt.content ? prompt.content.length : 0;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard icon={Bot} label="Agents Using" value={boundAgents.length} subtitle={`${pluralize(boundAgents.length, "agent")} connected`} />
      <StatCard icon={FileText} label="Content Length" value={contentLength} subtitle={pluralize(contentLength, "character")} />
      <StatCard icon={BarChart3} label="Assignment Runs" value={totalRuns ?? "—"} subtitle="from agents using this prompt" />
      <StatCard icon={Activity} label="Last Activity" value={lastRun ? formatShortDate(lastRun) : "—"} subtitle={lastRun ? formatTime(lastRun) : "No activity yet"} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Overview
// ---------------------------------------------------------------------------

export function OverviewTab({ prompt }: { prompt: Prompt }) {
  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.prompt_ids?.includes(prompt.id)) || [];

  return (
    <div className="space-y-6">
      <StatsCard prompt={prompt} />
      <OverviewCard prompt={prompt} />
      <AgentsUsingCard agents={boundAgents} entityLabel="Prompt" emptyHint="Bind this prompt to an agent in the agent's settings." />
    </div>
  );
}
