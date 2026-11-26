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

import { platformOrigin } from "@/lib/base-path";
import { useState, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useSectionParam } from "@/hooks/useSectionParam";
import { PICK_UNIT_PARAM } from "@/lib/roi-outcomes";
import { Bot, Wrench, BookOpen, Globe, Target } from "lucide-react";
import { OutcomesCard } from "@/components/roi/OutcomesCard";
import { hasStructuredOutput } from "@/components/roi/roi-outcomes";
import { SectionNav, type SectionNavItem } from "@/components/ui/section-nav";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { ToolApprovalCard } from "@/pages/agent-detail/config/ToolApprovalCard";
import { BuiltinLayersCard } from "@/components/fleets/BuiltinLayersCard";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { SharedBanner } from "@/components/ui/shared-banner";
import { AgentToolScopeLoader } from "./config/AgentToolScopeLoader";
import { GeneralSection } from "./config/GeneralSection";
import { IntentsSection } from "./config/IntentsSection";
import { ToolingSection } from "./config/ToolingSection";
import { prunedLiveWrites, type LiveWritesMode } from "./config/LiveWritesControl";
import { BrowserProfileSelectorCard } from "./config/BrowserProfileSelectorCard";
import { RunnerSelectorCard } from "./config/RunnerSelectorCard";
import { KnowledgeSection, type MemoryTierField } from "./config/KnowledgeSection";
import { ChannelsSection } from "./config/ChannelsSection";
import { useToast } from "@/components/ui/toast";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUpdateAgent, useAgents, useGenerateSuggestedPrompts } from "@/hooks/api/useAgentApi";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useChannelBots, useChannelBotChannelsMap } from "@/hooks/api/useChannelBotApi";
import { useIntegrations, useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useBindableKnowledgeBases, useKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useAvailableModels, useGeneralAssistantConfig, useLLMProviders } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { toggledInAgentConfig } from "@/lib/builtin-tool-offers";
import { builtinAllowedPayload, readsOtherAgentsTraces, sameBuiltinAllowed, scopedFunctions, withGroupScope, type BuiltinAllowedTools } from "@/lib/builtin-allowed-tools";
import { normalizeScopeEnforcement, withScopeEnforcement } from "@/lib/element-scope";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { explicitHandoverTypes, runsEveryHandoverType, withHandoverType } from "@/lib/handover-types";
import { agentModelChoice, isEntityShared, parseModelChoice, resolvesPerUser, ROUTING_MODEL_CHOICE, toggleArrayItem } from "@/lib/utils";
import { isToolScopeKeyActive, toolScopeKey } from "@/lib/tool-scope";
import { CHANNEL_ROUTING_ICON_KEY, EXTERNAL_CHANNEL_TYPES, COPY_FEEDBACK_DELAY, MAX_ITERATIONS_DEFAULT, clampMaxIterations, isCoordinationBuiltinTool } from "@/lib/constants";
import { accessDraft, normalizeAccess, sameAccess, type Access as CoordinationAccessDraft } from "@/lib/coordination-access";
import { useFormatter } from "@/i18n";
import type { Agent, ChannelBot, IntegrationElement, ChannelConfig, ChannelEntry, OrganizationContextMode } from "@/types";

type JsonMap = Record<string, unknown>;

type DiffPayload<TTarget extends object> = ({ id: string; commit_message?: string } & Partial<TTarget>);

function normalizeNullableObject<T extends JsonMap>(value: T | null | undefined): T | null {
  if (!value || Object.keys(value).length === 0) return null;
  return value;
}

function buildMappedDiffPayload<TNormalized extends JsonMap, TTarget extends object>(args: {
  id: string;
  current: TNormalized;
  baseline: TNormalized;
  fieldMap: Record<keyof TNormalized, keyof TTarget>;
  commitMessage?: string;
}): DiffPayload<TTarget> {
  const payload = { id: args.id } as DiffPayload<TTarget>;

  for (const [sourceKey, targetKey] of Object.entries(args.fieldMap) as [keyof TNormalized, keyof TTarget][]) {
    const nextValue = args.current[sourceKey];
    const prevValue = args.baseline[sourceKey];
    if (JSON.stringify(nextValue) !== JSON.stringify(prevValue)) {
      (payload as Record<string, unknown>)[targetKey as string] = nextValue;
    }
  }

  if (args.commitMessage?.trim()) {
    payload.commit_message = args.commitMessage.trim();
  }

  return payload;
}

// Sub-tab navigation within the configuration panel (URL-backed via path segment)
type ConfigSection = "general" | "tooling" | "knowledge" | "channels" | "outcomes";

const CONFIG_SECTIONS: readonly SectionNavItem<ConfigSection>[] = [
  { key: "general", label: "General", icon: Bot },
  { key: "tooling", label: "Tooling", icon: Wrench },
  { key: "knowledge", label: "Knowledge", icon: BookOpen },
  { key: "channels", label: "Channels & Bots", icon: Globe },
  // The unit of work the agent's runs count under in ROI (#9536, design dev-docs/roi-outcomes.md 6.1).
  { key: "outcomes", label: "Outcomes", icon: Target },
];

// ---------------------------------------------------------------------------
// Configuration Tab
// ---------------------------------------------------------------------------
export function ConfigTab({
  agent,
  canManage,
  canManageIntegrations,
}: {
  agent: Agent;
  canManage: boolean;
  // A built-in's integration configuration is admin-editable while the rest of
  // it is not (see `_is_integration_binding_change` on the backend), so this
  // cannot ride on `canManage`, which is hard-false for built-ins in XTM One.
  canManageIntegrations: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const update = useUpdateAgent();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();
  const { describe: describeBuiltinTool, carries: carriesBuiltin, canonicalIds: canonicalBuiltinIds } = useBuiltinTools();
  const isXtmOne = useIsXtmOne();
  const isShared = isEntityShared(agent, user?.id);
  // Each picker offers the viewer's library set plus what the agent is already
  // bound to: an admin editing a colleague's agent sees, keeps and can unbind
  // every binding, including rows only the admin scope holds.
  const { data: visibleIntegrations } = useIntegrations();
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const integrations = useMemo(
    () => withBoundEntities(visibleIntegrations, allIntegrations, Object.values(agent.integration_bindings ?? {}).flat()),
    [visibleIntegrations, allIntegrations, agent.integration_bindings],
  );
  const { data: visibleChannelBots } = useChannelBots();
  const { data: allChannelBots } = useChannelBots({ scope: "all" });
  const channelBots = useMemo(
    () => withBoundEntities(
      visibleChannelBots,
      allChannelBots,
      Object.values(agent.channel_config ?? {}).map((entry) => entry.channel_bot_id),
    ),
    [visibleChannelBots, allChannelBots, agent.channel_config],
  );
  const { data: visibleMcpServers } = useMCPServers();
  const { data: allMcpServers } = useMCPServers({ scope: "all" });
  const mcpServers = useMemo(
    () => withBoundEntities(visibleMcpServers, allMcpServers, agent.mcp_server_ids),
    [visibleMcpServers, allMcpServers, agent.mcp_server_ids],
  );
  const { data: visibleCustomTools } = useCustomTools();
  const { data: allCustomTools } = useCustomTools({ scope: "all" });
  const customTools = useMemo(
    () => withBoundEntities(visibleCustomTools, allCustomTools, agent.custom_tool_ids),
    [visibleCustomTools, allCustomTools, agent.custom_tool_ids],
  );
  const { data: visibleKbs } = useBindableKnowledgeBases();
  const { data: allKbs } = useKnowledgeBases({ scope: "all" });
  const kbs = useMemo(
    () => withBoundEntities(visibleKbs, allKbs, agent.knowledge_base_ids),
    [visibleKbs, allKbs, agent.knowledge_base_ids],
  );
  const { data: visibleAgents } = useAgents();
  const { data: everyAgent } = useAgents({ scope: "all" });
  const allAgents = useMemo(
    () => withBoundEntities(visibleAgents, everyAgent, [
      ...(agent.handover_agent_ids ?? []),
      ...(agent.work_item_owner_agent_ids ?? []),
      ...(agent.trace_supervision_target_agent_ids ?? []),
      ...(agent.coordination_access?.read_agent_ids ?? []),
      ...(agent.coordination_access?.write_agent_ids ?? []),
    ]),
    [visibleAgents, everyAgent, agent.handover_agent_ids, agent.work_item_owner_agent_ids, agent.trace_supervision_target_agent_ids, agent.coordination_access],
  );
  const { data: authConfig } = useAuthConfig();
  const runnersEnabled = useRunnersEnabled();
  const { data: availableModels } = useAvailableModels({ scope: "all" });
  const { data: llmProviders } = useLLMProviders();
  const { data: gaConfig } = useGeneralAssistantConfig();
  // Clamp into [1, 100] so a legacy/out-of-band GA value never feeds the agent
  // slider an out-of-range platform-default readout; the server also clamps on save.
  const platformMaxIterations = clampMaxIterations(gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT);
  const [persona, setPersona] = useState(agent.persona || "");
  const [generalCommitMessage, setGeneralCommitMessage] = useState("");
  const [name, setName] = useState(agent.name);
  const [icon, setIcon] = useState(agent.icon);
  const [description, setDescription] = useState(agent.description || "");
  const [slug, setSlug] = useState(agent.slug || "");
  // The picker carries `<provider_id>::<model>`, so the saved reference is the
  // config the admin actually selected. Deriving it from the model id instead
  // picks an arbitrary one of the configs advertising it.
  // `ROUTING_MODEL_CHOICE` runs the agent on the platform routing model.
  const [llmChoice, setLlmChoice] = useState(agentModelChoice(agent));
  const onRoutingModel = llmChoice === ROUTING_MODEL_CHOICE;
  const llmModel = useMemo(
    () => (onRoutingModel ? "" : parseModelChoice(llmChoice).model),
    [llmChoice, onRoutingModel],
  );
  const llmProviderId = useMemo(
    () => (onRoutingModel ? null : parseModelChoice(llmChoice).providerId),
    [llmChoice, onRoutingModel],
  );
  const [outputFormat, setOutputFormat] = useState(agent.output_format || "markdown");
  const [outputSchema, setOutputSchema] = useState<Record<string, unknown> | null>(agent.output_schema || null);
  const [temperature, setTemperature] = useState<number | null>(agent.temperature);
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(agent.reasoning_effort);
  const [organizationContextMode, setOrganizationContextMode] = useState<OrganizationContextMode>(agent.organization_context_mode ?? "inherit");
  const [maxIterations, setMaxIterations] = useState<number | null>(agent.max_iterations);
  const [guardrails, setGuardrails] = useState(agent.guardrails || "");
  const [bindings, setBindings] = useState<Record<string, string[]>>(agent.integration_bindings || {});
  const [templateBindings, setTemplateBindings] = useState<string[]>(agent.integration_template_bindings || []);
  // Per-user providers kept to their read tools (#10741).
  const [templateReadOnly, setTemplateReadOnly] = useState<string[]>(agent.integration_template_read_only || []);
  // OpenCTI Live writes per binding (#11958): absent = the integration's setting.
  const [liveWrites, setLiveWrites] = useState<Record<string, "on" | "off">>(agent.integration_live_writes || {});
  const [mcpIds, setMcpIds] = useState<string[]>(agent.mcp_server_ids || []);
  const [customToolIds, setCustomToolIds] = useState<string[]>(agent.custom_tool_ids || []);
  const [builtinToolIds, setBuiltinToolIds] = useState<string[]>(agent.builtin_tool_ids || []);
  const [workItemOwnerIds, setWorkItemOwnerIds] = useState<string[]>(agent.work_item_owner_agent_ids || []);
  const [traceSupervisionTargetIds, setTraceSupervisionTargetIds] = useState<string[]>(agent.trace_supervision_target_agent_ids || []);
  const [coordinationDraft, setCoordinationDraft] = useState<CoordinationAccessDraft>(accessDraft(agent.coordination_access));
  const [promptIds, setPromptIds] = useState<string[]>(agent.prompt_ids || []);
  const [skillIds, setSkillIds] = useState<string[]>(agent.skill_ids || []);
  const [skillLoadModes, setSkillLoadModes] = useState<Record<string, "progressive" | "always">>(
    agent.skill_load_modes || {},
  );
  const [kbIds, setKbIds] = useState<string[]>(agent.knowledge_base_ids || []);
  const [kbPermissions, setKbPermissions] = useState<Record<string, "read" | "write">>(
    agent.knowledge_base_permissions || {},
  );
  const [handoverIds, setHandoverIds] = useState<string[]>(agent.handover_agent_ids || []);
  const [handoverTypes, setHandoverTypes] = useState<Record<string, string>>(agent.handover_types || {});
  // Set when editing starts: the saved targets have no type, so each runs every type.
  const [handoverLegacy, setHandoverLegacy] = useState(false);
  const [handoverTypeConfirm, confirmHandoverTypes] = useConfirm();
  const [handoverConditions, setHandoverConditions] = useState<Record<string, string>>(agent.handover_conditions || {});
  const [tags, setTags] = useState<string[]>(agent.tags || []);
  const [memoryShortTerm, setMemoryShortTerm] = useState(agent.memory_short_term_enabled);
  const [memoryMediumTerm, setMemoryMediumTerm] = useState(agent.memory_medium_term_enabled);
  const [memoryPersistent, setMemoryPersistent] = useState(agent.memory_persistent_enabled);
  const [memoryUser, setMemoryUser] = useState(agent.memory_user_enabled);
  const [disableChat, setDisableChat] = useState(agent.disable_chat);
  const [hideRuns, setHideRuns] = useState(agent.hide_runs);
  const [hideActivity, setHideActivity] = useState(agent.hide_activity_from_non_admins ?? false);
  const [humanInTheLoop, setHumanInTheLoop] = useState(agent.human_in_the_loop);
  const [hitlPrompt, setHitlPrompt] = useState<string | null>(agent.human_in_the_loop_prompt);
  const [companyManaged, setCompanyManaged] = useState(agent.is_company_managed);
  const [sharedGroupIds, setSharedGroupIds] = useState<string[] | null>(agent.shared_group_ids ?? null);
  const [sharedGroupPermissions, setSharedGroupPermissions] = useState<Record<string, string> | null>(agent.shared_group_permissions ?? null);
  // Per-user grants + access restriction. This section saves each
  // sharing control immediately rather than through a batched edit form, so
  // these mirror the group handlers above instead of using
  // ``useUserShareGrants`` (which models an edit session).
  const [sharedUserIds, setSharedUserIds] = useState<string[] | null>(agent.shared_user_ids ?? null);
  const [sharedUserPermissions, setSharedUserPermissions] = useState<Record<string, string> | null>(agent.shared_user_permissions ?? null);
  const [accessRestricted, setAccessRestricted] = useState<boolean>(agent.access_restricted ?? false);
  const [embedDialogOpen, setEmbedDialogOpen] = useState(false);
  const [snippetCopied, setSnippetCopied] = useState(false);

  const { data: catalog } = useProviderCatalog();

  // Tool-level scoping state
  const [integAllowed, setIntegAllowed] = useState<Record<string, string[] | null>>(agent.integration_allowed_tools || {});
  const [mcpAllowed, setMcpAllowed] = useState<Record<string, string[] | null>>(agent.mcp_allowed_tools || {});
  const [builtinAllowed, setBuiltinAllowed] = useState<BuiltinAllowedTools>(agent.builtin_allowed_tools || {});
  const [toolScopeDialog, setToolScopeDialog] = useState<{ type: "integration" | "mcp" | "channel-bot" | "builtin"; id: string; label: string } | null>(null);

  // Element bindings state
  const [editingElements, setEditingElements] = useState(false);
  const [elementDraft, setElementDraft] = useState<Record<string, IntegrationElement[]>>(agent.integration_element_bindings || {});
  const [enforcementDraft, setEnforcementDraft] = useState<string[]>(agent.integration_element_enforcement ?? []);

  // ---- Per-section editing state ----
  const [personaExpanded, setPersonaExpanded] = useState(false);
  const [editingGeneral, setEditingGeneral] = useState(false);
  const [editingGuardrails, setEditingGuardrails] = useState(false);
  const [editingIntegrations, setEditingIntegrations] = useState(false);
  const [editingMcp, setEditingMcp] = useState(false);
  const [editingBuiltinTools, setEditingBuiltinTools] = useState(false);
  const [editingCustomTools, setEditingCustomTools] = useState(false);
  const [editingKbs, setEditingKbs] = useState(false);
  const [editingPrompts, setEditingPrompts] = useState(false);
  const [editingSkills, setEditingSkills] = useState(false);
  const [editingHandover, setEditingHandover] = useState(false);
  const [editingChannels, setEditingChannels] = useState(false);

  // Channel/bot config state
  const [channelConfigDraft, setChannelConfigDraft] = useState<ChannelConfig>(agent.channel_config || {});

  const [rawSection, setConfigSection] = useSectionParam<ConfigSection>("general");
  const configSection: ConfigSection = CONFIG_SECTIONS.some(({ key }) => key === rawSection)
    ? rawSection
    : "general";
  // "Pick a unit" from the agent Overview's ROI opens Outcomes on the unit picker (#9810).
  const [searchParams] = useSearchParams();
  const pickUnit = searchParams.get(PICK_UNIT_PARAM.key) === PICK_UNIT_PARAM.value;

  // Tooling search filters
  const [integrationSearch, setIntegrationSearch] = useState("");
  const [mcpSearch, setMcpSearch] = useState("");
  const [customToolSearch, setCustomToolSearch] = useState("");
  // Knowledge search filters
  const [kbSearch, setKbSearch] = useState("");
  const [handoverSearch, setHandoverSearch] = useState("");

  const connectedIntegrations = integrations?.filter((i) => i.status === "connected" || i.status === "configured") || [];

  const availableIntegrations = connectedIntegrations
    .filter((i) => {
      if (companyManaged && !agent.is_builtin && !i.is_company_managed) return false;
      if (integrationSearch) {
        const q = integrationSearch.toLowerCase();
        return i.name.toLowerCase().includes(q) || i.provider.toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const connectedBots = useMemo(() => (channelBots ?? []).filter((b) => b.status === "connected"), [channelBots]);
  const availableBots = useMemo(() => connectedBots.filter((b) => {
    if (companyManaged) return true;
    if (isAdmin) return true;
    return (b.allowed_receive_channels ?? []).length > 0 || (b.allowed_publish_channels ?? []).length > 0;
  }), [connectedBots, companyManaged, isAdmin]);
  const restrictedBots = !companyManaged && !isAdmin
    ? connectedBots.filter((b) => {
        const hasAnyWhitelist = (b.allowed_receive_channels ?? []).length > 0 || (b.allowed_publish_channels ?? []).length > 0;
        return !hasAnyWhitelist;
      })
    : [];

  const activeBotIds = useMemo(() => {
    if (!editingChannels) return [];
    return availableBots
      .filter((b) => {
        const chType = EXTERNAL_CHANNEL_TYPES.find(
          (ct) => CHANNEL_ROUTING_ICON_KEY[ct] === b.provider,
        );
        return chType && !!channelConfigDraft[chType];
      })
      .map((b) => b.id);
  }, [editingChannels, availableBots, channelConfigDraft]);
  const { map: platformChannelsMap, loadingMap: channelsLoadingMap } = useChannelBotChannelsMap(activeBotIds);

  const availableMcpServers = (mcpServers ?? [])
    .filter((s) => {
      if (!s.enabled) return false;
      if (companyManaged && !agent.is_builtin && !s.is_company_managed) return false;
      if (mcpSearch) {
        const q = mcpSearch.toLowerCase();
        return s.name.toLowerCase().includes(q) || (s.transport ?? "").toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  const availableKbs = (kbs ?? [])
    .filter((kb) => {
      if (companyManaged && !agent.is_builtin && !kb.is_company_managed) return false;
      if (kbSearch) {
        const q = kbSearch.toLowerCase();
        return kb.name.toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const toggleMcp = (id: string) => {
    setMcpIds((prev) => {
      if (prev.includes(id)) {
        setMcpAllowed((ma) => { const n = { ...ma }; delete n[id]; return n; });
        return prev.filter((x) => x !== id);
      }
      return [...prev, id];
    });
  };
  const toggleCustomTool = (id: string) => setCustomToolIds((prev) => toggleArrayItem(prev, id));
  const toggleBuiltinTool = (id: string) =>
    setBuiltinToolIds((prev) => toggledInAgentConfig(prev, describeBuiltinTool(id), agent.builtin_tool_ids ?? []));
  const toggleWorkItemOwner = (id: string) => setWorkItemOwnerIds((prev) => toggleArrayItem(prev, id));
  const toggleTraceSupervisionTarget = (id: string) => setTraceSupervisionTargetIds((prev) => toggleArrayItem(prev, id));
  const toggleKb = (id: string) => {
    setKbIds((prev) => {
      if (prev.includes(id)) {
        // Detaching: drop any write grant so it never lingers. Decide from
        // `prev` (not the outer `kbIds` closure, which can be stale under
        // rapid/concurrent toggles) so the permission is only dropped on detach.
        setKbPermissions((perms) => {
          if (!perms[id]) return perms;
          const next = { ...perms };
          delete next[id];
          return next;
        });
        return prev.filter((x) => x !== id);
      }
      return [...prev, id];
    });
  };
  const setKbPermission = (id: string, perm: "read" | "write") =>
    setKbPermissions((prev) => {
      const next = { ...prev };
      if (perm === "write") next[id] = "write";
      else delete next[id];
      return next;
    });
  const toggleHandover = (id: string) => {
    const next = toggleArrayItem(handoverIds, id);
    setHandoverIds(next);
    // Legacy only while it has targets: a first target is typed (`runsEveryHandoverType`).
    if (next.length === 0) setHandoverLegacy(false);
  };
  const selectableHandoverIds = useMemo(
    () => new Set(
      (allAgents ?? [])
        .filter((a) => a.id !== agent.id && a.enabled && (!companyManaged || a.is_company_managed))
        .map((a) => a.id),
    ),
    [allAgents, agent.id, companyManaged],
  );
  const handoverCandidates = (allAgents ?? [])
    .filter((a) => {
      if (a.id === agent.id || !a.enabled) return false;
      // When editing a company-managed agent, handover targets must also be
      // company-managed. Still surface the current user's OWN non-company-managed
      // agents so they can be promoted inline (other users' private agents stay
      // hidden); they render as non-selectable with a promote action until
      // promoted.
      if (companyManaged && !a.is_company_managed && a.user_id !== user?.id) return false;
      if (handoverSearch) {
        const q = handoverSearch.toLowerCase();
        return a.name.toLowerCase().includes(q) || (a.slug ?? "").toLowerCase().includes(q) || (a.description ?? "").toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a, b) => {
      // Selectable agents first, then promotable (non-company-managed) ones,
      // alphabetical within each group.
      const aRank = selectableHandoverIds.has(a.id) ? 0 : 1;
      const bRank = selectableHandoverIds.has(b.id) ? 0 : 1;
      if (aRank !== bRank) return aRank - bRank;
      return a.name.localeCompare(b.name);
    });

  const toggleIntegration = (integId: string, provider: string) => {
    const allIds = Object.values(bindings).flat();
    const isBound = allIds.includes(integId);
    if (isBound) {
      setBindings((prev) => {
        const next: Record<string, string[]> = {};
        for (const [k, ids] of Object.entries(prev)) {
          const filtered = ids.filter((id) => id !== integId);
          if (filtered.length > 0) next[k] = filtered;
        }
        return next;
      });
      // An OpenAPI integration's scope is its own (see lib/tool-scope.ts);
      // other providers share one scope per provider.
      setIntegAllowed((prev) => { const n = { ...prev }; delete n[toolScopeKey({ id: integId, provider })]; return n; });
    } else {
      setBindings((prev) => ({
        ...prev,
        [provider]: [...(prev[provider] || []), integId],
      }));
      setTemplateBindings((prev) => prev.filter((p) => p !== provider));
    }
  };

  const setTemplateProviderReadOnly = (provider: string, on: boolean) =>
    setTemplateReadOnly((prev) => (on ? [...prev.filter((p) => p !== provider), provider] : prev.filter((p) => p !== provider)));

  const setBindingLiveWrites = (key: string, mode: LiveWritesMode) =>
    setLiveWrites((prev) => {
      const rest = Object.fromEntries(Object.entries(prev).filter(([k]) => k !== key));
      return mode === "inherit" ? rest : { ...rest, [key]: mode };
    });

  const toggleTemplateBinding = (provider: string) => {
    const isTemplate = templateBindings.includes(provider);
    if (isTemplate) {
      setTemplateBindings((prev) => prev.filter((p) => p !== provider));
      setTemplateReadOnly((prev) => prev.filter((p) => p !== provider));
    } else {
      setTemplateBindings((prev) => [...prev, provider]);
      setBindings((prev) => {
        const next = { ...prev };
        delete next[provider];
        return next;
      });
      setIntegAllowed((prev) => { const n = { ...prev }; delete n[provider]; return n; });
    }
  };

  const updateElementDraft = (key: string, items: IntegrationElement[]) => {
    setElementDraft((prev) => {
      const next = { ...prev };
      if (items.length > 0) {
        next[key] = items;
      } else {
        delete next[key];
      }
      return next;
    });
  };

  const updateHandoverType = async (agentId: string, type: string) => {
    if (handoverLegacy) {
      // A type is set per target, so the first one types every target.
      const change = withHandoverType(handoverIds, {}, agentId, type);
      if (change.narrowed.length > 0) {
        const names = change.narrowed.map((id) => allAgents?.find((a) => a.id === id)?.name ?? t_i18n("Unknown agent"));
        const confirmed = await confirmHandoverTypes({
          title: t_i18n("Give each agent a handover type?"),
          description: t_i18n(
            "Without handover types, this agent can delegate to, consult or transfer to each selected agent. Types are set per agent, so picking one sets them all: {count, plural, one {{names} becomes Delegate} other {{names} become Delegate}}. You can change each one before you save.",
            { count: change.narrowed.length, names: intl.formatList(names, { type: "conjunction" }) },
          ),
          confirmLabel: t_i18n("Set types"),
          variant: "warning",
        });
        if (!confirmed) return;
      }
      setHandoverLegacy(false);
      setHandoverTypes(change.types);
    } else {
      setHandoverTypes((prev) => ({ ...prev, [agentId]: type }));
    }
    // Clear condition if type is no longer mandatory_transfer
    if (type !== "mandatory_transfer") {
      setHandoverConditions((prev) => {
        const next = { ...prev };
        delete next[agentId];
        return next;
      });
    }
  };

  const updateHandoverCondition = (agentId: string, condition: string) => {
    setHandoverConditions((prev) => ({ ...prev, [agentId]: condition }));
  };

  const handleCompanyManagedToggle = (val: boolean) => {
    setCompanyManaged(val);
    if (val) {
      const cmMcpSet = new Set((mcpServers ?? []).filter((s) => s.is_company_managed).map((s) => s.id));
      const cmKbSet = new Set((kbs ?? []).filter((kb) => kb.is_company_managed).map((kb) => kb.id));
      const cmIntegSet = new Set((integrations ?? []).filter((i) => i.is_company_managed).map((i) => i.id));
      setMcpIds((prev) => prev.filter((id) => cmMcpSet.has(id)));
      setMcpAllowed((prev) => {
        const next: Record<string, string[] | null> = {};
        for (const [k, v] of Object.entries(prev)) { if (cmMcpSet.has(k)) next[k] = v; }
        return next;
      });
      setKbIds((prev) => prev.filter((id) => cmKbSet.has(id)));
      setKbPermissions((prev) => {
        const next: Record<string, "read" | "write"> = {};
        for (const [k, v] of Object.entries(prev)) { if (cmKbSet.has(k)) next[k] = v; }
        return next;
      });
      setBindings((prev) => {
        const next: Record<string, string[]> = {};
        for (const [k, ids] of Object.entries(prev)) { const filtered = ids.filter((iid) => cmIntegSet.has(iid)); if (filtered.length > 0) next[k] = filtered; }
        return next;
      });
      setIntegAllowed((prev) => {
        const cmProviders = new Set((integrations ?? []).filter((i) => i.is_company_managed).map((i) => i.provider));
        const next: Record<string, string[] | null> = {};
        for (const [k, v] of Object.entries(prev)) { if (isToolScopeKeyActive(k, cmProviders, cmIntegSet)) next[k] = v; }
        return next;
      });
    }
  };

  // ---- Per-section save handlers ----
  const handleSaveGeneral = async () => {
    const payload = buildGeneralUpdatePayload();

    if (Object.keys(payload).length === 1) {
      toast({ title: t_i18n("No changes to save"), variant: "info" });
      return;
    }
    // A variable referenced in the persona or the HITL prompt resolves in the
    // agent's runs.
    const { persona: nextPersona, human_in_the_loop_prompt: nextHitl } = payload as Partial<Agent>;
    const prompts = {
      ...(nextPersona !== undefined ? { persona: nextPersona } : {}),
      ...(nextHitl !== undefined ? { human_in_the_loop_prompt: nextHitl } : {}),
    };
    if (Object.keys(prompts).length && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: prompts }))) return;

    update.mutate(
      payload,
      {
        onSuccess: () => {
          toast({ title: t_i18n("General settings saved"), variant: "success" });
          setEditingGeneral(false);
          setGeneralCommitMessage("");
        },
        onError: (error) => {
          toast({ title: t_i18n("Could not save general settings"), description: error.message, variant: "error" });
        },
      },
    );
  };
  const handleSaveGuardrails = async () => {
    // A variable referenced here resolves in the agent's runs.
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { guardrails } }))) return;
    update.mutate(
      { id: agent.id, guardrails: guardrails || undefined } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Guardrails saved"), variant: "success" }); setEditingGuardrails(false); } },
    );
  };
  const handleSaveIntegrations = async () => {
    const { payload, cleanedAllowed } = buildIntegrationsUpdatePayload();

    setIntegAllowed(cleanedAllowed);

    if (Object.keys(payload).length === 1) {
      toast({ title: t_i18n("No changes to save"), variant: "info" });
      return;
    }
    // Fixed bindings, and per-user ones the agent's assignments resolve to
    // whom they act as.
    const { integration_bindings: nextBindings, integration_template_bindings: nextTemplates } = payload as Partial<Agent>;
    const bindingChange = {
      ...(nextBindings !== undefined ? { integration_bindings: nextBindings } : {}),
      ...(nextTemplates !== undefined ? { integration_template_bindings: nextTemplates } : {}),
    };
    if (Object.keys(bindingChange).length && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: bindingChange }))) {
      return;
    }

    update.mutate(
      payload,
      { onSuccess: () => { toast({ title: t_i18n("Integrations saved"), variant: "success" }); setEditingIntegrations(false); setIntegrationSearch(""); } },
    );
  };
  const handleSaveMcp = async () => {
    const payload = buildMcpUpdatePayload();

    if (Object.keys(payload).length === 1) {
      toast({ title: t_i18n("No changes to save"), variant: "info" });
      return;
    }
    const nextMcp = (payload as Partial<Agent>).mcp_server_ids;
    if (nextMcp && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { mcp_server_ids: nextMcp } }))) return;

    update.mutate(
      payload,
      { onSuccess: () => { toast({ title: t_i18n("MCP servers saved"), variant: "success" }); setEditingMcp(false); setMcpSearch(""); } },
    );
  };
  const handleSaveElements = (draft: Record<string, IntegrationElement[]>) => {
    const cleaned = Object.fromEntries(Object.entries(draft).filter(([, v]) => v.length > 0));
    update.mutate(
      {
        id: agent.id,
        integration_element_bindings: Object.keys(cleaned).length > 0 ? cleaned : null,
        integration_element_enforcement: normalizeScopeEnforcement(enforcementDraft, cleaned),
      } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Target elements saved"), variant: "success" }); setEditingElements(false); } },
    );
  };
  const resetElementDrafts = () => {
    setElementDraft(agent.integration_element_bindings || {});
    setEnforcementDraft(agent.integration_element_enforcement ?? []);
  };
  const startEditingElements = () => { resetElementDrafts(); setEditingElements(true); };
  const cancelEditingElements = () => { resetElementDrafts(); setEditingElements(false); };
  const toggleEnforcement = (key: string, enforced: boolean) => {
    setEnforcementDraft((prev) => withScopeEnforcement(prev, key, enforced));
  };
  const handleSaveBuiltinTools = () => {
    // Persist the work-item peer roster alongside the tool selection, but only
    // when the Work Items tool is enabled. Omitting it otherwise leaves the
    // stored roster untouched, so toggling the tool off (or saving while it's
    // unchecked) doesn't irreversibly wipe an unrelated roster config.
    // The trace supervision allow-list follows the same preserve-on-disable
    // pattern: it's inert while other agents' traces are off (the tool isn't
    // injected) and never widens visibility beyond trace_visible_query, so
    // keeping it avoids wiping a curated list on an accidental toggle.
    const payload: { id: string } & Partial<Agent> = {
      id: agent.id,
      builtin_tool_ids: builtinToolIds,
    };
    // Per-function scoping: only sent when it changed, so a save that
    // only toggles groups never rewrites it; a group switched off drops its entry.
    const allowed = builtinAllowedPayload(builtinAllowed, builtinToolIds);
    if (!sameBuiltinAllowed(allowed, agent.builtin_allowed_tools ?? null)) {
      payload.builtin_allowed_tools = allowed;
    }
    if (carriesBuiltin(builtinToolIds, "coordination")) {
      payload.work_item_owner_agent_ids = workItemOwnerIds;
    }
    if (readsOtherAgentsTraces(builtinToolIds, builtinAllowed)) {
      payload.trace_supervision_target_agent_ids = traceSupervisionTargetIds;
    }
    // Coordination access follows the same preserve-on-disable pattern, and is
    // only sent when it changed so an unrelated save never rewrites it.
    if (companyManaged && builtinToolIds.some(isCoordinationBuiltinTool)) {
      const coordination = normalizeAccess(coordinationDraft, agent.id);
      if (!sameAccess(coordination, agent.coordination_access ?? null)) {
        payload.coordination_access = coordination;
      }
    }
    update.mutate(
      payload,
      { onSuccess: () => { toast({ title: t_i18n("Built-in tools saved"), variant: "success" }); setEditingBuiltinTools(false); } },
    );
  };
  const handleSaveCustomTools = async () => {
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { custom_tool_ids: customToolIds } }))) return;
    update.mutate(
      { id: agent.id, custom_tool_ids: customToolIds } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Custom tools saved"), variant: "success" }); setEditingCustomTools(false); setCustomToolSearch(""); } },
    );
  };
  const handleSaveKbs = async () => {
    // Only keep write grants for KBs that are still attached.
    const cleanedPermissions = Object.fromEntries(
      Object.entries(kbPermissions).filter(([id]) => kbIds.includes(id)),
    ) as Record<string, "read" | "write">;
    // What a knowledge base syncs with is searched for the agent's audience.
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { knowledge_base_ids: kbIds } }))) return;
    update.mutate(
      {
        id: agent.id,
        knowledge_base_ids: kbIds,
        knowledge_base_permissions: cleanedPermissions,
      } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Knowledge bases saved"), variant: "success" }); setEditingKbs(false); setKbSearch(""); } },
    );
  };
  const handleSaveHandover = () => {
    // Until the agents are known a stale id cannot be told from a bound one.
    const effectiveHandoverIds = allAgents === undefined
      ? handoverIds
      : handoverIds.filter((id) => selectableHandoverIds.has(id));
    if (effectiveHandoverIds.length !== handoverIds.length) {
      setHandoverIds(effectiveHandoverIds);
    }
    // A legacy draft stays empty: filling it in would narrow every target to delegate.
    const cleanTypes: Record<string, string> = handoverLegacy
      ? {}
      : explicitHandoverTypes(effectiveHandoverIds, handoverTypes);
    // Only keep conditions for mandatory_transfer targets
    const cleanConditions: Record<string, string> = {};
    for (const id of effectiveHandoverIds) {
      if (cleanTypes[id] === "mandatory_transfer" && handoverConditions[id]?.trim()) {
        cleanConditions[id] = handoverConditions[id].trim();
      }
    }

    // Validate: when multiple mandatory transfers, at most one may omit a condition
    const mandatoryIds = effectiveHandoverIds.filter(id => cleanTypes[id] === "mandatory_transfer");
    if (mandatoryIds.length > 1) {
      const withoutCondition = mandatoryIds.filter(id => !cleanConditions[id]);
      if (withoutCondition.length > 1) {
        toast({ title: t_i18n("Missing routing conditions"), description: t_i18n("When multiple mandatory transfers are configured, at most one target may omit a condition (fallback). Please add conditions to the others."), variant: "error" });
        return;
      }
    }

    // Validate: no duplicate conditions
    const conditionValues = Object.values(cleanConditions).map(c => c.toLowerCase());
    const seen = new Set<string>();
    for (const c of conditionValues) {
      if (seen.has(c)) {
        toast({ title: t_i18n("Duplicate routing condition"), description: t_i18n("Each mandatory transfer target must have a unique condition."), variant: "error" });
        return;
      }
      seen.add(c);
    }

    update.mutate(
      { id: agent.id, handover_agent_ids: effectiveHandoverIds, handover_types: cleanTypes, handover_conditions: cleanConditions } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Agent handover saved"), variant: "success" }); setEditingHandover(false); setHandoverSearch(""); } },
    );
  };
  const handleSaveMemoryTier = (tier: MemoryTierField, val: boolean) => {
    if (tier === "memory_short_term_enabled") setMemoryShortTerm(val);
    if (tier === "memory_medium_term_enabled") setMemoryMediumTerm(val);
    if (tier === "memory_persistent_enabled") setMemoryPersistent(val);
    if (tier === "memory_user_enabled") setMemoryUser(val);
    update.mutate(
      { id: agent.id, [tier]: val } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: t_i18n(val ? "Memory tier enabled" : "Memory tier disabled"), variant: "success" }) },
    );
  };
  const handleSaveCompanyManaged = async (val: boolean) => {
    // The worksets the agent owns move to its tier with it: a personal
    // corpus becomes readable by every user, which the confirmation says.
    const worksetNote = carriesBuiltin(agent.builtin_tool_ids, "coordination")
      ? t_i18n("The worksets this agent has built become company-managed with it: every user can read them in Operations > Worksets.")
      : undefined;
    if (val && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, is_company_managed: true }, { confirmLabel: t_i18n("Make company-managed"), note: worksetNote }))) {
      return;
    }
    const prevBindings = { ...bindings };
    const prevMcpIds = [...mcpIds];
    const prevKbIds = [...kbIds];
    const prevKbPermissions = { ...kbPermissions };
    const prevIntegAllowed = { ...integAllowed };
    const prevMcpAllowed = { ...mcpAllowed };
    const prevSharedGroupIds = sharedGroupIds;
    const prevSharedGroupPermissions = sharedGroupPermissions;
    handleCompanyManagedToggle(val);
    if (val) { setSharedGroupIds(null); setSharedGroupPermissions(null); }
    update.mutate(
      { id: agent.id, is_company_managed: val, ...(val ? { shared_group_ids: null, shared_group_permissions: null } : {}) } as { id: string } & Partial<Agent>,
      {
        onSuccess: () => toast({ title: val ? t_i18n("Agent is now company-managed") : t_i18n("Agent is now private"), variant: "success" }),
        onError: () => {
          setCompanyManaged(!val);
          setBindings(prevBindings);
          setMcpIds(prevMcpIds);
          setKbIds(prevKbIds);
          setKbPermissions(prevKbPermissions);
          setIntegAllowed(prevIntegAllowed);
          setMcpAllowed(prevMcpAllowed);
          setSharedGroupIds(prevSharedGroupIds);
          setSharedGroupPermissions(prevSharedGroupPermissions);
        },
      },
    );
  };
  const handleSaveSharedGroups = async (ids: string[] | null) => {
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, shared_group_ids: ids }))) return;
    setSharedGroupIds(ids);
    update.mutate(
      { id: agent.id, shared_group_ids: ids && ids.length > 0 ? ids : null } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: ids !== null ? t_i18n("Group sharing updated") : t_i18n("Group sharing disabled"), variant: "success" }) },
    );
  };
  const handleSaveSharedUsers = async (ids: string[] | null) => {
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, shared_user_ids: ids }))) return;
    setSharedUserIds(ids);
    update.mutate(
      { id: agent.id, shared_user_ids: ids && ids.length > 0 ? ids : null } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: ids !== null ? t_i18n("Sharing updated") : t_i18n("Sharing disabled"), variant: "success" }) },
    );
  };
  const handleSaveSharedUserPermissions = (perms: Record<string, string> | null) => {
    setSharedUserPermissions(perms);
    update.mutate(
      { id: agent.id, shared_user_permissions: perms && Object.keys(perms).length > 0 ? perms : null } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: t_i18n("Permissions updated"), variant: "success" }) },
    );
  };
  const handleSaveAccessRestricted = async (value: boolean) => {
    if (!value && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, access_restricted: false }))) return;
    setAccessRestricted(value);
    update.mutate(
      { id: agent.id, access_restricted: value } as { id: string } & Partial<Agent>,
      {
        onSuccess: () =>
          toast({
            title: value ? t_i18n("Access restricted") : t_i18n("Access restriction removed"),
            variant: "success",
          }),
      },
    );
  };
  const handleSaveSharedGroupPermissions = (perms: Record<string, string> | null) => {
    setSharedGroupPermissions(perms);
    update.mutate(
      { id: agent.id, shared_group_permissions: perms && Object.keys(perms).length > 0 ? perms : null } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: t_i18n("Group permissions updated"), variant: "success" }) },
    );
  };
  const handleSaveOwner = async (userId: string) => {
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, new_owner_id: userId }, { confirmLabel: t_i18n("Transfer") }))) return;
    update.mutate(
      { id: agent.id, user_id: userId } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
    );
  };
  // Lend the owner's personal integrations through this agent only: the
  // people it is shared with use them in their chats with it, never in their
  // own (an agent-scoped grant, not a share of the integration itself).
  const handleLendIntegrations = (integrationIds: string[]) => {
    update.mutate(
      { id: agent.id, lent_integration_ids: integrationIds } as { id: string } & Partial<Agent> & { lent_integration_ids: string[] },
      {
        onSuccess: () => toast({
          title: integrationIds.length
            ? t_i18n("This agent now uses your integrations in the chats of the people it is shared with")
            : t_i18n("This agent no longer uses your integrations in their chats"),
          variant: "success",
        }),
      },
    );
  };
  // Revert a personal integration binding from "shared" back to "per-user":
  // drop the fixed integration UUIDs for the affected providers and add those
  // providers to the per-user (template) bindings, so every user who runs the
  // agent uses their OWN integration of the same provider instead of the
  // owner's credentials. This is the rollback path.
  const handleMakeBindingsPerUser = async (integrationIds: string[]) => {
    // A provider with no per-user form (OpenAPI: each integration is its own
    // API) keeps its specific binding — converting it would unbind the API.
    const convertible = integrationIds.filter((iid) => {
      const integ = integrations?.find((i) => i.id === iid);
      return !!integ && resolvesPerUser(integ.provider);
    });
    const idsToRemove = new Set(convertible);
    // Providers of the integrations actually being converted.
    const affectedProviders = new Set<string>();
    for (const iid of convertible) {
      const integ = integrations?.find((i) => i.id === iid);
      if (integ) affectedProviders.add(integ.provider);
    }
    if (affectedProviders.size === 0) return;
    // Derive from the current local binding state (not the persisted agent) so
    // any unsaved edits are preserved, and capture the previous state so the
    // optimistic update can be rolled back if the mutation fails.
    const prevBindings = bindings;
    const prevTemplate = templateBindings;
    // Drop ONLY the selected integration IDs — keep any other binding under the
    // same provider (e.g. a company-managed or second-instance integration) so
    // converting one personal integration doesn't silently unbind the rest.
    const nextBindings: Record<string, string[]> = {};
    for (const [prov, ids] of Object.entries(bindings)) {
      const kept = ids.filter((id) => !idsToRemove.has(id));
      if (kept.length > 0) nextBindings[prov] = kept;
    }
    // Switch a provider to per-user (template) resolution only once it has no
    // fixed binding left — a remaining fixed binding still takes precedence.
    const providersToTemplate = [...affectedProviders].filter((prov) => !nextBindings[prov]?.length);
    const nextTemplate = Array.from(new Set([...templateBindings, ...providersToTemplate]));
    const switchedCount = Object.values(prevBindings).flat().filter((id) => idsToRemove.has(id)).length;
    // Who stops using them, and what the agent's assignments keep using: they
    // resolve a per-user binding to whom they act as.
    if (!(await confirmSharing(
      { entity_type: "agent", entity_id: agent.id, change: { integration_bindings: nextBindings, integration_template_bindings: nextTemplate } },
      { mode: "unshare", confirmLabel: t_i18n("Switch to per-user") },
    ))) {
      return;
    }
    setBindings(nextBindings);
    setTemplateBindings(nextTemplate);
    update.mutate(
      { id: agent.id, integration_bindings: nextBindings, integration_template_bindings: nextTemplate } as { id: string } & Partial<Agent>,
      {
        onSuccess: () => toast({ title: t_i18n("Switched {count, plural, one {# integration} other {# integrations}} to per-user", { count: switchedCount }), variant: "success" }),
        onError: (error) => {
          setBindings(prevBindings);
          setTemplateBindings(prevTemplate);
          toast({ title: t_i18n("Could not switch to per-user"), description: error.message, variant: "error" });
        },
      },
    );
  };
  const handlePublicCatalogToggle = (val: boolean) => {
    update.mutate(
      { id: agent.id, is_public_catalog: val } as { id: string } & Partial<Agent>,
      { onSuccess: () => toast({ title: val ? t_i18n("Published to catalog") : t_i18n("Removed from catalog"), variant: "success" }) },
    );
  };
  const handleCatalogPersonalToolsToggle = (val: boolean) => {
    update.mutate(
      { id: agent.id, catalog_include_personal_tools: val } as { id: string } & Partial<Agent>,
      {
        onSuccess: () => toast({
          title: val
            ? t_i18n("Catalog download includes your personal custom tools")
            : t_i18n("Catalog download leaves out your personal custom tools"),
          variant: "success",
        }),
      },
    );
  };

  // ---- Start / Cancel editing (resets local state from agent) ----
  const startEditingGeneral = () => {
    setName(agent.name); setIcon(agent.icon); setDescription(agent.description || "");
    setSlug(agent.slug || ""); setTags(agent.tags || []); setLlmChoice(agentModelChoice(agent));
    setTemperature(agent.temperature); setReasoningEffort(agent.reasoning_effort); setOrganizationContextMode(agent.organization_context_mode ?? "inherit"); setMaxIterations(agent.max_iterations); setPersona(agent.persona || ""); setOutputFormat(agent.output_format || "markdown"); setOutputSchema(agent.output_schema || null); setDisableChat(agent.disable_chat); setHideRuns(agent.hide_runs); setHideActivity(agent.hide_activity_from_non_admins ?? false);
    setHumanInTheLoop(agent.human_in_the_loop); setHitlPrompt(agent.human_in_the_loop_prompt);
    setGeneralCommitMessage("");
    setEditingGeneral(true);
  };
  const cancelEditingGeneral = () => {
    setName(agent.name); setIcon(agent.icon); setDescription(agent.description || "");
    setSlug(agent.slug || ""); setTags(agent.tags || []); setLlmChoice(agentModelChoice(agent));
    setTemperature(agent.temperature); setReasoningEffort(agent.reasoning_effort); setOrganizationContextMode(agent.organization_context_mode ?? "inherit"); setMaxIterations(agent.max_iterations); setPersona(agent.persona || ""); setOutputFormat(agent.output_format || "markdown"); setOutputSchema(agent.output_schema || null); setDisableChat(agent.disable_chat); setHideRuns(agent.hide_runs); setHideActivity(agent.hide_activity_from_non_admins ?? false);
    setHumanInTheLoop(agent.human_in_the_loop); setHitlPrompt(agent.human_in_the_loop_prompt);
    setGeneralCommitMessage("");
    setEditingGeneral(false);
  };
  const startEditingGuardrails = () => { setGuardrails(agent.guardrails || ""); setEditingGuardrails(true); };
  const cancelEditingGuardrails = () => { setGuardrails(agent.guardrails || ""); setEditingGuardrails(false); };
  const startEditingIntegrations = () => {
    setBindings(agent.integration_bindings || {}); setIntegAllowed(agent.integration_allowed_tools || {});
    setTemplateBindings(agent.integration_template_bindings || []);
    setTemplateReadOnly(agent.integration_template_read_only || []);
    setLiveWrites(agent.integration_live_writes || {});
    setEditingIntegrations(true);
  };
  const cancelEditingIntegrations = () => {
    setBindings(agent.integration_bindings || {}); setIntegAllowed(agent.integration_allowed_tools || {});
    setTemplateBindings(agent.integration_template_bindings || []);
    setTemplateReadOnly(agent.integration_template_read_only || []);
    setLiveWrites(agent.integration_live_writes || {});
    setEditingIntegrations(false); setIntegrationSearch("");
  };
  const handleSaveChannels = async () => {
    const cleanConfig = Object.keys(channelConfigDraft).length > 0 ? channelConfigDraft : null;
    // A channel bot, and channels answering people without an account, widen
    // who the agent's bindings serve.
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { channel_config: cleanConfig } }))) return;
    update.mutate(
      { id: agent.id, channel_config: cleanConfig } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Channel configuration saved"), variant: "success" }); setEditingChannels(false); } },
    );
  };
  const startEditingChannels = () => {
    setChannelConfigDraft(agent.channel_config || {});
    setEditingChannels(true);
  };
  const cancelEditingChannels = () => {
    setChannelConfigDraft(agent.channel_config || {});
    setEditingChannels(false);
  };

  const toggleChannelBot = (chType: string, botInteg: ChannelBot) => {
    setChannelConfigDraft((prev) => {
      const next = { ...prev };
      if (next[chType]) {
        delete next[chType];
      } else {
        next[chType] = {
          channel_bot_id: botInteg.id,
          receive_enabled: false,
          publish_enabled: true,
          receive_channels: [],
          publish_channels: [],
          allowed_tools: null,
        };
      }
      return next;
    });
  };

  const updateChannelEntry = (chType: string, updates: Partial<ChannelEntry>) => {
    setChannelConfigDraft((prev) => ({
      ...prev,
      [chType]: { ...prev[chType], ...updates },
    }));
  };
  const startEditingMcp = () => {
    setMcpIds(agent.mcp_server_ids || []); setMcpAllowed(agent.mcp_allowed_tools || {});
    setEditingMcp(true);
  };
  const cancelEditingMcp = () => {
    setMcpIds(agent.mcp_server_ids || []); setMcpAllowed(agent.mcp_allowed_tools || {});
    setEditingMcp(false); setMcpSearch("");
  };
  const startEditingBuiltinTools = () => {
    // A [DEPRECATED] former id edits as its group card.
    setBuiltinToolIds(canonicalBuiltinIds(agent.builtin_tool_ids));
    setBuiltinAllowed(agent.builtin_allowed_tools || {});
    setWorkItemOwnerIds(agent.work_item_owner_agent_ids || []);
    setTraceSupervisionTargetIds(agent.trace_supervision_target_agent_ids || []);
    setCoordinationDraft(accessDraft(agent.coordination_access));
    setEditingBuiltinTools(true);
  };
  const cancelEditingBuiltinTools = () => {
    setBuiltinToolIds(agent.builtin_tool_ids || []);
    setBuiltinAllowed(agent.builtin_allowed_tools || {});
    setWorkItemOwnerIds(agent.work_item_owner_agent_ids || []);
    setTraceSupervisionTargetIds(agent.trace_supervision_target_agent_ids || []);
    setCoordinationDraft(accessDraft(agent.coordination_access));
    setEditingBuiltinTools(false);
  };
  const startEditingCustomTools = () => { setCustomToolIds(agent.custom_tool_ids || []); setEditingCustomTools(true); };
  const cancelEditingCustomTools = () => { setCustomToolIds(agent.custom_tool_ids || []); setEditingCustomTools(false); setCustomToolSearch(""); };
  const startEditingKbs = () => { setKbIds(agent.knowledge_base_ids || []); setKbPermissions(agent.knowledge_base_permissions || {}); setEditingKbs(true); setKbSearch(""); };
  const cancelEditingKbs = () => { setKbIds(agent.knowledge_base_ids || []); setKbPermissions(agent.knowledge_base_permissions || {}); setEditingKbs(false); setKbSearch(""); };
  const startEditingPrompts = () => { setPromptIds(agent.prompt_ids || []); setEditingPrompts(true); };
  const cancelEditingPrompts = () => { setPromptIds(agent.prompt_ids || []); setEditingPrompts(false); };
  const handleSavePrompts = async () => {
    // An attached prompt's variables resolve in the agent's runs.
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { prompt_ids: promptIds } }))) return;
    update.mutate(
      { id: agent.id, prompt_ids: promptIds } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Prompts saved"), variant: "success" }); setEditingPrompts(false); } },
    );
  };
  const startEditingSkills = () => { setSkillIds(agent.skill_ids || []); setSkillLoadModes(agent.skill_load_modes || {}); setEditingSkills(true); };
  const cancelEditingSkills = () => { setSkillIds(agent.skill_ids || []); setSkillLoadModes(agent.skill_load_modes || {}); setEditingSkills(false); };
  const handleSaveSkills = async () => {
    // A skill's variables and private source reach the agent's audience.
    if (!(await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { skill_ids: skillIds } }))) return;
    update.mutate(
      { id: agent.id, skill_ids: skillIds, skill_load_modes: skillLoadModes } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Skills saved"), variant: "success" }); setEditingSkills(false); } },
    );
  };
  const startEditingHandover = () => {
    const existingHandoverIds = agent.handover_agent_ids || [];
    const draftIds = allAgents === undefined
      ? existingHandoverIds
      : existingHandoverIds.filter((id) => selectableHandoverIds.has(id));
    const draftTypes = Object.fromEntries(
      Object.entries(agent.handover_types || {}).filter(([id]) => draftIds.includes(id)),
    );
    const draftConditions = Object.fromEntries(
      Object.entries(agent.handover_conditions || {}).filter(([id]) => draftIds.includes(id)),
    );
    setHandoverIds(draftIds); setHandoverTypes(draftTypes); setHandoverConditions(draftConditions);
    setHandoverLegacy(runsEveryHandoverType(draftIds, agent.handover_types));
    setEditingHandover(true); setHandoverSearch("");
  };
  const cancelEditingHandover = () => {
    setHandoverIds(agent.handover_agent_ids || []); setHandoverTypes(agent.handover_types || {}); setHandoverConditions(agent.handover_conditions || {});
    setHandoverLegacy(false);
    setEditingHandover(false); setHandoverSearch("");
  };

  // Web exposure
  const baseUrl = authConfig?.base_url ?? platformOrigin();
  const embedSnippet = agent.web_token
    ? `<script\n  src="${baseUrl}/api/v1/public/widget.js"\n  data-token="${agent.web_token}"\n  data-context='{"page":"docs","product":"MyProduct"}'\n></script>`
    : "";

  const handleWebToggle = async (enabled: boolean) => {
    if (enabled && !(await confirmSharing({ entity_type: "agent", entity_id: agent.id, web_enabled: true }, { confirmLabel: t_i18n("Enable") }))) return;
    update.mutate(
      { id: agent.id, web_enabled: enabled },
      {
        onSuccess: () => toast({
          title: enabled ? t_i18n("Web exposure enabled") : t_i18n("Web exposure disabled"),
          variant: "success",
        }),
      },
    );
  };

  const handleWebUploadsToggle = (enabled: boolean) => {
    update.mutate(
      { id: agent.id, web_uploads_enabled: enabled },
      {
        onSuccess: () => toast({
          title: enabled ? t_i18n("Visitor uploads allowed") : t_i18n("Visitor uploads turned off"),
          variant: "success",
        }),
      },
    );
  };

  const handleSelfFanoutToggle = (enabled: boolean) => {
    update.mutate(
      { id: agent.id, allow_self_fanout: enabled },
      {
        onSuccess: () => toast({
          title: enabled ? t_i18n("Parallel copies allowed") : t_i18n("Parallel copies turned off"),
          variant: "success",
        }),
      },
    );
  };

  const handleCallerOutputFormatToggle = (enabled: boolean) => {
    update.mutate(
      { id: agent.id, accept_caller_output_format: enabled },
      {
        onSuccess: () => toast({
          title: enabled ? t_i18n("Caller formats accepted") : t_i18n("Caller formats refused"),
          variant: "success",
        }),
      },
    );
  };

  const copySnippet = () => {
    navigator.clipboard.writeText(embedSnippet);
    setSnippetCopied(true);
    setTimeout(() => setSnippetCopied(false), COPY_FEEDBACK_DELAY);
  };

  // Suggested prompts for web widget
  const [suggestedPrompts, setSuggestedPrompts] = useState<string[]>(agent.web_suggested_prompts || []);
  const [newPromptText, setNewPromptText] = useState("");
  const [editingSuggested, setEditingSuggested] = useState(false);
  const generateSuggested = useGenerateSuggestedPrompts();

  type GeneralNormalized = {
    name: string;
    icon: string;
    description: string | null;
    slug: string;
    tags: string[];
    llm_model: string | null;
    llm_provider_id: string | null;
    llm_model_source: "routing" | null;
    temperature: number | null;
    reasoning_effort: string | null;
    organization_context_mode: OrganizationContextMode;
    max_iterations: number | null;
    persona: string;
    output_format: string;
    output_schema: Record<string, unknown> | null;
    disable_chat: boolean;
    hide_runs: boolean;
    hide_activity_from_non_admins: boolean;
    human_in_the_loop: boolean;
    human_in_the_loop_prompt: string | null;
  };

  const normalizeGeneralCurrent = (): GeneralNormalized => ({
    name,
    icon,
    description: description.trim() ? description : null,
    slug: slug.trim(),
    tags,
    llm_model: llmModel || null,
    llm_provider_id: llmProviderId,
    llm_model_source: onRoutingModel ? "routing" : null,
    temperature,
    reasoning_effort: reasoningEffort || null,
    organization_context_mode: organizationContextMode,
    max_iterations: maxIterations,
    persona,
    output_format: outputFormat,
    output_schema: outputFormat === "json" ? outputSchema : null,
    disable_chat: disableChat,
    hide_runs: hideRuns,
    hide_activity_from_non_admins: hideActivity,
    human_in_the_loop: humanInTheLoop,
    human_in_the_loop_prompt: hitlPrompt?.trim() ? hitlPrompt : null,
  });

  const normalizeGeneralAgent = (a: Agent): GeneralNormalized => ({
    name: a.name,
    icon: a.icon,
    description: a.description,
    slug: a.slug || "",
    tags: a.tags || [],
    llm_model: a.llm_model,
    llm_provider_id: a.llm_provider_id,
    llm_model_source: a.llm_model_source ?? null,
    temperature: a.temperature,
    reasoning_effort: a.reasoning_effort,
    organization_context_mode: a.organization_context_mode ?? "inherit",
    max_iterations: a.max_iterations,
    persona: a.persona || "",
    output_format: a.output_format,
    output_schema: a.output_schema,
    disable_chat: a.disable_chat,
    hide_runs: a.hide_runs,
    hide_activity_from_non_admins: a.hide_activity_from_non_admins ?? false,
    human_in_the_loop: a.human_in_the_loop,
    human_in_the_loop_prompt: a.human_in_the_loop_prompt,
  });

  const buildGeneralUpdatePayload = (): ({ id: string; commit_message?: string } & Partial<Agent>) => {
    const current = normalizeGeneralCurrent();
    const baseline = normalizeGeneralAgent(agent);

    const fieldMap: Record<keyof GeneralNormalized, keyof Agent> = {
      name: "name",
      icon: "icon",
      description: "description",
      slug: "slug",
      tags: "tags",
      llm_model: "llm_model",
      llm_provider_id: "llm_provider_id",
      llm_model_source: "llm_model_source",
      temperature: "temperature",
      reasoning_effort: "reasoning_effort",
      organization_context_mode: "organization_context_mode",
      max_iterations: "max_iterations",
      persona: "persona",
      output_format: "output_format",
      output_schema: "output_schema",
      disable_chat: "disable_chat",
      hide_runs: "hide_runs",
      hide_activity_from_non_admins: "hide_activity_from_non_admins",
      human_in_the_loop: "human_in_the_loop",
      human_in_the_loop_prompt: "human_in_the_loop_prompt",
    };

    return buildMappedDiffPayload<GeneralNormalized, Agent>({
      id: agent.id,
      current,
      baseline,
      fieldMap,
      commitMessage: generalCommitMessage,
    });
  };

  type IntegrationsNormalized = {
    integration_bindings: Record<string, string[]>;
    integration_template_bindings: string[];
    integration_template_read_only: string[];
    integration_live_writes: Record<string, "on" | "off"> | null;
    integration_allowed_tools: Record<string, string[] | null> | null;
  };

  const buildIntegrationsUpdatePayload = () => {
    const providers = new Set([
      ...Object.keys(bindings),
      ...templateBindings,
    ]);
    const boundIds = new Set(Object.values(bindings).flat());
    const cleanedAllowed = Object.fromEntries(
      Object.entries(integAllowed).filter(([k]) => isToolScopeKeyActive(k, providers, boundIds)),
    ) as Record<string, string[] | null>;

    const current: IntegrationsNormalized = {
      integration_bindings: bindings,
      integration_template_bindings: templateBindings,
      // Only providers still bound per user: the server refuses any other.
      integration_template_read_only: templateReadOnly.filter((p) => templateBindings.includes(p)).sort(),
      // Only bindings still bound: the server refuses an override of any other.
      integration_live_writes: prunedLiveWrites(liveWrites, bindings, templateBindings),
      integration_allowed_tools: normalizeNullableObject(cleanedAllowed),
    };
    const baseline: IntegrationsNormalized = {
      integration_bindings: agent.integration_bindings || {},
      integration_template_bindings: agent.integration_template_bindings || [],
      integration_template_read_only: [...(agent.integration_template_read_only || [])].sort(),
      integration_live_writes: prunedLiveWrites(agent.integration_live_writes || {}, agent.integration_bindings || {}, agent.integration_template_bindings || []),
      integration_allowed_tools: normalizeNullableObject(agent.integration_allowed_tools || undefined),
    };

    const fieldMap: Record<keyof IntegrationsNormalized, keyof Agent> = {
      integration_bindings: "integration_bindings",
      integration_template_bindings: "integration_template_bindings",
      integration_template_read_only: "integration_template_read_only",
      integration_live_writes: "integration_live_writes",
      integration_allowed_tools: "integration_allowed_tools",
    };

    return {
      payload: buildMappedDiffPayload<IntegrationsNormalized, Agent>({
        id: agent.id,
        current,
        baseline,
        fieldMap,
      }),
      cleanedAllowed,
    };
  };

  type McpNormalized = {
    mcp_server_ids: string[];
    mcp_allowed_tools: Record<string, string[] | null> | null;
  };

  const buildMcpUpdatePayload = () => {
    const current: McpNormalized = {
      mcp_server_ids: mcpIds,
      mcp_allowed_tools: normalizeNullableObject(mcpAllowed),
    };
    const baseline: McpNormalized = {
      mcp_server_ids: agent.mcp_server_ids || [],
      mcp_allowed_tools: normalizeNullableObject(agent.mcp_allowed_tools || undefined),
    };

    const fieldMap: Record<keyof McpNormalized, keyof Agent> = {
      mcp_server_ids: "mcp_server_ids",
      mcp_allowed_tools: "mcp_allowed_tools",
    };

    return buildMappedDiffPayload<McpNormalized, Agent>({
      id: agent.id,
      current,
      baseline,
      fieldMap,
    });
  };

  const addSuggestedPrompt = () => {
    const text = newPromptText.trim();
    if (!text || suggestedPrompts.includes(text)) return;
    setSuggestedPrompts([...suggestedPrompts, text]);
    setNewPromptText("");
    if (!editingSuggested) setEditingSuggested(true);
  };

  const removeSuggestedPrompt = (index: number) => {
    setSuggestedPrompts(suggestedPrompts.filter((_, i) => i !== index));
    if (!editingSuggested) setEditingSuggested(true);
  };

  const saveSuggestedPrompts = () => {
    update.mutate(
      { id: agent.id, web_suggested_prompts: suggestedPrompts },
      {
        onSuccess: () => {
          setEditingSuggested(false);
          toast({ title: t_i18n("Suggested prompts saved"), variant: "success" });
        },
      },
    );
  };

  const cancelEditingSuggested = () => {
    setSuggestedPrompts(agent.web_suggested_prompts || []);
    setEditingSuggested(false);
  };

  const handleGenerateSuggested = () => {
    generateSuggested.mutate(agent.id, {
      onSuccess: (data) => {
        if (data.suggested_prompts.length > 0) {
          setSuggestedPrompts(data.suggested_prompts);
          setEditingSuggested(true);
          toast({ title: t_i18n("Generated {count} suggested prompts", { count: data.suggested_prompts.length }), variant: "success" });
        } else {
          toast({ title: t_i18n("Could not generate prompts. Try adding a persona first."), variant: "error" });
        }
      },
    });
  };

  const onOpenToolScope = (config: { type: "integration" | "mcp" | "channel-bot" | "builtin"; id: string; label: string }) => {
    setToolScopeDialog(config);
  };

  return (
    <div className="space-y-6">
      {confirmSharingElement}
      {/* Built-in or company-managed read-only banner */}
      {!canManage && agent.is_builtin && (
        <CompanyManagedBanner
          entityLabel="agent"
          isBuiltin
          // Where the two-layer card renders, the banner says what is locked and what stays yours (UX r1 B1 on #994).
          hasCustomerLayer={isXtmOne && !agent.read_only_reason}
          // An administrator still customizes a built-in agent's ROI outcomes (#9836 R3b, UX round 2 B3).
          exception={isAdmin ? t_i18n("Its ROI outcomes stay customizable under Outcomes.") : undefined}
        />
      )}
      {!canManage && agent.is_company_managed && !agent.is_builtin && (
        <CompanyManagedBanner entityLabel="agent" />
      )}
      {/* Shared entity banner */}
      {isShared && <SharedBanner entityLabel="agent" ownerName={agent.owner_name} readOnly={!canManage} />}

      {/* Sub-tab navigation */}
      <SectionNav
        sections={CONFIG_SECTIONS}
        active={configSection}
        onSelect={setConfigSection}
        label="Agent configuration sections"
        idPrefix="agent-config"
      />

      {/* ═══════════════ GENERAL SECTION ═══════════════ */}
      {configSection === "general" && (
        <>
        {/* A built-in's two layers (#914 slice 4): an administrator edits the customer layer, as the API allows. */}
        {agent.is_builtin && isXtmOne && !agent.read_only_reason && (
          <BuiltinLayersCard agent={agent} canEditCustomerLayer={canManageIntegrations} />
        )}
        <GeneralSection
          agent={agent}
          canManage={canManage}
          isAdmin={isAdmin}
          isPending={update.isPending}
          editingGeneral={editingGeneral}
          name={name}
          setName={setName}
          icon={icon}
          setIcon={setIcon}
          description={description}
          setDescription={setDescription}
          slug={slug}
          setSlug={setSlug}
          tags={tags}
          setTags={setTags}
          llmChoice={llmChoice}
          setLlmChoice={setLlmChoice}
          temperature={temperature}
          setTemperature={setTemperature}
          reasoningEffort={reasoningEffort}
          setReasoningEffort={setReasoningEffort}
          organizationContextMode={organizationContextMode}
          setOrganizationContextMode={setOrganizationContextMode}
          maxIterations={maxIterations != null ? clampMaxIterations(maxIterations) : null}
          setMaxIterations={setMaxIterations}
          persona={persona}
          setPersona={setPersona}
          commitMessage={generalCommitMessage}
          setCommitMessage={setGeneralCommitMessage}
          personaExpanded={personaExpanded}
          setPersonaExpanded={setPersonaExpanded}
          outputFormat={outputFormat}
          setOutputFormat={setOutputFormat}
          outputSchema={outputSchema}
          setOutputSchema={setOutputSchema}
          disableChat={disableChat}
          setDisableChat={setDisableChat}
          hideRuns={hideRuns}
          setHideRuns={setHideRuns}
          hideActivity={hideActivity}
          setHideActivity={setHideActivity}
          humanInTheLoop={humanInTheLoop}
          setHumanInTheLoop={setHumanInTheLoop}
          hitlPrompt={hitlPrompt}
          setHitlPrompt={setHitlPrompt}
          platformMaxIterations={platformMaxIterations}
          availableModels={availableModels}
          llmProviders={llmProviders ?? []}
          onStartEditGeneral={startEditingGeneral}
          onSaveGeneral={handleSaveGeneral}
          onCancelGeneral={cancelEditingGeneral}
          editingGuardrails={editingGuardrails}
          guardrails={guardrails}
          setGuardrails={setGuardrails}
          onStartEditGuardrails={startEditingGuardrails}
          onSaveGuardrails={handleSaveGuardrails}
          onCancelGuardrails={cancelEditingGuardrails}
          editingHandover={editingHandover}
          handoverIds={handoverIds}
          onToggleHandover={toggleHandover}
          handoverTypes={handoverTypes}
          handoverLegacy={handoverLegacy}
          onUpdateHandoverType={updateHandoverType}
          handoverConditions={handoverConditions}
          onUpdateHandoverCondition={updateHandoverCondition}
          handoverSearch={handoverSearch}
          setHandoverSearch={setHandoverSearch}
          handoverCandidates={handoverCandidates}
          allAgents={allAgents}
          currentUserId={user?.id}
          onStartEditHandover={startEditingHandover}
          onSaveHandover={handleSaveHandover}
          onCancelHandover={cancelEditingHandover}
          onSelfFanoutToggle={handleSelfFanoutToggle}
          onCallerOutputFormatToggle={handleCallerOutputFormatToggle}
          companyManaged={companyManaged}
          sharedGroupIds={sharedGroupIds}
          sharedGroupPermissions={sharedGroupPermissions}
          onSaveCompanyManaged={handleSaveCompanyManaged}
          onSaveSharedGroups={handleSaveSharedGroups}
          onSaveOwner={handleSaveOwner}
          onSaveSharedGroupPermissions={handleSaveSharedGroupPermissions}
          userSharing={{
            userIds: sharedUserIds,
            onUserIdsChange: handleSaveSharedUsers,
            userPermissions: sharedUserPermissions,
            onUserPermissionsChange: handleSaveSharedUserPermissions,
            accessRestricted,
            onAccessRestrictedChange: handleSaveAccessRestricted,
          }}
          onPublicCatalogToggle={handlePublicCatalogToggle}
          onCatalogPersonalToolsToggle={handleCatalogPersonalToolsToggle}
          integrations={integrations}
          catalog={catalog}
          onLendIntegrations={handleLendIntegrations}
          onMakeBindingsPerUser={handleMakeBindingsPerUser}
        />
        <IntentsSection agent={agent} canManage={canManage} />
        </>
      )}

      {/* ═══════════════ TOOLING SECTION ═══════════════ */}
      {/* Whether this agent asks before running tools, beside the tools
          it is about. On a built-in agent in XTM One mode it is the one field
          an admin may still change, matching the backend carve-out. */}
      {configSection === "tooling" && (canManage || (agent.is_builtin && isAdmin && isXtmOne)) && (
        <ToolApprovalCard agent={agent} />
      )}
      {configSection === "tooling" && (
        <ToolingSection
          agent={agent}
          canManage={canManage}
          canManageIntegrations={canManageIntegrations}
          isPending={update.isPending}
          companyManaged={companyManaged}
          isXtmOne={isXtmOne}
          editingIntegrations={editingIntegrations}
          bindings={bindings}
          integAllowed={integAllowed}
          integrationSearch={integrationSearch}
          setIntegrationSearch={setIntegrationSearch}
          connectedIntegrations={connectedIntegrations}
          availableIntegrations={availableIntegrations}
          integrations={integrations}
          templateBindings={templateBindings}
          templateReadOnly={templateReadOnly}
          onSetTemplateReadOnly={setTemplateProviderReadOnly}
          liveWrites={liveWrites}
          onSetLiveWrites={setBindingLiveWrites}
          isAdmin={isAdmin}
          onToggleIntegration={toggleIntegration}
          onToggleTemplateBinding={toggleTemplateBinding}
          onStartEditIntegrations={startEditingIntegrations}
          onSaveIntegrations={handleSaveIntegrations}
          onCancelIntegrations={cancelEditingIntegrations}
          editingElements={editingElements}
          elementDraft={elementDraft}
          onUpdateElementDraft={updateElementDraft}
          enforcementDraft={enforcementDraft}
          onToggleEnforcement={toggleEnforcement}
          catalog={catalog}
          onStartEditElements={startEditingElements}
          onCancelEditElements={cancelEditingElements}
          onSaveElements={handleSaveElements}
          editingMcp={editingMcp}
          mcpIds={mcpIds}
          onToggleMcp={toggleMcp}
          mcpAllowed={mcpAllowed}
          mcpSearch={mcpSearch}
          setMcpSearch={setMcpSearch}
          availableMcpServers={availableMcpServers}
          mcpServers={mcpServers}
          onStartEditMcp={startEditingMcp}
          onSaveMcp={handleSaveMcp}
          onCancelMcp={cancelEditingMcp}
          editingBuiltinTools={editingBuiltinTools}
          builtinToolIds={builtinToolIds}
          builtinAllowed={builtinAllowed}
          onToggleBuiltinTool={toggleBuiltinTool}
          onStartEditBuiltinTools={startEditingBuiltinTools}
          onSaveBuiltinTools={handleSaveBuiltinTools}
          onCancelBuiltinTools={cancelEditingBuiltinTools}
          allAgents={allAgents}
          workItemOwnerIds={workItemOwnerIds}
          onToggleWorkItemOwner={toggleWorkItemOwner}
          traceSupervisionTargetIds={traceSupervisionTargetIds}
          onToggleTraceSupervisionTarget={toggleTraceSupervisionTarget}
          coordinationDraft={coordinationDraft}
          onChangeCoordination={setCoordinationDraft}
          editingCustomTools={editingCustomTools}
          customToolIds={customToolIds}
          onToggleCustomTool={toggleCustomTool}
          customToolSearch={customToolSearch}
          setCustomToolSearch={setCustomToolSearch}
          customTools={customTools}
          onStartEditCustomTools={startEditingCustomTools}
          onSaveCustomTools={handleSaveCustomTools}
          onCancelCustomTools={cancelEditingCustomTools}
          onOpenToolScope={onOpenToolScope}
        />
      )}
      {/* Runner (native OS-control) targeting — only when the deployment has
          Runners enabled AND the agent has the Computer Use tool bound. Not
          while `/auth/config` loads: the card lists `/runners`, which answers
          404 on a deployment without runners. */}
      {configSection === "tooling" && runnersEnabled === true && (
        <RunnerSelectorCard agent={agent} canManage={canManage} />
      )}
      {/* The saved sign-ins the Browser tool opens — only while it is bound. */}
      {configSection === "tooling" && <BrowserProfileSelectorCard agent={agent} canManage={canManage} />}

      {/* ═══════════════ KNOWLEDGE SECTION ═══════════════ */}
      {configSection === "knowledge" && (
        <KnowledgeSection
          agent={agent}
          canManage={canManage}
          isPending={update.isPending}
          companyManaged={companyManaged}
          promptIds={promptIds}
          setPromptIds={setPromptIds}
          editingPrompts={editingPrompts}
          onStartEditPrompts={startEditingPrompts}
          onSavePrompts={handleSavePrompts}
          onCancelPrompts={cancelEditingPrompts}
          skillIds={skillIds}
          skillLoadModes={skillLoadModes}
          setSkillLoadModes={setSkillLoadModes}
          setSkillIds={setSkillIds}
          editingSkills={editingSkills}
          onStartEditSkills={startEditingSkills}
          onSaveSkills={handleSaveSkills}
          onCancelSkills={cancelEditingSkills}
          editingKbs={editingKbs}
          kbIds={kbIds}
          onToggleKb={toggleKb}
          kbPermissions={kbPermissions}
          onSetKbPermission={setKbPermission}
          kbSearch={kbSearch}
          setKbSearch={setKbSearch}
          availableKbs={availableKbs}
          kbs={kbs}
          onStartEditKbs={startEditingKbs}
          onSaveKbs={handleSaveKbs}
          onCancelKbs={cancelEditingKbs}
          memoryShortTerm={memoryShortTerm}
          memoryMediumTerm={memoryMediumTerm}
          memoryPersistent={memoryPersistent}
          memoryUser={memoryUser}
          onSaveMemoryTier={handleSaveMemoryTier}
        />
      )}

      {/* ═══════════════ CHANNELS SECTION ═══════════════ */}
      {configSection === "channels" && (
        <ChannelsSection
          agent={agent}
          canManage={canManage}
          isPending={update.isPending}
          isAdmin={isAdmin}
          onWebToggle={handleWebToggle}
          onWebUploadsToggle={handleWebUploadsToggle}
          embedDialogOpen={embedDialogOpen}
          setEmbedDialogOpen={setEmbedDialogOpen}
          snippetCopied={snippetCopied}
          embedSnippet={embedSnippet}
          onCopySnippet={copySnippet}
          baseUrl={baseUrl}
          suggestedPrompts={suggestedPrompts}
          newPromptText={newPromptText}
          setNewPromptText={setNewPromptText}
          editingSuggested={editingSuggested}
          onAddSuggestedPrompt={addSuggestedPrompt}
          onRemoveSuggestedPrompt={removeSuggestedPrompt}
          onSaveSuggestedPrompts={saveSuggestedPrompts}
          onCancelSuggestedPrompts={cancelEditingSuggested}
          onGenerateSuggested={handleGenerateSuggested}
          isGeneratingSuggested={generateSuggested.isPending}
          editingChannels={editingChannels}
          channelConfigDraft={channelConfigDraft}
          onToggleChannelBot={toggleChannelBot}
          onUpdateChannelEntry={updateChannelEntry}
          availableBots={availableBots}
          restrictedBots={restrictedBots}
          channelBots={channelBots}
          platformChannelsMap={platformChannelsMap}
          channelsLoadingMap={channelsLoadingMap}
          onStartEditChannels={startEditingChannels}
          onSaveChannels={handleSaveChannels}
          onCancelChannels={cancelEditingChannels}
          onOpenToolScope={onOpenToolScope}
        />
      )}

      {/* ═══════════════ OUTCOMES SECTION ═══════════════ */}
      {configSection === "outcomes" && (
        <OutcomesCard
          subject={{ type: "agent", id: agent.id }}
          parents={agent.fleet ? [{ subject: { type: "fleet", id: agent.fleet.fleet_id }, kind: "fleet", name: agent.fleet.fleet_name }] : []}
          structuredOutput={hasStructuredOutput(agent)}
          // A built-in agent's declaration is a default an administrator customizes and
          // resets (#9836 R3b, UX round 1 B1), like its integrations: `canManage` is
          // hard-false for built-ins in XTM One; the API lets an administrator write it.
          canManage={canManage || (agent.is_builtin && isAdmin)}
          pickOnOpen={pickUnit}
        />
      )}

      {/* Tool Scope Dialog */}
      {toolScopeDialog && (
        <AgentToolScopeLoader
          type={toolScopeDialog.type}
          sourceId={toolScopeDialog.id}
          sourceLabel={toolScopeDialog.label}
          currentAllowed={
            toolScopeDialog.type === "integration"
              ? (() => {
                  const integ = connectedIntegrations.find((i) => i.id === toolScopeDialog.id);
                  return integ ? integAllowed[toolScopeKey(integ)] ?? null : null;
                })()
              : toolScopeDialog.type === "channel-bot"
                ? Object.values(channelConfigDraft).find((e) => e.channel_bot_id === toolScopeDialog.id)?.allowed_tools ?? null
                : toolScopeDialog.type === "builtin"
                  ? scopedFunctions(builtinAllowed, toolScopeDialog.id)
                  : mcpAllowed[toolScopeDialog.id] ?? null
          }
          cachedMcpTools={
            toolScopeDialog.type === "mcp"
              ? mcpServers?.find((s) => s.id === toolScopeDialog.id)?.discovered_tools ?? undefined
              : undefined
          }
          onSave={(allowed) => {
            if (toolScopeDialog.type === "integration") {
              const integ = connectedIntegrations.find((i) => i.id === toolScopeDialog.id);
              if (integ) {
                const key = toolScopeKey(integ);
                setIntegAllowed((prev) => {
                  const next = { ...prev };
                  if (allowed === null) { delete next[key]; } else { next[key] = allowed; }
                  return next;
                });
              }
            } else if (toolScopeDialog.type === "builtin") {
              setBuiltinAllowed((prev) => withGroupScope(prev, toolScopeDialog.id, allowed));
            } else if (toolScopeDialog.type === "channel-bot") {
              setChannelConfigDraft((prev) => {
                const next = { ...prev };
                for (const [chType, entry] of Object.entries(next)) {
                  if (entry.channel_bot_id === toolScopeDialog.id) {
                    next[chType] = { ...entry, allowed_tools: allowed };
                  }
                }
                return next;
              });
            } else {
              setMcpAllowed((prev) => {
                const next = { ...prev };
                if (allowed === null) { delete next[toolScopeDialog.id]; } else { next[toolScopeDialog.id] = allowed; }
                return next;
              });
            }
            setToolScopeDialog(null);
          }}
          onClose={() => setToolScopeDialog(null)}
        />
      )}

      {handoverTypeConfirm}
    </div>
  );
}
