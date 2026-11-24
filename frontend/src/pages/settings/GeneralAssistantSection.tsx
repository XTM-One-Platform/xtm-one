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

import { useState, useMemo } from "react";
import { Slider } from "@filigran/design-system";
import {
  Bot,
  BookMarked,
  Check,
  Pencil,
  MessageSquare,
  Sparkles,
  BookOpen,
  Terminal,
  Wrench,
  Cog,
  ArrowLeftRight,
  Filter,
  AlertTriangle,
  MessageCircle,
  AtSign,
  ChevronDown,
  Repeat,
  X,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useUpdateGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useChannelBots, useChannelBotChannelsMap } from "@/hooks/api/useChannelBotApi";
import { cn, formatKBStats, modelChoiceValue, parseModelChoice, toggleArrayItem, pluralize } from "@/lib/utils";
import { canStandInForGeneralAssistant, generalAssistantStandIn, standInRefusal } from "@/lib/general-assistant-stand-in";
import type { Agent, AvailableModel, ChannelConfig, ChannelEntry, CustomTool, GeneralAssistantConfig, Integration, KnowledgeBase, LLMProvider, MCPServer } from "@/types";
import {
  INTEGRATION_PROVIDER_NAMES, TRANSPORT_DISPLAY_NAMES,
  ALL_CHANNEL_TYPES, EXTERNAL_CHANNEL_TYPES, CHANNEL_TYPE_LABELS, CHANNEL_ROUTING_ICON_KEY,
  CHANNEL_ICONS, GA_HANDOVER_TYPES, type GAHandoverType,
  MAX_ITERATIONS_CEILING, MAX_ITERATIONS_DEFAULT, clampMaxIterations,
  providerLogoSrc } from "@/lib/constants";
import {
  runnableHandoverTypes,
  withAddedHandoverTarget,
  withHandoverType,
  withRemovedHandoverTarget,
} from "@/lib/handover-types";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { ChannelPicker } from "@/components/ui/channel-picker";
import { useIsCommunityEdition, useCustomToolsEnabled } from "@/hooks/usePlatformConfig";
import { useBuiltinTools, type BuiltinToolCatalog } from "@/hooks/useBuiltinTools";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import { Tip } from "@/components/ui/tooltip";
import { ModelSelect } from "@/components/ui/model-select";
import { useFormatter } from "@/i18n";
import { GAHandoverPicker } from "./GAHandoverPicker";
import { GeneralAssistantSkills } from "./GeneralAssistantSkills";
import { carriedSkillIds, sameSkillChoice, useGeneralAssistantSkillCatalog } from "./ga-skills";
import { RESOURCE_GRID_CLASS, ResourceToggleCard } from "./ResourceToggleCard";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// General Assistant Configuration Section
// ---------------------------------------------------------------------------

/**
 * Renders the company-managed *custom tools* binding section.  Extracted
 * into a focused subcomponent so the ~40-line block (icon header, grid
 * of toggle buttons, empty state) stays readable when wrapped behind
 * the `disable_custom_tools` feature flag in the parent component — and
 * so future tweaks to the binding UI only have to land in one place.
 *
 * The parent owns the `enabled / attached / toggle` triplet so the
 * subcomponent stays purely presentational and doesn't need its own
 * state slice.
 */
interface CustomToolsBindingSectionProps {
  enabledTools: CustomTool[];
  gaToolIds: string[];
  toggleTool: (id: string) => void;
}

function CustomToolsBindingSection({
  enabledTools,
  gaToolIds,
  toggleTool,
}: CustomToolsBindingSectionProps) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Wrench className="h-4 w-4 text-accent" />
        <span className="content-base font-medium">{t_i18n("Custom Tools")}</span>
      </div>
      <p className="content-compact text-default-secondary">{t_i18n("Click to attach or detach company-managed custom tools.")}</p>
      {enabledTools.length > 0 ? (
        <div className={RESOURCE_GRID_CLASS}>
          {enabledTools.map((tool) => {
            const isAttached = gaToolIds.includes(tool.id);
            return (
              <ResourceToggleCard
                key={tool.id}
                attached={isAttached}
                onToggle={() => toggleTool(tool.id)}
                icon={<Wrench className={cn("h-5 w-5", isAttached ? "text-accent" : "text-default-secondary")} />}
                name={tool.name}
                description={tool.description || t_i18n("Custom tool")}
              />
            );
          })}
        </div>
      ) : (
        <div className="py-3 text-center">
          <Wrench className="h-5 w-5 text-default-disabled mx-auto mb-1" />
          <p className="content-compact text-default-secondary">{t_i18n("No company-managed custom tools available")}</p>
        </div>
      )}
    </div>
  );
}

/**
 * The built-in suites the General Assistant carries but the picker above does
 * not offer: switched off on this deployment, agent-only, or unknown to this
 * build. Listed with the reason and a remove action so an enabled suite is
 * never invisible.
 */
function UnofferedBuiltinTools({
  ids,
  describe,
  onRemove,
}: {
  ids: string[];
  describe: BuiltinToolCatalog["describe"];
  onRemove: (id: string) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 px-3 py-2">
      <span className="content-caption text-feedback-warning-primary">{t_i18n("Also enabled, not offered here:")}</span>
      {ids.map((id) => {
        const tool = describe(id);
        const Icon = tool.icon;
        const reason = !tool.known
          ? t_i18n("Unknown built-in tool")
          : tool.agentOnly
            ? t_i18n("Needs an agent: the General Assistant cannot run it")
            : t_i18n("Switched off on this deployment");
        return (
          <span
            key={id}
            className="inline-flex h-6 items-center gap-1 rounded-sm border border-elevation-subtle-soft bg-elevation-hover pl-2 pr-1 content-caption"
          >
            <Tip content={reason}>
              <span className="inline-flex items-center gap-1" tabIndex={0}>
                <Icon className="h-3 w-3 text-default-secondary" />
                {tool.label}
              </span>
            </Tip>
            <button
              type="button"
              className="text-default-secondary transition-colors hover:text-default-primary"
              onClick={() => onRemove(id)}
              aria-label={t_i18n("Remove {name}", { name: tool.label })}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        );
      })}
    </div>
  );
}

interface GeneralAssistantSectionProps {
  gaConfig: GeneralAssistantConfig | null;
  updateGAConfig: ReturnType<typeof useUpdateGeneralAssistantConfig>;
  availableModels: AvailableModel[];
  integrations: Integration[];
  mcpServers: MCPServer[];
  kbs: KnowledgeBase[];
  customTools: CustomTool[];
  allAgents: Agent[];
  gaEditing: boolean;
  setGaEditing: (v: boolean) => void;
  gaPrompt: string;
  setGaPrompt: (v: string) => void;
  gaModel: string;
  setGaModel: (v: string) => void;
  /** Provider configs, so a model option can name the one that offers it. */
  providers: LLMProvider[];
  gaMaxIterations: number;
  setGaMaxIterations: (v: number) => void;
  gaKbIds: string[];
  setGaKbIds: (v: string[]) => void;
  gaMcpIds: string[];
  setGaMcpIds: (v: string[]) => void;
  gaToolIds: string[];
  setGaToolIds: (v: string[]) => void;
  gaBuiltinToolIds: string[];
  setGaBuiltinToolIds: (v: string[]) => void;
  gaBindings: Record<string, string[]>;
  setGaBindings: (v: Record<string, string[]>) => void;
  gaHandoverIds: string[];
  setGaHandoverIds: (v: string[]) => void;
  gaHandoverTypes: Record<string, string>;
  setGaHandoverTypes: (v: Record<string, string>) => void;
}

/** The option a picker value refers to, or null when it matches none. */
function chosenModel(options: AvailableModel[], choice: string) {
  const { model, providerId } = parseModelChoice(choice);
  return (
    options.find(
      (m) => m.model === model && (m.provider_id ?? null) === providerId,
    ) ?? null
  );
}

export function GeneralAssistantSection({
  gaConfig,
  updateGAConfig,
  availableModels,
  integrations: allIntegrations,
  mcpServers,
  kbs,
  customTools,
  allAgents,
  gaEditing,
  setGaEditing,
  gaPrompt,
  setGaPrompt,
  gaModel,
  setGaModel,
  providers,
  gaMaxIterations,
  setGaMaxIterations,
  gaKbIds,
  setGaKbIds,
  gaMcpIds,
  setGaMcpIds,
  gaToolIds,
  setGaToolIds,
  gaBuiltinToolIds,
  setGaBuiltinToolIds,
  gaBindings,
  setGaBindings,
  gaHandoverIds,
  setGaHandoverIds,
  gaHandoverTypes,
  setGaHandoverTypes,
}: GeneralAssistantSectionProps) {
  const { t_i18n, intl } = useFormatter();
  const [handoverTypeConfirm, confirm] = useConfirm();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();
  // Only company-managed resources can be bound to the GA (exclude channel bots)
  const connectedIntegrations = allIntegrations.filter(
    (i) => i.status === "connected" && i.is_company_managed,
  );
  const enabledMcps = mcpServers.filter((s) => s.enabled && s.is_company_managed);
  const enabledTools = customTools.filter((t) => t.enabled && t.is_company_managed);
  const companyKbs = kbs.filter((kb) => kb.is_company_managed);
  const {
    offered: offeredBuiltinTools,
    describe: describeBuiltinTool,
    carries: carriesBuiltin,
    canonicalIds: canonicalBuiltinIds,
    isReady: builtinToolsReady,
    isError: builtinToolsFailed,
  } = useBuiltinTools();
  const gaBuiltinOptions = offeredBuiltinTools("general_assistant");
  const offeredBuiltinIds = new Set(gaBuiltinOptions.map((tool) => tool.id));
  // A [DEPRECATED] former id is offered as its group, never listed apart.
  // Until the registry is known nothing can be judged "not offered": an empty
  // list here would offer a remove button on every tool the GA carries.
  const unofferedBuiltinIds = builtinToolsReady
    ? canonicalBuiltinIds(gaBuiltinToolIds).filter((id) => !offeredBuiltinIds.has(id))
    : [];

  // The GA chat model is read-only when pinned via DEFAULT_GENERAL_ASSISTANT_MODEL
  // (env always wins on restart — mirrors the other env-managed defaults). When
  // locked, the Chat Model picker is disabled with an "Env" badge and the llm
  // fields are omitted from the save payload (the backend 403s on a locked
  // field).
  const gaModelEnvLocked = (gaConfig?.env_managed_fields ?? []).includes("llm_model");

  // CE-only (no Filigran license, no EE platform) — the General Assistant
  // can't run at all in this tier, so locking the editor surface
  // prevents writes to a config nothing will read. The read-only view
  // stays fully visible so admins can still inspect the current
  // values.
  const isCeOnly = useIsCommunityEdition();
  const ceOnlyTooltip = t_i18n("Activate a license or register an EE platform to configure the General Assistant.");
  const customToolsEnabled = useCustomToolsEnabled();

  // Channel bots (platform-wide resources)
  const { data: channelBots } = useChannelBots();
  const connectedBots = (channelBots ?? []).filter((b) => b.status === "connected");

  const [promptExpanded, setPromptExpanded] = useState(false);

  // Skills, a resource of the Configure panel: the
  // panel's draft holds the choice (`null` = the built-in skills) and Save
  // writes it to `skill_ids` when it changed.
  const skillCatalog = useGeneralAssistantSkillCatalog(gaConfig);
  const [gaSkillChoice, setGaSkillChoice] = useState<string[] | null>(gaConfig?.skill_ids ?? null);

  // Channel config state — same structure as Agent.channel_config
  const [editingChannels, setEditingChannels] = useState(false);
  const [channelConfigDraft, setChannelConfigDraft] = useState<ChannelConfig>(gaConfig?.channel_config || {});

  // Only fetch channels for bots whose toggle is ON in the draft (lazy loading).
  const activeBotIds = useMemo(() => {
    if (!editingChannels) return [];
    return connectedBots
      .filter((b) => {
        const chType = EXTERNAL_CHANNEL_TYPES.find(
          (ct) => CHANNEL_ROUTING_ICON_KEY[ct] === b.provider,
        );
        return chType && !!channelConfigDraft[chType];
      })
      .map((b) => b.id);
  }, [editingChannels, connectedBots, channelConfigDraft]);
  const { map: platformChannelsMap, loadingMap: channelsLoadingMap } = useChannelBotChannelsMap(activeBotIds);
  // Web channel toggle state
  const isWebEnabled = gaConfig?.available_channel_types === null || gaConfig?.available_channel_types === undefined || (gaConfig?.available_channel_types ?? []).includes("web");

  const toggleChannelBot = (chType: string, botId: string) => {
    setChannelConfigDraft((prev) => {
      const next = { ...prev };
      if (next[chType]) {
        delete next[chType];
      } else {
        next[chType] = {
          channel_bot_id: botId,
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

  const startEditingChannels = () => {
    setChannelConfigDraft(gaConfig?.channel_config || {});
    setEditingChannels(true);
  };

  const cancelEditingChannels = () => {
    setChannelConfigDraft(gaConfig?.channel_config || {});
    setEditingChannels(false);
  };

  const handleSaveChannels = async () => {
    const cleanConfig = Object.keys(channelConfigDraft).length > 0 ? channelConfigDraft : null;
    if (!(await confirmSharing({ entity_type: "general_assistant", change: { channel_config: cleanConfig } }))) return;
    updateGAConfig.mutate(
      { channel_config: cleanConfig },
      { onSuccess: () => setEditingChannels(false) },
    );
  };

  const handleWebToggle = (enabled: boolean) => {
    const current = gaConfig?.available_channel_types ?? [...ALL_CHANNEL_TYPES];
    const next = enabled
      ? [...new Set([...current, "web"])]
      : current.filter((c) => c !== "web");
    const value = next.length === ALL_CHANNEL_TYPES.length ? null : next;
    updateGAConfig.mutate({ available_channel_types: value });
  };

  const toggleMcp = (id: string) => setGaMcpIds(toggleArrayItem(gaMcpIds, id));
  const toggleTool = (id: string) => setGaToolIds(toggleArrayItem(gaToolIds, id));
  const toggleBuiltinTool = (id: string) => setGaBuiltinToolIds(toggleArrayItem(canonicalBuiltinIds(gaBuiltinToolIds), id));
  const toggleKb = (id: string) => setGaKbIds(toggleArrayItem(gaKbIds, id));
  const toggleHandover = (id: string) => {
    const next = toggleArrayItem(gaHandoverIds, id);
    setGaHandoverIds(next);
    setGaHandoverTypes(
      next.includes(id)
        ? withAddedHandoverTarget(gaHandoverIds, gaHandoverTypes, id)
        : withRemovedHandoverTarget(gaHandoverTypes, id),
    );
  };
  const handoverTargetName = (id: string) =>
    allAgents.find((agent) => agent.id === id)?.name ?? t_i18n("Unknown agent");
  const setHandoverType = async (id: string, type: GAHandoverType) => {
    const change = withHandoverType(gaHandoverIds, gaHandoverTypes, id, type);
    if (change.narrowed.length > 0) {
      const names = intl.formatList(change.narrowed.map(handoverTargetName), { type: "conjunction" });
      const confirmed = await confirm({
        title: t_i18n("Give each agent a handover type?"),
        description: t_i18n(
          "Without handover types, the General Assistant can delegate to, consult or transfer to each selected agent. Types are set per agent, so picking one sets them all: {count, plural, one {{names} becomes Delegate} other {{names} become Delegate}}. You can change each one before you save.",
          { count: change.narrowed.length, names },
        ),
        confirmLabel: t_i18n("Set types"),
        variant: "warning",
      });
      if (!confirmed) return;
    }
    setGaHandoverTypes(change.types);
  };

  const enterEdit = () => {
    setGaPrompt(gaConfig?.system_prompt || "");
    setGaModel(modelChoiceValue(gaConfig?.llm_model, gaConfig?.llm_provider_id));
    setGaMaxIterations(clampMaxIterations(gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT));
    setGaKbIds(gaConfig?.knowledge_base_ids || []);
    setGaMcpIds(gaConfig?.mcp_server_ids || []);
    setGaToolIds(gaConfig?.custom_tool_ids || []);
    setGaBuiltinToolIds(gaConfig?.builtin_tool_ids || []);
    setGaBindings(gaConfig?.integration_bindings || {});
    const handoverIds = gaConfig?.handover_agent_ids || [];
    setGaHandoverIds(handoverIds);
    setGaHandoverTypes(runnableHandoverTypes(handoverIds, gaConfig?.handover_types, GA_HANDOVER_TYPES));
    setGaSkillChoice(gaConfig?.skill_ids ?? null);
    setGaEditing(true);
  };

  const handleSave = async () => {
    // Everything the General Assistant carries reaches every user.
    if (!(await confirmSharing({
      entity_type: "general_assistant",
      change: {
        integration_bindings: gaBindings,
        mcp_server_ids: gaMcpIds,
        custom_tool_ids: gaToolIds,
        knowledge_base_ids: gaKbIds,
        system_prompt: gaPrompt,
      },
    }))) {
      return;
    }
    // Resolve the provider for the chosen model. When the model isn't in
    // availableModels (e.g. a previously stored model that dropped out of the
    // catalog), fall back to the currently loaded provider rather than clearing
    // it to NULL, which would leave an inconsistent model/provider pairing.
    const chosen = chosenModel(availableModels, gaModel);
    const { model: gaModelId } = parseModelChoice(gaModel);
    const provider = gaModelId
      ? (chosen?.provider ?? gaConfig?.llm_provider ?? "")
      : "";
    // Omit the llm fields when env-managed: they are seeded from
    // DEFAULT_GENERAL_ASSISTANT_MODEL on every restart and the backend rejects
    // any attempt to mutate a locked field.
    const llmFields = gaModelEnvLocked
      ? {}
      : {
          llm_provider: provider,
          llm_provider_id: parseModelChoice(gaModel).providerId,
          llm_model: gaModelId,
        };
    updateGAConfig.mutate(
      {
        ...llmFields,
        max_iterations: gaMaxIterations,
        system_prompt: gaPrompt || undefined,
        knowledge_base_ids: gaKbIds,
        mcp_server_ids: gaMcpIds,
        custom_tool_ids: gaToolIds,
        builtin_tool_ids: gaBuiltinToolIds,
        integration_bindings: gaBindings,
        handover_agent_ids: gaHandoverIds,
        handover_types: gaHandoverTypes,
        ...(sameSkillChoice(gaSkillChoice, gaConfig?.skill_ids ?? null) ? {} : { skill_ids: gaSkillChoice }),
      },
      { onSuccess: () => setGaEditing(false) },
    );
  };

  // Read-only summary counts
  const boundKbs = gaConfig?.knowledge_base_ids?.length ?? 0;
  const boundMcps = gaConfig?.mcp_server_ids?.length ?? 0;
  const boundTools = gaConfig?.custom_tool_ids?.length ?? 0;
  const boundBuiltins = gaConfig?.builtin_tool_ids?.length ?? 0;
  const boundIntegrations = Object.keys(gaConfig?.integration_bindings ?? {}).length;
  const boundHandover = gaConfig?.handover_agent_ids?.length ?? 0;
  const boundSkills = carriedSkillIds(gaConfig?.skill_ids, skillCatalog.defaults).length;

  // Linked agent state: the stored link, and the agent that answers in the
  // General Assistant's place - the link only when it still qualifies.
  const linkedAgentId = gaConfig?.linked_agent_id || null;
  const storedLink = linkedAgentId ? allAgents.find((a) => a.id === linkedAgentId) : undefined;
  const linkedAgent = generalAssistantStandIn(linkedAgentId, allAgents);
  const linkNotFollowedReason = (() => {
    if (!linkedAgentId || linkedAgent) return null;
    if (!storedLink) return t_i18n("Deleted, or not visible to you");
    switch (standInRefusal(storedLink)) {
      case "not_company_managed":
        return t_i18n("Not company-managed: only the users who can see it reach it");
      case "access_restricted":
        return t_i18n("Restricted access: only the users granted access reach it");
      case "disabled":
        return t_i18n("Disabled: the General Assistant skips it");
      case "chat_disabled":
        return t_i18n("Chat disabled: nobody can chat with it");
      default:
        return null;
    }
  })();
  // The stored link stays listed even once it stopped qualifying, so the select shows it.
  const chatAgents = allAgents.filter((a) => canStandInForGeneralAssistant(a) || a.id === linkedAgentId);

  return (
    <>
    {confirmSharingElement}
    <Card>
      <CardContent className="pt-6 space-y-6">
        {/* Link to Agent selector */}
        <div className="space-y-2">
          <Label htmlFor="ga-linked-agent" className="flex items-center gap-1.5">
            <Bot className="h-3.5 w-3.5" /> {t_i18n("Link to Agent")}
          </Label>
          <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly} wrapClassName="inline-flex w-full">
            <SearchableSelect
              id="ga-linked-agent"
              aria-label={t_i18n("Link to Agent")}
              aria-describedby="ga-linked-agent-help"
              options={chatAgents.map((a) => agentSelectOption(a))}
              pinned={[
                { value: "__none__", label: t_i18n("None (standalone assistant)") },
                ...(linkedAgentId && !storedLink ? [{ value: linkedAgentId, label: t_i18n("Unknown agent") }] : []),
              ]}
              value={linkedAgentId || "__none__"}
              onValueChange={(v) => {
                updateGAConfig.mutate({ linked_agent_id: v === "__none__" ? null : v });
              }}
              disabled={isCeOnly}
            />
          </Tip>
          <p id="ga-linked-agent-help" className="content-compact text-default-secondary">
            {t_i18n("Optionally link a specific agent to act as the General Assistant. When linked, the GA inherits the agent's model, persona, tools, and all configuration.")}{" "}
            {t_i18n("Only enabled company-managed agents every user can chat with can be linked.")}
          </p>
          {linkNotFollowedReason && (
            <div
              className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2"
              data-testid="ga-link-not-followed"
            >
              <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
              <div className="space-y-0.5">
                <p className="content-compact font-medium text-feedback-warning-primary">
                  {t_i18n("The link is not followed: the General Assistant answers with its own configuration below.")}
                </p>
                <p className="content-compact text-feedback-warning-primary">
                  <span className="font-medium">{storedLink?.name ?? t_i18n("Unknown agent")}</span>
                  {" · "}
                  {linkNotFollowedReason}
                </p>
              </div>
            </div>
          )}
        </div>

        {linkedAgent ? (
          /* Linked agent banner — hides the GA config */
          <div className="rounded-sm border border-accent/20 bg-accent/5 p-4 space-y-2">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-accent" />
              <span className="content-base font-medium">{linkedAgent.name}</span>
              <Badge variant="outline" className="content-caption px-1.5 py-0 h-4 border-accent/30 text-accent">
                {t_i18n("General Assistant")}
              </Badge>
            </div>
            <p className="content-compact text-default-secondary">
              {t_i18n('Configuration is inherited from this agent. To customize the General Assistant directly, select "None (standalone assistant)" above.')}
            </p>
          </div>
        ) : (
          /* Original GA config editing */
          <>
        <div className="flex items-center justify-between">
          <p className="content-compact text-default-secondary">
            {t_i18n("Configure the model, system prompt, resources, and handover behavior. By default the assistant has no resources bound.")}
          </p>
          {!gaEditing ? (
            <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
              <Button variant="secondary" className="gap-1.5 shrink-0 ml-4" onClick={enterEdit} disabled={isCeOnly}>
                <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
              </Button>
            </Tip>
          ) : (
            <div className="flex gap-2 shrink-0 ml-4">
              <Button variant="secondary" onClick={() => setGaEditing(false)}>{t_i18n("Cancel")}</Button>
              <Button className="gap-1.5" onClick={handleSave} disabled={isCeOnly || updateGAConfig.isPending} aria-busy={updateGAConfig.isPending || undefined}>
                {updateGAConfig.isPending && <Spinner size="sm" tone="inherit" />}{!updateGAConfig.isPending && <Check className="h-3 w-3" />}
                {t_i18n("Save")}
              </Button>
            </div>
          )}
        </div>
        {gaEditing ? (
          // Defense in depth: if an admin somehow lands in edit mode in
          // CE-only (e.g. the license expired while the panel was open),
          // wrapping the editor in a disabled <fieldset> locks every
          // button, input and select inside without touching each
          // individual widget. Save is also guarded above; Cancel stays
          // enabled so the admin can always exit.
          <fieldset disabled={isCeOnly} className="min-w-0 space-y-6 disabled:opacity-60">
            {/* Model + System Prompt */}
            <div className="space-y-4">
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label htmlFor="ga-chat-model">{t_i18n("Chat Model")}</Label>
                  {gaModelEnvLocked && <EnvLockBadge />}
                </div>
                {/* A pinned model (e.g. via DEFAULT_GENERAL_ASSISTANT_MODEL,
                    resolved by name-prefix inference) may not appear in
                    availableModels: `ModelSelect` offers it as its own entry so
                    the field shows the actual pinned value. */}
                <ModelSelect
                  id="ga-chat-model"
                  aria-label={t_i18n("Chat Model")}
                  aria-describedby="ga-chat-model-help"
                  models={availableModels}
                  providers={providers}
                  pinned={[{ value: "__agent_default__", label: t_i18n("Agent default") }]}
                  value={gaModel || "__agent_default__"}
                  onValueChange={(v) => setGaModel(v === "__agent_default__" ? "" : v)}
                  disabled={gaModelEnvLocked}
                />
                <p id="ga-chat-model-help" className="content-compact text-default-secondary">
                  {gaModelEnvLocked
                    ? t_i18n("Pinned via DEFAULT_GENERAL_ASSISTANT_MODEL (environment variable).")
                    : t_i18n('Leave as "Agent default" to follow the platform agent default model.')}
                </p>
              </div>
              {/* Max Iterations */}
              <div className="space-y-2">
                <Label htmlFor="ga-max-iterations" className="flex items-center gap-1.5">
                  <Repeat className="h-3.5 w-3.5" /> {t_i18n("Max Iterations")}
                </Label>
                <div className="flex items-center gap-3">
                  <Slider
                    min={1}
                    max={MAX_ITERATIONS_CEILING}
                    step={1}
                    value={[gaMaxIterations]}
                    onValueChange={([value]) => setGaMaxIterations(value)}
                    aria-label={t_i18n("Max Iterations")}
                    aria-describedby="ga-max-iterations-help"
                    wrapperClassName="flex-1"
                  />
                  <Input
                    id="ga-max-iterations"
                    aria-label={t_i18n("Max Iterations")}
                    aria-describedby="ga-max-iterations-help"
                    type="number"
                    min={1}
                    max={MAX_ITERATIONS_CEILING}
                    step={1}
                    value={gaMaxIterations}
                    onChange={(e) => {
                      const value = parseInt(e.target.value, 10);
                      if (Number.isFinite(value)) setGaMaxIterations(clampMaxIterations(value));
                    }}
                    className="h-8 w-24 text-right font-mono-plex tabular-nums"
                  />
                </div>
                <p id="ga-max-iterations-help" className="content-compact text-default-secondary">
                  {t_i18n("Platform-wide default for the maximum tool-calling rounds per chat message: each round can fire several parallel tool calls, so the real tool-call budget is higher. Agents can override this individually. Default is {default}.", { default: MAX_ITERATIONS_DEFAULT })}{" "}
                  {t_i18n("The images it may look at in one message grow with it, so long visual work (videos, decks) keeps its sight.")}
                </p>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="ga-system-prompt">{t_i18n("System Prompt")}</Label>
                  <PromptGenerator contextType="system_prompt" currentText={gaPrompt} onGenerated={setGaPrompt} />
                </div>
                <textarea
                  id="ga-system-prompt"
                  className="w-full min-h-[120px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex resize-y focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  value={gaPrompt}
                  onChange={(e) => setGaPrompt(e.target.value)}
                />
              </div>
            </div>

            {/* Integration bindings */}
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Cog className="h-4 w-4 text-accent" />
                <span className="content-base font-medium">{t_i18n("Integrations")}</span>
              </div>
              <p className="content-compact text-default-secondary">{t_i18n("Click to bind or unbind company-managed integrations. Bound integrations provide tools to the General Assistant.")}</p>
              {connectedIntegrations.length > 0 ? (
                <div className={RESOURCE_GRID_CLASS}>
                  {connectedIntegrations.map((integ) => {
                    const isBound = Object.values(gaBindings).flat().includes(integ.id);
                    return (
                      <ResourceToggleCard
                        key={integ.id}
                        attached={isBound}
                        onToggle={() => {
                          if (isBound) {
                            setGaBindings((() => {
                              const next = { ...gaBindings };
                              for (const key of Object.keys(next)) {
                                next[key] = (next[key] || []).filter((id) => id !== integ.id);
                                if (next[key].length === 0) delete next[key];
                              }
                              return next;
                            })());
                          } else {
                            setGaBindings({ ...gaBindings, [integ.provider]: [...(gaBindings[integ.provider] || []), integ.id] });
                          }
                        }}
                        icon={<img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-5 w-5" />}
                        name={integ.name}
                        description={INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}
                      />
                    );
                  })}
                </div>
              ) : (
                <div className="py-3 text-center">
                  <Cog className="h-5 w-5 text-default-disabled mx-auto mb-1" />
                  <p className="content-compact text-default-secondary">{t_i18n("No company-managed connected integrations")}</p>
                </div>
              )}
            </div>

            {/* MCP servers */}
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Terminal className="h-4 w-4 text-accent" />
                <span className="content-base font-medium">{t_i18n("MCP Tool Servers")}</span>
              </div>
              <p className="content-compact text-default-secondary">{t_i18n("Click to attach or detach company-managed MCP tool servers.")}</p>
              {enabledMcps.length > 0 ? (
                <div className={RESOURCE_GRID_CLASS}>
                  {enabledMcps.map((srv) => {
                    const isAttached = gaMcpIds.includes(srv.id);
                    return (
                      <ResourceToggleCard
                        key={srv.id}
                        attached={isAttached}
                        onToggle={() => toggleMcp(srv.id)}
                        icon={<Terminal className={cn("h-5 w-5", isAttached ? "text-accent" : "text-default-secondary")} />}
                        name={srv.name}
                        description={TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport}
                      />
                    );
                  })}
                </div>
              ) : (
                <div className="py-3 text-center">
                  <Terminal className="h-5 w-5 text-default-disabled mx-auto mb-1" />
                  <p className="content-compact text-default-secondary">{t_i18n("No company-managed MCP servers available")}</p>
                </div>
              )}
            </div>

            {/* Built-in tools */}
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-accent" />
                <span className="content-base font-medium">{t_i18n("Built-in Tools")}</span>
              </div>
              <p className="content-compact text-default-secondary">{t_i18n("Enable or disable built-in platform tools for the General Assistant.")}</p>
              {!builtinToolsReady ? (
                <BuiltinToolsPending isError={builtinToolsFailed} gridClassName={RESOURCE_GRID_CLASS} testId="ga-builtin-tools" />
              ) : (
              <div className={RESOURCE_GRID_CLASS}>
                {gaBuiltinOptions.map((tool) => {
                  const isEnabled = carriesBuiltin(gaBuiltinToolIds, tool.id);
                  const Icon = tool.icon;
                  return (
                    <ResourceToggleCard
                      key={tool.id}
                      testId={`ga-builtin-tool-${tool.id}`}
                      attached={isEnabled}
                      onToggle={() => toggleBuiltinTool(tool.id)}
                      icon={<Icon className={cn("h-5 w-5", isEnabled ? "text-accent" : "text-default-secondary")} />}
                      name={tool.label}
                      description={tool.description}
                    />
                  );
                })}
              </div>
              )}
              {unofferedBuiltinIds.length > 0 && (
                <UnofferedBuiltinTools
                  ids={unofferedBuiltinIds}
                  describe={describeBuiltinTool}
                  onRemove={toggleBuiltinTool}
                />
              )}
            </div>

            {/* Skills, a resource of the panel */}
            <GeneralAssistantSkills catalog={skillCatalog} choice={gaSkillChoice} onChange={setGaSkillChoice} />

            {/* Custom tools — gated behind `disable_custom_tools`. */}
            {customToolsEnabled && (
              <CustomToolsBindingSection
                enabledTools={enabledTools}
                gaToolIds={gaToolIds}
                toggleTool={toggleTool}
              />
            )}

            {/* Knowledge bases */}
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <BookOpen className="h-4 w-4 text-accent" />
                <span className="content-base font-medium">{t_i18n("Knowledge Bases")}</span>
              </div>
              <p className="content-compact text-default-secondary">{t_i18n("Click to attach or detach company-managed knowledge bases for RAG search.")}</p>
              {companyKbs.length > 0 ? (
                <div className={RESOURCE_GRID_CLASS}>
                  {companyKbs.map((kb) => {
                    const isAttached = gaKbIds.includes(kb.id);
                    return (
                      <ResourceToggleCard
                        key={kb.id}
                        attached={isAttached}
                        onToggle={() => toggleKb(kb.id)}
                        icon={<BookOpen className={cn("h-5 w-5", isAttached ? "text-accent" : "text-default-secondary")} />}
                        name={kb.name}
                        description={formatKBStats(kb.document_count, kb.chunk_count)}
                      />
                    );
                  })}
                </div>
              ) : (
                <div className="py-3 text-center">
                  <BookOpen className="h-5 w-5 text-default-disabled mx-auto mb-1" />
                  <p className="content-compact text-default-secondary">{t_i18n("No company-managed knowledge bases")}</p>
                </div>
              )}
            </div>

            <GAHandoverPicker
              agents={allAgents}
              selectedIds={gaHandoverIds}
              types={gaHandoverTypes}
              onToggle={toggleHandover}
              onTypeChange={setHandoverType}
            />
          </fieldset>
        ) : (
          /* ─── Read-only view ─── */
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="content-compact font-medium text-default-secondary uppercase tracking-wider shrink-0 whitespace-nowrap">{t_i18n("Chat Model")}</span>
              <span className="content-base">
                {gaConfig?.llm_model ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Sparkles className="h-3 w-3 text-accent opacity-60" />
                    {gaConfig.llm_model}
                  </span>
                ) : (
                  <span className="text-default-secondary italic">{t_i18n("Agent default")}</span>
                )}
              </span>
              {gaModelEnvLocked && <EnvLockBadge />}
            </div>
            <div className="flex items-center gap-3">
              <span className="content-compact font-medium text-default-secondary uppercase tracking-wider shrink-0 whitespace-nowrap">{t_i18n("Max Iterations")}</span>
              <span className="content-base font-mono-plex">{clampMaxIterations(gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT)}</span>
            </div>
            <div>
              <div className="flex items-center justify-between">
                <span className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("System Prompt")}</span>
                <button
                  onClick={() => setPromptExpanded((v) => !v)}
                  className="flex items-center gap-1 content-caption text-default-secondary hover:text-default-primary transition-colors"
                >
                  {promptExpanded ? t_i18n("Collapse") : t_i18n("Expand")}
                  <ChevronDown className={cn("h-3 w-3 transition-transform", promptExpanded && "rotate-180")} />
                </button>
              </div>
              <div className="bg-elevation-default rounded-sm p-3 mt-1">
                <div className={cn("content-compact overflow-hidden", !promptExpanded && "max-h-[6em]")}>
                  <ChatMarkdown content={gaConfig?.system_prompt || t_i18n("Default system prompt")} className="content-compact text-default-secondary [&_p]:content-compact [&_p]:leading-relaxed [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-compact [&_h1]:content-compact [&_h2]:content-compact [&_h3]:content-compact" />
                </div>
                {!promptExpanded && (
                  <div className="h-6 -mt-6 relative bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent pointer-events-none" />
                )}
              </div>
            </div>

            {/* Summary badges */}
            <div className="flex flex-wrap gap-2 pt-1">
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <Cog className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundIntegrations} {t_i18n(pluralize(boundIntegrations, "integration"))}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <Terminal className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundMcps} MCP {t_i18n(pluralize(boundMcps, "server"))}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <Sparkles className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{t_i18n("{count, plural, one {# built-in tool} other {# built-in tools}}", { count: boundBuiltins })}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft" data-testid="ga-chip-skills">
                <BookMarked className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundSkills} {t_i18n(pluralize(boundSkills, "skill"))}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <Wrench className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundTools} {t_i18n("custom")} {t_i18n(pluralize(boundTools, "tool"))}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <BookOpen className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundKbs} {t_i18n(pluralize(boundKbs, "knowledge base"))}</span>
              </div>
              <div className="flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft">
                <ArrowLeftRight className="h-3 w-3 text-default-secondary" />
                <span className="content-caption text-default-secondary">{boundHandover} {t_i18n("handover")} {t_i18n(pluralize(boundHandover, "agent"))}</span>
              </div>
            </div>
          </div>
        )}
          </>
        )}
      </CardContent>
    </Card>

    {/* Channel Routing for GA — also where the linked agent answers in its place */}
    <Card className="mt-6">
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <div>
          <CardTitle className="content-base flex items-center gap-2">
            <Bot className="h-4 w-4 text-accent" /> {t_i18n("Channels & Routing")}
            {(() => { const count = Object.keys(gaConfig?.channel_config || {}).length + (isWebEnabled ? 1 : 0); return count > 0 && !editingChannels ? <CountBadge variant="secondary">{count}</CountBadge> : null; })()}
            <Badge className="content-caption bg-feedback-error-primary/10 text-feedback-error-primary border border-feedback-error-primary/20 gap-1 ml-1"><AlertTriangle className="h-3 w-3" /> {t_i18n("Danger Zone")}</Badge>
          </CardTitle>
          <CardDescription className="content-compact mt-0.5">
            {t_i18n("Configure which messaging channels the General Assistant can be reached through. By default, only Web Chat is enabled: external channels must be explicitly turned on.")}
            {linkedAgent && (
              <>
                {" "}
                {t_i18n("{name} answers on these channels in the General Assistant's place.", { name: linkedAgent.name })}
              </>
            )}
          </CardDescription>
        </div>
        <CardEditActions editing={editingChannels} onEdit={startEditingChannels} onCancel={cancelEditingChannels} onSave={handleSaveChannels} saveDisabled={updateGAConfig.isPending} />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        {editingChannels ? (
          <div className="space-y-3">
            <p className="content-compact text-default-secondary">{t_i18n("Enable channels below and configure receive (incoming messages) and publish (outgoing messages) capabilities independently.")}</p>

            {/* Web chat toggle */}
            <div className={cn(
              "rounded-sm border transition-all",
              isWebEnabled ? "border-accent/30 bg-accent/[0.03]" : "border-elevation-subtle-soft bg-elevation-hover",
            )}>
              <div className="flex items-center gap-3 p-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                  <MessageSquare className="h-3.5 w-3.5 text-accent" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className={cn("content-base font-medium", isWebEnabled && "text-accent")}>{t_i18n("Web Chat")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Built-in chat interface")}</p>
                </div>
                <Switch aria-label={t_i18n("Web Chat")} checked={isWebEnabled} onCheckedChange={handleWebToggle} />
              </div>
            </div>

            {/* External channels — same UI as agent ConfigTab */}
            {EXTERNAL_CHANNEL_TYPES.map((chType) => {
              const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
              const botInteg = connectedBots.find((b) => b.provider === botProvider);
              const chCfg = channelConfigDraft[chType];
              const isActive = !!chCfg;
              const iconConfig = CHANNEL_ICONS[botProvider] ?? { icon: MessageSquare, bg: "bg-elevation-hover", fg: "text-default-secondary" };
              const IconComp = iconConfig.icon;
              const logoSrc = providerLogoSrc(botProvider);
              const receiveWhitelist = botInteg?.allowed_receive_channels ?? [];
              const publishWhitelist = botInteg?.allowed_publish_channels ?? [];
              // When the bot has no whitelist, fall back to all platform channels
              const platformCh = botInteg ? (platformChannelsMap[botInteg.id] ?? []) : [];
              const availableReceiveChannels = receiveWhitelist.length > 0 ? receiveWhitelist : platformCh;
              const availablePublishChannels = publishWhitelist.length > 0 ? publishWhitelist : platformCh;

              // No bot connected
              if (!botInteg) {
                return (
                  <div key={chType} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover opacity-40 p-3">
                    <div className="flex items-center gap-3">
                      <div className={`flex h-8 w-8 items-center justify-center rounded-sm ${iconConfig.bg}`}>
                        <IconComp className={`h-3.5 w-3.5 ${iconConfig.fg}`} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="content-base font-medium text-default-secondary">{CHANNEL_TYPE_LABELS[chType]}</p>
                        <p className="content-compact text-default-secondary">{t_i18n("No bot configured: set up in")} <span className="text-accent">{t_i18n("Settings → Integrations → Channels & Bots")}</span></p>
                      </div>
                    </div>
                  </div>
                );
              }

              return (
                <div key={chType} className={cn(
                  "rounded-sm border transition-all",
                  isActive ? "border-accent/30 bg-accent/[0.03]" : "border-elevation-subtle-soft bg-elevation-hover",
                )}>
                  {/* Channel header with enable toggle */}
                  <div className="flex items-center gap-3 p-3">
                    <img src={logoSrc} alt={chType} className="h-6 w-6 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className={cn("content-base font-medium", isActive && "text-accent")}>{CHANNEL_TYPE_LABELS[chType]}</p>
                      <p className="content-compact text-default-secondary">{botInteg.name}</p>
                    </div>
                    <Switch aria-label={CHANNEL_TYPE_LABELS[chType]} checked={isActive} onCheckedChange={() => toggleChannelBot(chType, botInteg.id)} />
                  </div>

                  {/* Per-channel config when enabled */}
                  {isActive && chCfg && (
                    <div className="px-3 pb-3 space-y-3 border-t border-elevation-subtle-soft pt-3 mx-3">
                      {/* Receive */}
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <MessageSquare className="h-3.5 w-3.5 text-feedback-success-primary" />
                            <span className="content-base font-medium">{t_i18n("Receive")}</span>
                            <span className="content-compact text-default-secondary">{t_i18n("(incoming messages)")}</span>
                          </div>
                          <Switch aria-label={t_i18n("Receive")} checked={chCfg.receive_enabled} onCheckedChange={(v) => {
                            updateChannelEntry(chType, { receive_enabled: v });
                          }} />
                        </div>
                        {chCfg.receive_enabled && (availableReceiveChannels.length > 0 || (botInteg ? (channelsLoadingMap[botInteg.id] ?? false) : false)) && (
                          <div className="ml-5 space-y-1">
                            <p className="content-compact text-default-secondary flex items-center gap-1">
                              <Filter className="h-2.5 w-2.5" /> {t_i18n("Restrict to specific channels")}
                            </p>
                            <ChannelPicker
                              selected={chCfg.receive_channels}
                              available={availableReceiveChannels}
                              onChange={(next) => updateChannelEntry(chType, { receive_channels: next })}
                              variant="emerald"
                              placeholder={t_i18n("Search receive channels...")}
                              ariaLabel={t_i18n("Restrict to specific channels")}
                              loading={botInteg ? (channelsLoadingMap[botInteg.id] ?? false) : false}
                            />
                          </div>
                        )}

                        {/* Receive sub-options: allow private, only mention */}
                        <div className={cn("ml-5 space-y-2 pt-1", !chCfg.receive_enabled && "opacity-50 pointer-events-none")}>
                          {/* Allow private conversations */}
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-2">
                              <MessageCircle className="h-3 w-3 text-accent" />
                              <span className="content-base font-medium">{t_i18n("Allow private")}</span>
                              <span className="content-compact text-default-secondary">{t_i18n("(DMs bypass channel restrictions)")}</span>
                            </div>
                            <Switch aria-label={t_i18n("Allow private")}
                              checked={!chCfg.receive_enabled ? false : !(botInteg?.allow_private ?? true) ? false : (chCfg.allow_private ?? true)}
                              onCheckedChange={(v) => updateChannelEntry(chType, { allow_private: v })}
                              disabled={!chCfg.receive_enabled || !(botInteg?.allow_private ?? true)}
                            />
                          </div>

                          {/* Only when mentioned */}
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-2">
                              <AtSign className="h-3 w-3 text-filigran-ia-primary" />
                              <span className="content-base font-medium">{t_i18n("Only mention")}</span>
                              <span className="content-compact text-default-secondary">{t_i18n("(respond only when @mentioned)")}</span>
                            </div>
                            <Switch aria-label={t_i18n("Only mention")}
                              checked={!chCfg.receive_enabled ? false : (botInteg?.only_mention ?? false) ? true : (chCfg.only_mention ?? false)}
                              onCheckedChange={(v) => updateChannelEntry(chType, { only_mention: v })}
                              disabled={!chCfg.receive_enabled || (botInteg?.only_mention ?? false)}
                            />
                          </div>
                        </div>
                      </div>

                      {/* Publish */}
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <ArrowLeftRight className="h-3.5 w-3.5 text-feedback-info-primary" />
                            <span className="content-base font-medium">{t_i18n("Publish")}</span>
                            <span className="content-compact text-default-secondary">{t_i18n("(outgoing messages via bot API)")}</span>
                          </div>
                          <Switch aria-label={t_i18n("Publish")} checked={chCfg.publish_enabled} onCheckedChange={(v) => updateChannelEntry(chType, { publish_enabled: v })} />
                        </div>
                        {chCfg.publish_enabled && (availablePublishChannels.length > 0 || (botInteg ? (channelsLoadingMap[botInteg.id] ?? false) : false)) && (
                          <div className="ml-5 space-y-1">
                            <p className="content-compact text-default-secondary flex items-center gap-1">
                              <Filter className="h-2.5 w-2.5" /> {t_i18n("Restrict to specific channels")}
                            </p>
                            <ChannelPicker
                              selected={chCfg.publish_channels}
                              available={availablePublishChannels}
                              onChange={(next) => updateChannelEntry(chType, { publish_channels: next })}
                              variant="blue"
                              placeholder={t_i18n("Search publish channels...")}
                              ariaLabel={t_i18n("Restrict to specific channels")}
                              loading={botInteg ? (channelsLoadingMap[botInteg.id] ?? false) : false}
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            <p className="content-compact text-default-secondary mt-1">
              {t_i18n("Platform bots are configured in")} <span className="text-accent">{t_i18n("Settings → Integrations → Channels & Bots")}</span>. {t_i18n("Separate receive and publish whitelists are defined there.")}
            </p>
          </div>
        ) : (
          /* Read-only view */
          <div className="space-y-2">
            {/* Web chat */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2.5">
              <div className="flex items-center gap-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                  <MessageSquare className="h-3.5 w-3.5 text-accent" />
                </div>
                <div>
                  <p className="content-base font-medium">{t_i18n("Web Chat")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Built-in chat interface")}</p>
                </div>
              </div>
              {isWebEnabled
                ? <Badge className="bg-feedback-success-primary/10 text-feedback-success-primary border-feedback-success-primary/20 content-caption">{t_i18n("Enabled")}</Badge>
                : <Badge className="bg-elevation-hover text-default-secondary border-elevation-subtle-soft content-caption">{t_i18n("Disabled")}</Badge>}
            </div>

            {/* External channels */}
            {EXTERNAL_CHANNEL_TYPES.map((chType) => {
              const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
              const chCfg = (gaConfig?.channel_config || {})[chType];
              const botInteg = channelBots?.find((b) => b.id === chCfg?.channel_bot_id);
              const logoSrc = providerLogoSrc(botProvider);
              return (
                <div key={chType} className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2.5">
                  <div className="flex items-center gap-3">
                    <img src={logoSrc} alt={chType} className={cn("h-6 w-6 shrink-0", !chCfg && "grayscale opacity-40")} />
                    <div>
                      <p className={cn("content-base font-medium", !chCfg && "text-default-secondary")}>{CHANNEL_TYPE_LABELS[chType]}</p>
                      <p className="content-compact text-default-secondary">
                        {chCfg ? (botInteg?.name ?? t_i18n("Configured")) : t_i18n("Not configured")}
                      </p>
                    </div>
                  </div>
                  {chCfg ? (
                    <div className="flex gap-1.5">
                      {chCfg.receive_enabled && <Badge className="bg-feedback-success-primary/10 text-feedback-success-primary border-feedback-success-primary/20 content-caption">{t_i18n("Receive")}</Badge>}
                      {chCfg.publish_enabled && <Badge className="bg-feedback-info-primary/10 text-feedback-info-primary border-feedback-info-primary/20 content-caption">{t_i18n("Publish")}</Badge>}
                      {!chCfg.receive_enabled && !chCfg.publish_enabled && <Badge className="bg-feedback-alert-primary/10 text-feedback-warning-tertiary border-feedback-alert-primary/20 content-caption">{t_i18n("Enabled (idle)")}</Badge>}
                    </div>
                  ) : (
                    <Badge className="bg-elevation-hover text-default-secondary border-elevation-subtle-soft content-caption">{t_i18n("Disabled")}</Badge>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
    {handoverTypeConfirm}
    </>
  );
}
