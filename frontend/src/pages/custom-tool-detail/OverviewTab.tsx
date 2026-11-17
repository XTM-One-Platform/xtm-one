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
  Code2, Bot, Zap, Activity, Tag, GraduationCap,
} from "lucide-react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatCard } from "@/components/ui/stat-card";
import { ToolGateCard } from "@/pages/tools/ToolGateCard";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { AgentsUsingCard } from "@/components/ui/agents-using-card";
import { ResourceSharingCard } from "@/components/ui/resource-sharing-card";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useCustomToolSkills } from "@/hooks/api/useCustomToolApi";
import type { CustomTool } from "@/types";
import { formatShortDate, formatTime, pluralize, reachesOthers } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { parseSchemaProps } from "./schema-utils";

// ---------------------------------------------------------------------------
// Overview Card
// ---------------------------------------------------------------------------

function OverviewCard({ tool }: { tool: CustomTool }) {
  const { t_i18n } = useFormatter();
  const paramCount = tool.input_schema
    ? Object.keys(tool.input_schema.properties ?? {}).length
    : 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Code2 className="h-4 w-4 text-accent" /> {t_i18n("Overview")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Description */}
        {tool.description && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Description")}</div>
            <p className="content-base text-default-secondary">{tool.description}</p>
          </div>
        )}

        {/* Language & Timeout */}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-sm bg-elevation-hover p-3 flex items-center gap-3">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Language")}</div>
            <Badge variant="outline" className="content-caption">
              {tool.language === "python" ? "Python" : "JavaScript"}
            </Badge>
          </div>
          <div className="rounded-sm bg-elevation-hover p-3 flex items-center gap-3">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Execution Timeout")}</div>
            <Badge variant="outline" className="content-caption">
              {tool.timeout >= 60
                ? `${Math.floor(tool.timeout / 60)}m${tool.timeout % 60 ? ` ${tool.timeout % 60}s` : ""}`
                : `${tool.timeout}s`}
            </Badge>
          </div>
        </div>

        {/* Parameters */}
        {paramCount > 0 && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Input Parameters ({count})", { count: paramCount })}</div>
            <div className="space-y-1.5">
              {parseSchemaProps(tool.input_schema).map((p) => (
                <div key={p.name} className="flex items-center gap-2">
                  <code className="shrink-0 rounded-sm bg-accent/10 px-1.5 py-0.5 text-[11px] font-mono-plex text-accent">
                    {tool.language === "python" ? p.name : `$${p.name}`}
                  </code>
                  <Badge variant="outline" className="text-[9px]">{p.type}</Badge>
                  {p.required && <Badge variant="cyan" className="text-[8px]">{t_i18n("required")}</Badge>}
                  {p.description && <span className="content-caption text-default-secondary truncate">{p.description}</span>}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Tags */}
        {tool.tags && tool.tags.length > 0 && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5"><Tag className="h-3 w-3" />{t_i18n("Tags")}</div>
            <div className="flex flex-wrap gap-1.5">
              {tool.tags.map((tag) => (
                <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
              ))}
            </div>
          </div>
        )}

        {/* Timestamps */}
        <TimestampGrid createdAt={tool.created_at} updatedAt={tool.updated_at} />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Stats Card
// ---------------------------------------------------------------------------

function StatsCard({ tool }: { tool: CustomTool }) {
  const { t_i18n } = useFormatter();
  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.custom_tool_ids?.includes(tool.id)) || [];

  const lastCalled = tool.last_called_at ? new Date(tool.last_called_at) : null;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard icon={Bot} label="Agents Using" value={boundAgents.length} subtitle={t_i18n("{count, plural, one {agent connected} other {agents connected}}", { count: boundAgents.length })} />
      <StatCard icon={Zap} label="Tool Calls" value={tool.call_count} subtitle={t_i18n("across {unit} connected", { unit: t_i18n(pluralize(boundAgents.length, "agent")) })} />
      <StatCard icon={Activity} label="Last Activity" value={lastCalled ? formatShortDate(lastCalled) : "—"} subtitle={lastCalled ? formatTime(lastCalled) : "No calls yet"} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Overview
// ---------------------------------------------------------------------------

export function OverviewTab({ tool }: { tool: CustomTool }) {
  const { t_i18n } = useFormatter();
  const { data: agents } = useAgents({ scope: "all" });
  const { data: linkedSkills } = useCustomToolSkills(tool.id);
  const boundAgents = agents?.filter((a) => a.custom_tool_ids?.includes(tool.id)) || [];

  return (
    <div className="space-y-6">
      <StatsCard tool={tool} />
      <OverviewCard tool={tool} />
      {/* Whether calling it pauses, and the two ways to change that (FR-045).
          Keyed by name, which is what the gate looks a tool up by. */}
      <ToolGateCard
        toolKeys={[tool.name]}
        affectsOthers={reachesOthers(tool)}
      />
      <AgentsUsingCard agents={boundAgents} entityLabel="Tool" emptyHint="Bind this tool to an agent in the agent's settings." />
      {/* To whom it is shared and through what: its managers only. */}
      <ResourceSharingCard kind="custom_tool" id={tool.id} enabled={tool.can_manage !== false} />
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <GraduationCap className="h-4 w-4 text-accent" /> {t_i18n("Skills using this tool")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {!linkedSkills || linkedSkills.length === 0 ? (
            <p className="content-base text-default-secondary">{t_i18n("No skills are linked to this tool.")}</p>
          ) : (
            <div className="space-y-2">
              {linkedSkills.map((item) => (
                <div key={`${item.skill_id}-${item.resource_path}`} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <Link to={`/skills/${item.skill_id}`} className="content-base font-medium text-accent hover:underline">
                        {item.skill_name}
                      </Link>
                      {item.skill_description && (
                        <p className="content-compact text-default-secondary mt-0.5 line-clamp-2">{item.skill_description}</p>
                      )}
                    </div>
                    <Badge variant="outline" className="content-caption shrink-0">{item.resource_kind}</Badge>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="content-caption">{item.resource_path}</Badge>
                    <span className="content-caption text-default-secondary">{t_i18n("linked {date}", { date: formatShortDate(item.linked_at) })}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
