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

import { useRef, useMemo, useState, useCallback } from "react";
import { Icon, Slider } from "@filigran/design-system";
import type { Dispatch, SetStateAction } from "react";
import { useNavigate } from "react-router-dom";
import {
  FileText,
  ChevronDown,
  ChevronUp,
  MessageSquareOff,
  ShieldAlert,
  Globe,
  Building2,
  AlertTriangle,
  HandMetal,
  RotateCcw,
  ArrowLeftRight,
  Bot,
  CheckCircle2,
  Plus,
  ArrowRight,
  Zap,
  X,
  EyeOff,
  Code,
  Copy,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Input } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { IconPicker } from "@/components/ui/icon-picker";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { TagInput } from "@/components/ui/tag-input";
import { SearchInput } from "@/components/ui/search-input";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import type { UserSharingProps } from "@/hooks/useUserShareGrants";
import { OwnerCard } from "@/components/ui/owner-card";
import { CompanyManagedSwitch } from "@/components/ui/company-managed-switch";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { OverviewGrid, OverviewTile } from "@/components/ui/overview-grid";
import { TagList } from "@/components/ui/tag-list";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";

import { ModelSelect } from "@/components/ui/model-select";
import { cn, ROUTING_MODEL_CHOICE } from "@/lib/utils";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useRoutingModelLabel } from "@/hooks/useRoutingModelLabel";
import { ICON_MAP, OUTPUT_FORMATS, OUTPUT_FORMAT_CONFIG, REASONING_EFFORT_LEVELS, REASONING_EFFORT_CONFIG, ORGANIZATION_CONTEXT_MODE_LABELS, INTEGRATION_PROVIDER_NAMES, DEFAULT_HITL_PROMPT, HANDOVER_TYPES, HANDOVER_TYPE_CONFIG, ALL_HANDOVER_TYPES_CONFIG, MAX_ITERATIONS_CEILING, clampMaxIterations, handoverTypeConfig } from "@/lib/constants";
import { ALL_HANDOVER_TYPES, consultsBackgroundDelegator, shownHandoverType } from "@/lib/handover-types";
import type { Agent, AvailableModel, Integration, LLMProvider, OrganizationContextMode, ProviderCatalogItem, ToolRef } from "@/types";
import { OrganizationContextModeSelect, useOrganizationContextEffectiveLabel } from "@/components/organization-context-mode-select";
import {
  OrganizationContextCategoriesSelect,
  useOrganizationContextCategoriesLabel,
} from "@/components/organization-context-categories-select";
import { useUpdateAgent, usePipelineTools, useAgentPersonaVersions } from "@/hooks/api/useAgentApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useToast } from "@/components/ui/toast";
import { useCustomToolsEnabled, useAICatalogEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { PersonalIntegrationsBanner } from "./PersonalIntegrationsBanner";
import { OrphanedIntegrationsNotice } from "./OrphanedIntegrationsNotice";
import { AGENT_OVERVIEW_SPANS } from "./agent-overview-spans";
import { Spinner } from "@filigran/design-system";

/** The ids of the model fields the SettingsField labels and helpers name (one General form per page). */
const TEMPERATURE_FIELD_ID = "agent-general-temperature";
const REASONING_EFFORT_FIELD_ID = "agent-general-reasoning-effort";
const MAX_ITERATIONS_FIELD_ID = "agent-general-max-iterations";
const NAME_FIELD_ID = "agent-general-name";
const DESCRIPTION_FIELD_ID = "agent-general-description";
const SLUG_FIELD_ID = "agent-general-slug";
const TAGS_FIELD_ID = "agent-general-tags";
const MODEL_FIELD_ID = "agent-general-model";
const PERSONA_FIELD_ID = "agent-general-persona";
const COMMIT_MESSAGE_FIELD_ID = "agent-general-commit-message";

/* ── Searchable Tool Picker ──────────────────────────────────────────── */
type ToolOption = { id: string; name: string; group: string; ref: { source: string; tool_id?: string; server_id?: string; provider?: string; tool_name: string } };

function SearchableToolPicker({
  label,
  hint,
  options,
  value,
  onChange,
}: {
  label: string;
  hint: string;
  options: ToolOption[];
  value: ToolRef | null;
  onChange: (ref: ToolRef | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const selectedName = value
    ? options.find((t) => t.ref.tool_name === value.tool_name && t.ref.source === value.source)?.name
    : null;

  const filtered = useMemo(() => {
    if (!search.trim()) return options;
    const q = search.toLowerCase();
    return options.filter(
      (t) => t.name.toLowerCase().includes(q) || t.group.toLowerCase().includes(q),
    );
  }, [options, search]);

  const groups = useMemo(() => {
    const g: Record<string, ToolOption[]> = {};
    for (const t of filtered) (g[t.group] ??= []).push(t);
    return Object.entries(g);
  }, [filtered]);

  const handleSelect = useCallback(
    (opt: ToolOption | null) => {
      onChange(opt ? opt.ref as ToolRef : null);
      setOpen(false);
      setSearch("");
    },
    [onChange],
  );

  // Close on outside click
  const handleBlur = useCallback((e: React.FocusEvent) => {
    if (containerRef.current && !containerRef.current.contains(e.relatedTarget as Node)) {
      setOpen(false);
      setSearch("");
    }
  }, []);

  return (
    <div className="space-y-1.5" ref={containerRef} onBlur={handleBlur}>
      <label className="content-compact text-default-secondary">{label}</label>
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className={cn(
            "flex h-10 w-full items-center justify-between gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-compact shadow-xs",
            "ring-offset-focus focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
            !selectedName && "text-default-secondary",
          )}
        >
          <span className="truncate">{selectedName || t_i18n("None")}</span>
          <div className="flex items-center gap-1">
            {selectedName && (
              <span
                role="button"
                tabIndex={-1}
                className="rounded-sm p-0.5 hover:bg-elevation-hover"
                onClick={(e) => { e.stopPropagation(); handleSelect(null); }}
              >
                <X className="h-3 w-3 opacity-50" />
              </span>
            )}
            <ChevronDown className="h-3.5 w-3.5 opacity-50" />
          </div>
        </button>

        {open && (
          <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay">
            <div className="border-b border-elevation-subtle p-1.5">
              <SearchInput
                autoFocus
                value={search}
                onChange={setSearch}
                placeholder={t_i18n("Search tools…")}
              />
            </div>
            <div className="max-h-48 overflow-y-auto p-1">
              <button
                type="button"
                className="w-full rounded-sm px-2 py-1.5 text-left content-compact hover:bg-elevation-hover text-default-secondary"
                onMouseDown={(e) => { e.preventDefault(); handleSelect(null); }}
              >
                {t_i18n("None")}
              </button>
              {groups.map(([group, tools]) => (
                <div key={group}>
                  <div className="px-2 py-1 text-[9px] uppercase tracking-wider text-default-secondary">
                    {group}
                  </div>
                  {tools.map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      className={cn(
                        "w-full rounded-sm px-2 py-1.5 text-left content-compact hover:bg-elevation-hover",
                        value?.tool_name === t.ref.tool_name && value?.source === t.ref.source && "bg-elevation-hover text-accent",
                      )}
                      onMouseDown={(e) => { e.preventDefault(); handleSelect(t); }}
                    >
                      {t.name}
                    </button>
                  ))}
                </div>
              ))}
              {filtered.length === 0 && (
                <p className="px-2 py-3 text-center content-caption text-default-secondary">{t_i18n("No matching tools")}</p>
              )}
            </div>
          </div>
        )}
      </div>
      <p className="content-caption text-default-secondary">{hint}</p>
    </div>
  );
}

/* ── Handover: promote a non-company-managed agent inline ─────────────── */
function HandoverPromoteButton({
  agentId,
  agentName,
  isAdmin,
}: {
  agentId: string;
  agentName: string;
  isAdmin: boolean;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const promote = useUpdateAgent();
  const { toast } = useToast();
  const [error, setError] = useState<string | null>(null);

  if (!isAdmin) {
    return (
      <Tip content={t_i18n("Only admins can promote an agent to company-managed.")} wrap>
        <Button variant="outline" size="sm" disabled className="h-7 gap-1.5 text-[11px]">
          <Building2 className="h-3 w-3" /> {t_i18n("Promote to company-managed")}
        </Button>
      </Tip>
    );
  }

  const handlePromote = () => {
    setError(null);
    promote.mutate(
      { id: agentId, is_company_managed: true } as { id: string } & Partial<Agent>,
      {
        onSuccess: () => toast({ title: t_i18n('"{name}" is now company-managed', { name: agentName }), variant: "success" }),
        onError: (err) => setError(err instanceof Error ? err.message : t_i18n("Could not promote this agent.")),
      },
    );
  };

  return (
    <div className="space-y-1">
      <Button
        variant="outline"
        size="sm"
        disabled={promote.isPending || aiOff}
        onClick={handlePromote}
        className="h-7 gap-1.5 text-[11px] border-accent/40 text-accent hover:bg-accent/10" aria-busy={promote.isPending || undefined}>
        {promote.isPending && <Spinner size="sm" tone="inherit" />}{!promote.isPending && <Building2 className="h-3 w-3" />}
        {promote.isPending ? t_i18n("Promoting...") : t_i18n("Promote to company-managed")}
      </Button>
      {error && <p className="content-caption text-feedback-error-primary">{error}</p>}
    </div>
  );
}

type GeneralSectionProps = {
  agent: Agent;
  canManage: boolean;
  isAdmin: boolean;
  isPending: boolean;
  // General card
  editingGeneral: boolean;
  name: string;
  setName: (v: string) => void;
  icon: string;
  setIcon: (v: string) => void;
  description: string;
  setDescription: (v: string) => void;
  slug: string;
  setSlug: (v: string) => void;
  tags: string[];
  setTags: (v: string[]) => void;
  /** `<provider_id>::<model>` — see modelChoiceValue. "" = platform default. */
  llmChoice: string;
  setLlmChoice: (v: string) => void;
  /** Provider configs, so an option can name the one that offers the model. */
  llmProviders?: LLMProvider[] | null;
  temperature: number | null;
  setTemperature: (v: number | null) => void;
  reasoningEffort: string | null;
  setReasoningEffort: (v: string | null) => void;
  organizationContextMode: OrganizationContextMode;
  setOrganizationContextMode: (v: OrganizationContextMode) => void;
  organizationContextCategories: string[] | null;
  setOrganizationContextCategories: (v: string[] | null) => void;
  maxIterations: number | null;
  setMaxIterations: (v: number | null) => void;
  persona: string;
  setPersona: (v: string) => void;
  commitMessage: string;
  setCommitMessage: (v: string) => void;
  personaExpanded: boolean;
  setPersonaExpanded: Dispatch<SetStateAction<boolean>>;
  outputFormat: string;
  setOutputFormat: (v: string) => void;
  outputSchema: Record<string, unknown> | null;
  setOutputSchema: (v: Record<string, unknown> | null) => void;
  disableChat: boolean;
  setDisableChat: (v: boolean) => void;
  hideRuns: boolean;
  setHideRuns: (v: boolean) => void;
  hideActivity: boolean;
  setHideActivity: (v: boolean) => void;
  humanInTheLoop: boolean;
  setHumanInTheLoop: (v: boolean) => void;
  hitlPrompt: string | null;
  setHitlPrompt: (v: string | null) => void;
  platformMaxIterations: number;
  availableModels: AvailableModel[] | undefined;
  onStartEditGeneral: () => void;
  onSaveGeneral: () => void;
  onCancelGeneral: () => void;
  // Guardrails
  editingGuardrails: boolean;
  guardrails: string;
  setGuardrails: (v: string) => void;
  onStartEditGuardrails: () => void;
  onSaveGuardrails: () => void;
  onCancelGuardrails: () => void;
  // Handover
  editingHandover: boolean;
  handoverIds: string[];
  onToggleHandover: (id: string) => void;
  handoverTypes: Record<string, string>;
  /** The draft's targets each run every handover type (the legacy mode, until a type is picked). */
  handoverLegacy: boolean;
  onUpdateHandoverType: (agentId: string, type: string) => void;
  handoverConditions: Record<string, string>;
  onUpdateHandoverCondition: (agentId: string, condition: string) => void;
  handoverSearch: string;
  setHandoverSearch: (v: string) => void;
  handoverCandidates: Agent[];
  allAgents: Agent[] | undefined;
  currentUserId?: string;
  onStartEditHandover: () => void;
  onSaveHandover: () => void;
  onCancelHandover: () => void;
  /** Whether the agent may split its work across background copies of itself. */
  onSelfFanoutToggle: (enabled: boolean) => void;
  /** Whether a caller's per-call output schema may replace the agent's own format. */
  onCallerOutputFormatToggle: (enabled: boolean) => void;
  // Admin
  companyManaged: boolean;
  sharedGroupIds: string[] | null;
  sharedGroupPermissions: Record<string, string> | null;
  onSaveCompanyManaged: (val: boolean) => void;
  onSaveSharedGroups: (ids: string[] | null) => void;
  onSaveSharedGroupPermissions: (perms: Record<string, string> | null) => void;
  /** Per-user grants + the company-managed access restriction. */
  userSharing: UserSharingProps;
  onSaveOwner: (userId: string) => void;
  onPublicCatalogToggle: (val: boolean) => void;
  /** Whether the catalog download carries the code of personal custom tools. */
  onCatalogPersonalToolsToggle: (val: boolean) => void;
  integrations?: Integration[];
  catalog?: ProviderCatalogItem[];
  /** Lend the owner's personal integrations through this agent (`[]` stops). */
  onLendIntegrations?: (integrationIds: string[]) => void;
  onMakeBindingsPerUser?: (integrationIds: string[]) => void;
};

export function GeneralSection({
  agent,
  canManage,
  isAdmin,
  isPending,
  editingGeneral,
  name,
  setName,
  icon,
  setIcon,
  description,
  setDescription,
  slug,
  setSlug,
  tags,
  setTags,
  llmChoice,
  setLlmChoice,
  llmProviders,
  temperature,
  setTemperature,
  reasoningEffort,
  setReasoningEffort,
  organizationContextMode,
  setOrganizationContextMode,
  organizationContextCategories,
  setOrganizationContextCategories,
  maxIterations,
  setMaxIterations,
  persona,
  setPersona,
  commitMessage,
  setCommitMessage,
  personaExpanded,
  setPersonaExpanded,
  outputFormat,
  setOutputFormat,
  outputSchema,
  setOutputSchema,
  disableChat,
  setDisableChat,
  hideRuns,
  setHideRuns,
  hideActivity,
  setHideActivity,
  humanInTheLoop,
  setHumanInTheLoop,
  hitlPrompt,
  setHitlPrompt,
  platformMaxIterations,
  availableModels,
  onStartEditGeneral,
  onSaveGeneral,
  onCancelGeneral,
  editingGuardrails,
  guardrails,
  setGuardrails,
  onStartEditGuardrails,
  onSaveGuardrails,
  onCancelGuardrails,
  editingHandover,
  handoverIds,
  onToggleHandover,
  handoverTypes,
  handoverLegacy,
  onUpdateHandoverType,
  handoverConditions,
  onUpdateHandoverCondition,
  handoverSearch,
  setHandoverSearch,
  handoverCandidates,
  allAgents,
  currentUserId,
  onStartEditHandover,
  onSaveHandover,
  onCancelHandover,
  onSelfFanoutToggle,
  onCallerOutputFormatToggle,
  companyManaged,
  sharedGroupIds,
  sharedGroupPermissions,
  onSaveCompanyManaged,
  onSaveSharedGroups,
  onSaveSharedGroupPermissions,
  userSharing,
  onSaveOwner,
  onPublicCatalogToggle,
  onCatalogPersonalToolsToggle,
  integrations,
  onLendIntegrations,
  onMakeBindingsPerUser,
}: GeneralSectionProps) {
  const aiOff = useAiActionDisabled();
  const { t_i18n, intl } = useFormatter();
  const routingModel = useRoutingModelLabel();
  const organizationContextInEffect = useOrganizationContextEffectiveLabel("agent", agent.organization_context_mode ?? "inherit");
  const organizationCategoriesInEffect = useOrganizationContextCategoriesLabel("agent", agent.organization_context_categories ?? null);
  const navigate = useNavigate();
  const personaRef = useRef<HTMLTextAreaElement>(null);
  const guardrailsRef = useRef<HTMLTextAreaElement>(null);
  const [showHitlPrompt, setShowHitlPrompt] = useState(false);
  const catalogEnabled = useAICatalogEnabled();

  // Only the active persona version badge is rendered here, so a
  // single-row fetch suffices: the list is ordered by newest first and
  // the active row (``is_active``) is at position 0 or absent.  Fetching
  // ``limit: 50`` shipped 50 PromptVersion rows on every agent detail
  // view without using 49 of them.
  const { data: personaVersionsData } = useAgentPersonaVersions(agent.id, { limit: 1, offset: 0 });
  const activePersonaVersionNumber = personaVersionsData?.items.find((v) => v.is_active)?.version_number;

  // ── Pipeline: local state + self-contained save ──────────────────────
  const pipelineUpdate = useUpdateAgent();
  const [pipelineConsent, confirmSharing] = useCredentialSharingConfirm();
  const { toast } = useToast();
  const { data: pipelineTools } = usePipelineTools();
  const customToolsEnabled = useCustomToolsEnabled();
  const [pipelineEditing, setPipelineEditing] = useState(false);
  const [prepareRef, setPrepareRef] = useState<ToolRef | null>(agent.prepare_tool_ref);
  const [finalizeRef, setFinalizeRef] = useState<ToolRef | null>(agent.finalize_tool_ref);
  const [skipLlm, setSkipLlm] = useState(agent.skip_llm);

  const prepareOptions: ToolOption[] = useMemo(() => pipelineTools?.prepare ?? [], [pipelineTools]);
  const finalizeOptions: ToolOption[] = useMemo(() => pipelineTools?.finalize ?? [], [pipelineTools]);

  const startPipelineEdit = () => {
    setPrepareRef(agent.prepare_tool_ref);
    setFinalizeRef(agent.finalize_tool_ref);
    setSkipLlm(agent.skip_llm);
    setPipelineEditing(true);
  };
  const cancelPipeline = () => {
    setPrepareRef(agent.prepare_tool_ref);
    setFinalizeRef(agent.finalize_tool_ref);
    setSkipLlm(agent.skip_llm);
    setPipelineEditing(false);
  };
  const savePipeline = async () => {
    // A pipeline step's integration or custom tool runs for the agent's audience.
    if (!(await confirmSharing({
      entity_type: "agent",
      entity_id: agent.id,
      change: { prepare_tool_ref: prepareRef, finalize_tool_ref: finalizeRef },
    }))) {
      return;
    }
    pipelineUpdate.mutate(
      { id: agent.id, prepare_tool_ref: prepareRef, finalize_tool_ref: finalizeRef, skip_llm: skipLlm, output_format: outputFormat, output_schema: outputSchema } as { id: string } & Partial<Agent>,
      { onSuccess: () => { toast({ title: t_i18n("Pipeline saved"), variant: "success" }); setPipelineEditing(false); } },
    );
  };
  const pipelinePrepareName = prepareRef?.tool_name || null;
  const pipelineFinalizeName = finalizeRef?.tool_name || null;

  // Local state for JSON schema textarea — keeps raw text so user can type
  // freely even when intermediate JSON is invalid. Reset when edit mode toggles
  // by deriving initial value from the current outputSchema.
  const schemaInitial = useMemo(
    () => (outputSchema ? JSON.stringify(outputSchema, null, 2) : ""),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pipelineEditing],
  );
  const [schemaText, setSchemaText] = useState(schemaInitial);
  const [schemaValid, setSchemaValid] = useState(true);
  if (schemaText !== schemaInitial && !pipelineEditing) {
    // Pipeline edit mode toggled off — reset local state
    setSchemaText(schemaInitial);
    setSchemaValid(true);
  }
  const handleSchemaChange = (raw: string) => {
    setSchemaText(raw);
    const trimmed = raw.trim();
    if (!trimmed) {
      setOutputSchema(null);
      setSchemaValid(true);
      return;
    }
    try {
      const parsed = JSON.parse(trimmed);
      setOutputSchema(parsed);
      setSchemaValid(true);
    } catch {
      setSchemaValid(false);
    }
  };


  return (
    <>
      {pipelineConsent}
      {/* Basic info */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <CardTitle className="content-base">{t_i18n("General")}</CardTitle>
          <CardEditActions editing={editingGeneral} canEdit={canManage} onEdit={onStartEditGeneral} onCancel={onCancelGeneral} onSave={onSaveGeneral} saveDisabled={isPending} testIds={{ edit: "edit-general" }} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingGeneral ? (
            <div className="space-y-4">
              {/* Identity and model fields (UX s.2: DS Label at body size, info bubble, helper tied to the control, #11585) */}
              <div className="grid gap-4 sm:grid-cols-3">
                <SettingsField
                  id={NAME_FIELD_ID}
                  label={t_i18n("Name")}
                  info={t_i18n("The agent's display name in lists, chats and mentions, for example \"Threat Intel Analyst\".")}
                  helper={t_i18n("Shown wherever the agent appears; its icon is picked on the left.")}
                >
                  <div className="flex items-center gap-2">
                    <IconPicker value={icon} onChange={setIcon} />
                    <Input id={NAME_FIELD_ID} aria-labelledby={settingsFieldLabelId(NAME_FIELD_ID)} aria-describedby={settingsFieldHintId(NAME_FIELD_ID)} className="flex-1" value={name} onChange={(e) => setName(e.target.value)} />
                  </div>
                </SettingsField>
                <SettingsField
                  id={DESCRIPTION_FIELD_ID}
                  className="sm:col-span-2"
                  label={t_i18n("Description")}
                  info={t_i18n("One sentence on what the agent does, for example \"Triages new phishing reports\". Shown on the agent's page. Empty leaves the agent without a summary.")}
                  helper={t_i18n("What the agent does, in one sentence.")}
                >
                  <Input id={DESCRIPTION_FIELD_ID} aria-labelledby={settingsFieldLabelId(DESCRIPTION_FIELD_ID)} aria-describedby={settingsFieldHintId(DESCRIPTION_FIELD_ID)} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What does this agent do?")} />
                </SettingsField>
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <SettingsField
                  id={SLUG_FIELD_ID}
                  label={t_i18n("Slug")}
                  info={t_i18n("The agent's short unique identifier: lower-case letters, digits and hyphens, for example \"threat-intel-analyst\". Empty generates it from the name.")}
                  helper={t_i18n("Lower-case letters, digits and hyphens; generated from the name when empty.")}
                >
                  <Input id={SLUG_FIELD_ID} aria-labelledby={settingsFieldLabelId(SLUG_FIELD_ID)} aria-describedby={settingsFieldHintId(SLUG_FIELD_ID)} value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} placeholder={t_i18n("auto-generated")} className="font-mono-plex content-base" />
                </SettingsField>
                <SettingsField
                  id={TAGS_FIELD_ID}
                  label={t_i18n("Tags")}
                  info={t_i18n("Labels to find and filter agents, for example \"cti\" or \"triage\". Empty leaves the agent untagged.")}
                  helper={t_i18n("Used to filter the agent list.")}
                >
                  <TagInput id={TAGS_FIELD_ID} aria-describedby={settingsFieldHintId(TAGS_FIELD_ID)} value={tags} onChange={setTags} placeholder={t_i18n("Add tags…")} />
                </SettingsField>
                <SettingsField
                  id={MODEL_FIELD_ID}
                  label={t_i18n("Model")}
                  info={t_i18n("The language model that answers for this agent, for example a GPT or Claude model. Platform default uses the platform's default model; Platform routing model uses the model set as the platform's routing model.")}
                  helper={t_i18n("The model that answers for this agent.")}
                >
                  <ModelSelect
                    id={MODEL_FIELD_ID}
                    aria-labelledby={settingsFieldLabelId(MODEL_FIELD_ID)}
                    aria-describedby={settingsFieldHintId(MODEL_FIELD_ID)}
                    models={availableModels}
                    providers={llmProviders}
                    pinned={[
                      { value: "__default__", label: t_i18n("Platform default") },
                      { value: ROUTING_MODEL_CHOICE, label: t_i18n("Platform routing model"), selectedLabel: routingModel.label },
                    ]}
                    value={llmChoice || "__default__"}
                    onValueChange={(v) => setLlmChoice(v === "__default__" ? "" : v)}
                  />
                </SettingsField>
              </div>
              {/* Model settings, compact (#11694): Temperature and Max Iterations 50/50, then the three selects 33/33/33; one column under sm, DOM order = reading order */}
              <div className="grid items-start gap-4 sm:grid-cols-2" data-testid="agent-general-sliders">
              {/* Temperature (UX s.2: DS Label at body size, info bubble, helper tied to the control, #11585) */}
              <SettingsField
                id={TEMPERATURE_FIELD_ID}
                label={t_i18n("Temperature")}
                info={t_i18n("How freely the model picks its words, from 0.0 (the same answer every time) to 2.0 (the most varied). Empty uses the provider's default.")}
                helper={t_i18n("Controls LLM randomness. Lower (0.0) = deterministic, higher (2.0) = creative. Default varies by provider (~0.7).")}
                action={temperature !== null && (
                  <button type="button" className="content-caption text-accent hover:text-accent/80" onClick={() => setTemperature(null)}>
                    {t_i18n("Reset to default")}
                  </button>
                )}
              >
                {/* min-h-9 on both slider rows: the Temperature value text and the Max Iterations input share one control-row height, so the two tracks align (UX round 1, B1) */}
                <div className="flex min-h-9 items-center gap-3">
                  <Slider
                    min={0} max={2} step={0.1}
                    value={[temperature ?? 0.7]}
                    onValueChange={([value]) => setTemperature(Math.round(value * 10) / 10)}
                    aria-labelledby={settingsFieldLabelId(TEMPERATURE_FIELD_ID)}
                    aria-describedby={settingsFieldHintId(TEMPERATURE_FIELD_ID)}
                    wrapperClassName="flex-1"
                  />
                  <span className="content-base font-mono-plex w-10 text-right tabular-nums">{temperature !== null ? temperature.toFixed(1) : "—"}</span>
                </div>
              </SettingsField>
              {/* Max Iterations */}
              <SettingsField
                id={MAX_ITERATIONS_FIELD_ID}
                label={t_i18n("Max Iterations")}
                info={t_i18n("The number of tool-calling rounds the agent may take to answer one message, from 1 to {ceiling}. Empty uses the platform default ({default}).", { default: platformMaxIterations, ceiling: MAX_ITERATIONS_CEILING })}
                helper={
                  <>
                    {t_i18n("Maximum tool-calling rounds per chat message. Near this limit the agent wraps up, unless it is still making progress and the platform lets it continue, up to {ceiling} rounds. Platform default is {default}.", { default: platformMaxIterations, ceiling: MAX_ITERATIONS_CEILING })}{" "}
                    {t_i18n("The images it may look at in one message grow with it, so long visual work (videos, decks) keeps its sight.")}
                  </>
                }
                action={maxIterations !== null && (
                  <button type="button" className="content-caption text-accent hover:text-accent/80" onClick={() => setMaxIterations(null)}>
                    {t_i18n("Reset to default")}
                  </button>
                )}
              >
                <div className="flex min-h-9 items-center gap-3">
                  <Slider
                    min={1} max={MAX_ITERATIONS_CEILING} step={1}
                    value={[maxIterations ?? platformMaxIterations]}
                    onValueChange={([value]) => setMaxIterations(value)}
                    aria-labelledby={settingsFieldLabelId(MAX_ITERATIONS_FIELD_ID)}
                    aria-describedby={settingsFieldHintId(MAX_ITERATIONS_FIELD_ID)}
                    wrapperClassName="flex-1"
                  />
                  <Input
                    id={MAX_ITERATIONS_FIELD_ID}
                    aria-labelledby={settingsFieldLabelId(MAX_ITERATIONS_FIELD_ID)}
                    aria-describedby={settingsFieldHintId(MAX_ITERATIONS_FIELD_ID)}
                    type="number"
                    min={1}
                    max={MAX_ITERATIONS_CEILING}
                    step={1}
                    value={maxIterations ?? ""}
                    placeholder={String(platformMaxIterations)}
                    onChange={(e) => {
                      const value = parseInt(e.target.value, 10);
                      setMaxIterations(Number.isFinite(value) ? clampMaxIterations(value) : null);
                    }}
                    className="w-24 text-right font-mono-plex tabular-nums"
                  />
                </div>
              </SettingsField>
              </div>
              <div className="grid items-start gap-4 sm:grid-cols-3" data-testid="agent-general-selects">
              {/* Reasoning Effort */}
              <SettingsField
                id={REASONING_EFFORT_FIELD_ID}
                label={t_i18n("Reasoning Effort")}
                info={t_i18n("How long a reasoning model thinks before it answers: more effort gives deeper answers, slower and at a higher cost. Models without reasoning ignore it.")}
                helper={t_i18n("Controls thinking depth for reasoning models (OpenAI o-series, Anthropic Opus, Gemini 2.5). Leave as default for non-reasoning models.")}
                action={reasoningEffort !== null && (
                  <button type="button" className="content-caption text-accent hover:text-accent/80" onClick={() => setReasoningEffort(null)}>
                    {t_i18n("Reset to default")}
                  </button>
                )}
              >
                <Select value={reasoningEffort || "__default__"} onValueChange={(v) => setReasoningEffort(v === "__default__" ? null : v)}>
                  <SelectTrigger id={REASONING_EFFORT_FIELD_ID} aria-describedby={settingsFieldHintId(REASONING_EFFORT_FIELD_ID)}>
                    <SelectValue placeholder={t_i18n("Platform default")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__default__">{t_i18n("Platform default")}</SelectItem>
                    {REASONING_EFFORT_LEVELS.map((level) => (
                      <SelectItem key={level} value={level}>
                        {t_i18n(REASONING_EFFORT_CONFIG[level].label)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </SettingsField>
              {/* Organization context (#11533) */}
              <div className="min-w-0">
                <OrganizationContextModeSelect scope="agent" value={organizationContextMode} onChange={setOrganizationContextMode} />
              </div>
              {organizationContextMode !== "off" && (
                <div className="min-w-0">
                  <OrganizationContextCategoriesSelect
                    scope="agent"
                    value={organizationContextCategories}
                    onChange={setOrganizationContextCategories}
                  />
                </div>
              )}
              </div>

              <SettingsField
                id={PERSONA_FIELD_ID}
                label={t_i18n("Persona / System Prompt")}
                info={t_i18n("The instructions the agent follows in every run: its role, tone and rules, for example \"You are a CTI analyst; answer in English\". Saving a change makes a new version. Empty leaves the agent without standing instructions.")}
                helper={t_i18n("The agent's standing instructions; saving a change makes a new version.")}
                action={
                  <div className="flex items-center gap-1" data-testid="persona-tools">
                    {activePersonaVersionNumber !== undefined && (
                      <button
                        type="button"
                        onClick={() => navigate(`/agents/${agent.id}/versioning`)}
                        className="rounded-sm"
                        aria-label={t_i18n("Open persona versioning")}
                      >
                        <Badge variant="outline" className="h-4 px-1.5 content-caption border-elevation-subtle-soft text-default-secondary hover:border-accent/40 hover:text-accent transition-colors cursor-pointer">
                          v{activePersonaVersionNumber}
                        </Badge>
                      </button>
                    )}
                    <PromptGenerator contextType="persona" currentText={persona} onGenerated={setPersona} agentName={agent.name} />
                    <VariableInsertButton textareaRef={personaRef} value={persona} onChange={setPersona} />
                    <CopyIconButton value={persona} label={t_i18n("Copy persona")} />
                  </div>
                }
              >
                <textarea id={PERSONA_FIELD_ID} aria-describedby={settingsFieldHintId(PERSONA_FIELD_ID)} ref={personaRef} data-testid="agent-persona" className={`flex min-h-[150px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`} value={persona} onChange={(e) => setPersona(e.target.value)} />
              </SettingsField>

              <SettingsField
                id={COMMIT_MESSAGE_FIELD_ID}
                label={t_i18n("Commit message (optional)")}
                info={t_i18n("A short note stored with the new persona version, for example \"Stricter sourcing rules\". Shown in the version history. Empty saves the version without a note.")}
                helper={t_i18n("Stored with the persona version this save creates.")}
              >
                <Input
                  id={COMMIT_MESSAGE_FIELD_ID}
                  aria-labelledby={settingsFieldLabelId(COMMIT_MESSAGE_FIELD_ID)}
                  aria-describedby={settingsFieldHintId(COMMIT_MESSAGE_FIELD_ID)}
                  value={commitMessage}
                  onChange={(e) => setCommitMessage(e.target.value)}
                  placeholder={t_i18n("Describe persona/config change")}
                />
              </SettingsField>

              {/* Disable Chat & Proxy */}
              <div className="flex items-center justify-between rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <div className="space-y-0.5 flex-1 mr-4">
                  <p className="content-base font-medium flex items-center gap-2">
                    <MessageSquareOff className="h-3.5 w-3.5 text-default-secondary" /> {t_i18n("Not suitable for chat")}
                  </p>
                  <p className="content-caption text-default-secondary">{t_i18n("Hide from LLM proxy & profile, grey-out in chat picker. Useful for assignment-only agents.")}</p>
                </div>
                <Switch aria-label={t_i18n("Not suitable for chat")} checked={disableChat} onCheckedChange={setDisableChat} />
              </div>

              {/* Hide runs from other users (company-managed agents only) */}
              {agent.is_company_managed && (
                <div className="flex items-center justify-between rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                  <div className="space-y-0.5 flex-1 mr-4">
                    <p className="content-base font-medium flex items-center gap-2">
                      <EyeOff className="h-3.5 w-3.5 text-default-secondary" /> {t_i18n("Hide runs from other users")}
                    </p>
                    <p className="content-caption text-default-secondary">{t_i18n("Each run is private to the user it executed as (not visible to everyone). Default for assignments; they can override. Lets non-admins trigger per-user assignments.")}</p>
                  </div>
                  <Switch aria-label={t_i18n("Hide runs from other users")} checked={hideRuns} onCheckedChange={setHideRuns} />
                </div>
              )}

              {/* Hide activity from non-admins (company-managed agents, admins only, #11624) */}
              {agent.is_company_managed && isAdmin && (
                <div className="flex items-center justify-between rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                  <div className="space-y-0.5 flex-1 mr-4">
                    <p className="content-base font-medium flex items-center gap-2">
                      <Icon name="shield-check" size={16} className="size-3.5 shrink-0 text-default-secondary" /> {t_i18n("Hide activity from non-admins")}
                    </p>
                    <p className="content-caption text-default-secondary">{t_i18n("Runs, traces, work items and shared state of this agent are visible to administrators only.")}</p>
                  </div>
                  <Switch aria-label={t_i18n("Hide activity from non-admins")} checked={hideActivity} onCheckedChange={setHideActivity} />
                </div>
              )}

              {/* Human in the loop (agent-level default) */}
              <div className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5 flex-1 mr-4">
                    <p className="content-base font-medium flex items-center gap-2">
                      <HandMetal className="h-3.5 w-3.5 text-feedback-warning-primary" /> {t_i18n("Human in the loop")}
                    </p>
                    <p className="content-caption text-default-secondary">{t_i18n("Enable HITL by default for all assignments. Individual assignments can override this setting.")}</p>
                  </div>
                  <Switch aria-label={t_i18n("Human in the loop")} checked={humanInTheLoop} onCheckedChange={setHumanInTheLoop} />
                </div>
                {humanInTheLoop && (
                  <div className="space-y-2">
                    <button type="button" className="flex items-center gap-1.5 content-caption text-default-secondary hover:text-default-primary transition-colors" onClick={() => setShowHitlPrompt((v) => !v)}>
                      {showHitlPrompt ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                      {t_i18n("System prompt addition")} {hitlPrompt ? t_i18n("(customized)") : t_i18n("(default)")}
                    </button>
                    {showHitlPrompt && (
                      <div className="space-y-1.5">
                        <div className="flex justify-end">
                          <PromptGenerator contextType="hitl_prompt" currentText={hitlPrompt ?? DEFAULT_HITL_PROMPT} onGenerated={setHitlPrompt} agentName={agent.name} />
                        </div>
                        <textarea
                          className={`w-full min-h-[140px] rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-primary placeholder:text-default-secondary ${FIELD_FOCUS_RING} resize-y`}
                          value={hitlPrompt ?? DEFAULT_HITL_PROMPT}
                          onChange={(e) => setHitlPrompt(e.target.value)}
                        />
                        {hitlPrompt && (
                          <button type="button" className="flex items-center gap-1 content-caption text-accent hover:text-accent/80" onClick={() => setHitlPrompt(null)}>
                            <RotateCcw className="h-3 w-3" /> {t_i18n("Reset to default")}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {/* Two full rows on a wide screen (#11838): identity and access, then behaviour. The spans of
                  AGENT_OVERVIEW_SPANS fill every row at every breakpoint, in this DOM order. */}
              <OverviewGrid testId="agent-general-overview">
                <OverviewTile label={t_i18n("Name")} span={AGENT_OVERVIEW_SPANS.name}>
                  <div className="flex min-w-0 items-center gap-2">
                    {(() => { const I = ICON_MAP[agent.icon]; return I ? <I className="h-4 w-4 shrink-0 text-accent" /> : null; })()}
                    <TruncatedText text={agent.name} className="content-base font-semibold" />
                  </div>
                </OverviewTile>
                <OverviewTile label={t_i18n("Slug")} span={AGENT_OVERVIEW_SPANS.slug}>
                  {agent.slug ? <TruncatedText text={agent.slug} className="content-base font-mono-plex" /> : <span className="content-base text-default-secondary italic">{t_i18n("auto")}</span>}
                </OverviewTile>
                <OverviewTile label={t_i18n("Tags")} span={AGENT_OVERVIEW_SPANS.tags}>
                  {(agent.tags ?? []).length > 0 ? (
                    <TagList tags={agent.tags} max={agent.tags?.length} badgeMin="whole" className="h-5" />
                  ) : <span className="content-base text-default-secondary italic">{t_i18n("None")}</span>}
                </OverviewTile>
                <OverviewTile label={t_i18n("Model")} span={AGENT_OVERVIEW_SPANS.model}>
                  {agent.llm_model_source === "routing" ? routingModel.label : agent.llm_model || <span className="content-base text-default-secondary italic">{t_i18n("Platform default")}</span>}
                </OverviewTile>
                <OverviewTile label={t_i18n("Chat")} icon={<MessageSquareOff className="h-3 w-3 shrink-0 text-default-secondary" />} span={AGENT_OVERVIEW_SPANS.chat}>
                  <div className="flex min-w-0 items-center gap-2">
                    <div className={cn("h-2 w-2 shrink-0 rounded-full", !agent.disable_chat ? "bg-feedback-success-primary shadow-[0_0_6px_rgba(52,211,153,0.4)]" : "bg-elevation-hover")} />
                    <TruncatedText text={agent.disable_chat ? t_i18n("Assignment-only") : t_i18n("Available")} className={cn("content-base font-semibold", !agent.disable_chat ? "text-feedback-success-primary" : "text-default-secondary")} />
                  </div>
                </OverviewTile>
                <OverviewTile label={t_i18n("Human in the loop")} icon={<HandMetal className="h-3 w-3 shrink-0 text-feedback-warning-primary" />} span={AGENT_OVERVIEW_SPANS.humanInTheLoop}>
                  <div className="flex min-w-0 items-center gap-2">
                    <div className={cn("h-2 w-2 shrink-0 rounded-full", agent.human_in_the_loop ? "bg-feedback-warning-primary shadow-[0_0_6px_rgba(251,146,60,0.4)]" : "bg-elevation-hover")} />
                    <TruncatedText
                      text={agent.human_in_the_loop ? (agent.human_in_the_loop_prompt ? t_i18n("On (custom prompt)") : t_i18n("On")) : t_i18n("Off")}
                      className={cn("content-base font-semibold", agent.human_in_the_loop ? "text-feedback-warning-primary" : "text-default-secondary")}
                    />
                  </div>
                </OverviewTile>
                <OverviewTile label={t_i18n("Temperature")} span={AGENT_OVERVIEW_SPANS.temperature}>
                  {agent.temperature !== null && agent.temperature !== undefined ? <span className="content-base font-mono-plex">{agent.temperature.toFixed(1)}</span> : <span className="content-base text-default-secondary italic">{t_i18n("Default")}</span>}
                </OverviewTile>
                <OverviewTile label={t_i18n("Reasoning Effort")} span={AGENT_OVERVIEW_SPANS.reasoningEffort}>
                  {agent.reasoning_effort ? t_i18n(REASONING_EFFORT_CONFIG[agent.reasoning_effort as keyof typeof REASONING_EFFORT_CONFIG]?.label ?? agent.reasoning_effort) : <span className="content-base text-default-secondary italic">{t_i18n("Default")}</span>}
                </OverviewTile>
                <OverviewTile label={t_i18n("Organization context")} span={AGENT_OVERVIEW_SPANS.organizationContext} hint={organizationContextInEffect} testId="agent.organization-context-read">
                  {t_i18n(ORGANIZATION_CONTEXT_MODE_LABELS[agent.organization_context_mode ?? "inherit"])}
                </OverviewTile>
                <OverviewTile label={t_i18n("Preloaded organization categories")} span={AGENT_OVERVIEW_SPANS.organizationCategories} hint={organizationCategoriesInEffect} testId="agent.organization-categories-read">
                  {agent.organization_context_categories == null ? t_i18n("Inherit") : t_i18n("Set on this agent")}
                </OverviewTile>
                <OverviewTile label={t_i18n("Max Iterations")} span={AGENT_OVERVIEW_SPANS.maxIterations}>
                  {agent.max_iterations !== null && agent.max_iterations !== undefined ? <span className="content-base font-mono-plex">{agent.max_iterations}</span> : <TruncatedText text={t_i18n("Default ({value})", { value: platformMaxIterations })} className="content-base text-default-secondary italic" />}
                </OverviewTile>
              </OverviewGrid>
              {agent.persona && (
                <div className="rounded-sm bg-elevation-hover px-3 py-2.5 min-w-0 overflow-hidden">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5">
                      <p className="content-caption text-default-secondary leading-none">{t_i18n("Persona")}</p>
                      {activePersonaVersionNumber !== undefined && (
                        <button
                          type="button"
                          onClick={() => navigate(`/agents/${agent.id}/versioning`)}
                          className="rounded-sm"
                          aria-label={t_i18n("Open persona versioning")}
                        >
                          <Badge variant="outline" className="h-4 px-1.5 content-caption border-elevation-subtle-soft text-default-secondary hover:border-accent/40 hover:text-accent transition-colors cursor-pointer">
                            v{activePersonaVersionNumber}
                          </Badge>
                        </button>
                      )}
                    </div>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setPersonaExpanded((v) => !v)}
                        className="flex items-center gap-1 content-caption text-default-secondary hover:text-default-primary transition-colors"
                      >
                        {personaExpanded ? t_i18n("Collapse") : t_i18n("Expand")}
                        <ChevronDown className={cn("h-3 w-3 transition-transform", personaExpanded && "rotate-180")} />
                      </button>
                      <CopyIconButton value={agent.persona} label={t_i18n("Copy persona")} />
                    </div>
                  </div>
                  <div className={cn("mt-1.5 content-base overflow-hidden min-w-0", !personaExpanded && "max-h-[4.5em]")}>
                    <ChatMarkdown content={agent.persona} className="content-base min-w-0 [&_div]:min-w-0 [&_div]:max-w-full [&_code]:whitespace-pre-wrap [&_code]:break-all [&_p]:content-base [&_p]:leading-relaxed [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-base [&_h1]:content-base [&_h2]:content-base [&_h3]:content-base" />
                  </div>
                  {!personaExpanded && (
                    <div className="h-6 -mt-6 relative bg-gradient-to-t from-[var(--bg-elevation-hover)] to-transparent pointer-events-none" />
                  )}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Pipeline */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <CardTitle className="content-base flex items-center gap-2">
              <Zap className="h-4 w-4 text-accent" /> {t_i18n("Pipeline")}
            </CardTitle>
          <CardEditActions editing={pipelineEditing} canEdit={canManage} onEdit={startPipelineEdit} onCancel={cancelPipeline} onSave={savePipeline} saveDisabled={pipelineUpdate.isPending || !schemaValid || aiOff} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0 space-y-4">
          {/* Visual flow */}
          <div className="flex items-center gap-2 content-compact text-default-secondary">
            <span className="font-medium">{t_i18n("Input")}</span>
            <ArrowRight className="h-3 w-3" />
            <span className={cn("px-1.5 py-0.5 rounded-sm", pipelinePrepareName ? "bg-accent/10 text-accent font-medium" : "bg-elevation-hover")}>
              {pipelinePrepareName || t_i18n("Prepare")}
            </span>
            <ArrowRight className="h-3 w-3" />
            <span className={cn("px-1.5 py-0.5 rounded-sm", skipLlm ? "bg-elevation-hover line-through opacity-50" : "bg-filigran-ia-primary/10 text-filigran-ia-primary font-medium")}>
              LLM
            </span>
            <ArrowRight className="h-3 w-3" />
            <span className={cn("px-1.5 py-0.5 rounded-sm", pipelineFinalizeName ? "bg-feedback-alert-primary/10 text-feedback-warning-tertiary font-medium" : "bg-elevation-hover")}>
              {pipelineFinalizeName || t_i18n("Finalize")}
            </span>
            <ArrowRight className="h-3 w-3" />
            <span className="font-medium">{t_i18n("Output")}</span>
          </div>

          {pipelineEditing ? (
            <div className="space-y-4">
              {/* ── Prepare ─────────────────────────────────── */}
              <div className="rounded-sm border border-accent/20 bg-accent/[0.03] p-3 space-y-2">
                <p className="content-compact font-medium text-accent flex items-center gap-1.5">{t_i18n("Prepare")}</p>
                <SearchableToolPicker
                  label={t_i18n("Prepare tool")}
                  hint={t_i18n("Runs before the LLM. Output replaces the user message.")}
                  options={prepareOptions}
                  value={prepareRef}
                  onChange={setPrepareRef}
                />
              </div>

              {/* ── LLM ─────────────────────────────────────── */}
              <div className={cn("rounded-sm border p-3 space-y-3", skipLlm ? "border-elevation-subtle-soft bg-elevation-hover opacity-60" : "border-filigran-ia-primary/20 bg-filigran-ia-primary/[0.03]")}>
                <p className="content-compact font-medium text-filigran-ia-primary flex items-center gap-1.5">{t_i18n("LLM")}</p>
                {/* Skip LLM */}
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5 flex-1 mr-4">
                    <p className="content-base font-medium">{t_i18n("Skip LLM")}</p>
                    <p className="content-caption text-default-secondary">{t_i18n("Bypass the agentic loop entirely. The prepare tool output (or raw user message) is returned directly.")}</p>
                  </div>
                  <Switch aria-label={t_i18n("Skip LLM")} checked={skipLlm} onCheckedChange={setSkipLlm} />
                </div>
                {!skipLlm && (
                  <>
                    {/* Output Format */}
                    <div className="space-y-1.5">
                      <label className="content-compact text-default-secondary flex items-center gap-1.5">
                        <FileText className="h-3.5 w-3.5" /> {t_i18n("Output Format")}
                      </label>
                      <Select value={outputFormat} onValueChange={(v) => {
                          setOutputFormat(v);
                          if (v !== "json") {
                            setOutputSchema(null);
                            setSchemaValid(true);
                            setSchemaText("");
                          }
                        }}>
                        <SelectTrigger>
                          <SelectValue placeholder={t_i18n("Markdown")} />
                        </SelectTrigger>
                        <SelectContent>
                          {OUTPUT_FORMATS.map((fmt) => (
                            <SelectItem key={fmt} value={fmt}>
                              <span className="font-medium">{t_i18n(OUTPUT_FORMAT_CONFIG[fmt].label)}</span>
                              <span className="ml-2 text-default-secondary content-caption">— {t_i18n(OUTPUT_FORMAT_CONFIG[fmt].description)}</span>
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    {/* JSON Schema Editor */}
                    {outputFormat === "json" && (
                      <div className="space-y-1.5">
                        <label className="content-compact text-default-secondary flex items-center gap-1.5">
                          <FileText className="h-3.5 w-3.5" /> {t_i18n("Output JSON Schema")}
                          <span className="content-caption text-default-secondary">{t_i18n("(optional)")}</span>
                        </label>
                        <textarea
                          className={cn(
                            "flex min-h-[180px] w-full rounded-sm border bg-elevation-default px-3 py-2 content-compact font-mono-plex placeholder:text-default-secondary",
                            FIELD_FOCUS_RING,
                            schemaValid ? "border-elevation-subtle" : "border-feedback-error-primary/60"
                          )}
                          placeholder={`{\n  "type": "object",\n  "properties": {\n    "summary": { "type": "string" },\n    "score": { "type": "integer" }\n  },\n  "required": ["summary", "score"]\n}`}
                          value={schemaText}
                          onChange={(e) => handleSchemaChange(e.target.value)}
                        />
                        {!schemaValid && (
                          <p className="content-caption text-feedback-error-primary">{t_i18n("Invalid JSON: fix syntax errors before saving.")}</p>
                        )}
                        <p className="content-caption text-default-secondary">
                          {t_i18n("Define a JSON Schema to enforce structured output. The schema is injected into the system prompt and used for response validation. Providers with native support (OpenAI, Gemini) will use constrained decoding.")}
                        </p>
                      </div>
                    )}
                  </>
                )}
              </div>

              {/* ── Finalize ────────────────────────────────── */}
              <div className="rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/[0.03] p-3 space-y-2">
                <p className="content-compact font-medium text-feedback-warning-primary flex items-center gap-1.5">{t_i18n("Finalize")}</p>
                <SearchableToolPicker
                  label={t_i18n("Finalize tool")}
                  hint={t_i18n("Runs after the LLM. Receives the response and replaces it.")}
                  options={finalizeOptions}
                  value={finalizeRef}
                  onChange={setFinalizeRef}
                />
              </div>

              {prepareOptions.length === 0 && finalizeOptions.length === 0 && (
                <p className="content-caption text-default-secondary italic">
                  {t_i18n("No tools available. Create integrations or custom tools first.")}
                </p>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {/* ── Prepare (read-only) ──────────────────────── */}
              <div className="rounded-sm border border-accent/10 bg-elevation-hover px-3 py-2.5">
                <p className="content-caption text-accent/70 leading-none mb-1">{t_i18n("Prepare")}</p>
                <p className="content-base">
                  {agent.prepare_tool_ref?.tool_name
                    ? <>{agent.prepare_tool_ref.tool_name}{agent.prepare_tool_ref.source === "integration" && agent.prepare_tool_ref.provider && <span className="text-default-secondary content-compact ml-1.5">({INTEGRATION_PROVIDER_NAMES[agent.prepare_tool_ref.provider as keyof typeof INTEGRATION_PROVIDER_NAMES] || agent.prepare_tool_ref.provider})</span>}{agent.prepare_tool_ref.source === "custom_tool" && !customToolsEnabled && <Badge variant="outline" className="ml-2 text-[8px] text-feedback-warning-primary border-feedback-alert-primary/30">{t_i18n("Disabled")}</Badge>}</>
                    : <span className="text-default-secondary italic">{t_i18n("None")}</span>}
                </p>
              </div>

              {/* ── LLM (read-only) ──────────────────────────── */}
              <div className={cn("rounded-sm border px-3 py-2.5", agent.skip_llm ? "border-elevation-subtle-soft bg-elevation-hover opacity-60" : "border-filigran-ia-primary/10 bg-elevation-hover")}>
                <p className="content-caption text-filigran-ia-primary/70 leading-none mb-2">{t_i18n("LLM")}</p>
                <div className="flex items-center gap-2">
                  <div className={cn("h-2 w-2 rounded-full", agent.skip_llm ? "bg-feedback-alert-primary shadow-[0_0_6px_rgba(251,191,36,0.4)]" : "bg-elevation-hover")} />
                  <span className={cn("content-base font-semibold", agent.skip_llm ? "text-feedback-warning-primary" : "text-default-secondary")}>{agent.skip_llm ? t_i18n("Skip LLM: On") : t_i18n("Active")}</span>
                </div>
                {!agent.skip_llm && (
                  <div className="mt-3 space-y-2">
                    <div>
                      <p className="content-caption text-default-secondary leading-none flex items-center gap-1.5"><FileText className="h-3 w-3" /> {t_i18n("Output Format")}</p>
                      <p className="content-base mt-1">{t_i18n(OUTPUT_FORMAT_CONFIG[agent.output_format as keyof typeof OUTPUT_FORMAT_CONFIG]?.label || "Markdown")}</p>
                    </div>
                    {agent.output_format === "json" && agent.output_schema && (
                      <div>
                        <p className="content-caption text-default-secondary mb-1">{t_i18n("Schema defined")}</p>
                        <pre className="content-caption bg-elevation-hover rounded-sm p-2 overflow-auto max-h-[120px] max-w-full font-mono-plex text-default-secondary whitespace-pre-wrap break-all">
                          {JSON.stringify(agent.output_schema, null, 2)}
                        </pre>
                      </div>
                    )}
                    <SettingsSwitchField
                      id={`agent-${agent.id}-accept-caller-output-format`}
                      className="pt-1"
                      label={t_i18n("Accept a format requested by the caller")}
                      helper={t_i18n("When off, this agent always answers in its configured format; a caller passing an output schema is refused.")}
                      info={t_i18n("Read when another agent calls this one with consult_agent or spawn_background_task and passes an output schema. On: that schema replaces this agent's configured format for the call. Off: the call is refused before the agent runs. Off by default.")}
                      checked={agent.accept_caller_output_format === true}
                      onCheckedChange={onCallerOutputFormatToggle}
                      disabled={!canManage || isPending}
                    />
                  </div>
                )}
              </div>

              {/* ── Finalize (read-only) ─────────────────────── */}
              <div className="rounded-sm border border-feedback-alert-primary/10 bg-elevation-hover px-3 py-2.5">
                <p className="content-caption text-feedback-warning-primary leading-none mb-1">{t_i18n("Finalize")}</p>
                <p className="content-base">
                  {agent.finalize_tool_ref?.tool_name
                    ? <>{agent.finalize_tool_ref.tool_name}{agent.finalize_tool_ref.source === "integration" && agent.finalize_tool_ref.provider && <span className="text-default-secondary content-compact ml-1.5">({INTEGRATION_PROVIDER_NAMES[agent.finalize_tool_ref.provider as keyof typeof INTEGRATION_PROVIDER_NAMES] || agent.finalize_tool_ref.provider})</span>}{agent.finalize_tool_ref.source === "custom_tool" && !customToolsEnabled && <Badge variant="outline" className="ml-2 text-[8px] text-feedback-warning-primary border-feedback-alert-primary/30">{t_i18n("Disabled")}</Badge>}</>
                    : <span className="text-default-secondary italic">{t_i18n("None")}</span>}
                </p>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <ArrowLeftRight className="h-4 w-4 text-accent" /> {t_i18n("Agent Handover")}
            {(agent.handover_agent_ids || []).length > 0 && !editingHandover && (
              <CountBadge variant="secondary">{(agent.handover_agent_ids || []).length}</CountBadge>
            )}
          </CardTitle>
          <CardEditActions editing={editingHandover} canEdit={canManage} onEdit={onStartEditHandover} onCancel={onCancelHandover} onSave={onSaveHandover} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingHandover ? (
            <div>
              <p className="content-caption text-default-secondary mb-3">{t_i18n("Select agents and define the handover type for each.")}</p>
              {(allAgents ?? []).filter((a) => a.id !== agent.id && a.enabled && (!companyManaged || a.is_company_managed || a.user_id === currentUserId)).length > 3 && (
                <SearchInput placeholder={t_i18n("Filter agents...")} value={handoverSearch} onChange={setHandoverSearch} className="mb-3 max-w-full" />
              )}
              {handoverCandidates.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-3">
                  {handoverCandidates.map((ha) => {
                    const HaIcon = ICON_MAP[ha.icon] ?? Bot;
                    const isSelected = handoverIds.includes(ha.id);
                    const currentType = handoverLegacy ? ALL_HANDOVER_TYPES : (handoverTypes[ha.id] || "delegate");
                    const typeCfg = handoverTypeConfig(currentType);
                    // A company-managed agent can only hand over to other
                    // company-managed agents. The current user's own private
                    // agents are surfaced here so they can be promoted inline;
                    // until promoted they are NOT selectable.
                    const needsPromotion = companyManaged && !ha.is_company_managed;
                    if (needsPromotion) {
                      return (
                        <div
                          key={ha.id}
                          className="min-w-0 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.04] p-3 space-y-2"
                        >
                          <div className="flex items-center gap-3">
                            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
                              <HaIcon className="h-4 w-4 text-default-secondary" />
                            </div>
                            <div className="min-w-0 flex-1">
                              <TruncatedText text={ha.name} className="content-base font-medium" />
                              {ha.slug && <TruncatedText text={`@${ha.slug}`} className="content-caption font-mono-plex text-default-secondary" />}
                              {ha.description && <TruncatedText text={ha.description} className="content-caption text-default-secondary" />}
                            </div>
                            <Tip content={t_i18n("This agent is not company-managed and cannot be used in handover flows until promoted.")}>
                              <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0" />
                            </Tip>
                          </div>
                          <HandoverPromoteButton agentId={ha.id} agentName={ha.name} isAdmin={isAdmin} />
                        </div>
                      );
                    }
                    return (
                      <div
                        key={ha.id}
                        className={cn(
                          "min-w-0 rounded-sm border transition-all",
                          isSelected ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                        )}
                      >
                        <button
                          type="button"
                          className={cn("flex items-center gap-3 p-3 text-left w-full rounded-sm", BUTTON_ROLE_FOCUS_RING)}
                          onClick={() => onToggleHandover(ha.id)}
                        >
                          <div className={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-sm", isSelected ? "bg-accent/20" : "bg-elevation-hover")}>
                            <HaIcon className={cn("h-4 w-4", isSelected ? "text-accent" : "text-default-secondary")} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <TruncatedText text={ha.name} className={cn("content-base font-medium", isSelected && "text-accent")} />
                            {ha.slug && <TruncatedText text={`@${ha.slug}`} className="content-caption font-mono-plex text-default-secondary" />}
                            {ha.description && <TruncatedText text={ha.description} className="content-caption text-default-secondary" />}
                          </div>
                          {isSelected ? (
                            <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                          ) : (
                            <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                          )}
                        </button>
                        {isSelected && (() => {
                          const mandatoryCount = handoverIds.filter(id => (handoverTypes[id] || "delegate") === "mandatory_transfer").length;
                          const conditionRequired = currentType === "mandatory_transfer" && mandatoryCount > 1;
                          const currentCondition = handoverConditions[ha.id] || "";
                          const isDuplicateCondition = currentType === "mandatory_transfer" && currentCondition.trim() !== "" && handoverIds.some(
                            otherId => otherId !== ha.id
                              && (handoverTypes[otherId] || "delegate") === "mandatory_transfer"
                              && (handoverConditions[otherId] || "").trim() === currentCondition.trim(),
                          );
                          return (
                          <div className="px-3 pb-3 pt-0 space-y-2">
                            <Select
                              value={currentType}
                              onValueChange={(v) => {
                                if (v !== ALL_HANDOVER_TYPES) onUpdateHandoverType(ha.id, v);
                              }}
                            >
                              <Tip content={consultsBackgroundDelegator(currentType, ha) ? t_i18n("{name} delegates in the background: a consult may answer before its delegated tasks finish.", { name: ha.name }) : undefined}>
                                <SelectTrigger className="w-full" aria-label={t_i18n("Handover type")}>
                                  {/* The type label alone: its description is printed below (as GAHandoverPicker, #10876). */}
                                  <SelectValue>
                                    {currentType === ALL_HANDOVER_TYPES ? (
                                      <span className={cn("font-medium", ALL_HANDOVER_TYPES_CONFIG.color)}>{t_i18n(ALL_HANDOVER_TYPES_CONFIG.label)}</span>
                                    ) : (
                                      <span className={cn("font-medium", HANDOVER_TYPE_CONFIG[currentType]?.color)}>{t_i18n(HANDOVER_TYPE_CONFIG[currentType]?.label ?? currentType)}</span>
                                    )}
                                  </SelectValue>
                                </SelectTrigger>
                              </Tip>
                              <SelectContent>
                                {handoverLegacy && (
                                  <SelectItem value={ALL_HANDOVER_TYPES}>
                                    <span className={cn("font-medium", ALL_HANDOVER_TYPES_CONFIG.color)}>{t_i18n(ALL_HANDOVER_TYPES_CONFIG.label)}</span>
                                    <span className="ml-1.5 text-default-secondary">{t_i18n("Delegate, consult or transfer")}</span>
                                  </SelectItem>
                                )}
                                {HANDOVER_TYPES.map((t) => (
                                  <SelectItem key={t} value={t}>
                                    <span className={cn("font-medium", HANDOVER_TYPE_CONFIG[t]?.color)}>{t_i18n(HANDOVER_TYPE_CONFIG[t]?.label ?? t)}</span>
                                    <span className="ml-1.5 text-default-secondary">{t_i18n(HANDOVER_TYPE_CONFIG[t]?.description ?? "").split(".")[0]}</span>
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            <p className="text-[9px] text-default-secondary">{t_i18n(typeCfg.description)}</p>
                            {currentType === "mandatory_transfer" && (
                              <div className="mt-1">
                                <label className="content-caption text-default-secondary block mb-1">
                                  {t_i18n("Routing condition")}{" "}
                                  {conditionRequired
                                    ? <span className="text-feedback-error-primary">{t_i18n("(required: multiple mandatory transfers need distinct conditions)")}</span>
                                    : <span className="text-default-secondary">{t_i18n("(optional: leave empty for fallback)")}</span>}
                                </label>
                                <textarea
                                  className={cn(
                                    "w-full rounded-sm border bg-elevation-hover px-2.5 py-1.5 text-[11px] text-default-primary placeholder:text-default-disabled resize-none",
                                    FIELD_FOCUS_RING,
                                    (conditionRequired && !currentCondition.trim()) || isDuplicateCondition
                                      ? "border-feedback-error-primary/50 focus:border-feedback-error-primary/70"
                                      : "border-elevation-subtle-soft",
                                  )}
                                  rows={2}
                                  placeholder={t_i18n('e.g. "The user is asking about billing, payments, or invoicing"')}
                                  value={handoverConditions[ha.id] || ""}
                                  onChange={(e) => onUpdateHandoverCondition(ha.id, e.target.value)}
                                />
                                {isDuplicateCondition && (
                                  <p className="content-caption text-feedback-error-primary mt-0.5">{t_i18n("Duplicate condition: each mandatory transfer must have a unique condition.")}</p>
                                )}
                                {conditionRequired && !currentCondition.trim() && (
                                  <p className="content-caption text-feedback-error-primary mt-0.5">{t_i18n("A routing condition is required when multiple mandatory transfers are configured.")}</p>
                                )}
                              </div>
                            )}
                          </div>
                          );
                        })()}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="py-4 text-center">
                  <ArrowLeftRight className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                  <p className="content-base text-default-secondary">{t_i18n("No other agents available")}</p>
                  <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create more agents to enable handover between them.")}</p>
                </div>
              )}
              {handoverIds.length > 0 && (
                <>
                  <div className="mt-3 rounded-sm bg-elevation-hover p-4 space-y-2">
                    <p className="content-compact font-medium text-default-primary">{t_i18n("Handover types")}</p>
                    <p className="content-caption text-default-secondary">{t_i18n("The system prompt for handover is automatically generated based on the type configured for each agent above.")}</p>
                    <ul className="text-[11px] text-default-secondary space-y-1.5 list-disc pl-4">
                      <li>{intl.formatMessage(
                        { id: "{label}: Spawns an asynchronous background task on the target agent. Use for long-running work." },
                        { label: <span className="text-accent font-medium">{t_i18n("Delegate")}</span> },
                      )}</li>
                      <li>{intl.formatMessage(
                        { id: "{label}: Asks the target agent a question and receives the answer synchronously. The conversation stays with this agent." },
                        { label: <span className="text-feedback-success-primary font-medium">{t_i18n("Consult")}</span> },
                      )}</li>
                      <li>{intl.formatMessage(
                        { id: "{label}: Permanently hands the conversation to the target agent. Chat only." },
                        { label: <span className="text-filigran-ia-primary font-medium">{t_i18n("Transfer")}</span> },
                      )}</li>
                      <li>{intl.formatMessage(
                        { id: "{label}: Automatically transfers after the agent completes its response. Supports multiple targets with natural language conditions for routing." },
                        { label: <span className="text-feedback-warning-primary font-medium">{t_i18n("Mandatory transfer")}</span> },
                      )}</li>
                    </ul>
                  </div>
                  {Object.values(handoverTypes).filter(t => t === "mandatory_transfer").length > 1 && (
                    <div className="mt-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3 flex items-start gap-2">
                      <AlertTriangle className="h-4 w-4 text-feedback-warning-primary mt-0.5 shrink-0" />
                      <div>
                        <p className="content-caption font-medium text-feedback-warning-primary">{t_i18n("Multiple mandatory transfers configured")}</p>
                        <p className="content-caption text-default-secondary">{t_i18n("Each mandatory target must have a unique routing condition so the routing model can pick the right target. At most one target may omit the condition to act as a fallback.")}</p>
                      </div>
                    </div>
                  )}
                  {Object.values(handoverTypes).filter(t => t === "mandatory_transfer").length === 1 && (
                    <div className="mt-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3 flex items-start gap-2">
                      <AlertTriangle className="h-4 w-4 text-feedback-warning-primary mt-0.5 shrink-0" />
                      <div>
                        <p className="content-caption font-medium text-feedback-warning-primary">{t_i18n("Mandatory transfer is active")}</p>
                        <p className="content-caption text-default-secondary">{t_i18n("The agent will automatically transfer to the mandatory target after each response. The agent cannot see or control this transfer.")}</p>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          ) : (
            <div>
              {(agent.handover_agent_ids || []).length > 0 ? (
                <div className="space-y-1.5">
                  {(agent.handover_agent_ids || [])
                    .map((haId) => ({ haId, ha: allAgents?.find((a) => a.id === haId) }))
                    .filter((x): x is { haId: string; ha: NonNullable<typeof x.ha> } => !!x.ha)
                    .sort((a, b) => a.ha.name.localeCompare(b.ha.name))
                    .map(({ haId, ha }) => {
                    const HaIcon = ICON_MAP[ha.icon] ?? Bot;
                    const hType = shownHandoverType(agent.handover_types, haId, HANDOVER_TYPES);
                    const hCfg = handoverTypeConfig(hType);
                    const hCondition = hType === "mandatory_transfer" ? (agent.handover_conditions || {})[haId] : undefined;
                    return (
                      <div key={haId} className="rounded-sm bg-elevation-hover px-3 py-2.5">
                        <div className="flex items-center gap-3">
                          <HaIcon className="h-4 w-4 text-accent shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="content-base font-medium">{ha.name}</p>
                            {ha.slug && <p className="content-caption text-default-secondary font-mono-plex">@{ha.slug}</p>}
                          </div>
                          <Tip content={consultsBackgroundDelegator(hType, ha) ? t_i18n("{name} delegates in the background: a consult may answer before its delegated tasks finish.", { name: ha.name }) : undefined}>
                            <Badge variant="outline" className={cn("text-[9px] shrink-0", hCfg.color)}>{t_i18n(hCfg.label)}</Badge>
                          </Tip>
                        </div>
                        {hCondition && (
                          <div className="mt-2 ml-7 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/[0.04] px-2.5 py-1.5">
                            <p className="content-caption text-feedback-warning-primary font-medium mb-0.5">{t_i18n("Routing condition")}</p>
                            <p className="text-[11px] text-default-secondary leading-relaxed">{hCondition}</p>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No handover agents configured")}</p>
              )}
            </div>
          )}

          {/* Self-fan-out */}
          <div className="mt-4 border-t border-elevation-subtle-soft pt-4 flex items-start justify-between gap-4">
            <div className="space-y-1">
              <p className="content-base font-medium flex items-center gap-1.5">
                <Copy className="h-3.5 w-3.5 text-accent" /> {t_i18n("Parallel copies of itself")}
              </p>
              <p className="content-caption text-default-secondary">
                {t_i18n("For a large workload, the agent can split the work across background copies of itself that run in parallel. Turn it off to keep it from spawning copies of itself; delegating to the agents above still works.")}
              </p>
            </div>
            {canManage ? (
              <Switch
                checked={agent.allow_self_fanout !== false}
                onCheckedChange={onSelfFanoutToggle}
                disabled={isPending}
                aria-label={t_i18n("Parallel copies of itself")}
              />
            ) : (
              <Badge variant="secondary" className="text-[9px] shrink-0">
                {agent.allow_self_fanout !== false ? t_i18n("On") : t_i18n("Off")}
              </Badge>
            )}
          </div>
        </CardContent>
      </Card>



      {/* Guardrails */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <div>
            <CardTitle className="content-base flex items-center gap-2"><ShieldAlert className="h-4 w-4 text-accent" /> {t_i18n("Guardrails")}</CardTitle>
            <p className="content-caption text-default-secondary mt-1">{t_i18n("Safety constraints injected into the system prompt, separate from the persona.")}</p>
          </div>
          <CardEditActions editing={editingGuardrails} canEdit={canManage} onEdit={onStartEditGuardrails} onCancel={onCancelGuardrails} onSave={onSaveGuardrails} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingGuardrails ? (
            <div className="space-y-3">
              <div className="flex items-center justify-end gap-1">
                <VariableInsertButton textareaRef={guardrailsRef} value={guardrails} onChange={setGuardrails} />
                <CopyIconButton value={guardrails} label={t_i18n("Copy guardrail")} />
              </div>
              <textarea
                ref={guardrailsRef}
                className={`flex min-h-[100px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                placeholder={t_i18n("Examples:\n- Never share personal data or internal credentials\n- Never execute destructive actions (delete, drop) without explicit approval\n- Always cite sources when referencing knowledge base content\n- Refuse to generate harmful, misleading, or discriminatory content")}
                value={guardrails}
                onChange={(e) => setGuardrails(e.target.value)}
              />
              <div className="rounded-sm bg-elevation-hover p-3 space-y-1.5">
                <p className="content-caption font-medium text-default-primary">{t_i18n("How it works")}</p>
                <ul className="content-caption text-default-secondary space-y-1 list-disc pl-4">
                  <li>{t_i18n("Guardrails are injected as a mandatory section in the system prompt, after the persona.")}</li>
                  <li>{intl.formatMessage(
                    { id: "They apply to {all} assignments and chat conversations for this agent." },
                    { all: <span className="text-default-primary font-medium">{t_i18n("all")}</span> },
                  )}</li>
                  <li>{t_i18n("Unlike the persona, guardrails are framed as hard constraints the LLM must always respect.")}</li>
                </ul>
              </div>
            </div>
          ) : (
            <div>
              {agent.guardrails ? (
                <div className="relative rounded-sm bg-elevation-hover px-4 py-3">
                  <div className="absolute right-2 top-2">
                    <CopyIconButton value={agent.guardrails} label={t_i18n("Copy guardrail")} />
                  </div>
                  <p className="content-base whitespace-pre-wrap line-clamp-4 pr-8">{agent.guardrails}</p>
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No guardrails configured")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Owner transfer + Group sharing (any user who can manage) + Company-managed toggle (admin only) */}
      {(canManage || isAdmin) && (
        <Card>
          <CardContent className="p-4 space-y-3">
            {/* Owner transfer — admin only, and only when the admin can
                actually manage this agent (canManage is true for admins on
                company-managed agents, so the control still shows for them per
                the always-set-owner invariant). Builtin agents are
                excluded (their owner is resolved by the startup seed). */}
            {isAdmin && canManage && !agent.is_builtin && (
              <OwnerCard
                userId={agent.user_id}
                ownerName={agent.owner_name}
                onTransfer={onSaveOwner}
                disabled={isPending}
                entityLabel="agent"
              />
            )}
            {/* Sharing — visible to any user who can manage. Rendered for
                company-managed agents too: there the picker names the
                audience an access restriction narrows the agent down to.
                Read-only where the viewer cannot manage the agent (a
                built-in in XTM One mode, administrators included): the
                server answers 403 to the write (#2203). */}
            <PrincipalSharingCard
              sharedGroupIds={sharedGroupIds}
              onChange={onSaveSharedGroups}
              sharedGroupPermissions={sharedGroupPermissions}
              onPermissionsChange={onSaveSharedGroupPermissions}
              userSharing={userSharing}
              entityType="agent"
              isCompanyManaged={companyManaged}
              disabled={isPending || !canManage}
            />
            {!companyManaged && (
              <PersonalIntegrationsBanner
                agent={agent}
                integrations={integrations}
                sharedGroupIds={sharedGroupIds}
                currentUserId={currentUserId}
                disabled={isPending}
                onLend={onLendIntegrations}
                onMakeBindingsPerUser={onMakeBindingsPerUser}
              />
            )}
            <OrphanedIntegrationsNotice agent={agent} canManage={canManage} />
            {/* Company-managed toggle — admin only */}
            {isAdmin && (
              <CompanyManagedSwitch
                label={t_i18n("Company-managed agent")}
                description={
                  agent.is_builtin
                    ? t_i18n("Built-in agents are always company-managed. This cannot be changed.")
                    : companyManaged
                      ? t_i18n("Shared with all users. Only admins can edit. Toggling off will make it private and remove access for other users.")
                      : t_i18n("Private to you. Toggle on to share with all users.")
                }
                checked={companyManaged}
                onCheckedChange={onSaveCompanyManaged}
                disabled={isPending || agent.is_builtin}
              />
            )}
          </CardContent>
        </Card>
      )}

      {/* Public catalog toggle */}
      {catalogEnabled && canManage && (
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Globe className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Include in AI Catalog")}</p>
                  <p className="text-[11px] text-default-secondary">
                    {agent.is_public_catalog
                      ? t_i18n("This agent is visible in the AI Catalog.")
                      : t_i18n("Publish this agent to the AI Catalog.")}
                  </p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Include in AI Catalog")}
                checked={agent.is_public_catalog ?? false}
                onCheckedChange={onPublicCatalogToggle}
                disabled={isPending}
              />
            </div>
            <div className="mt-2 flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Code className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">
                    {t_i18n("Include the code of my personal custom tools in the catalog download")}
                  </p>
                  <p className="text-[11px] text-default-secondary">
                    {agent.catalog_include_personal_tools
                      ? t_i18n("The downloaded file carries the code of the custom tools bound to this agent that are not company-managed.")
                      : t_i18n("The downloaded file leaves out the custom tools bound to this agent that are not company-managed, and says which. Company-managed tools are always included.")}
                  </p>
                </div>
              </div>
              <Switch
                checked={agent.catalog_include_personal_tools ?? false}
                onCheckedChange={onCatalogPersonalToolsToggle}
                disabled={isPending}
                aria-label={t_i18n("Include the code of my personal custom tools in the catalog download")}
              />
            </div>
          </CardContent>
        </Card>
      )}

    </>
  );
}

