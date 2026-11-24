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

import { useNavigate } from "react-router-dom";
import { ArrowRight, Bot } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { ICON_MAP } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Agent } from "@/types";

interface AgentsUsingCardProps {
  /** Pre-filtered list of agents using this entity. */
  agents: Agent[];
  /** Human-readable entity label, e.g. "Integration", "Server", "Tool", "Knowledge Base". */
  entityLabel: string;
  /** Optional secondary hint shown in the empty state. */
  emptyHint?: string;
}

export function AgentsUsingCard({ agents, entityLabel, emptyHint }: AgentsUsingCardProps) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Bot className="h-4 w-4 text-accent" />
          {t_i18n("Agents Using This {entity}", { entity: t_i18n(entityLabel) })}
          <CountBadge variant="cyan" className="ml-auto">{agents.length}</CountBadge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {agents.length === 0 ? (
          <div className="text-center py-6">
            <Bot className="h-8 w-8 text-default-disabled mx-auto mb-2" />
            {/* The label is translated as passed - transforming the message id
                (e.g. toLowerCase) would miss catalog keys and break lookups in
                non-English locales; translations own their own casing. */}
            <p className="content-base text-default-secondary">{t_i18n("No agents are using this {entity} yet.", { entity: t_i18n(entityLabel) })}</p>
            {emptyHint && <p className="content-compact text-default-secondary mt-1">{t_i18n(emptyHint)}</p>}
          </div>
        ) : (
          <div className="space-y-2">
            {agents.map((agent) => {
              const IconComp = ICON_MAP[agent.icon] ?? Bot;
              return (
                <button
                  key={agent.id}
                  onClick={() => navigate(`/agents/${agent.id}`)}
                  className="w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 text-left transition-colors hover:bg-elevation-highlight group"
                >
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                    <IconComp className="h-4 w-4 text-accent" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="content-base font-medium truncate">{agent.name}</p>
                    <p className="content-caption text-default-secondary">
                      {t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: agent.assignment_count })}
                    </p>
                  </div>
                  <Badge variant={agent.enabled ? "success" : "outline"} className="text-[9px] shrink-0">
                    {agent.enabled ? t_i18n("Active") : t_i18n("Disabled")}
                  </Badge>
                  <ArrowRight className="h-3.5 w-3.5 text-default-disabled group-hover:text-accent transition-colors shrink-0" />
                </button>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
