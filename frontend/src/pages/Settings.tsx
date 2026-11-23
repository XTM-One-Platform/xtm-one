// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useId, useRef, useState, useMemo } from "react";
import { SettingsField, SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar, settingsFieldHintId } from "@/components/ui/settings-form";
import { useAgenticAIEnabled, useIsXtmOne } from "@/hooks/usePlatformConfig";
import { CtemStageAgentsCard } from "@/pages/xtm-one/ctem/CtemStageAgentsCard";
import {
  ArrowLeftRight,
  Bot,
  Building2,
  Key,
  Plus,
  Check,
  Pencil,
  Shield,
  Sparkles,
  Plug2,
  BarChart3,
  Radio,
  Image,
  Server,
  SlidersHorizontal,
  BrainCircuit,
  ClipboardCheck,
  Gauge,
  HardDrive,
  Info,
  LifeBuoy,
  MousePointerClick,
  Upload,
  Coins,
  LayoutGrid,
  Ruler,
  Timer,
  Users,
  Workflow,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SecretField } from "@/components/ui/secret-field";
import { ModelListRefreshing, ModelSelect } from "@/components/ui/model-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { MusicGenerationSection } from "@/pages/settings/MusicGenerationSection";
import { emptyChatProviders } from "@/pages/settings/emptyChatProviders";
import { PronunciationLexiconSection } from "@/pages/settings/PronunciationLexiconSection";
import { HealthSection } from "@/pages/settings/HealthSection";
import { RenderRetentionSection } from "@/pages/settings/RenderRetentionSection";
import { SpeechGenerationSection } from "@/pages/settings/SpeechGenerationSection";
import { VideoGenerationSection } from "@/pages/settings/VideoGenerationSection";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import {
  useLLMProviders, useCreateLLMProvider, useUpdateLLMProvider,
  useGeneralAssistantConfig,
  useUpdateGeneralAssistantConfig, useAvailableModels, useAvailableEmbeddingModels, useAvailableDecisionModels,
  useAvailableImageModels, useTestEmbeddingModel, useValidateProvider,
  useModelAnnouncementStatus,
} from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import {
  PROVIDER_DISPLAY_NAMES, REASONING_EFFORT_LEVELS, REASONING_EFFORT_CONFIG,
  SINGLE_CAPABILITY_PROVIDERS,
} from "@/lib/constants";
import { cn, isFeatureEnabled, modelChoiceValue, modelOptionLabel, parseModelChoice, providerDisplayName, toggleArrayItem, typedDecisionProviders } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useNavigate, useParams } from "react-router-dom";
import { SectionNav, type SectionNavItem } from "@/components/ui/section-nav";
import RouteNotFound from "@/pages/RouteNotFound";
import {
  automationsSection,
  DEFAULT_SETTINGS_TAB,
  isSettingsTab,
  pickSection,
  SETTINGS_TABS,
  settingsSectionPath,
  type AiSection,
  type AutomationsSection,
  type IntegrationsSettingsSection,
  type OrganizationSubsection,
  type SettingsTab,
  type UsageSection,
} from "./settings/settings-routes";
import { FleetsSettingsSection } from "./settings/FleetsSettingsSection";
import { IntegrationCatalogSection } from "./settings/IntegrationCatalogSection";
import { OAuthDefaultsSection } from "./settings/OAuthDefaultsSection";
import type { AvailableModel, LLMProvider, ModelCatalogChange } from "@/types";

import { usePlatformSettings } from "@/hooks/api/useAiAdoptionApi";
import { AgentRunsSettings } from "@/pages/settings/AgentRunsSettings";
import { ProviderCard } from "./settings/ProviderCard";
import { buildGatewayCredential } from "./settings/gateway-credential";
import { MAX_SERVICE_ACCOUNT_KEY_BYTES, serviceAccountSummary, vertexAiCredential } from "./settings/vertex-ai-credential";
import { IntegrationFedProviderHint } from "./settings/IntegrationFedProviderHint";
import { addableByHand } from "@/lib/integration-fed-providers";
import { canEmailUsers } from "@/lib/model-announcements";
import { DeleteProviderDialog } from "./settings/DeleteProviderDialog";
import { ChannelManagement } from "./settings/ChannelManagement";
import { SecuritySection } from "./settings/SecuritySection";
import { ObservabilitySection } from "./settings/ObservabilitySection";
import { WorkspaceKnowledgeSettingsCard } from "./settings/WorkspaceKnowledgeSettingsCard";
import { FeedbackCriteriaSection } from "./settings/FeedbackCriteriaSection";
import { ModelPricingSection } from "./settings/ModelPricingSection";
import { DepartmentsSection } from "./settings/DepartmentsSection";
import { RoiRatesSection } from "./settings/RoiRatesSection";
import { RoiUnitsSection } from "./settings/RoiUnitsSection";
import { RoiPrivacyCard } from "./settings/RoiPrivacyCard";
import { OrganizationSection } from "./settings/OrganizationSection";
import { PlatformModelSetting } from "./settings/PlatformModelSetting";
import { GeneralAssistantSection } from "./settings/GeneralAssistantSection";
import { MCPEndpointSection } from "./settings/MCPEndpointSection";
import { ParametersSection } from "./settings/ParametersSection";
import { FileRetentionSection } from "./settings/FileRetentionSection";
import { DefaultModelAnnouncementDialog, ModelAnnouncementPanel, ModelChangesDialog } from "./settings/ModelAnnouncements";
import { AiReadOnly } from "@/components/ui/ai-disabled";
import { EnterpriseUpsellNotice } from "@/components/ui/enterprise-upsell";
import QuotasSection from "./settings/QuotasSection";
import { decisionListingNotes } from "./settings/decision-listing";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useEmailingToast } from "@/hooks/useEmailingToast";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Tab definition
// ---------------------------------------------------------------------------

const ORGANIZATION_SECTIONS: readonly SectionNavItem<OrganizationSubsection>[] = [
  { key: "profile", label: "Profile", icon: Building2 },
  { key: "departments", label: "Departments", icon: Users },
  { key: "units", label: "Units of work", icon: Ruler },
  { key: "rates", label: "Rates by role", icon: Coins },
];
const AUTOMATIONS_SECTIONS: readonly SectionNavItem<AutomationsSection>[] = [
  { key: "fleets", label: "Fleets", icon: Workflow },
  { key: "ctem-agents", label: "CTEM stage agents", icon: Sparkles },
  { key: "agent-runs", label: "Agent runs", icon: Timer },
  { key: "quality", label: "Quality", icon: ClipboardCheck },
  { key: "health", label: "Platform Health", icon: Sparkles },
];
const INTEGRATIONS_SECTIONS: readonly SectionNavItem<IntegrationsSettingsSection>[] = [
  { key: "catalog", label: "Catalog", icon: LayoutGrid },
  { key: "oauth", label: "OAuth Defaults", icon: Key },
  { key: "channels", label: "Channels & Bots", icon: Radio },
  { key: "mcp", label: "MCP Endpoint", icon: Server },
];
const AI_SECTIONS: readonly SectionNavItem<AiSection>[] = [
  { key: "models", label: "Models", icon: Bot },
  { key: "media", label: "Media", icon: Image },
  { key: "assistant", label: "General Assistant", icon: Sparkles },
  { key: "knowledge", label: "Workspace knowledge", icon: BrainCircuit },
];
const USAGE_SECTIONS: readonly SectionNavItem<UsageSection>[] = [
  { key: "quotas", label: "Quotas", icon: Gauge },
  { key: "observability", label: "Observability", icon: BarChart3 },
  { key: "pricing", label: "Model pricing", icon: Coins },
  { key: "storage", label: "Storage & retention", icon: HardDrive },
];
type AddProviderMode = "reasoning" | "embedding" | "image" | "all";

/**
 * `/settings/:tab?/:section?`: a segment that is not a section is not found,
 * a former tab route included (#797: no redirect).
 */
export default function Settings() {
  const { tab } = useParams<{ tab?: string }>();
  if (!isSettingsTab(tab)) return <RouteNotFound />;
  return <SettingsPage />;
}

function SettingsPage() {
  const aiOff = useAiActionDisabled();
  const { isAdmin } = useAuth();
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const isXtmOne = useIsXtmOne();
  // Whether a configured provider can actually be USED here: the AI Models
  // copy stops promising that adding one unblocks anything on Community
  // Edition, where nothing runs until Enterprise Edition is active.
  const aiLicensed = useAgenticAIEnabled();
  const { tab: rawTab, section: rawSection } = useParams<{ tab?: string; section?: string }>();
  const navigate = useNavigate();
  const tab: SettingsTab = (SETTINGS_TABS as readonly string[]).includes(rawTab ?? "") ? (rawTab as SettingsTab) : DEFAULT_SETTINGS_TAB;

  const { data: authConfig } = useAuthConfig();
  // The video and music models serve the Video Studio alone. Speech
  // stays: the chat reads answers aloud with it.
  const videoStudioEnabled = isFeatureEnabled(authConfig?.enabled_features, "VIDEO_STUDIO");
  // The sub-section of each section, from the URL. An unknown sub-section
  // lands on the section's default, never on a blank page (UX C3).
  const organizationSection = pickSection<OrganizationSubsection>(rawSection, ["profile", "departments", "units", "rates"]);
  const automations = automationsSection(rawSection, { isXtmOne });
  const integrationsSection = pickSection<IntegrationsSettingsSection>(rawSection, ["catalog", "oauth", "channels", "mcp"]);
  const aiSection = pickSection<AiSection>(rawSection, aiLicensed ? ["models", "media", "assistant", "knowledge"] : ["models", "media", "assistant"]);
  const usageSection = pickSection<UsageSection>(rawSection, ["quotas", "observability", "pricing", "storage"]);
  const firstSection: Record<SettingsTab, string> = {
    organization: "profile",
    automations: isXtmOne ? "fleets" : "agent-runs",
    integrations: "catalog",
    ai: "models",
    security: "audit",
    usage: "quotas",
    platform: "",
  };
  const selectSection = (next: string) => {
    navigate(settingsSectionPath(tab, next, firstSection[tab]), { replace: true });
  };
  const { data: providers } = useLLMProviders();
  const { data: platformSettings } = usePlatformSettings();
  const modelsEnvProvidingOnly = platformSettings?.models_env_providing_only ?? false;
  const canAddProviders = isAdmin && !modelsEnvProvidingOnly;
  const canManageProviders = isAdmin && !modelsEnvProvidingOnly;
  const addProvider = useCreateLLMProvider();
  const updateProvider = useUpdateLLMProvider();
  // The provider whose deletion is being confirmed.
  const [providerToDelete, setProviderToDelete] = useState<LLMProvider | null>(null);

  // LLM form state
  const validateProvider = useValidateProvider();
  const [addProviderMode, setAddProviderMode] = useState<AddProviderMode | null>(null);
  // Bumped when the add-provider dialog closes and when a new validation starts,
  // so a reply from an abandoned attempt cannot land in the dialog that replaced
  // it — otherwise cancelling mid-validation leaks the old provider's model list
  // and its "validated" flag into the next provider you try to add.
  const validateAttemptRef = useRef(0);
  // ``closeAddProvider`` has to reset the validation mutation, but it also sits
  // in an effect's dependency array below and ``useMutation`` returns a fresh
  // object every render. Hold it in a ref so the callback stays stable.
  const validateProviderRef = useRef(validateProvider);
  validateProviderRef.current = validateProvider;
  const addProviderFormRef = useRef<HTMLDivElement | null>(null);
  const addProviderApiKeyRef = useRef<HTMLInputElement | null>(null);
  const [newProvider, setNewProvider] = useState<string>("anthropic");
  const [newApiKey, setNewApiKey] = useState("");
  const [noApiKeyRequired, setNoApiKeyRequired] = useState(false);
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [gatewayToken, setGatewayToken] = useState("");
  // The Vertex AI region of a Google Vertex AI provider; empty: the backend's default (us-central1).
  const [vertexLocation, setVertexLocation] = useState("");
  const serviceAccountKeyFileRef = useRef<HTMLInputElement | null>(null);
  const [newModel, setNewModel] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [resolvedModels, setResolvedModels] = useState<string[]>([]);
  // Models grouped by upstream provider (the gateway sets this); drives the
  // grouped picker so the user sees which models are enabled per provider.
  const [modelGroups, setModelGroups] = useState<Record<string, string[]> | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [providerValidationMessage, setProviderValidationMessage] = useState<string | null>(null);
  const [providerValidated, setProviderValidated] = useState(false);
  const [editingModelsFor, setEditingModelsFor] = useState<string | null>(null);
  const [newDisplayName, setNewDisplayName] = useState("");

  // General Assistant config
  const { data: gaConfig } = useGeneralAssistantConfig();
  const updateGAConfig = useUpdateGeneralAssistantConfig();
  const emailingToast = useEmailingToast();
  const { data: announcementStatus, refetch: refetchAnnouncementStatus } = useModelAnnouncementStatus(isAdmin);
  // Chat models a provider save added or retired, offered to the users by email.
  const [catalogChange, setCatalogChange] = useState<ModelCatalogChange | null>(null);
  // The editor checks the models against the ones users can pick: it opens on the status after the save.
  const offerCatalogChange = async (change: ModelCatalogChange | null | undefined) => {
    if (!isAdmin || !change || !canEmailUsers(announcementStatus)) return;
    await refetchAnnouncementStatus();
    setCatalogChange(change);
  };
  // A delete answers 204: the change it recorded is the status's newest untold one.
  const offerCatalogChangeAfterDelete = async () => {
    if (!isAdmin || !canEmailUsers(announcementStatus)) return;
    const before = announcementStatus?.unannounced_change?.id ?? null;
    const { data } = await refetchAnnouncementStatus();
    if (data?.unannounced_change && data.unannounced_change.id !== before) setCatalogChange(data.unannounced_change);
  };
  // A new default model the admin is saving, held while they choose whether
  // the users are emailed about it.
  const [defaultModelChange, setDefaultModelChange] = useState<{
    payload: { agent_default_model: string; agent_default_provider: string | null; agent_default_provider_id: string | null };
    fromEditor: boolean;
  } | null>(null);
  const { data: availableModels } = useAvailableModels({ scope: "all" });
  const {
    data: availableEmbeddingModels,
    isFetching: refreshingEmbeddingModels,
    refetch: refetchEmbeddingModels,
  } = useAvailableEmbeddingModels();
  const {
    data: decisionListing,
    isPending: decisionModelsPending,
    isFetching: refreshingDecisionModels,
    refetch: refetchDecisionModels,
  } = useAvailableDecisionModels();
  const decisionModels = decisionListing?.models ?? [];
  const {
    data: availableImageModels,
    isFetching: refreshingImageModels,
    refetch: refetchImageModels,
  } = useAvailableImageModels();
  const testEmbeddingModel = useTestEmbeddingModel();
  const { data: gaIntegrations } = useIntegrations();
  const { data: gaMcpServers } = useMCPServers();
  const { data: gaKbs } = useBindableKnowledgeBases();
  const { data: gaCustomTools } = useCustomTools();
  const { data: gaAllAgents } = useAgents();
  const [gaPrompt, setGaPrompt] = useState("");
  const [gaModel, setGaModel] = useState("");
  const [gaMaxIterations, setGaMaxIterations] = useState(10);
  const [gaEditing, setGaEditing] = useState(false);
  // GA resource bindings state
  const [gaKbIds, setGaKbIds] = useState<string[]>([]);
  const [gaMcpIds, setGaMcpIds] = useState<string[]>([]);
  const [gaToolIds, setGaToolIds] = useState<string[]>([]);
  const [gaBuiltinToolIds, setGaBuiltinToolIds] = useState<string[]>([]);
  const [gaBindings, setGaBindings] = useState<Record<string, string[]>>({});
  const [gaHandoverIds, setGaHandoverIds] = useState<string[]>([]);
  const [gaHandoverTypes, setGaHandoverTypes] = useState<Record<string, string>>({});

  const reasoningModels = useMemo(() => {
    if (!availableModels) return [];
    const embeddingModelIds = new Set(availableEmbeddingModels?.map(m => m.model) || []);
    const imageModelIds = new Set(availableImageModels?.map(m => m.model) || []);
    return availableModels.filter((m: AvailableModel) => !embeddingModelIds.has(m.model) && !imageModelIds.has(m.model));
  }, [availableModels, availableEmbeddingModels, availableImageModels]);

  // Embedding model editing state
  const [embeddingEditing, setEmbeddingEditing] = useState(false);
  const [embeddingModelDraft, setEmbeddingModelDraft] = useState("");
  const [embeddingDimensionsDraft, setEmbeddingDimensionsDraft] = useState<string>("");
  const [embeddingTestResult, setEmbeddingTestResult] = useState<{
    capable: boolean;
    message: string;
    returnedDimensions: number | null;
    requestedDimensions: number | null;
    suggestedDimensions: number[];
    expectedDimensions: number | null;
  } | null>(null);

  // Image model editing state
  const [imageEditing, setImageEditing] = useState(false);
  const [imageModelDraft, setImageModelDraft] = useState("");

  // Reasoning effort editing state
  const [reasoningEditing, setReasoningEditing] = useState(false);
  const [reasoningDraft, setReasoningDraft] = useState("");

  // Agent LLM model editing state
  const [agentLLMEditing, setAgentLLMEditing] = useState(false);
  const [agentLLMModelDraft, setAgentLLMModelDraft] = useState("");

  // Fallback model editing state
  const [fallbackEditing, setFallbackEditing] = useState(false);
  const [fallbackModelDraft, setFallbackModelDraft] = useState("");

  // Env-managed default fields are seeded from environment variables and are
  // read-only in the UI (mirrors the env-seeded LLM provider lock). When a
  // field is env-managed, its "Configure" control is replaced with an "Env"
  // lock badge and the backend rejects any mutation of it.
  const envManagedFields = gaConfig?.env_managed_fields ?? [];
  const agentDefaultEnvLocked = envManagedFields.includes("agent_default_model");
  const reasoningEnvLocked = envManagedFields.includes("default_reasoning_effort");
  const routingEnvLocked = envManagedFields.includes("routing_model");
  const fallbackEnvLocked = envManagedFields.includes("fallback_model");
  // Embedding defaults are env-managed exactly like the others: their boot
  // seeding is unconditional (env always wins on restart), so the Embedding
  // control is read-only when DEFAULT_EMBEDDING_* is set. ``embedding_dimensions``
  // is included because the backend locks it independently (DEFAULT_EMBEDDING_DIMENSIONS
  // alone), and the grouped embedding save payload always sends it — without it
  // the card would stay editable yet 403 on save. See _env_managed_ga_fields.
  const embeddingEnvLocked =
    envManagedFields.includes("embedding_model") ||
    envManagedFields.includes("embedding_provider") ||
    envManagedFields.includes("embedding_dimensions");
  const imageEnvLocked =
    envManagedFields.includes("image_model") || envManagedFields.includes("image_provider");
  // DEFAULT_VIDEO_* / DEFAULT_SPEECH_* / DEFAULT_MUSIC_* / DEFAULT_DECISION_* lock their whole group.
  const videoEnvLocked = envManagedFields.includes("video_model");
  const speechEnvLocked = envManagedFields.includes("speech_model");
  const musicEnvLocked = envManagedFields.includes("music_model");
  const decisionEnvLocked = envManagedFields.includes("decision_model");

  // `env_managed_fields` arrives asynchronously with `gaConfig`, so a Configure
  // click can land before the lock is known (the flags default to false). When
  // the response then marks a field env-managed, the header swaps to the
  // read-only "Env" badge and drops Save/Cancel — but the body edit form stays
  // mounted, stranding the user. Auto-cancel any in-progress edit the moment its
  // env-lock becomes active.
  useEffect(() => {
    if (agentDefaultEnvLocked) setAgentLLMEditing(false);
  }, [agentDefaultEnvLocked]);
  useEffect(() => {
    if (reasoningEnvLocked) setReasoningEditing(false);
  }, [reasoningEnvLocked]);
  useEffect(() => {
    if (fallbackEnvLocked) setFallbackEditing(false);
  }, [fallbackEnvLocked]);
  useEffect(() => {
    if (embeddingEnvLocked) setEmbeddingEditing(false);
  }, [embeddingEnvLocked]);
  useEffect(() => {
    if (imageEnvLocked) setImageEditing(false);
  }, [imageEnvLocked]);

  // The 7 sections (#9173): administrator only, Settings sits behind AdminRoute.
  const tabs: { key: SettingsTab; label: string; icon: React.ElementType }[] = [
    { key: "organization", label: "Organization", icon: Building2 },
    { key: "automations", label: "Agents and automations", icon: Workflow },
    { key: "integrations", label: "Integrations", icon: Plug2 },
    { key: "ai", label: "AI and models", icon: Bot },
    { key: "security", label: "Access and security", icon: Shield },
    { key: "usage", label: "Usage and quotas", icon: Gauge },
    { key: "platform", label: "Platform", icon: SlidersHorizontal },
  ];
  const automationsSections = AUTOMATIONS_SECTIONS.filter(({ key }) =>
    key === "ctem-agents" ? isXtmOne : true,
  );

  // For custom_openai, litellm, tei and the media providers, compose the compound key from separate fields
  const isAddProviderOpen = addProviderMode !== null;
  const hasBaseUrlField = ["custom_openai", "litellm", "tei", "elevenlabs", "kling", "seedance"].includes(newProvider);
  // The media providers answer on their global endpoint; a regional one (data residency) is optional.
  const baseUrlOptional = ["elevenlabs", "kling", "seedance"].includes(newProvider);
  const singleCapability = SINGLE_CAPABILITY_PROVIDERS[newProvider];
  // The Filigran AI Gateway needs no API key: it authenticates with a per-request
  // XTM JWT signed automatically by this platform. The only input is the gateway
  // URL (stored as the credential), so the form shows a single "Gateway URL"
  // field and hides the API-key field + "no key" toggle entirely.
  const isGatewayProvider = newProvider === "filigran_gateway";
  // Google Vertex AI authenticates with a service account key (JSON), kept masked, and a region.
  const isVertexProvider = newProvider === "vertex_ai";
  const serviceAccount = isVertexProvider ? serviceAccountSummary(newApiKey) : null;
  // Chat/reasoning-capable flows discover a model list to pick from. The generic
  // "Add Provider" (mode "all") validates against the reasoning capability, so it
  // gets the same model picker as the dedicated reasoning flow.
  const showsModelPicker =
    (addProviderMode === "reasoning" || addProviderMode === "all") && !singleCapability;
  // Embedding-only providers (TEI `/embed` servers) can't do chat/image and
  // expose no model discovery, so they must always be validated for the
  // embedding capability regardless of which Add Provider flow opened the dialog.
  const isEmbeddingOnlyProvider = singleCapability === "embedding";
  // The embedding-model field is shown when adding an embedding provider (or an
  // embedding-only provider from any flow) once it has validated.
  const embeddingModelFieldShown =
    (addProviderMode === "embedding" || isEmbeddingOnlyProvider) && providerValidated;
  // When the validated provider exposes NO discoverable models (TEI, or
  // custom_openai/litellm embedding endpoints with no /models route), the typed
  // model id is the only source of truth — so it's required. Without it the
  // provider saves with zero models and never appears in the embedding picker.
  const embeddingModelRequired =
    embeddingModelFieldShown && resolvedModels.length === 0;
  const addProviderTitleByMode: Record<AddProviderMode, string> = {
    reasoning: "Add Reasoning Provider",
    embedding: "Add Embedding Provider",
    image: "Add Image Provider",
    all: "Add Provider",
  };
  // Addable provider types — a video-only provider hides while the Video
  // Studio is off (nothing else generates video); a provider only its
  // integration creates (LM Studio) never shows; providers already configured
  // keep working regardless (they're rendered from `providers` data, not this list).
  const addableProviderKeys = Object.keys(PROVIDER_DISPLAY_NAMES).filter(
    (provider) =>
      (SINGLE_CAPABILITY_PROVIDERS[provider] !== "video" || videoStudioEnabled)
      && addableByHand(provider),
  );
  const allowedProvidersByMode: Record<AddProviderMode, string[]> = {
    reasoning: addableProviderKeys.filter((provider) => !SINGLE_CAPABILITY_PROVIDERS[provider]),
    embedding: ["openai", "gemini", "azure_openai", "mistral", "bedrock", "ollama", "custom_openai", "litellm", "openrouter", "tei"],
    image: ["openai", "gemini", "azure_openai"],
    all: addableProviderKeys,
  };
  const providerOptionsForMode = addProviderMode
    ? allowedProvidersByMode[addProviderMode]
    : addableProviderKeys;
  const resolveConfiguredProvider = (provider?: string | null, providerId?: string | null) => {
    if (providerId) {
      const byId = providers?.find((p) => p.id === providerId);
      if (byId) return byId;
    }
    if (provider) {
      return providers?.find((p) => p.provider === provider) ?? null;
    }
    return null;
  };

  // Scope the platform-default highlight to the provider that owns the
  // default: two providers can expose the same model id (e.g. gpt-4o via
  // OpenAI and via a LiteLLM gateway), and a model-id-only comparison would
  // mark BOTH tiles as "default" — leaving the non-default provider's tile
  // unclickable so the default could never be moved to it. Match by
  // provider row id first. Legacy configs that predate the provider fields
  // fall back to the provider key, then the model id, but only when the
  // match is UNAMBIGUOUS: several rows can share a provider key (no
  // uniqueness constraint) and several providers can expose the same model,
  // and marking them all "default" would disable every candidate tile and
  // block moving the default. When ambiguous, highlight none — clicking any
  // tile re-persists a fully-qualified default (with provider_id) and
  // self-heals the legacy config.
  const platformDefaultModelFor = (p: LLMProvider) => {
    const model = gaConfig?.agent_default_model;
    if (!model) return null;
    if (gaConfig.agent_default_provider_id) {
      return gaConfig.agent_default_provider_id === p.id ? model : null;
    }
    const candidates = (providers ?? []).filter((row) =>
      gaConfig.agent_default_provider
        ? row.provider === gaConfig.agent_default_provider
        : (row.available_models ?? []).includes(model),
    );
    return candidates.length === 1 && candidates[0].id === p.id ? model : null;
  };

  const selectedEmbeddingProvider = resolveConfiguredProvider(
    gaConfig?.embedding_provider,
    gaConfig?.embedding_provider_id,
  );
  const selectedImageProvider = resolveConfiguredProvider(
    gaConfig?.image_provider,
    gaConfig?.image_provider_id,
  );
  const hasReasoningCompatibleProvider = (availableModels?.length ?? 0) > 0;
  // Configured, but no model listed: the fix is the model list, not another provider.
  const modellessProviders = emptyChatProviders(providers);
  // Env-only mode hides every provider control: the env variable is the one way in.
  const envHintProvider = modelsEnvProvidingOnly
    ? modellessProviders[0]
    : modellessProviders.find((p) => p.models_env_locked);
  const modelsEnvVariable = (p: LLMProvider) => `LLM_PROVIDER_${p.provider.toUpperCase()}_MODELS`;

  const getModelOptionLabel = (m: AvailableModel) => modelOptionLabel(m, providers);

  /**
   * Name the provider serving a stored (model, reference) pair.
   *
   * Prefers the referenced config, then the model's own provider kind — passing
   * only the reference would render blank when it no longer resolves, which
   * happens while the provider list is still loading and after a delete.
   */
  const configuredModelProvider = (
    model?: string | null,
    providerId?: string | null,
  ) => {
    const options = reasoningModels as AvailableModel[];
    const offering =
      (providerId
        ? options.find((m) => m.model === model && m.provider_id === providerId)
        : undefined) ?? options.find((m) => m.model === model);
    return (
      providerDisplayName(offering?.provider ?? "", providerId, providers)
      || t_i18n("unknown provider")
    );
  };

  /** The option a picker value refers to, or null when it pins no provider. */
  const chosenModel = (options: AvailableModel[], choice: string) => {
    const { model, providerId } = parseModelChoice(choice);
    return (
      options.find(
        (m) => m.model === model && (m.provider_id ?? null) === providerId,
      ) ?? null
    );
  };

  const agentModelLabel = (model: string, providerId: string | null, provider: string | null) =>
    getModelOptionLabel(
      chosenModel(reasoningModels, modelChoiceValue(model, providerId))
        ?? { model, provider: provider ?? "", provider_id: providerId ?? undefined },
    );

  /**
   * Save the default agent model an admin picked. A different model first asks
   * whether the users are emailed about it - unless email cannot be
   * sent here, where there is nothing to choose.
   */
  const saveAgentDefault = (
    payload: { agent_default_model: string; agent_default_provider: string | null; agent_default_provider_id: string | null },
    fromEditor: boolean,
  ) => {
    const model = payload.agent_default_model;
    if (isAdmin && model && model !== (gaConfig?.agent_default_model ?? "") && canEmailUsers(announcementStatus)) {
      setDefaultModelChange({ payload, fromEditor });
      return;
    }
    updateGAConfig.mutate(payload, { onSuccess: () => { if (fromEditor) setAgentLLMEditing(false); } });
  };

  const saveDefaultModelChange = (announce: boolean) => {
    if (!defaultModelChange) return;
    const { payload, fromEditor } = defaultModelChange;
    updateGAConfig.mutate(announce ? { ...payload, announce_default_model: true } : payload, {
      onSuccess: (saved) => {
        if (fromEditor) setAgentLLMEditing(false);
        setDefaultModelChange(null);
        if (announce && saved?.default_model_announced && announcementStatus) {
          emailingToast(announcementStatus);
        }
      },
    });
  };



  const effectiveApiKey = noApiKeyRequired && !isVertexProvider ? "none" : newApiKey;
  // Whether the credential needed to validate/save is filled. The gateway's
  // credential is its URL; every other provider needs the API key (plus a base
  // URL for the compound-key providers).
  const credentialFilled = isGatewayProvider
    ? customBaseUrl.trim().length > 0
    : effectiveApiKey.trim().length > 0 && (!hasBaseUrlField || baseUrlOptional || customBaseUrl.trim().length > 0);

  const openAddProvider = (mode: AddProviderMode) => {
    if (!canAddProviders) return;
    setAddProviderMode(mode);
    setModelsError(null);
    setProviderValidationMessage(null);
    setProviderValidated(false);
    setResolvedModels([]);
    setSelectedModels([]);
    setNewModel("");
    setNewDisplayName("");
    setNewDisplayName("");
    const availableProviders = allowedProvidersByMode[mode];
    if (!availableProviders.includes(newProvider)) {
      setNewProvider(availableProviders[0] ?? "openai");
    }
  };

  // Wrapped in ``useCallback`` so the env-only-flip ``useEffect`` below
  // can list it in its dependency array without re-firing on every
  // render (an inline arrow function would change identity each render
  // and either trigger ``react-hooks/exhaustive-deps`` or, if added to
  // deps, cause the effect to thrash).  The dependency array lists only
  // ``useState`` setters (stable for the component's lifetime); the refs and
  // never the mutation object itself complete the closure.
  const closeAddProvider = useCallback(() => {
    validateAttemptRef.current += 1;
    // ``isPending`` drives the Validate button's spinner and disabled state, and
    // it lives in the mutation rather than in the dialog — without this reset
    // the button reopens stuck on "Validating..." after a cancel mid-validation.
    validateProviderRef.current.reset();
    setAddProviderMode(null);
    setModelsError(null);
    setProviderValidationMessage(null);
    setProviderValidated(false);
    setResolvedModels([]);
    setSelectedModels([]);
    setNewModel("");
    setNewDisplayName("");
    setCustomBaseUrl("");
    setGatewayToken("");
    setVertexLocation("");
  }, [
    setAddProviderMode, setModelsError, setProviderValidationMessage, setProviderValidated, setResolvedModels,
    setSelectedModels, setNewModel, setNewDisplayName, setCustomBaseUrl, setGatewayToken, setVertexLocation,
  ]);

  // A credential edit invalidates what the last validation resolved.
  const resetProviderValidation = () => {
    setModelGroups(null);
    setResolvedModels([]);
    setSelectedModels([]);
    setProviderValidated(false);
    setProviderValidationMessage(null);
    setNewModel("");
    setModelsError(null);
  };

  const loadServiceAccountKeyFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > MAX_SERVICE_ACCOUNT_KEY_BYTES) {
      setModelsError(t_i18n("This file is too large to be a service account key."));
      return;
    }
    setNewApiKey((await file.text()).trim());
    resetProviderValidation();
  };

  const buildCompoundKey = () => {
    if (isGatewayProvider) return buildGatewayCredential(customBaseUrl, gatewayToken);
    if (isVertexProvider) return vertexAiCredential(newApiKey, vertexLocation);
    if (hasBaseUrlField && customBaseUrl.trim()) {
      return `${effectiveApiKey}||${customBaseUrl}`;
    }
    return effectiveApiKey;
  };

  const handleValidateProvider = () => {
    if (!credentialFilled) return;
    const attempt = (validateAttemptRef.current += 1);
    setModelsError(null);
    setProviderValidationMessage(null);
    setProviderValidated(false);
    setResolvedModels([]);
    setModelGroups(null);
    setNewModel("");
    setSelectedModels([]);
    const effectiveCapability = singleCapability
      ?? (addProviderMode === "all" ? "reasoning" : (addProviderMode ?? "reasoning"));
    validateProvider.mutate(
      {
        provider: newProvider,
        api_key: buildCompoundKey(),
        capability: effectiveCapability,
      },
      {
        onSuccess: (data) => {
          if (attempt !== validateAttemptRef.current) return;
          setProviderValidationMessage(data.message);
          setProviderValidated(data.connection_ok);
          setResolvedModels(data.models);
          setModelGroups(data.model_groups ?? null);
          if (effectiveCapability === "reasoning" || effectiveCapability === "image") {
            setSelectedModels(data.models);
          }
          if (effectiveCapability === "reasoning" && data.models.length > 0 && !gaConfig?.agent_default_model) {
            setNewModel(data.models[0]);
          }
          if (!data.connection_ok) {
            setModelsError(data.message || t_i18n("Provider validation failed."));
          }
        },
        onError: (err) => {
          if (attempt !== validateAttemptRef.current) return;
          setModelsError(err.message || t_i18n("Failed to validate provider. Check your settings."));
          setProviderValidated(false);
        },
      },
    );
  };

  const toggleModelSelection = (model: string) => {
    setSelectedModels((prev) => {
      const next = toggleArrayItem(prev, model);
      // Only manage default model selection when no agent default exists yet
      if (!gaConfig?.agent_default_model) {
        if (!next.includes(newModel) && next.length > 0) setNewModel(next[0]);
        if (next.length === 0) setNewModel("");
      }
      return next;
    });
  };

  const handleAddLLM = () => {
    if (!credentialFilled) return;
    if (!providerValidated) return;
    const mode = addProviderMode ?? "reasoning";
    const selectedModelsForMode =
      mode === "reasoning" || mode === "all" ? selectedModels : [];
    const chosenDefault = newModel || selectedModels[0];
    addProvider.mutate(
      {
        provider: newProvider,
        api_key: buildCompoundKey(),
        display_name: newDisplayName.trim() || undefined,
        ...((mode === "embedding" || isEmbeddingOnlyProvider) && newModel.trim()
          ? { default_model: newModel.trim(), available_models: [newModel.trim()] }
          : { available_models: selectedModelsForMode.length > 0 ? selectedModelsForMode : undefined }),
      },
      {
        onSuccess: (createdProvider: LLMProvider) => {
          void offerCatalogChange(createdProvider.model_catalog_change);
          // If no agent default yet, set it to the chosen model
          if (mode === "reasoning" && !gaConfig?.agent_default_model && chosenDefault) {
            updateGAConfig.mutate({
              agent_default_provider: newProvider,
              agent_default_provider_id: createdProvider.id,
              agent_default_model: chosenDefault,
            });
          }
          if (mode === "embedding") {
            const nextEmbeddingModel = resolvedModels[0] || newModel;
            updateGAConfig.mutate({
              embedding_provider: createdProvider.provider,
              embedding_provider_id: createdProvider.id,
              ...(nextEmbeddingModel ? { embedding_model: nextEmbeddingModel } : {}),
            });
          }
          if (mode === "image") {
            updateGAConfig.mutate({
              image_provider: createdProvider.provider,
              image_provider_id: createdProvider.id,
              image_model: resolvedModels[0] || "",
            });
          }
          setNewApiKey("");
          setNoApiKeyRequired(false);
          setCustomBaseUrl("");
          setGatewayToken("");
          setNewModel("");
          setSelectedModels([]);
          setResolvedModels([]);
          setModelsError(null);
          setProviderValidationMessage(null);
          setProviderValidated(false);
          setAddProviderMode(null);

          // Save first, then if duplicate visible names exist, prompt rename.

        },
      },
    );
  };


  useEffect(() => {
    if (!isAddProviderOpen) return;
    requestAnimationFrame(() => {
      const node = addProviderFormRef.current;
      if (!node) return;
      // Use immediate scrolling + autofocus to avoid sporadic "inactive"
      // form behavior observed while smooth scrolling is still animating.
      node.scrollIntoView({ behavior: "auto", block: "start" });
      const element = node as HTMLElement;
      if (!element.hasAttribute("tabindex")) {
        element.tabIndex = -1;
      }
      element.focus();

      requestAnimationFrame(() => {
        addProviderApiKeyRef.current?.focus({ preventScroll: true });
      });
    });
  }, [isAddProviderOpen]);

  useEffect(() => {
    if (!modelsEnvProvidingOnly) return;
    // ── Env-only mode flipped on ──
    // Close every provider-management UI surface so admins don't see
    // edit / rename controls that the backend would now 403 on. Without
    // this, anything opened BEFORE platform settings finished loading
    // (the add-provider form, the inline models editor, the rename
    // modal) would silently linger and surface stale management UI.
    if (isAddProviderOpen) {
      closeAddProvider();
    }
    if (editingModelsFor !== null) {
      setEditingModelsFor(null);
    }
    if (providerToDelete !== null) {
      setProviderToDelete(null);
    }
  }, [
    modelsEnvProvidingOnly,
    isAddProviderOpen,
    editingModelsFor,
    providerToDelete,
    closeAddProvider,
  ]);

  const saveEmbeddingConfig = () => {
    const model = parseModelChoice(embeddingModelDraft || "").model || "";
    const matchedModel = model
      ? (chosenModel(availableEmbeddingModels ?? [], embeddingModelDraft) ?? undefined)
      : undefined;
    const fallbackProvider = gaConfig?.embedding_provider || "";
    const fallbackProviderConfig = fallbackProvider
      ? providers?.find((p) => p.provider === fallbackProvider)
      : undefined;
    const prov = matchedModel?.provider ?? fallbackProvider;
    const provId =
      parseModelChoice(embeddingModelDraft || "").providerId
      ?? matchedModel?.provider_id
      ?? fallbackProviderConfig?.id
      ?? null;
    if (!prov) {
      setEmbeddingTestResult({
        capable: false,
        message: t_i18n("Select an embedding provider before saving."),
        returnedDimensions: null,
        requestedDimensions: null,
        suggestedDimensions: [],
        expectedDimensions: null,
      });
      return;
    }
    const parsed = embeddingDimensionsDraft ? parseInt(embeddingDimensionsDraft, 10) : null;
    const dims = Number.isFinite(parsed) ? parsed : null;
    if (dims !== null && dims <= 0) {
      setEmbeddingTestResult({
        capable: false,
        message: t_i18n("Vector Dimensions must be a positive integer."),
        returnedDimensions: null,
        requestedDimensions: null,
        suggestedDimensions: [],
        expectedDimensions: dims,
      });
      return;
    }
    updateGAConfig.mutate(
      { embedding_provider: prov, embedding_provider_id: provId, embedding_model: model, embedding_dimensions: dims },
      { onSuccess: () => setEmbeddingEditing(false) },
    );
  };

  return (
    <div className="space-y-6">
      {/* One header row (#10215): the page's help is the title's "i". */}
      <ListPageHeader
        title={t_i18n("Settings")}
        infoLabel={t_i18n("About settings")}
        info={t_i18n("Configure the organization, agents and automations, integrations, AI models, security, usage and the platform.")}
      />

      <DetailTabs tabs={tabs} active={tab} />

      {tab === "organization" && (
        <SectionNav sections={ORGANIZATION_SECTIONS} active={organizationSection} onSelect={selectSection} label="Organization sections" idPrefix="settings-organization" />
      )}
      {tab === "automations" && (
        <SectionNav sections={automationsSections} active={automations} onSelect={selectSection} label="Agents and automations sections" idPrefix="settings-automations" />
      )}
      {tab === "integrations" && (
        <SectionNav sections={INTEGRATIONS_SECTIONS} active={integrationsSection} onSelect={selectSection} label="Integrations sections" idPrefix="settings-integrations" />
      )}
      {tab === "ai" && (
        <SectionNav sections={AI_SECTIONS} active={aiSection} onSelect={selectSection} label="AI and models sections" idPrefix="settings-ai" />
      )}
      {tab === "usage" && (
        <SectionNav sections={USAGE_SECTIONS} active={usageSection} onSelect={selectSection} label="Usage and quotas sections" idPrefix="settings-usage" />
      )}

      {/* Platform: instance, licence, AI enablement, language, branding, XTM Hub. */}
      {tab === "platform" && <ParametersSection />}

      {/* Organization (#8987): the profile every agent reads. Not wrapped:
          filling it involves no model, so it stays editable with AI off. */}
      {/* The scheduled imports and the agents' context default sit in its "Knowledge and sources" tab (#12329). */}
      {tab === "organization" && organizationSection === "profile" && <OrganizationSection />}
      {/* Not wrapped either: the departments analytics roll usage up to
          involve no model, so they stay editable with AI off. */}
      {tab === "organization" && organizationSection === "departments" && <DepartmentsSection />}
      {/* ROI catalog and rates (#9448): read and edited without a model, both modes. */}
      {tab === "organization" && organizationSection === "units" && <RoiUnitsSection />}
      {tab === "organization" && organizationSection === "rates" && <RoiRatesSection />}
      {/* Who sees the value figures (#12077 L6b, design 12.5): the k rule and By person, no model. */}
      {tab === "organization" && organizationSection === "rates" && <RoiPrivacyCard />}

      {/* ================================================================ */}
      {/* AI and models: providers, model roles and media generation        */}
      {/* ================================================================ */}
      {/* Read-only rather than hidden (FR-022): an administrator must still
          see the provider configuration they are told is inactive. The
          Platform section is deliberately NOT wrapped: it holds the toggle. */}
      {tab === "ai" && (aiSection === "models" || aiSection === "media") && <AiReadOnly><div className="space-y-6">
          {/* Community Edition: say the honest thing first. Providers can be
              prepared here ahead of a license, but none of them runs until
              Enterprise Edition is active - the "configure one to unblock
              the platform" warning below is suppressed meanwhile, because it
              would guide the person through a setup that changes nothing. */}
          <EnterpriseUpsellNotice />
          {aiSection === "models" && (<>
          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-5 space-y-4 mb-8 backdrop-blur-sm">
            <div className="flex items-center justify-between">
              <div>
                <SettingsGroupTitle icon={Server}>{t_i18n("All Providers")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("Full provider registry. Domain sections below configure the models used for each capability.")}
                </SettingsGroupDescription>
              </div>
              {canAddProviders && (
                <Button size="sm" className="gap-1.5 shrink-0 ml-4" onClick={() => openAddProvider("all")}>
                  <Plus className="h-3 w-3" /> {t_i18n("Add Provider")}
                </Button>
              )}
            </div>
            {(() => {
              // Rendered in the order the endpoint returns (created_at, id) —
              // the same order `providerOrdinal` counts in. A local re-sort made
              // the "(2)" badge and the card order two sources for one ordering.
              const allProviders = providers ?? [];
              return allProviders.length > 0 ? (
                <div className="space-y-3">
                  {allProviders.map((p) => (
                    <ProviderCard
                      key={p.id}
                      provider={p}
                      allProviders={allProviders}
                      isAdmin={canManageProviders}
                      isEditingModels={editingModelsFor === p.id}
                      onEditModels={() => setEditingModelsFor(editingModelsFor === p.id ? null : p.id)}
                      onCloseEditModels={() => setEditingModelsFor(null)}
                      onDelete={() => setProviderToDelete(p)}
                      onRenameDisplayName={(displayName) =>
                        updateProvider.mutate({ id: p.id, display_name: displayName })
                      }
                      onUpdateModels={(available, contextWindows) =>
                        updateProvider.mutate(
                          { id: p.id, available_models: available, context_windows: contextWindows },
                          {
                            onSuccess: (saved) => {
                              setEditingModelsFor(null);
                              void offerCatalogChange(saved.model_catalog_change);
                            },
                          },
                        )
                      }
                      onUpdateAccess={(access, onSaved) =>
                        updateProvider.mutate({ id: p.id, ...access }, { onSuccess: onSaved })
                      }
                      isUpdating={updateProvider.isPending}
                      platformDefaultModel={platformDefaultModelFor(p)}
                      canSetPlatformDefault={!agentDefaultEnvLocked && !updateGAConfig.isPending}
                      onSetPlatformDefault={(provider, providerId, model) => {
                        // Mirror the Configure > Save guard: the backend 403s
                        // any mutation of an env-managed field, and duplicate
                        // clicks while a save is in flight would race.
                        if (agentDefaultEnvLocked || updateGAConfig.isPending) return;
                        saveAgentDefault({
                          agent_default_provider: provider,
                          agent_default_provider_id: providerId,
                          agent_default_model: model,
                        }, false);
                      }}
                    />
                  ))}
                </div>
              ) : (
                <div className="rounded-sm border border-dashed border-elevation-subtle-soft bg-elevation-hover p-10 text-center flex flex-col items-center justify-center">
                  <Server className="h-8 w-8 text-default-disabled mb-3" />
                  <p className="content-base text-default-secondary">{t_i18n("No providers configured.")}</p>
                  {canAddProviders && (
                    <Button variant="outline" size="sm" className="mt-4 gap-1.5" onClick={() => openAddProvider("all")}>
                      <Plus className="h-3 w-3" /> {t_i18n("Add Provider")}
                    </Button>
                  )}
                </div>
              );
            })()}
            {providerToDelete && canManageProviders && providers?.some((p) => p.id === providerToDelete.id) && (
              <DeleteProviderDialog
                key={providerToDelete.id}
                provider={providerToDelete}
                providers={providers ?? []}
                onDeleted={() => { void offerCatalogChangeAfterDelete(); }}
                onClose={() => setProviderToDelete(null)}
              />
            )}
            {catalogChange && announcementStatus && (
              <ModelChangesDialog
                key={catalogChange.id}
                change={catalogChange}
                status={announcementStatus}
                afterSave
                onClose={() => setCatalogChange(null)}
              />
            )}
            {defaultModelChange && announcementStatus && (
              <DefaultModelAnnouncementDialog
                mode="change"
                model={defaultModelChange.payload.agent_default_model}
                modelLabel={agentModelLabel(
                  defaultModelChange.payload.agent_default_model,
                  defaultModelChange.payload.agent_default_provider_id,
                  defaultModelChange.payload.agent_default_provider,
                )}
                provider={defaultModelChange.payload.agent_default_provider}
                previousModel={gaConfig?.agent_default_model || null}
                status={announcementStatus}
                saving={updateGAConfig.isPending}
                onSave={saveDefaultModelChange}
                onClose={() => setDefaultModelChange(null)}
              />
            )}
          </div>

            <div className="rounded-sm border border-filigran-tonic-primary/15 bg-gradient-to-br from-filigran-tonic-primary/[0.03] to-transparent p-5 space-y-6">
            <div className="flex items-center justify-between mb-3">
              <div>
                <SettingsGroupTitle icon={Bot}>{t_i18n("Agent LLM Models")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("Configure providers and defaults used by agents for chat, planning, routing support, and other LLM-driven tasks.")}
                </SettingsGroupDescription>
              </div>
              <div className="flex gap-2 shrink-0 ml-4">
                {agentDefaultEnvLocked ? (
                  <EnvLockBadge />
                ) : isAdmin && hasReasoningCompatibleProvider ? (
                  !agentLLMEditing ? (
                    <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => { setAgentLLMModelDraft(modelChoiceValue(gaConfig?.agent_default_model, gaConfig?.agent_default_provider_id)); setAgentLLMEditing(true); }}>
                      <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
                    </Button>
                  ) : null
                ) : null}
              </div>
            </div>

          {!hasReasoningCompatibleProvider && (
            <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 flex items-start gap-3 backdrop-blur-md shadow-inner">
              <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
                <Info className="h-3 w-3 text-default-secondary" />
              </div>
              {aiLicensed && modellessProviders.length > 0 ? (
                <p className="text-[13px] text-default-secondary leading-relaxed">
                  {t_i18n("The configured LLM providers offer no model. Agentic and LLM-powered platform features are unavailable until one does.")}
                  {envHintProvider && isAdmin
                    ? " " + (modelsEnvProvidingOnly
                      ? t_i18n("Set {variable} and restart.", { variable: modelsEnvVariable(envHintProvider) })
                      : t_i18n("Set {variable} and restart, or set LLM_PROVIDER_MODELS_EDITABLE=true to select its models here.", {
                          variable: modelsEnvVariable(envHintProvider),
                        }))
                    : canManageProviders && " " + t_i18n("Select models on a provider in this section.")}
                </p>
              ) : (
                <p className="text-[13px] text-default-secondary leading-relaxed">
                  {aiLicensed
                    ? t_i18n("No LLM provider configured. Agentic and LLM-powered platform features are unavailable until at least one LLM provider is configured.")
                    : t_i18n("No LLM provider configured. You can prepare one now; it is used as soon as Enterprise Edition is activated.")}
                  {canAddProviders && aiLicensed && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
                </p>
              )}
            </div>
          )}

          {hasReasoningCompatibleProvider && (
            <div>
              <div className="space-y-3">
                <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                      <Bot className="h-4 w-4 text-filigran-tonic-primary" />
                    </div>
                    <div>
                      <p className="content-base font-medium">
                        {gaConfig?.agent_default_model
                          ? agentModelLabel(gaConfig.agent_default_model, gaConfig.agent_default_provider_id, gaConfig.agent_default_provider)
                          : t_i18n("None")}
                      </p>
                      <p className="content-caption text-default-secondary">{t_i18n("Active default agent LLM model")}</p>
                    </div>
                  </div>
                </div>
                {isAdmin && (
                  <ModelAnnouncementPanel
                    labelOf={(model, provider) =>
                      agentModelLabel(model, model === gaConfig?.agent_default_model ? gaConfig.agent_default_provider_id : null, provider)
                    }
                  />
                )}
                {agentLLMEditing && (
                  <>
                    <SettingsField id={`${fieldId}-agent-model`} label={t_i18n("Agent LLM Model")} helper={t_i18n("The primary reasoning model used by agents.")}>
                      <ModelSelect
                        id={`${fieldId}-agent-model`}
                        aria-label={t_i18n("Agent LLM Model")}
                        aria-describedby={settingsFieldHintId(`${fieldId}-agent-model`)}
                        models={reasoningModels}
                        providers={providers}
                        pinned={[{ value: "__none__", label: t_i18n("None") }]}
                        value={agentLLMModelDraft || "__none__"}
                        onValueChange={(v) => setAgentLLMModelDraft(v === "__none__" ? "" : v)}
                      />
                    </SettingsField>
                    <SettingsSaveBar
                      dirty={agentLLMModelDraft !== modelChoiceValue(gaConfig?.agent_default_model, gaConfig?.agent_default_provider_id)}
                      saving={updateGAConfig.isPending}
                      onCancel={() => setAgentLLMEditing(false)}
                      onSave={() => {
                        const { model, providerId } = parseModelChoice(agentLLMModelDraft);
                        const selectedModel = chosenModel(reasoningModels, agentLLMModelDraft);
                        saveAgentDefault({
                          agent_default_model: model || "",
                          agent_default_provider: selectedModel?.provider || gaConfig?.agent_default_provider || null,
                          agent_default_provider_id: providerId
                        }, true);
                      }}
                    />
                  </>
                )}
              </div>
            </div>
          )}

          {/* ─── Default Reasoning Effort ─── */}
          {hasReasoningCompatibleProvider && (
            <div>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <SettingsGroupTitle icon={BrainCircuit}>{t_i18n("Default Reasoning Effort")}</SettingsGroupTitle>
                  <SettingsGroupDescription>
                    {t_i18n("Platform-wide default for thinking/reasoning models (OpenAI o-series, Anthropic Opus, Gemini 2.5). Agents can override this in their configuration. Only applied to supported models: ignored for standard models.")}
                  </SettingsGroupDescription>
                </div>
                {reasoningEnvLocked ? (
                  <div className="shrink-0 ml-4"><EnvLockBadge /></div>
                ) : isAdmin ? (
                  !reasoningEditing ? (
                    <Button variant="secondary" size="sm" className="gap-1.5 shrink-0 ml-4" onClick={() => { setReasoningDraft(gaConfig?.default_reasoning_effort || ""); setReasoningEditing(true); }}>
                      <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
                    </Button>
                  ) : null
                ) : null}
              </div>
              <div className="space-y-3">
                <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                      <BrainCircuit className="h-4 w-4 text-accent" />
                    </div>
                    <div>
                      <p className="content-base font-medium">
                        {gaConfig?.default_reasoning_effort
                          ? t_i18n(REASONING_EFFORT_CONFIG[gaConfig.default_reasoning_effort as keyof typeof REASONING_EFFORT_CONFIG]?.label ?? gaConfig.default_reasoning_effort)
                          : t_i18n("None")}
                      </p>
                      <span className="content-compact text-default-secondary">
                        {gaConfig?.default_reasoning_effort
                          ? t_i18n(REASONING_EFFORT_CONFIG[gaConfig.default_reasoning_effort as keyof typeof REASONING_EFFORT_CONFIG]?.description ?? "Custom level")
                          : t_i18n("Provider default: no reasoning effort override")}
                      </span>
                    </div>
                  </div>
                </div>
                {reasoningEditing && (
                  <>
                  <SettingsField id={`${fieldId}-reasoning-effort`} label={t_i18n("Reasoning Effort Level")} helper={t_i18n("Controls how much compute budget is allocated to thinking/reasoning. Only affects supported models.")}>
                    <Select
                      value={reasoningDraft || "__none__"}
                      onValueChange={(v) => setReasoningDraft(v === "__none__" ? "" : v)}
                    >
                      <SelectTrigger id={`${fieldId}-reasoning-effort`} aria-describedby={settingsFieldHintId(`${fieldId}-reasoning-effort`)} className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__none__">{t_i18n("None (provider default)")}</SelectItem>
                        {REASONING_EFFORT_LEVELS.map((level) => (
                          <SelectItem key={level} value={level}>
                            {t_i18n(REASONING_EFFORT_CONFIG[level].label)} — {t_i18n(REASONING_EFFORT_CONFIG[level].description)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </SettingsField>
                  <SettingsSaveBar
                    dirty={reasoningDraft !== (gaConfig?.default_reasoning_effort || "")}
                    saving={updateGAConfig.isPending}
                    onCancel={() => setReasoningEditing(false)}
                    onSave={() => updateGAConfig.mutate({ default_reasoning_effort: reasoningDraft || "" }, { onSuccess: () => setReasoningEditing(false) })}
                  />
                  </>
                )}
              </div>
            </div>
          )}

          </div>

          <div className="rounded-sm border border-feedback-alert-primary/15 bg-gradient-to-br from-feedback-alert-primary/[0.03] to-transparent p-5">

          {/* ─── Routing Model ─── */}
          <PlatformModelSetting
            fieldId={`${fieldId}-routing`}
            icon={ArrowLeftRight}
            tileClassName="bg-feedback-alert-primary/10"
            iconClassName="text-feedback-warning-primary"
            title={t_i18n("Routing Model")}
            description={t_i18n("Lightweight model used to evaluate mandatory transfer routing conditions. A fast, cheap model (e.g. GPT-4o-mini, Gemini Flash) is recommended. Falls back to the platform default model when not set.")}
            emptyMessage={t_i18n("No compatible LLM model available for routing. Add a provider in this section to configure routing.")}
            pickHint={t_i18n("Pick a fast, cheap model: routing is a simple classification task (~100 tokens).")}
            noneLabel={t_i18n("Platform default")}
            models={reasoningModels}
            providers={providers}
            model={gaConfig?.routing_model}
            providerId={gaConfig?.routing_provider_id}
            unsetBadge={
              <Badge variant="outline" className="content-caption border-accent/40 bg-accent/10 text-accent">
                {gaConfig?.agent_default_model
                  ? getModelOptionLabel(chosenModel(reasoningModels, modelChoiceValue(gaConfig.agent_default_model, gaConfig.agent_default_provider_id)) ?? { model: gaConfig.agent_default_model, provider: gaConfig.agent_default_provider ?? "", provider_id: gaConfig.agent_default_provider_id ?? undefined })
                  : t_i18n("Default model: not configured")}
              </Badge>
            }
            setSubtitle={gaConfig?.routing_model ? t_i18n("Custom routing model: {provider}", { provider: configuredModelProvider(gaConfig.routing_model, gaConfig.routing_provider_id) }) : ""}
            unsetSubtitle={t_i18n("Uses the platform default model for routing classification")}
            envLocked={routingEnvLocked}
            canEdit={isAdmin}
            isSaving={updateGAConfig.isPending}
            onSave={(model, providerId, done) => updateGAConfig.mutate({ routing_model: model, routing_provider_id: providerId }, { onSuccess: done })}
          />

          {/* ─── Reply suggestions, generated on the routing model ─── */}
          <div className="mt-4 flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-3.5" data-testid="settings.reply-suggestions">
            <div className="min-w-0">
              <label htmlFor={`${fieldId}-reply-suggestions`} className="content-base text-default-primary">{t_i18n("Reply suggestions")}</label>
              <p className="content-compact text-default-secondary">
                {gaConfig?.routing_model
                  ? t_i18n("Once an agent has answered, the routing model proposes the user's next message in the chat's message box (Tab to use it). Each user can turn it off in their profile.")
                  : t_i18n("Needs a routing model: with none set, no reply suggestion is generated.")}
              </p>
            </div>
            <Switch
              id={`${fieldId}-reply-suggestions`}
              checked={gaConfig?.reply_suggestions_enabled !== false}
              disabled={!isAdmin || updateGAConfig.isPending}
              onCheckedChange={(checked) => updateGAConfig.mutate({ reply_suggestions_enabled: checked })}
            />
          </div>

          {/* ─── Fallback Model ─── */}
          <div className="mt-6">
            <div className="flex items-center justify-between mb-3">
              <div>
                <SettingsGroupTitle icon={LifeBuoy}>{t_i18n("Fallback Model")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("Model tried first when the primary model fails (rate limit, overload, or transient provider error) after retries are exhausted. Pick a resilient model on a different provider or with higher quota. When not set, failover uses other configured providers' default models.")}
                </SettingsGroupDescription>
              </div>
              <div className="flex gap-2 shrink-0 ml-4">
                {fallbackEnvLocked ? (
                  <EnvLockBadge />
                ) : isAdmin && reasoningModels.length > 0 ? (
                  !fallbackEditing ? (
                    <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => { setFallbackModelDraft(modelChoiceValue(gaConfig?.fallback_model, gaConfig?.fallback_provider_id)); setFallbackEditing(true); }}>
                      <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
                    </Button>
                  ) : null
                ) : null}
              </div>
            </div>

            {reasoningModels.length === 0 ? (
              <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 flex items-start gap-3 backdrop-blur-md shadow-inner">
                <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
                  <Info className="h-3 w-3 text-default-secondary" />
                </div>
                <p className="text-[13px] text-default-secondary leading-relaxed">
                  {t_i18n("No compatible LLM model available for fallback. Add a provider in this section to configure a fallback model.")}
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-feedback-success-primary/10">
                      <LifeBuoy className="h-4 w-4 text-feedback-success-primary" />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="content-base font-medium">
                          {gaConfig?.fallback_model || t_i18n("Provider defaults")}
                        </p>
                      </div>
                      <span className="content-compact text-default-secondary">
                        {gaConfig?.fallback_model
                          ? t_i18n("Tried first on failover: {provider}", { provider: configuredModelProvider(gaConfig.fallback_model, gaConfig.fallback_provider_id) })
                          : t_i18n("Failover uses other configured providers' own default models")}
                      </span>
                    </div>
                  </div>
                </div>
                {fallbackEditing && (
                  <>
                    <SettingsField id={`${fieldId}-fallback-model`} label={t_i18n("Fallback Model")} helper={t_i18n("Choose a model that stays available when your primary model is rate-limited or overloaded.")}>
                      <ModelSelect
                        id={`${fieldId}-fallback-model`}
                        aria-label={t_i18n("Fallback Model")}
                        aria-describedby={settingsFieldHintId(`${fieldId}-fallback-model`)}
                        models={reasoningModels}
                        providers={providers}
                        pinned={[{ value: "__none__", label: t_i18n("Provider defaults") }]}
                        value={fallbackModelDraft || "__none__"}
                        onValueChange={(v) => setFallbackModelDraft(v === "__none__" ? "" : v)}
                      />
                    </SettingsField>
                    <SettingsSaveBar
                      dirty={fallbackModelDraft !== modelChoiceValue(gaConfig?.fallback_model, gaConfig?.fallback_provider_id)}
                      saving={updateGAConfig.isPending}
                      onCancel={() => setFallbackEditing(false)}
                      onSave={() => {
                        const { model, providerId } = parseModelChoice(fallbackModelDraft);
                        updateGAConfig.mutate({ fallback_model: model || "", fallback_provider_id: providerId }, { onSuccess: () => setFallbackEditing(false) });
                      }}
                    />
                  </>
                )}
              </div>
            )}
          </div>
          </div>

          <div className="rounded-sm border border-filigran-ia-primary/15 bg-gradient-to-br from-filigran-ia-primary/[0.03] to-transparent p-5">

          {/* ─── Embedding (Knowledge Bases) ─── */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <div>
                <SettingsGroupTitle icon={Key}>{t_i18n("Embedding Provider (Knowledge Bases)")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("Used to generate vector embeddings for knowledge base documents and semantic search. Requires a provider with embedding support (OpenAI, Gemini, Azure OpenAI, Mistral, Bedrock, Ollama, or a Custom OpenAI-compatible endpoint).")}
                </SettingsGroupDescription>
              </div>
              <div className="flex gap-2 shrink-0 ml-4">
                {embeddingEnvLocked ? (
                  <EnvLockBadge />
                ) : isAdmin && ((availableEmbeddingModels ?? []).length > 0 || !!gaConfig?.embedding_provider) ? (
                  !embeddingEditing ? (
                    <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => {
                      setEmbeddingModelDraft(modelChoiceValue(gaConfig?.embedding_model, gaConfig?.embedding_provider_id));
                      const initialDims = gaConfig?.embedding_dimensions;
                      setEmbeddingDimensionsDraft(initialDims ? String(initialDims) : "");
                      setEmbeddingTestResult(null);
                      setEmbeddingEditing(true);
                      void refetchEmbeddingModels();
                    }}>
                      <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
                    </Button>
                  ) : null
                ) : null}
              </div>
            </div>
            {(() => {
              const activeEmbeddingProviderKey = selectedEmbeddingProvider?.provider || gaConfig?.embedding_provider || availableEmbeddingModels?.[0]?.provider;
              const hasEmbeddingModels = (availableEmbeddingModels ?? []).length > 0;
              const hasConfiguredEmbeddingProvider = !!selectedEmbeddingProvider || !!gaConfig?.embedding_provider;
              const embeddingProviderName = activeEmbeddingProviderKey ? (PROVIDER_DISPLAY_NAMES[activeEmbeddingProviderKey] || activeEmbeddingProviderKey) : null;
              const currentEmbeddingModel = gaConfig?.embedding_model || "—";
              return hasEmbeddingModels || hasConfiguredEmbeddingProvider ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-feedback-success-primary/10">
                        <Key className="h-4 w-4 text-feedback-success-primary" />
                      </div>
                      <div>
                        <p className="content-base font-medium">
                          {currentEmbeddingModel === "—" ? t_i18n("Provider default model") : currentEmbeddingModel}
                        </p>
                        <p className="content-compact text-default-secondary">
                          {embeddingProviderName ? t_i18n("Embedding Provider: {provider}", { provider: embeddingProviderName }) : t_i18n("Embedding provider configured")}
                        </p>
                      </div>
                    </div>
                    <Badge variant="secondary" className="shrink-0 ml-3">
                      {gaConfig?.embedding_dimensions
                        ? t_i18n("{count} dimensions", { count: gaConfig.embedding_dimensions })
                        : t_i18n("Provider default dimensions")}
                    </Badge>
                  </div>
                      {embeddingEditing && (
                        <div className="space-y-4">
                      <ModelListRefreshing active={refreshingEmbeddingModels} />
                      <div className="rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-2.5">
                        <p className="content-compact text-feedback-warning-primary">
                          {t_i18n("Model discovery can include false positives for some providers (especially Custom OpenAI-compatible endpoints where embedding candidates are partly inferred from model name markers). Run a live embedding test before pushing to production.")}
                        </p>
                      </div>
                      <SettingsField id={`${fieldId}-embedding-model`} label={t_i18n("Embedding Model")} helper={t_i18n("Model used for knowledge base embeddings and semantic search.")}>
                      {hasEmbeddingModels ? (
                        <ModelSelect
                          id={`${fieldId}-embedding-model`}
                          aria-label={t_i18n("Embedding Model")}
                          aria-describedby={settingsFieldHintId(`${fieldId}-embedding-model`)}
                          models={availableEmbeddingModels}
                          providers={providers}
                          pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                          value={embeddingModelDraft || "__none__"}
                          onValueChange={(v) => {
                            setEmbeddingModelDraft(v === "__none__" ? "" : v);
                            setEmbeddingTestResult(null);
                          }}
                        />
                      ) : (
                        <Input
                          id={`${fieldId}-embedding-model`}
                          aria-label={t_i18n("Embedding Model")}
                          aria-describedby={settingsFieldHintId(`${fieldId}-embedding-model`)}
                          value={parseModelChoice(embeddingModelDraft).model}
                          onChange={(e) => {
                            // The draft carries the `<provider_id>::<model>`
                            // encoding; this branch edits the model half only,
                            // and keeps the stored reference.
                            setEmbeddingModelDraft(
                              modelChoiceValue(
                                e.target.value,
                                parseModelChoice(embeddingModelDraft).providerId,
                              ),
                            );
                            setEmbeddingTestResult(null);
                          }}
                          placeholder={t_i18n("Enter embedding model id (e.g. nomic-embed-text-v1.5)")}
                        />
                      )}
                      </SettingsField>
                      <SettingsField
                        id={`${fieldId}-vector-dimensions`}
                        label={t_i18n("Vector Dimensions")}
                        helper={t_i18n('Number of dimensions used for KB vectors. Higher values usually improve semantic precision/recall, but increase indexing/search cost and latency. Lower values are faster and lighter, with possible retrieval quality loss. Use "Detect" to auto-fill from a live probe.')}
                      >
                        <div className="flex items-center gap-2">
                          <Input aria-label={t_i18n("Vector Dimensions")}
                            id={`${fieldId}-vector-dimensions`}
                            aria-describedby={settingsFieldHintId(`${fieldId}-vector-dimensions`)}
                            type="number"
                            min={1}
                            step={1}
                            className="w-40"
                            placeholder="e.g. 1024"
                            value={embeddingDimensionsDraft}
                            onChange={(e) => {
                              setEmbeddingDimensionsDraft(e.target.value);
                              setEmbeddingTestResult(null);
                            }}
                          />
                          <Button
                            variant="secondary"
                            size="sm"
                            className="gap-1.5"
                            disabled={!embeddingModelDraft || testEmbeddingModel.isPending || aiOff}
                            onClick={() => {
                              const model = parseModelChoice(embeddingModelDraft).model;
                              if (!model) {
                                setEmbeddingTestResult({
                                  capable: false,
                                  message: t_i18n("Select an embedding model first."),
                                  returnedDimensions: null,
                                  requestedDimensions: null,
                                  suggestedDimensions: [],
                                  expectedDimensions: null,
                                });
                                return;
                              }
                              const matched = chosenModel(availableEmbeddingModels ?? [], embeddingModelDraft);
                              const provider = matched?.provider || gaConfig?.embedding_provider || "";
                              const providerId = matched?.provider_id || providers?.find((p) => p.provider === provider)?.id;
                              if (!provider) {
                                setEmbeddingTestResult({
                                  capable: false,
                                  message: t_i18n("Unable to resolve provider for this model."),
                                  returnedDimensions: null,
                                  requestedDimensions: null,
                                  suggestedDimensions: [],
                                  expectedDimensions: null,
                                });
                                return;
                              }
                              testEmbeddingModel.mutate(
                                { provider, model, provider_id: providerId },
                                {
                                  onSuccess: (res) => {
                                    if (res.returned_dimensions !== null) {
                                      setEmbeddingDimensionsDraft(String(res.returned_dimensions));
                                    }
                                    setEmbeddingTestResult({
                                      capable: res.capable,
                                      message: res.message,
                                      returnedDimensions: res.returned_dimensions,
                                      requestedDimensions: res.requested_dimensions,
                                      suggestedDimensions: res.suggested_dimensions ?? [],
                                      expectedDimensions: res.expected_dimensions,
                                    });
                                  },
                                  onError: (err) => {
                                    setEmbeddingTestResult({
                                      capable: false,
                                      message: err.message || t_i18n("Embedding test failed."),
                                      returnedDimensions: null,
                                      requestedDimensions: null,
                                      suggestedDimensions: [],
                                      expectedDimensions: null,
                                    });
                                  },
                                },
                              );
                            }}
                          >
                            {testEmbeddingModel.isPending ? (
                              <><Spinner size="sm" tone="inherit" /> {t_i18n("Detecting...")}</>
                            ) : (
                              t_i18n("Detect")
                            )}
                          </Button>
                          {embeddingDimensionsDraft && !testEmbeddingModel.isPending && (
                            <Button
                              variant="outline"
                              size="sm"
                              className="gap-1.5"
                              disabled={!embeddingModelDraft || aiOff}
                              onClick={() => {
                                const model = parseModelChoice(embeddingModelDraft).model;
                                const matched = chosenModel(availableEmbeddingModels ?? [], embeddingModelDraft);
                                const provider = matched?.provider || gaConfig?.embedding_provider || "";
                                const providerId = matched?.provider_id || providers?.find((p) => p.provider === provider)?.id;
                                if (!provider || !model) return;
                                testEmbeddingModel.mutate(
                                  { provider, model, provider_id: providerId },
                                  {
                                    onSuccess: (res) => {
                                      const entered = parseInt(embeddingDimensionsDraft, 10);
                                      if (!Number.isFinite(entered) || entered <= 0) {
                                        setEmbeddingTestResult({
                                          capable: false,
                                          message: t_i18n("Enter a valid positive integer dimension before validating."),
                                          returnedDimensions: res.returned_dimensions,
                                          requestedDimensions: res.requested_dimensions,
                                          suggestedDimensions: res.suggested_dimensions ?? [],
                                          expectedDimensions: null,
                                        });
                                        return;
                                      }
                                      const detected = res.returned_dimensions;
                                      if (detected !== null && entered !== detected) {
                                        setEmbeddingTestResult({
                                          capable: false,
                                          message: t_i18n("Model returned {detected} dimensions but you entered {entered}. Update the field or use Detect to auto-fill.", { detected, entered }),
                                          returnedDimensions: detected,
                                          requestedDimensions: res.requested_dimensions,
                                          suggestedDimensions: res.suggested_dimensions ?? [],
                                          expectedDimensions: entered,
                                        });
                                      } else {
                                        setEmbeddingTestResult({
                                          capable: res.capable,
                                          message: res.capable
                                            ? t_i18n("Confirmed: model produces {detected} dimensions. Click Save to apply.", { detected: detected ?? "" })
                                            : res.message,
                                          returnedDimensions: detected,
                                          requestedDimensions: res.requested_dimensions,
                                          suggestedDimensions: res.suggested_dimensions ?? [],
                                          expectedDimensions: entered,
                                        });
                                      }
                                    },
                                    onError: (err) => {
                                      setEmbeddingTestResult({
                                        capable: false,
                                        message: err.message || t_i18n("Validation failed."),
                                        returnedDimensions: null,
                                        requestedDimensions: null,
                                        suggestedDimensions: [],
                                        expectedDimensions: null,
                                      });
                                    },
                                  },
                                );
                              }}
                            >
                              {t_i18n("Validate")}
                            </Button>
                          )}
                        </div>
                        {embeddingTestResult && embeddingTestResult.suggestedDimensions.length > 0 && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="content-caption text-default-secondary">{t_i18n("Suggested:")}</span>
                            {embeddingTestResult.suggestedDimensions.map((dim) => (
                              <Button
                                key={dim}
                                variant="outline"
                                size="sm"
                                className="h-6 px-2 content-caption"
                                onClick={() => {
                                  setEmbeddingDimensionsDraft(String(dim));
                                  setEmbeddingTestResult((prev) => prev ? { ...prev, expectedDimensions: dim } : prev);
                                }}
                              >
                                {dim}
                              </Button>
                            ))}
                          </div>
                        )}
                      </SettingsField>
                      {embeddingTestResult && (
                        <div
                          className={cn(
                            "rounded-sm border p-2.5 content-compact",
                            embeddingTestResult.capable
                              ? "border-feedback-success-primary/30 bg-feedback-success-primary/[0.08] text-feedback-success-primary"
                              : "border-feedback-error-primary/30 bg-feedback-error-primary/[0.08] text-feedback-error-primary",
                          )}
                        >
                          <p className="font-medium">{embeddingTestResult.capable ? t_i18n("Embedding test passed") : t_i18n("Embedding test failed")}</p>
                          <p>{embeddingTestResult.message}</p>
                          {embeddingTestResult.returnedDimensions !== null && (
                            <p className="mt-1 content-caption opacity-90">
                              {t_i18n("Detected: {count} dimensions", { count: embeddingTestResult.returnedDimensions })}
                              {embeddingTestResult.requestedDimensions !== null && (
                                <> | {t_i18n("Requested: {count}", { count: embeddingTestResult.requestedDimensions })}</>
                              )}
                              {embeddingTestResult.expectedDimensions !== null && embeddingTestResult.expectedDimensions !== embeddingTestResult.returnedDimensions && (
                                <> | {t_i18n("Configured: {count}", { count: embeddingTestResult.expectedDimensions })}</>
                              )}
                            </p>
                          )}
                        </div>
                      )}
                      <SettingsSaveBar
                        dirty={
                          embeddingModelDraft !== modelChoiceValue(gaConfig?.embedding_model, gaConfig?.embedding_provider_id)
                          || embeddingDimensionsDraft !== (gaConfig?.embedding_dimensions ? String(gaConfig.embedding_dimensions) : "")
                        }
                        saving={updateGAConfig.isPending}
                        onSave={saveEmbeddingConfig}
                        onCancel={() => { setEmbeddingEditing(false); setEmbeddingTestResult(null); setEmbeddingDimensionsDraft(""); }}
                      />
                        </div>
                      )}
                </div>
              ) : (
                <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 flex items-start gap-3 backdrop-blur-md shadow-inner">
                  <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
                    <Info className="h-3 w-3 text-default-secondary" />
                  </div>
                  <p className="text-[13px] text-default-secondary leading-relaxed">
                    {t_i18n("No embedding-capable provider configured. Knowledge base operations (embedding, semantic search, style learning) require a provider with embedding support (OpenAI, Gemini, Azure OpenAI, Mistral, Bedrock, Ollama, or a Custom OpenAI-compatible endpoint).")}
                    {canAddProviders && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
                  </p>
                </div>
              );
            })()}
          </div>
          </div>
          </>)}

          {aiSection === "media" && (<>
          <div className="rounded-sm border border-feedback-error-primary/15 bg-gradient-to-br from-feedback-error-primary/[0.03] to-transparent p-5">

          {/* ─── Image Generation ─── */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <div>
                <SettingsGroupTitle icon={Image}>{t_i18n("Image Generation")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("Used to generate images from text prompts. Requires OpenAI, Gemini or an Azure OpenAI image deployment (other providers do not support image generation).")}
                </SettingsGroupDescription>
              </div>
              <div className="flex gap-2 shrink-0 ml-4">
                {(() => {
                  if (imageEnvLocked) return <EnvLockBadge />;
                  // Capability-based (mirrors the Embedding section): any provider
                  // that surfaces image models — including the Filigran AI Gateway
                  // fronting OpenAI/Gemini — enables the configurator.
                  return isAdmin && (availableImageModels ?? []).length > 0 ? (
                    !imageEditing ? (
                      <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => { setImageModelDraft(modelChoiceValue(gaConfig?.image_model, gaConfig?.image_provider_id)); setImageEditing(true); void refetchImageModels(); }}>
                        <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
                      </Button>
                    ) : null
                  ) : null;
                })()}
              </div>
            </div>
            {(() => {
              const imageProvider = selectedImageProvider;
              // Mirror the Embedding section pattern: keep the editor reachable
              // as soon as the backend has discovered at least one image model,
              // even when the platform default (gaConfig.image_*) is still unset.
              // Otherwise the "Configure" header button toggles state but the
              // dropdown lives on the unreachable branch of this conditional
              // and the admin has no way to ever set a default.
              const hasImageModels = (availableImageModels ?? []).length > 0;
              const hasConfiguredImageProvider = !!imageProvider || !!gaConfig?.image_provider;
              const fallbackImageProviderKey = !imageProvider
                ? (gaConfig?.image_provider || availableImageModels?.[0]?.provider)
                : null;
              const imageProviderName = imageProvider
                ? (PROVIDER_DISPLAY_NAMES[imageProvider.provider] || imageProvider.provider)
                : fallbackImageProviderKey
                  ? (PROVIDER_DISPLAY_NAMES[fallbackImageProviderKey] || fallbackImageProviderKey)
                  : null;
              const currentImageModel = gaConfig?.image_model || "—";
              const hasActiveImageModel = currentImageModel !== "—";
              return hasImageModels || hasConfiguredImageProvider ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
                        <Image className="h-4 w-4 text-filigran-ia-primary" />
                      </div>
                      <div>
                        <p className="content-base font-medium">
                          {hasActiveImageModel ? currentImageModel : t_i18n("No image model selected")}
                        </p>
                        <span className="content-compact text-default-secondary">
                          {imageProviderName
                            ? t_i18n("Image Provider: {provider}", { provider: imageProviderName })
                            : t_i18n('Click "Configure" to pick a default image model')}
                        </span>
                      </div>
                    </div>
                    {hasActiveImageModel && (
                      <Badge variant="success" className="content-caption"><Check className="h-3 w-3 mr-1" />{t_i18n("Active")}</Badge>
                    )}
                  </div>
                  {imageEditing && (
                    <>
                      <ModelListRefreshing active={refreshingImageModels} />
                      <SettingsField id={`${fieldId}-image-model`} label={t_i18n("Image Generation Model")} helper={t_i18n("Model used for generating images from text prompts.")}>
                        <ModelSelect
                          id={`${fieldId}-image-model`}
                          aria-label={t_i18n("Image Generation Model")}
                          aria-describedby={settingsFieldHintId(`${fieldId}-image-model`)}
                          models={availableImageModels}
                          providers={providers}
                          pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                          value={imageModelDraft || "__none__"}
                          onValueChange={(v) => setImageModelDraft(v === "__none__" ? "" : v)}
                        />
                      </SettingsField>
                      <SettingsSaveBar
                        dirty={imageModelDraft !== modelChoiceValue(gaConfig?.image_model, gaConfig?.image_provider_id)}
                        saving={updateGAConfig.isPending}
                        onCancel={() => setImageEditing(false)}
                        onSave={() => {
                          const model = parseModelChoice(imageModelDraft || "").model || "";
                          const matchedModel = model
                            ? (chosenModel(availableImageModels ?? [], imageModelDraft) ?? undefined)
                            : undefined;
                          // For "Auto (provider default)" (empty model), fall back to the
                          // first discovered image-capable provider so the platform at
                          // least gets a provider binding: otherwise Save would silently
                          // clear the binding, which is exactly the bug to avoid.
                          const fallback = !model ? (availableImageModels ?? [])[0] : undefined;
                          const prov = matchedModel?.provider ?? fallback?.provider ?? "";
                          const provId =
                            parseModelChoice(imageModelDraft || "").providerId
                            ?? matchedModel?.provider_id
                            ?? fallback?.provider_id
                            ?? null;
                          updateGAConfig.mutate(
                            { image_provider: prov, image_provider_id: provId, image_model: model },
                            { onSuccess: () => setImageEditing(false) },
                          );
                        }}
                      />
                    </>
                  )}
                </div>
              ) : (
                <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 flex items-start gap-3 backdrop-blur-md shadow-inner">
                  <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
                    <Info className="h-3 w-3 text-default-secondary" />
                  </div>
                  <p className="text-[13px] text-default-secondary leading-relaxed">
                    {t_i18n("No image-capable provider configured. Image generation requires OpenAI, Gemini or an Azure OpenAI image deployment.")}
                    {canAddProviders && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
                  </p>
                </div>
              );
            })()}
          </div>
          </div>

          {videoStudioEnabled && (
          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Video Generation ─── */}
          <VideoGenerationSection
            gaConfig={gaConfig}
            providers={providers}
            isAdmin={isAdmin}
            canAddProviders={canAddProviders}
            envLocked={videoEnvLocked}
          />
          </div>
          )}

          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Speech Generation ─── */}
          <SpeechGenerationSection
            gaConfig={gaConfig}
            providers={providers}
            isAdmin={isAdmin}
            canAddProviders={canAddProviders}
            envLocked={speechEnvLocked}
          />
          </div>

          {videoStudioEnabled && (
          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Pronunciation lexicon ─── */}
          <PronunciationLexiconSection isAdmin={isAdmin} />
          </div>
          )}

          {videoStudioEnabled && (
          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Render retention ─── */}
          <RenderRetentionSection isAdmin={isAdmin} />
          </div>
          )}

          {videoStudioEnabled && (
          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Music Generation ─── */}
          <MusicGenerationSection
            gaConfig={gaConfig}
            providers={providers}
            isAdmin={isAdmin}
            canAddProviders={canAddProviders}
            envLocked={musicEnvLocked}
          />
          </div>
          )}
          </>)}

          {aiSection === "models" && (<>
          <div className="rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5">
          {/* ─── Decision Model ─── */}
          <PlatformModelSetting
            fieldId={`${fieldId}-decision`}
            icon={MousePointerClick}
            tileClassName="bg-accent/10"
            iconClassName="text-accent"
            title={t_i18n("Decision Model")}
            description={t_i18n("Decision model (e.g. Clef Flash) that chooses each click when an agent acts on the page of an embedded chat. Agents cannot act on pages while it is not set.")}
            emptyMessage={t_i18n("No provider here lists a decision model. Add one that serves them (Ollama, OpenRouter, OpenAI, Filigran AI Gateway or a custom OpenAI-compatible endpoint), or check that a provider already added declares one.")}
            pickHint={t_i18n("A decision model is given a list of choices, such as the elements on a page, and returns one of them instead of writing a reply.")}
            noneLabel={t_i18n("Not configured")}
            models={decisionModels}
            providers={providers}
            model={gaConfig?.decision_model}
            providerId={gaConfig?.decision_provider_id}
            setSubtitle={gaConfig?.decision_model ? t_i18n("Decision model: {provider}", { provider: configuredModelProvider(gaConfig.decision_model, gaConfig.decision_provider_id) }) : ""}
            unsetSubtitle={t_i18n("Agents in embedded chats cannot act on the host page")}
            envLocked={decisionEnvLocked}
            canEdit={isAdmin}
            isSaving={updateGAConfig.isPending}
            typedProviders={typedDecisionProviders(providers)}
            keepUnlisted
            notes={decisionListingNotes(providers, decisionListing).map(({ provider: p, note }) => t_i18n(note, { provider: providerDisplayName(p.provider, p.id, providers) }))}
            onSave={(model, providerId, done) => updateGAConfig.mutate({ decision_model: model, decision_provider_id: providerId }, { onSuccess: done })}
            onOpen={() => void refetchDecisionModels()}
            refreshing={refreshingDecisionModels}
            loading={decisionModelsPending}
          />
          </div>
          </>)}

          <div className="border-t border-elevation-subtle-soft" />

          {/* ─── Add Provider Form ─── */}
          <Dialog open={isAddProviderOpen && canAddProviders && addProviderMode !== null} onOpenChange={(open) => !open && closeAddProvider()}>
            <DialogContent className="sm:max-w-xl max-h-[85vh] overflow-y-auto">
              <DialogHeader>
                <DialogTitle>{t_i18n(addProviderMode ? addProviderTitleByMode[addProviderMode] : "Add Provider")}</DialogTitle>
                <DialogDescription>
                  {addProviderMode === "reasoning"
                    ? t_i18n("Add an LLM provider and select models for agent capabilities.")
                    : addProviderMode === "embedding"
                      ? t_i18n("Add an embedding provider. If model discovery is unavailable, connection-only validation is supported for OpenAI-compatible or TEI endpoints.")
                      : addProviderMode === "image"
                        ? t_i18n("Add an image provider for image generation models.")
                        : t_i18n("Configure a new AI provider and fetch its available models.")}
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4 mt-2">

              {/* Step 1: Provider + API Key */}
              <div className="space-y-5">
                <div className="space-y-2">
                  <label htmlFor="add-provider-provider" className="content-base font-medium text-default-primary">{t_i18n("Provider")}</label>
                  <SearchableSelect
                    id="add-provider-provider"
                    aria-label={t_i18n("Provider")}
                    aria-describedby={`${fieldId}-provider-hint`}
                    options={providerOptionsForMode.map((key) => ({ value: key, label: PROVIDER_DISPLAY_NAMES[key] || key, keywords: [key] }))}
                    value={newProvider}
                    onValueChange={(v) => {
                      setNewProvider(v);
                      resetProviderValidation();
                      setCustomBaseUrl("");
                      setGatewayToken("");
                      setVertexLocation("");
                    }}
                  />
                  <div id={`${fieldId}-provider-hint`}>
                  {(newProvider === "openai" || newProvider === "gemini" || newProvider === "mistral") && (
                    <p className="content-caption text-feedback-success-primary/80">{t_i18n("{provider} supports both chat and embedding operations.", { provider: PROVIDER_DISPLAY_NAMES[newProvider] })}</p>
                  )}
                  {newProvider === "anthropic" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Anthropic is chat-only. Knowledge base embeddings still need an embedding-capable provider, such as OpenAI, Gemini, Mistral, Bedrock or Ollama.")}</p>
                  )}
                  {newProvider === "xai" && (
                    <p className="content-caption text-default-secondary">{t_i18n("SpaceXAI (Grok) is chat-only. Knowledge base embeddings still need an embedding-capable provider, such as OpenAI, Gemini, Mistral, Bedrock or Ollama.")}</p>
                  )}
                  {newProvider === "azure_openai" && (
                    <p className="content-caption text-default-secondary">{t_i18n("API key format:")} <code>endpoint||api_key||api_version</code> (e.g. <code>https://myresource.openai.azure.com||sk-...||2024-10-21</code>)</p>
                  )}
                  {newProvider === "bedrock" && (
                    <p className="content-caption text-default-secondary">{t_i18n("API key format:")} <code>aws_access_key||aws_secret_key||region</code> (e.g. <code>AKIA...||wJal...||us-east-1</code>)</p>
                  )}
                  {newProvider === "ollama" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Enter your Ollama server URL")} (e.g. <code>http://localhost:11434</code>). {t_i18n("Leave default for local Ollama.")}</p>
                  )}
                  {newProvider === "litellm" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Connect to a self-hosted LiteLLM proxy. Enter the base URL and API key separately.")}</p>
                  )}
                  {newProvider === "openrouter" && (
                    <p className="content-caption text-feedback-success-primary/80">{t_i18n("OpenRouter provides access to hundreds of models (Claude, GPT, Gemini, Llama, Mistral, etc.) through a single API key.")}</p>
                  )}
                  {newProvider === "custom_openai" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Works with vLLM, TGI, LocalAI, LM Studio, and any OpenAI-compatible endpoint.")}</p>
                  )}
                  {newProvider === "tei" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Works with any embedding server exposing")} <code>POST /embed</code>.</p>
                  )}
                  {newProvider === "elevenlabs" && (
                    <p className="content-caption text-default-secondary">{t_i18n("No chat: its speech models and voice library (cloned voices included), Eleven Music, and its image and video models (Pro plan) are offered in the generation sections.")}</p>
                  )}
                  {newProvider === "kling" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Video only: enter a key of the Kling AI API platform (api-key-kling-...). Its models are chosen under Video Generation.")}</p>
                  )}
                  {newProvider === "runway" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Video only: enter a Runway API key (key_...). Its models, Gen-4.5 first, are chosen under Video Generation.")}</p>
                  )}
                  {newProvider === "seedance" && (
                    <p className="content-caption text-default-secondary">{t_i18n("Video only: enter a BytePlus ModelArk API key, with the Seedance models activated in the ModelArk console. Its models are chosen under Video Generation.")}</p>
                  )}
                  {isVertexProvider && (
                    <p className="content-caption text-default-secondary">{t_i18n("Video only (Veo): load the JSON key of a Google Cloud service account, in a project with the Vertex AI API enabled, granted the Vertex AI User role (roles/aiplatform.user). Its Veo models are chosen under Video Generation.")}</p>
                  )}
                  {newProvider === "filigran_gateway" && (
                    <p className="content-caption text-default-secondary">{t_i18n("By default this platform authenticates with a signed XTM JWT on every request. Enter the gateway URL below, and a token only if the gateway requires one.")}</p>
                  )}
                  </div>
                  <IntegrationFedProviderHint llmProvider={newProvider} integrations={gaIntegrations ?? []} providers={providers ?? []} />
                </div>

                <div className="space-y-2">
                  <label htmlFor="add-provider-display-name" className="content-base font-medium text-default-primary">{t_i18n("Display Name")} <span className="content-caption text-default-secondary font-normal">{t_i18n("(optional)")}</span></label>
                  <Input aria-label={t_i18n("Display Name")}
                    id="add-provider-display-name"
                    aria-describedby={`${fieldId}-display-name-hint`}
                    value={newDisplayName}
                    onChange={(e) => setNewDisplayName(e.target.value)}
                    placeholder={PROVIDER_DISPLAY_NAMES[newProvider] || newProvider}
                  />
                  <p id={`${fieldId}-display-name-hint`} className="content-compact text-default-secondary">
                    {t_i18n("Custom name to distinguish this provider if you have multiple connections of the same type.")}
                  </p>
                </div>

                {/* Base URL field — shown for custom_openai, litellm, tei and the media providers */}
                {hasBaseUrlField && (
                  <div className="space-y-2">
                    <label htmlFor="add-provider-base-url" className="content-base font-medium text-default-primary">
                      {t_i18n("Base URL")}
                      {baseUrlOptional && <span className="content-caption text-default-secondary font-normal"> {t_i18n("(optional)")}</span>}
                    </label>
                    <Input aria-label={t_i18n("Base URL")}
                      id="add-provider-base-url"
                      aria-describedby={`${fieldId}-base-url-hint`}
                      type="text"
                      value={customBaseUrl}
                      onChange={(e) => {
                        setCustomBaseUrl(e.target.value);
                        resetProviderValidation();
                      }}
                      placeholder={
                        newProvider === "litellm" ? "http://localhost:4000" :
                        newProvider === "elevenlabs" ? "https://api.elevenlabs.io" :
                        newProvider === "kling" ? "https://api-singapore.klingai.com" :
                        newProvider === "seedance" ? "https://ark.ap-southeast.bytepluses.com/api/v3" :
                        "https://my-server.example.com"
                      }
                    />
                    <p id={`${fieldId}-base-url-hint`} className="content-compact text-default-secondary">
                      {newProvider === "litellm"
                        ? t_i18n("Full URL to your LiteLLM proxy (e.g. http://localhost:4000).")
                        : newProvider === "tei"
                          ? t_i18n("Full URL to your embedding endpoint (e.g. https://embedder.example.com).")
                          : newProvider === "elevenlabs"
                            ? t_i18n("Leave empty for the global endpoint, or set a regional one for data residency (e.g. https://api.eu.residency.elevenlabs.io).")
                            : newProvider === "kling"
                              ? t_i18n("Leave empty for the global endpoint, or set another Kling AI endpoint (e.g. https://api.klingai.com).")
                              : newProvider === "seedance"
                                ? t_i18n("Leave empty for BytePlus ModelArk, or set another endpoint (e.g. https://ark.cn-beijing.volces.com/api/v3 for Volcengine Ark).")
                                : t_i18n("Full URL to the OpenAI-compatible endpoint (e.g. https://my-server.example.com or https://my-server.example.com/v1).")}
                    </p>
                  </div>
                )}

                {/* Gateway URL field — the Filigran AI Gateway needs no API key
                    (per-request XTM JWT), so the URL is the only input. */}
                {isGatewayProvider && (
                  <div className="space-y-2">
                    <label htmlFor="add-provider-gateway-url" className="content-base font-medium text-default-primary">{t_i18n("Gateway URL")}</label>
                    <div className="flex gap-2">
                      <Input aria-label={t_i18n("Gateway URL")}
                        id="add-provider-gateway-url"
                        aria-describedby={`${fieldId}-gateway-url-hint`}
                        type="text"
                        className="flex-1"
                        value={customBaseUrl}
                        onChange={(e) => {
                          setCustomBaseUrl(e.target.value);
                          resetProviderValidation();
                        }}
                        placeholder="https://gateway.filigran.io"
                      />
                      <Button
                        variant="secondary"
                        className="shrink-0 gap-1.5"
                        onClick={handleValidateProvider}
                        disabled={!credentialFilled || validateProvider.isPending || aiOff} aria-busy={validateProvider.isPending || undefined}>
                        {validateProvider.isPending && <Spinner size="sm" tone="inherit" />}{!validateProvider.isPending && <Check className="h-3 w-3" />}
                        {validateProvider.isPending ? t_i18n("Validating...") : t_i18n("Validate Provider")}
                      </Button>
                    </div>
                    <p id={`${fieldId}-gateway-url-hint`} className="content-compact text-default-secondary">
                      {t_i18n("Without a token, authenticates with this platform's XTM signing key: the gateway must trust this platform's BASE_URL as an issuer.")}
                    </p>
                    <label htmlFor="add-provider-gateway-token" className="content-base font-medium text-default-primary">{t_i18n("Token (optional)")}</label>
                    <SecretField
                      aria-label={t_i18n("Token (optional)")}
                      id="add-provider-gateway-token"
                      aria-describedby={`${fieldId}-gateway-token-hint`}
                      value={gatewayToken}
                      onChange={(value) => {
                        setGatewayToken(value);
                        resetProviderValidation();
                      }}
                    />
                    <p id={`${fieldId}-gateway-token-hint`} className="content-compact text-default-secondary">
                      {t_i18n("When set, it is sent to the gateway instead of the XTM JWT.")}
                    </p>
                  </div>
                )}

                {isVertexProvider && (
                  <div className="space-y-2">
                    <label htmlFor="add-provider-vertex-location" className="content-base font-medium text-default-primary">
                      {t_i18n("Location")}
                      <span className="content-caption text-default-secondary font-normal"> {t_i18n("(optional)")}</span>
                    </label>
                    <Input aria-label={t_i18n("Location")}
                      id="add-provider-vertex-location"
                      aria-describedby={`${fieldId}-vertex-location-hint`}
                      type="text"
                      value={vertexLocation}
                      onChange={(e) => {
                        setVertexLocation(e.target.value);
                        resetProviderValidation();
                      }}
                      placeholder="us-central1"
                    />
                    <p id={`${fieldId}-vertex-location-hint`} className="content-compact text-default-secondary">
                      {t_i18n("Leave empty for us-central1, the region serving Veo 3.1.")}
                    </p>
                  </div>
                )}

                {!isGatewayProvider && (
                <div className="space-y-2">
                  <label htmlFor="add-provider-api-key" className="content-base font-medium text-default-primary">{isVertexProvider ? t_i18n("Service account key (JSON)") : t_i18n("API key")}</label>
                  <div className="flex gap-2">
                    <SecretField aria-label={isVertexProvider ? t_i18n("Service account key (JSON)") : t_i18n("API key")}
                      id="add-provider-api-key"
                      aria-describedby={
                        isVertexProvider
                          ? `${fieldId}-service-account-hint`
                          : hasBaseUrlField && !baseUrlOptional ? `${fieldId}-api-key-hint` : undefined
                      }
                      inputRef={addProviderApiKeyRef}
                      className="flex-1"
                      value={effectiveApiKey}
                      disabled={noApiKeyRequired}
                      allowVault={!noApiKeyRequired}
                      onChange={(value) => {
                        setNewApiKey(value);
                        resetProviderValidation();
                      }}
                      placeholder={
                        newProvider === "anthropic" ? "sk-ant-..." :
                        newProvider === "gemini" ? "AIza..." :
                        newProvider === "xai" ? "xai-..." :
                        newProvider === "azure_openai" ? "endpoint||api_key||api_version" :
                        newProvider === "bedrock" ? "access_key||secret_key||region" :
                        newProvider === "ollama" ? "http://localhost:11434" :
                        newProvider === "litellm" ? "sk-..." :
                        newProvider === "custom_openai" ? "sk-... or none" :
                        newProvider === "tei" ? "hf_... or none" :
                        newProvider === "elevenlabs" ? "sk_..." :
                        newProvider === "kling" ? "api-key-kling-..." :
                        newProvider === "runway" ? "key_..." :
                        newProvider === "seedance" ? t_i18n("ModelArk API key") :
                        isVertexProvider ? '{"type": "service_account", ...}' :
                        "sk-..."
                      }
                    />
                    {isVertexProvider && (
                      <>
                        <input
                          ref={serviceAccountKeyFileRef}
                          type="file"
                          accept="application/json,.json"
                          className="hidden"
                          aria-label={t_i18n("Load JSON key file")}
                          onChange={(e) => {
                            void loadServiceAccountKeyFile(e.target.files?.[0]);
                            e.target.value = "";
                          }}
                        />
                        <Button
                          variant="secondary"
                          className="shrink-0 gap-1.5"
                          onClick={() => serviceAccountKeyFileRef.current?.click()}
                        >
                          <Upload className="h-3 w-3" />
                          {t_i18n("Load JSON key file")}
                        </Button>
                      </>
                    )}
                    <Button
                      variant="secondary"
                      className="shrink-0 gap-1.5"
                      onClick={handleValidateProvider}
                      disabled={!credentialFilled || validateProvider.isPending || aiOff} aria-busy={validateProvider.isPending || undefined}>
                      {validateProvider.isPending && <Spinner size="sm" tone="inherit" />}{!validateProvider.isPending && <Check className="h-3 w-3" />}
                      {validateProvider.isPending ? t_i18n("Validating...") : t_i18n("Validate Provider")}
                    </Button>
                  </div>
                  {hasBaseUrlField && !baseUrlOptional && (
                    <p id={`${fieldId}-api-key-hint`} className="content-compact text-default-secondary">{t_i18n("Use")} <code>none</code> {t_i18n("or")} <code>dummy</code> {t_i18n("if authentication is not required.")}</p>
                  )}
                  {isVertexProvider && (
                    <p id={`${fieldId}-service-account-hint`} className="content-compact text-default-secondary">
                      {serviceAccount
                        ? t_i18n("Service account {email}, project {project}.", { email: serviceAccount.clientEmail, project: serviceAccount.projectId })
                        : t_i18n("Paste the service account key (JSON) or load its file: it stays masked.")}
                    </p>
                  )}
                  {!isVertexProvider && (
                  <div className="flex items-center gap-2 pt-1">
                    <Switch
                      id="add-provider-no-api-key"
                      checked={noApiKeyRequired}
                      onCheckedChange={(checked) => {
                        setNoApiKeyRequired(checked);
                        if (!checked && newApiKey.trim().toLowerCase() === "none") {
                          setNewApiKey("");
                        }
                        resetProviderValidation();
                      }}
                    />
                    <label htmlFor="add-provider-no-api-key" className="content-compact text-default-secondary">{t_i18n("No API key required")}</label>
                  </div>
                  )}
                </div>
                )}
              </div>

              {/* Error message */}
              {modelsError && (
                <div className="rounded-sm bg-feedback-error-primary/5 p-3 content-compact text-feedback-error-primary">
                  {modelsError}
                </div>
              )}

              {providerValidationMessage && !modelsError && (
                <div className={cn(
                  "rounded-sm p-3 content-compact",
                  providerValidated ? "bg-feedback-success-primary/5 text-feedback-success-primary" : "bg-feedback-alert-primary/5 text-feedback-warning-tertiary",
                )}>
                  {providerValidationMessage}
                </div>
              )}

              {embeddingModelFieldShown && (
                <div className="space-y-2">
                  <label htmlFor="add-provider-embedding-model" className="content-base font-medium text-default-primary">
                    {t_i18n("Embedding Model")}{" "}
                    {embeddingModelRequired ? (
                      <span aria-hidden="true" className="text-feedback-error-primary">*</span>
                    ) : (
                      <span className="text-default-secondary font-normal">{t_i18n("(optional)")}</span>
                    )}
                  </label>
                  <Input aria-label={t_i18n("Embedding Model")}
                    id="add-provider-embedding-model"
                    aria-required={embeddingModelRequired}
                    aria-describedby={`${fieldId}-add-embedding-model-hint`}
                    type="text"
                    value={newModel}
                    onChange={(e) => setNewModel(e.target.value)}
                    placeholder={newProvider === "tei" ? "Qwen/Qwen3-Embedding-0.6B" : "text-embedding-3-large"}
                  />
                  <p id={`${fieldId}-add-embedding-model-hint`} className="content-compact text-default-secondary">
                    {embeddingModelRequired
                      ? t_i18n("This provider exposes no model discovery, so a model id is required: it is stored as the platform embedding model and is what the embedding picker will list.")
                      : t_i18n("Used when provider model discovery is unavailable. This value is stored as the platform embedding model.")}
                  </p>
                </div>
              )}

              {/* Step 2: Model selection (reasoning mode only) */}
              {showsModelPicker && resolvedModels.length > 0 && (
                <>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <label id="add-provider-available-models" className="content-base font-medium text-default-primary">
                        {t_i18n("Available Models")}
                        <span className="ml-2 text-default-secondary font-normal">
                          {t_i18n("({selected}/{total} selected)", { selected: selectedModels.length, total: resolvedModels.length })}
                        </span>
                      </label>
                      <div className="flex items-center gap-2">
                        <button
                          className="content-caption text-default-secondary hover:text-accent transition-colors"
                          onClick={() => setSelectedModels([...resolvedModels])}
                        >
                          {t_i18n("Enable All")}
                        </button>
                        <span className="text-default-disabled content-caption">|</span>
                        <button
                          className="content-caption text-default-secondary hover:text-accent transition-colors"
                          onClick={() => { setSelectedModels([]); setNewModel(""); }}
                        >
                          {t_i18n("Disable All")}
                        </button>
                      </div>
                    </div>
                    <p id={`${fieldId}-available-models-hint`} className="content-compact text-default-secondary">
                      {modelGroups
                        ? t_i18n("These models are served through the gateway, grouped by their upstream provider. Enable the ones this connection may use.")
                        : t_i18n("Select which models to make available for agents and API proxy usage.")}
                    </p>
                    <div
                      role="group"
                      aria-labelledby="add-provider-available-models"
                      aria-describedby={`${fieldId}-available-models-hint`}
                      className="max-h-64 overflow-y-auto rounded-sm bg-elevation-default p-2 space-y-1"
                    >
                      {modelGroups
                        ? Object.entries(modelGroups)
                            .sort(([a], [b]) => a.localeCompare(b))
                            .map(([owner, models]) => (
                              <div key={owner} role="group" aria-label={PROVIDER_DISPLAY_NAMES[owner] || owner} className="space-y-1">
                                <div className="flex items-center gap-2 px-1 pt-1">
                                  <span className="content-caption font-semibold uppercase tracking-wide text-accent">
                                    {PROVIDER_DISPLAY_NAMES[owner] || owner}
                                  </span>
                                  <span className="h-px flex-1 bg-[var(--border-elevation-subtle)]" />
                                  <span className="content-caption text-default-secondary">{models.length}</span>
                                </div>
                                {[...models].sort((a, b) => a.localeCompare(b)).map((m) => (
                                  <div key={m} className="flex items-center gap-2 px-2 py-1 rounded-sm hover:bg-elevation-hover content-compact">
                                    <Switch aria-label={m} checked={selectedModels.includes(m)} onCheckedChange={() => toggleModelSelection(m)} />
                                    <span className="flex-1">{m}</span>
                                    {m === newModel && !gaConfig?.agent_default_model && <Badge variant="cyan" className="content-caption">{t_i18n("default")}</Badge>}
                                  </div>
                                ))}
                              </div>
                            ))
                        : [...resolvedModels].sort((a, b) => a.localeCompare(b)).map((m) => (
                            <div key={m} className="flex items-center gap-2 px-2 py-1 rounded-sm hover:bg-elevation-hover content-compact">
                              <Switch
                                aria-label={m}
                                checked={selectedModels.includes(m)}
                                onCheckedChange={() => toggleModelSelection(m)}
                              />
                              <span className="flex-1">{m}</span>
                              {m === newModel && !gaConfig?.agent_default_model && <Badge variant="cyan" className="content-caption">{t_i18n("default")}</Badge>}
                            </div>
                          ))}
                    </div>
                  </div>

                  {selectedModels.length > 0 && !gaConfig?.agent_default_model && (
                    <div className="space-y-2">
                      <label htmlFor="add-provider-default-model" className="content-base font-medium text-default-primary">{t_i18n("Agent Default Model")}</label>
                      <p id={`${fieldId}-default-model-hint`} className="content-compact text-default-secondary">{t_i18n("No agent default is set yet. Choose the fallback model used by agents when no override is configured.")}</p>
                      <SearchableSelect
                        id="add-provider-default-model"
                        aria-label={t_i18n("Agent Default Model")}
                        aria-describedby={`${fieldId}-default-model-hint`}
                        options={selectedModels.map((m) => ({ value: m, label: m }))}
                        value={newModel}
                        onValueChange={setNewModel}
                        placeholder={t_i18n("Select platform default...")}
                      />
                    </div>
                  )}

                  <div className="flex gap-2">
                    <Button
                      onClick={handleAddLLM}
                      disabled={!credentialFilled || selectedModels.length === 0 || !providerValidated || addProvider.isPending || aiOff}
                    >
                      {addProvider.isPending ? (
                        <><Spinner size="sm" tone="inherit" />{t_i18n("Saving...")}</>
                      ) : (
                        t_i18n("Save Provider")
                      )}
                    </Button>
                    <Button variant="ghost" onClick={closeAddProvider}>{t_i18n("Cancel")}</Button>
                  </div>
                </>
              )}

              {/* Fallback Save for flows without the model picker (embedding,
                  image, embedding-only providers). Reasoning/"all" use the
                  picker block's own Save. */}
              {!showsModelPicker && (
                <div className="flex gap-2">
                  <Button
                    onClick={handleAddLLM}
                    disabled={
                      !credentialFilled ||
                      !providerValidated ||
                      (embeddingModelRequired && !newModel.trim()) ||
                      addProvider.isPending
                     || aiOff}
                  >
                    {addProvider.isPending ? (
                      <><Spinner size="sm" tone="inherit" />{t_i18n("Saving...")}</>
                    ) : (
                      t_i18n("Save Provider")
                    )}
                  </Button>
                  <Button variant="ghost" onClick={closeAddProvider}>{t_i18n("Cancel")}</Button>
                </div>
              )}

              {showsModelPicker && resolvedModels.length === 0 && (
                <div className="flex gap-2">
                  <Button variant="ghost" onClick={closeAddProvider}>{t_i18n("Cancel")}</Button>
                </div>
              )}
            </div>
          </DialogContent>
        </Dialog>
      </div></AiReadOnly>}

      {/* ================================================================ */}
      {/* AI and models: General Assistant and workspace knowledge          */}
      {/* ================================================================ */}
      {tab === "ai" && aiSection === "assistant" && (
        <AiReadOnly>
          <GeneralAssistantSection
          gaConfig={gaConfig ?? null}
          updateGAConfig={updateGAConfig}
          availableModels={availableModels ?? []}
          providers={providers ?? []}
          integrations={gaIntegrations ?? []}
          mcpServers={gaMcpServers ?? []}
          kbs={gaKbs ?? []}
          customTools={gaCustomTools ?? []}
          allAgents={gaAllAgents ?? []}
          gaEditing={gaEditing}
          setGaEditing={setGaEditing}
          gaPrompt={gaPrompt}
          setGaPrompt={setGaPrompt}
          gaModel={gaModel}
          setGaModel={setGaModel}
          gaMaxIterations={gaMaxIterations}
          setGaMaxIterations={setGaMaxIterations}
          gaKbIds={gaKbIds}
          setGaKbIds={setGaKbIds}
          gaMcpIds={gaMcpIds}
          setGaMcpIds={setGaMcpIds}
          gaToolIds={gaToolIds}
          setGaToolIds={setGaToolIds}
          gaBuiltinToolIds={gaBuiltinToolIds}
          setGaBuiltinToolIds={setGaBuiltinToolIds}
          gaBindings={gaBindings}
          setGaBindings={setGaBindings}
          gaHandoverIds={gaHandoverIds}
          setGaHandoverIds={setGaHandoverIds}
          gaHandoverTypes={gaHandoverTypes}
          setGaHandoverTypes={setGaHandoverTypes}
          />
        </AiReadOnly>
      )}
      {tab === "ai" && aiSection === "knowledge" && (
        <AiReadOnly>
          <WorkspaceKnowledgeSettingsCard />
        </AiReadOnly>
      )}

      {/* ================================================================ */}
      {/* Agents and automations                                           */}
      {/* ================================================================ */}
      {tab === "automations" && automations === "fleets" && <FleetsSettingsSection />}
      {tab === "automations" && automations === "ctem-agents" && (
        <AiReadOnly>
          <CtemStageAgentsCard />
        </AiReadOnly>
      )}
      {tab === "automations" && automations === "agent-runs" && (
        <AiReadOnly>
          <div className="rounded-sm border border-elevation-subtle-soft p-5">
            <AgentRunsSettings fieldId={`${fieldId}-agent-runs`} canEdit={isAdmin} />
          </div>
        </AiReadOnly>
      )}
      {/* Not wrapped: rating agent work and defining what it is scored on
          involve no model, so they stay available with AI off. */}
      {tab === "automations" && automations === "quality" && <FeedbackCriteriaSection />}
      {tab === "automations" && automations === "health" && <HealthSection />}

      {/* ================================================================ */}
      {/* Integrations: catalog, OAuth defaults, channels, MCP endpoint    */}
      {/* ================================================================ */}
      {tab === "integrations" && integrationsSection === "catalog" && <IntegrationCatalogSection />}
      {tab === "integrations" && integrationsSection === "oauth" && <OAuthDefaultsSection />}
      {tab === "integrations" && integrationsSection === "channels" && <ChannelManagement isAdmin={isAdmin} />}
      {/* Not wrapped: MCPEndpointSection gates its own write controls and keeps
          the copy buttons live: copying the endpoint config is a read. */}
      {tab === "integrations" && integrationsSection === "mcp" && <MCPEndpointSection />}

      {/* ================================================================ */}
      {/* Access and security                                              */}
      {/* ================================================================ */}
      {tab === "security" && <SecuritySection />}

      {/* ================================================================ */}
      {/* Usage and quotas                                                 */}
      {/* ================================================================ */}
      {/* Not wrapped: QuotasSection gates its own Save and keeps its period
          switchers, charts and licence actions live (FR-022). */}
      {tab === "usage" && usageSection === "quotas" && <QuotasSection />}
      {tab === "usage" && usageSection === "observability" && (
        <AiReadOnly>
          <ObservabilitySection />
        </AiReadOnly>
      )}
      {/* Outside the read-only fieldset on purpose: a disabled fieldset
          disables *every* descendant control, and this table's search and
          pagination are how it is read. Its own writes are admin-gated. */}
      {tab === "usage" && usageSection === "pricing" && <ModelPricingSection />}
      {/* Storage & retention (#396): how long each category of stored file is kept. No model: not wrapped. */}
      {tab === "usage" && usageSection === "storage" && <FileRetentionSection isAdmin={isAdmin} />}
    </div>
  );
}
