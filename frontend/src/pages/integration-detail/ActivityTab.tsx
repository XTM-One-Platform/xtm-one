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

import { EntityActivityTab } from "@/components/ui/entity-activity-tab";
import { useAgents } from "@/hooks/api/useAgentApi";
import { isA2AProvider } from "@/hooks/api/useA2AApi";
import type { Integration } from "@/types";

// ---------------------------------------------------------------------------
// Tab: Activity
// ---------------------------------------------------------------------------

export function ActivityTab({ integration }: { integration: Integration }) {
  const { data: agents } = useAgents({ scope: "all" });

  // An A2A integration's activity is the work its partner submitted, scoped by
  // the registration recorded on each run. Bound-agent filtering cannot express
  // that: bind both A2A integrations to one agent — the ordinary case, since one
  // agent may serve a Google partner and an Azure one — and each would display
  // the other's traffic alongside every internal run of that agent.
  const isA2A = isA2AProvider(integration.provider);

  const boundAgentIds = new Set(
    (agents ?? [])
      .filter((a) => a.integration_bindings && Object.values(a.integration_bindings).flat().includes(integration.id))
      .map((a) => a.id),
  );

  return (
    <EntityActivityTab
      boundAgentIds={boundAgentIds}
      entityLabel="integration"
      a2aIntegrationId={isA2A ? integration.id : undefined}
    />
  );
}
