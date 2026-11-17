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

import { useChannelBotTools } from "@/hooks/api/useChannelBotApi";
import { useIntegrationTools } from "@/hooks/api/useIntegrationApi";
import { ToolScopeDialog } from "@/components/ui/tool-scope-dialog";
import { useFormatter } from "@/i18n";

// ---------------------------------------------------------------------------
// AssignmentToolScopeLoader — thin wrapper that resolves + filters tools then
// delegates to the shared ToolScopeDialog component.
// ---------------------------------------------------------------------------
export function AssignmentToolScopeLoader({
  integrationId,
  channelBotId,
  sourceLabel,
  currentAllowed,
  agentAllowed,
  onSave,
  onClose,
}: {
  integrationId?: string;
  channelBotId?: string;
  sourceLabel: string;
  currentAllowed: string[] | null;
  agentAllowed: string[] | null;
  onSave: (allowed: string[] | null) => void;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const integToolsQuery = useIntegrationTools(integrationId ?? "");
  const channelBotToolsQuery = useChannelBotTools(channelBotId ?? "");

  const isChannelBot = !!channelBotId;
  const activeQuery = isChannelBot ? channelBotToolsQuery : integToolsQuery;
  const rawTools = activeQuery.data?.tools ?? [];
  const tools = agentAllowed !== null
    ? rawTools.filter((t) => agentAllowed.includes(t.name))
    : rawTools;

  return (
    <ToolScopeDialog
      tools={tools}
      isLoading={activeQuery.isLoading}
      currentAllowed={currentAllowed}
      sourceLabel={sourceLabel}
      sourceType={isChannelBot ? "channel bot" : "integration"}
      contextLabel="assignment"
      note={agentAllowed !== null
        ? t_i18n("Note: Only showing {count, plural, one {# tool} other {# tools}} allowed by the parent agent's scoping.", { count: agentAllowed.length })
        : undefined}
      onSave={onSave}
      onClose={onClose}
    />
  );
}
