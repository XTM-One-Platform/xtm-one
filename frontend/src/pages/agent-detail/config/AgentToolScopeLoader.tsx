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

import { useState, useEffect } from "react";
import { ToolScopeDialog } from "@/components/ui/tool-scope-dialog";
import { useIntegrationTools } from "@/hooks/api/useIntegrationApi";
import { useChannelBotTools } from "@/hooks/api/useChannelBotApi";
import { useTestMCPServer } from "@/hooks/api/useMCPServerApi";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { builtinFunctionRows } from "@/lib/builtin-allowed-tools";
import { useFormatter } from "@/i18n";

export function AgentToolScopeLoader({
  type,
  sourceId,
  sourceLabel,
  currentAllowed,
  cachedMcpTools,
  onSave,
  onClose,
}: {
  type: "integration" | "mcp" | "channel-bot" | "builtin";
  sourceId: string;
  sourceLabel: string;
  currentAllowed: string[] | null;
  cachedMcpTools?: { name: string; description: string }[] | null;
  onSave: (allowed: string[] | null) => void;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const integToolsQuery = useIntegrationTools(type === "integration" ? sourceId : "");
  const channelBotToolsQuery = useChannelBotTools(type === "channel-bot" ? sourceId : "");
  const { mutate: testMcp } = useTestMCPServer();
  // A built-in group's functions come from the registry, no request.
  const { describe: describeBuiltinTool, isReady: builtinToolsReady } = useBuiltinTools();
  const needsMcpFetch = type === "mcp" && !cachedMcpTools;
  const [mcpTools, setMcpTools] = useState<{ name: string; description: string }[] | null>(cachedMcpTools ?? null);
  const [mcpLoading, setMcpLoading] = useState(needsMcpFetch);

  // Load MCP tools on mount: use cached if available, otherwise test connection.
  // All deps are stable (props from a fresh-mounted component + TanStack Query's stable mutate ref).
  useEffect(() => {
    if (needsMcpFetch) {
      testMcp(sourceId, {
        onSuccess: (data) => { setMcpTools(data.tools ?? []); setMcpLoading(false); },
        onError: () => setMcpLoading(false),
      });
    }
  }, [needsMcpFetch, sourceId, testMcp]);

  const tools = type === "integration"
    ? (integToolsQuery.data?.tools ?? [])
    : type === "channel-bot"
      ? (channelBotToolsQuery.data?.tools ?? [])
      : type === "builtin"
        ? builtinFunctionRows(describeBuiltinTool(sourceId).functions, t_i18n)
        : (mcpTools ?? []);
  const isLoading = type === "integration"
    ? integToolsQuery.isLoading
    : type === "channel-bot"
      ? channelBotToolsQuery.isLoading
      : type === "builtin"
        ? !builtinToolsReady
        : mcpLoading;

  return (
    <ToolScopeDialog
      tools={tools}
      isLoading={isLoading}
      currentAllowed={currentAllowed}
      sourceLabel={sourceLabel}
      sourceType={type === "integration" ? "integration" : type === "channel-bot" ? "channel bot" : type === "builtin" ? "built-in tool" : "MCP server"}
      contextLabel="agent"
      unit={type === "builtin" ? "function" : "tool"}
      onSave={onSave}
      onClose={onClose}
    />
  );
}
