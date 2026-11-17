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

import { useFormatter } from "@/i18n";
import { getAgentRequiredTemplateProviders } from "@/lib/utils";
import { useCustomToolsEnabled } from "@/hooks/usePlatformConfig";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { SessionToolsMenu } from "@/pages/chat/SessionToolsMenu";
import type { Agent, Integration, CustomTool, MCPServer, KnowledgeBase } from "@/types";

/** A chat's session tools and what changes them (`useSessionTools().pickerProps`). */
export interface SessionToolPickerProps {
  sessionBuiltinIds: string[];
  sessionCustomToolIds: string[];
  sessionIntegrationIds: string[];
  sessionMCPServerIds: string[];
  sessionKBIds: string[];
  onToggleBuiltin: (id: string) => void;
  onToggleCustomTool: (id: string) => void;
  onToggleIntegration: (id: string) => void;
  onToggleMCPServer: (id: string) => void;
  onToggleKB: (id: string) => void;
  connectedIntegrations: Integration[];
  enabledCustomTools: CustomTool[];
  enabledMCPServers: MCPServer[];
  availableKBs: KnowledgeBase[];
  hiddenIntegrationIds: string[];
  onHideIntegration: (id: string) => void;
  onUnhideIntegration: (id: string) => void;
  onEnableAll: () => void;
  onDisableAll: () => void;
  onResetDefault: () => void;
  selectedAgent: string | null;
  agents: Agent[] | undefined;
  // Platform scope: selected integration ID per scoped provider (from platform registrations)
  scopeValues: Record<string, string>;
  onScopeChange: (provider: string, integrationId: string) => void;
}

/**
 * The composer's Session tools button: the built-in suites, custom tools,
 * integrations, MCP servers and knowledge bases the next message may use, in
 * `SessionToolsMenu` (one sub-menu per kind of tool, #9406).
 */
export function SessionToolPicker({
  disabled,
  count,
  selectedAgent,
  agents,
  connectedIntegrations,
  enabledCustomTools,
  enabledMCPServers,
  availableKBs,
  ...picker
}: SessionToolPickerProps & { disabled: boolean; count: number }) {
  const { t_i18n } = useFormatter();
  const { offeredInChat, carries: carriesBuiltin, isReady: builtinToolsReady, isError: builtinToolsFailed } = useBuiltinTools();
  const customToolsEnabled = useCustomToolsEnabled();

  // When an agent is selected, only offer integrations bound to that agent
  // (static bindings + template-resolved). Pipeline tool refs (prepare/finalize)
  // are loaded independently by the backend and are NOT part of the LLM tool belt.
  const agentObj = agents?.find((a) => a.id === selectedAgent);
  const directBindingIds = Object.values(agentObj?.integration_bindings ?? {}).flat();
  const templateResolvedIds = getAgentRequiredTemplateProviders(agentObj).flatMap((provider) =>
    connectedIntegrations.filter((i) => i.provider === provider).map((i) => i.id),
  );
  const agentBoundIds = agentObj ? new Set([...directBindingIds, ...templateResolvedIds]) : null;
  const chatIntegrations = agentBoundIds
    ? connectedIntegrations.filter((i) => agentBoundIds.has(i.id))
    : connectedIntegrations;

  return (
    <SessionToolsMenu
      {...picker}
      label={t_i18n("Session tools")}
      disabled={disabled}
      count={count}
      builtinTools={builtinToolsReady ? offeredInChat(!!selectedAgent, agentObj) : []}
      builtinToolsReady={builtinToolsReady}
      builtinToolsFailed={builtinToolsFailed}
      carriesBuiltin={carriesBuiltin}
      customTools={customToolsEnabled ? enabledCustomTools : []}
      connectedIntegrations={connectedIntegrations}
      chatIntegrations={chatIntegrations}
      mcpServers={enabledMCPServers}
      knowledgeBases={availableKBs}
    />
  );
}
