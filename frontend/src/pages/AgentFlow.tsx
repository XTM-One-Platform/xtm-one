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

import { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { PageSkeleton } from "@/components/ui/skeleton";
import { BackButton } from "@/components/ui/back-button";
import { AgentFlowGraph } from "@/components/ui/agent-flow";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useActiveAgentIds } from "@/hooks/api/useAssignmentApi";
import { useFormatter } from "@/i18n";

export default function AgentFlow() {
  const { t_i18n } = useFormatter();
  const { data: agents, isLoading } = useAgents();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const { data: activeAgentIdList } = useActiveAgentIds();
  const activeAgentIds = useMemo(
    () => new Set(activeAgentIdList ?? []),
    [activeAgentIdList],
  );

  return (
    <div className="space-y-6">
      <BackButton to="/agents" label="Back to Agents" />
      <h1 className="text-3xl font-bold">{t_i18n("Agent Flow")}</h1>

      {isLoading ? (
        <PageSkeleton cards={1} />
      ) : (
        <Card className="overflow-hidden">
          <CardContent className="p-0">
            <AgentFlowGraph agents={agents ?? []} generalAssistant={gaConfig} activeAgentIds={activeAgentIds} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
