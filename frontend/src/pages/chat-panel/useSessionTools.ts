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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useMe } from "@/hooks/api/useAuthApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { sessionToolsResolved, type SessionToolSelection } from "@/lib/session-tool-selection";
import { eligibleTemplateIntegrations, toggleArrayItem } from "@/lib/utils";
import {
  agentDefaultToolSets,
  flattenDefaultToolSets,
  gaDefaultToolSets,
  type DefaultToolSets,
} from "@/pages/chat/session-tools";
import type { ConversationDetail } from "@/types";

const HIDDEN_INTEGRATIONS_KEY = "copilot:hidden-integrations";

function readHiddenIntegrations(): string[] {
  try {
    const stored = localStorage.getItem(HIDDEN_INTEGRATIONS_KEY);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
}

function writeHiddenIntegrations(ids: string[]): void {
  try {
    localStorage.setItem(HIDDEN_INTEGRATIONS_KEY, JSON.stringify(ids));
  } catch {
    // Storage unavailable: the choice lasts for the page.
  }
}

/** The fields of a message body that carry the session's tool selection. */
/** A message's tool fields; the lists are left out until the selection is resolved. */
interface SessionToolFields extends Partial<SessionToolSelection> {
  default_tool_ids?: string[];
  scope_headers?: Record<string, string>;
}

interface UseSessionToolsOptions {
  /** The agent the next message reaches: the conversation's, else the one picked for a new chat; null is the General Assistant. */
  agentId: string | null;
  /** The conversation on screen, once read; null for a new chat. */
  conversation: ConversationDetail | null | undefined;
  /** A conversation this page just created from a new chat: it keeps the selection made on the draft. */
  createdHere: (conversationId: string) => boolean;
}

/** A selection, order-free, to tell whether it changed since it was applied. */
function selectionKey(sets: DefaultToolSets): string {
  const sorted = (ids: string[]) => [...ids].sort();
  return JSON.stringify([sorted(sets.builtin), sorted(sets.custom), sorted(sets.integration), sorted(sets.mcp), sorted(sets.kb)]);
}

/**
 * A conversation's saved overrides, plus any default added to the agent since they
 * were saved (`default_tool_ids` tells those apart from tools switched off there).
 */
function withSavedOverrides(
  overrides: NonNullable<ConversationDetail["tool_overrides"]>,
  defaults: DefaultToolSets,
  hiddenIntegrationIds: string[],
): DefaultToolSets {
  const baseline = new Set(
    overrides.default_tool_ids ?? [
      ...overrides.builtin_tool_ids,
      ...overrides.custom_tool_ids,
      ...overrides.integration_ids,
      ...(overrides.mcp_server_ids ?? []),
      ...(overrides.knowledge_base_ids ?? []),
    ],
  );
  const withNewDefaults = (saved: string[], current: string[]) => [
    ...new Set([...saved, ...current.filter((id) => !baseline.has(id))]),
  ];
  return {
    builtin: withNewDefaults(overrides.builtin_tool_ids, defaults.builtin),
    custom: withNewDefaults(overrides.custom_tool_ids, defaults.custom),
    integration: withNewDefaults(
      overrides.integration_ids,
      defaults.integration.filter((id) => !hiddenIntegrationIds.includes(id)),
    ),
    mcp: withNewDefaults(overrides.mcp_server_ids ?? [], defaults.mcp),
    kb: withNewDefaults(overrides.knowledge_base_ids ?? [], defaults.kb),
  };
}

/**
 * The session tools of the chat page: what the next message may use, started
 * from the agent's (or the General Assistant's) defaults, restored from a
 * conversation's saved overrides, and sent with each message, on the shared
 * helpers of `pages/chat/session-tools.ts`.
 */
export function useSessionTools({ agentId, conversation, createdHere }: UseSessionToolsOptions) {
  const { data: agents } = useAgents();
  const { data: user } = useMe();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const { data: integrations } = useIntegrations();
  const { data: customTools } = useCustomTools();
  const { data: mcpServers } = useMCPServers();
  // Never a workspace's own knowledge base: it reaches a turn through the workspace.
  const { data: knowledgeBases } = useBindableKnowledgeBases();
  const { offeredInChat, canonicalIds: canonicalBuiltinIds, isReady: builtinToolsReady } = useBuiltinTools();

  const [builtinIds, setBuiltinIds] = useState<string[]>([]);
  const [customToolIds, setCustomToolIds] = useState<string[]>([]);
  const [integrationIds, setIntegrationIds] = useState<string[]>([]);
  const [mcpServerIds, setMcpServerIds] = useState<string[]>([]);
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [scopeValues, setScopeValues] = useState<Record<string, string>>({});
  const [hiddenIntegrationIds, setHiddenIntegrationIds] = useState<string[]>(readHiddenIntegrations);
  // What the selection was last started from, so it is started once per
  // conversation or agent rather than on every refetch.
  const [initializedFor, setInitializedFor] = useState<string | null>(null);
  // The saved overrides the session was started from and the selection that gave: a
  // conversation read again with other overrides (the cached copy it was opened from
  // predated its last send) is started again from them, while nobody changed the
  // selection since.
  const appliedRef = useRef<{ sessionKey: string; overrides: string | null; selection: string } | null>(null);

  const connectedIntegrations = useMemo(
    () => (integrations ?? []).filter((i) => i.status === "connected"),
    [integrations],
  );
  const enabledCustomTools = useMemo(
    () => (customTools ?? []).filter((t) => t.enabled && !t.hide_from_chat),
    [customTools],
  );
  const enabledMCPServers = useMemo(
    () => (mcpServers ?? []).filter((s) => s.enabled && !s.hide_from_chat),
    [mcpServers],
  );
  const availableKBs = useMemo(() => knowledgeBases ?? [], [knowledgeBases]);
  const agent = agentId ? agents?.find((a) => a.id === agentId) : undefined;

  const defaultsFor = useCallback(
    (forAgentId: string | null): DefaultToolSets | null => {
      if (forAgentId) {
        const target = agents?.find((a) => a.id === forAgentId);
        return target ? agentDefaultToolSets(target, connectedIntegrations, user?.id) : null;
      }
      return gaConfig ? gaDefaultToolSets(user, gaConfig, connectedIntegrations) : null;
    },
    [agents, connectedIntegrations, gaConfig, user],
  );

  const apply = useCallback((sets: DefaultToolSets) => {
    setBuiltinIds(sets.builtin);
    setCustomToolIds(sets.custom);
    setIntegrationIds(sets.integration);
    setMcpServerIds(sets.mcp);
    setKbIds(sets.kb);
  }, []);
  const selection = selectionKey({ builtin: builtinIds, custom: customToolIds, integration: integrationIds, mcp: mcpServerIds, kb: kbIds });

  const conversationId = conversation?.id ?? null;
  const overrides = conversation?.tool_overrides ?? null;
  const overridesKey = overrides ? JSON.stringify(overrides) : null;
  // A conversation and its agent: one continued with another agent starts again from the new agent's tools.
  const agentKey = agentId ?? "general";
  const sessionKey = conversationId ? `${conversationId}:${agentKey}` : `new:${agentKey}`;
  useEffect(() => {
    if (!user) return;
    const fromOverrides = (defaults: DefaultToolSets) =>
      overrides ? withSavedOverrides(overrides, defaults, hiddenIntegrationIds) : defaults;
    const record = (sets: DefaultToolSets | null) => {
      appliedRef.current = { sessionKey, overrides: overridesKey, selection: sets ? selectionKey(sets) : selection };
    };
    if (initializedFor === sessionKey) {
      // Read again with other saved overrides, the selection untouched since: start again.
      const applied = appliedRef.current;
      if (!conversationId || !overrides || applied?.sessionKey !== sessionKey) return;
      if (applied.overrides === overridesKey || applied.selection !== selection) return;
      const defaults = defaultsFor(agentId);
      if (!defaults) return;
      const sets = fromOverrides(defaults);
      apply(sets);
      record(sets);
      return;
    }
    // A conversation this page just created keeps what was picked on the draft.
    if (conversationId && createdHere(conversationId) && initializedFor === `new:${agentKey}`) {
      setInitializedFor(sessionKey);
      record(null);
      return;
    }
    const defaults = defaultsFor(agentId);
    if (!defaults) return;
    const sets = conversationId ? fromOverrides(defaults) : defaults;
    apply(sets);
    record(sets);
    setScopeValues({});
    setInitializedFor(sessionKey);
  }, [sessionKey, agentKey, initializedFor, user, conversationId, overrides, overridesKey, agentId, defaultsFor, apply, hiddenIntegrationIds, createdHere, selection]);

  const enableAll = useCallback(() => {
    // Every suite the picker offers, keeping a suite it does not show as configured.
    // Before the registry is known the offered list is empty: the picker keeps
    // the action disabled, and nothing runs here either.
    if (!builtinToolsReady) return;
    const offered = offeredInChat(!!agentId, agent).map((tool) => tool.id);
    setBuiltinIds((prev) => [...new Set([...prev, ...offered])]);
    setCustomToolIds(enabledCustomTools.map((t) => t.id));
    setIntegrationIds(connectedIntegrations.map((i) => i.id));
    setMcpServerIds(enabledMCPServers.map((s) => s.id));
    setKbIds(availableKBs.map((kb) => kb.id));
  }, [builtinToolsReady, offeredInChat, agentId, agent, enabledCustomTools, connectedIntegrations, enabledMCPServers, availableKBs]);

  const disableAll = useCallback(() => {
    apply({ builtin: [], custom: [], integration: [], mcp: [], kb: [] });
  }, [apply]);

  const resetToDefaults = useCallback(() => {
    const defaults = defaultsFor(agentId);
    if (defaults) apply(defaults);
  }, [defaultsFor, agentId, apply]);

  const hideIntegration = useCallback((id: string) => {
    setHiddenIntegrationIds((prev) => {
      const next = prev.includes(id) ? prev : [...prev, id];
      writeHiddenIntegrations(next);
      return next;
    });
    setIntegrationIds((prev) => prev.filter((x) => x !== id));
  }, []);

  const unhideIntegration = useCallback((id: string) => {
    setHiddenIntegrationIds((prev) => {
      const next = prev.filter((x) => x !== id);
      writeHiddenIntegrations(next);
      return next;
    });
    setIntegrationIds((prev) => (prev.includes(id) ? prev : [...prev, id]));
  }, []);

  const changeScope = useCallback(
    (provider: string, integrationId: string) => {
      setScopeValues((prev) => ({ ...prev, [provider]: integrationId }));
      // One platform of a scoped provider replaces all of them; "All platforms"
      // brings back every instance a per-user binding may use.
      setIntegrationIds((prev) => {
        const providerIds = new Set(connectedIntegrations.filter((i) => i.provider === provider).map((i) => i.id));
        const filtered = prev.filter((id) => !providerIds.has(id));
        if (integrationId) return [...filtered, integrationId];
        return [...filtered, ...eligibleTemplateIntegrations(provider, connectedIntegrations, user?.id).map((i) => i.id)];
      });
    },
    [connectedIntegrations, user?.id],
  );

  // Read at send time, so the latest selection always goes out.
  const latest = useRef({ builtinIds, customToolIds, integrationIds, mcpServerIds, kbIds, scopeValues, connectedIntegrations, defaultsFor, sessionKey, initializedFor });
  latest.current = { builtinIds, customToolIds, integrationIds, mcpServerIds, kbIds, scopeValues, connectedIntegrations, defaultsFor, sessionKey, initializedFor };

  /** The tool fields of a message to *forAgentId*. */
  const requestFields = useCallback((forAgentId: string | null): SessionToolFields => {
    const s = latest.current;
    const headers: Record<string, string> = {};
    for (const integ of s.connectedIntegrations) {
      const cfg = integ.scope_config;
      if (cfg?.resolve_from_header && s.scopeValues[integ.provider]) {
        headers[cfg.resolve_from_header.toLowerCase()] = s.scopeValues[integ.provider];
      }
    }
    const scope = Object.keys(headers).length > 0 ? { scope_headers: headers } : {};
    // Not started yet for this conversation and agent (their tools still
    // loading): no selection, so the server runs the agent's own tools and
    // saves nothing, where empty lists would strip them.
    if (!sessionToolsResolved(s.sessionKey, s.initializedFor)) return scope;
    // The defaults the selection was made against, so a reopened conversation
    // tells a tool added to the agent since apart from one switched off here.
    const defaults = s.defaultsFor(forAgentId);
    return {
      builtin_tool_ids: s.builtinIds,
      custom_tool_ids: s.customToolIds,
      integration_ids: s.integrationIds,
      mcp_server_ids: s.mcpServerIds,
      knowledge_base_ids: s.kbIds,
      ...(defaults ? { default_tool_ids: flattenDefaultToolSets(defaults) } : {}),
      ...scope,
    };
  }, []);

  return {
    agents,
    initialized: initializedFor === sessionKey,
    pickerProps: {
      sessionBuiltinIds: builtinIds,
      sessionCustomToolIds: customToolIds,
      sessionIntegrationIds: integrationIds,
      sessionMCPServerIds: mcpServerIds,
      sessionKBIds: kbIds,
      // A former id in the session toggles as its group's chip.
      onToggleBuiltin: (id: string) => setBuiltinIds((prev) => toggleArrayItem(canonicalBuiltinIds(prev), id)),
      onToggleCustomTool: (id: string) => setCustomToolIds((prev) => toggleArrayItem(prev, id)),
      onToggleIntegration: (id: string) => setIntegrationIds((prev) => toggleArrayItem(prev, id)),
      onToggleMCPServer: (id: string) => setMcpServerIds((prev) => toggleArrayItem(prev, id)),
      onToggleKB: (id: string) => setKbIds((prev) => toggleArrayItem(prev, id)),
      connectedIntegrations,
      enabledCustomTools,
      enabledMCPServers,
      availableKBs,
      hiddenIntegrationIds,
      onHideIntegration: hideIntegration,
      onUnhideIntegration: unhideIntegration,
      onEnableAll: enableAll,
      onDisableAll: disableAll,
      onResetDefault: resetToDefaults,
      selectedAgent: agentId,
      agents,
      scopeValues,
      onScopeChange: changeScope,
    },
    toolCount: builtinIds.length + customToolIds.length + integrationIds.length + mcpServerIds.length + kbIds.length,
    requestFields,
  };
}
