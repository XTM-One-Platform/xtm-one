import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bot, Brain, ChevronDown, ChevronRight, Eye, Link2, Server, Star, Unlink, Wrench,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Switch } from "@/components/ui/switch";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useModelAnnouncementStatus } from "@/hooks/api/useSettingsApi";
import {
  useLinkOpenRouterProvider,
  useOpenRouterModelEndpoints,
  useOpenRouterModels,
  useOpenRouterOverview,
  useUnlinkOpenRouterProvider,
  useUpdateOpenRouterProviderModels,
} from "@/hooks/api/useOpenRouterApi";
import { useFormatter } from "@/i18n";
import { canEmailUsers } from "@/lib/model-announcements";
import { cn } from "@/lib/utils";
import { ModelChangesDialog } from "@/pages/settings/ModelAnnouncements";
import type {
  Integration, LLMProvider, ModelCatalogChange, OpenRouterCatalogKind, OpenRouterEndpoint, OpenRouterModel,
} from "@/types";
import {
  catalogAuthors,
  formatPerMillion,
  formatTokens,
  orderedSelection,
  selectionChanged,
} from "./openrouter-console";
import { ConsoleErrorNote } from "./OpenRouterUsageTab";

/** What a model can do, as a filter value; every label is a catalog key `validate-i18n` checks. @i18n-keys */
const CAPABILITY_LABELS = {
  tools: "Calls tools",
  reasoning: "Reasons before answering",
  images: "Reads images",
  free: "Free",
} as const satisfies Record<ModelCapability, string>;

type ModelCapability = "tools" | "reasoning" | "images" | "free";

/** Whether the platform provider offers a model, as a filter value. @i18n-keys */
const OFFER_LABELS = {
  offered: "Offered",
  not_offered: "Not offered",
} as const satisfies Record<"offered" | "not_offered", string>;

function capabilitiesOf(model: OpenRouterModel): ModelCapability[] {
  const capabilities: ModelCapability[] = [];
  if (model.supports_tools) capabilities.push("tools");
  if (model.supports_reasoning) capabilities.push("reasoning");
  if (model.input_modalities.includes("image")) capabilities.push("images");
  if (model.is_free) capabilities.push("free");
  return capabilities;
}

/**
 * Column widths under the fixed layout (slice 7 of #9378): with a width on each column the
 * catalog keeps one row height and scrolls sideways inside its card on a narrow screen.
 */
export const MODEL_COLUMN_WIDTHS = {
  name: 320,
  capabilities: 140,
  figure: 104,
  date: 120,
  offered: 112,
} as const;

/**
 * Column widths of the providers table in an opened row (UX round 1 of #10686, B1): under the
 * fixed layout a long provider name and its tool icon stay on one line, so every row keeps
 * one height at 1080 as at 1728; the table scrolls sideways inside the card when narrower.
 * Every header, with its sort icon, fits its column in each locale (UX round 2 of #10686, B1:
 * "Disponibilité 30 min" ran 19 px into the next column at 120 px).
 */
export const ENDPOINT_COLUMN_WIDTHS = {
  provider: 200,
  quantization: 144,
  context: 128,
  input: 128,
  output: 120,
  uptime: 192,
  latency: 136,
} as const;

/** The providers table headers, one per column of ENDPOINT_COLUMN_WIDTHS, read through `t_i18n` by DataTable. @i18n-keys */
export const ENDPOINT_COLUMN_HEADERS = {
  provider: "Provider",
  quantization: "Quantization",
  context: "Context",
  input: "Input / 1M",
  output: "Output / 1M",
  uptime: "Uptime 30 min",
  latency: "Latency p50",
} as const satisfies Record<keyof typeof ENDPOINT_COLUMN_WIDTHS, string>;

/** The providers serving one model, in the opened row: the same shared table, sorted, no toolbar. */
function Endpoints({ integrationId, modelId }: { integrationId: string; modelId: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, error } = useOpenRouterModelEndpoints(integrationId, modelId);
  if (error) return <ConsoleErrorNote error={error as Error} />;
  const percent = (value: number | null) => (value === null ? "—" : `${value.toFixed(1)}%`);
  const columns: DataTableColumn<OpenRouterEndpoint>[] = [
    {
      key: "provider",
      header: ENDPOINT_COLUMN_HEADERS.provider,
      width: ENDPOINT_COLUMN_WIDTHS.provider,
      cell: (endpoint) => (
        <span data-testid="openrouter-endpoint-provider" className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
          <Tip content={endpoint.provider_name}>
            <span className="min-w-0 truncate">{endpoint.provider_name}</span>
          </Tip>
          {endpoint.supports_tools && (
            <Wrench role="img" aria-label={t_i18n("Calls tools")} className="h-3 w-3 shrink-0 text-default-secondary" />
          )}
        </span>
      ),
      sortValue: (endpoint) => endpoint.provider_name.toLowerCase(),
      firstSort: "asc",
    },
    {
      key: "quantization",
      header: ENDPOINT_COLUMN_HEADERS.quantization,
      width: ENDPOINT_COLUMN_WIDTHS.quantization,
      cell: (endpoint) => <span className="text-default-secondary">{endpoint.quantization ?? "—"}</span>,
      sortValue: (endpoint) => endpoint.quantization,
    },
    { key: "context", header: ENDPOINT_COLUMN_HEADERS.context, width: ENDPOINT_COLUMN_WIDTHS.context, align: "right", cell: (endpoint) => formatTokens(endpoint.context_length), sortValue: (endpoint) => endpoint.context_length },
    { key: "input", header: ENDPOINT_COLUMN_HEADERS.input, width: ENDPOINT_COLUMN_WIDTHS.input, align: "right", cell: (endpoint) => formatPerMillion(endpoint.pricing.prompt), sortValue: (endpoint) => endpoint.pricing.prompt, firstSort: "asc" },
    { key: "output", header: ENDPOINT_COLUMN_HEADERS.output, width: ENDPOINT_COLUMN_WIDTHS.output, align: "right", cell: (endpoint) => formatPerMillion(endpoint.pricing.completion), sortValue: (endpoint) => endpoint.pricing.completion, firstSort: "asc" },
    { key: "uptime", header: ENDPOINT_COLUMN_HEADERS.uptime, width: ENDPOINT_COLUMN_WIDTHS.uptime, align: "right", cell: (endpoint) => percent(endpoint.uptime_last_30m), sortValue: (endpoint) => endpoint.uptime_last_30m },
    {
      key: "latency",
      header: ENDPOINT_COLUMN_HEADERS.latency,
      width: ENDPOINT_COLUMN_WIDTHS.latency,
      align: "right",
      cell: (endpoint) => (endpoint.latency_p50_ms === null ? "—" : `${Math.round(endpoint.latency_p50_ms)} ms`),
      sortValue: (endpoint) => endpoint.latency_p50_ms,
      firstSort: "asc",
    },
  ];
  return (
    <DataTable
      rows={data?.endpoints ?? []}
      columns={columns}
      rowKey={(endpoint) => `${endpoint.provider_name}-${endpoint.tag ?? ""}`}
      defaultSort={{ key: "provider", direction: "asc" }}
      layout="fixed"
      isLoading={isLoading}
      showToolbar={false}
      emptyLabel={t_i18n("No provider serves this model right now.")}
      rowTestId="openrouter-endpoint-row"
    />
  );
}

function ProviderCard({
  integration, provider, canLink, linkRefusal, adoptable, selectionSize, onCreate, onAdopt, onUnlink, busy,
}: {
  integration: Integration;
  provider: LLMProvider | undefined;
  canLink: boolean;
  linkRefusal: string | null;
  adoptable: { id: string; display_name: string | null }[];
  selectionSize: number;
  onCreate: () => void;
  onAdopt: (id: string) => void;
  onUnlink: () => void;
  busy: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const [adoptId, setAdoptId] = useState<string>("");
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Server className="h-4 w-4 text-accent" />
          {t_i18n("Platform LLM provider")}
        </CardTitle>
        <CardDescription>
          {t_i18n("The OpenRouter provider every agent and the chat can run on, fed with this integration's API key and the models you offer below.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {provider ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <p className="font-medium">{provider.display_name || "OpenRouter"}</p>
              <p className="content-compact text-default-secondary">
                {t_i18n("{count, plural, one {# model offered} other {# models offered}}", { count: provider.available_models?.length ?? 0 })}
                {provider.default_model ? ` · ${t_i18n("Default: {model}", { model: provider.default_model })}` : ""}
              </p>
              <p className="content-compact text-default-secondary">
                {t_i18n("Its API key follows this integration's: replacing the key here replaces it there.")}
              </p>
              {provider.models_env_locked && (
                <p className="content-compact text-feedback-warning-primary">{t_i18n("Its models are set by the environment and cannot be changed here.")}</p>
              )}
            </div>
            <div className="flex items-center gap-2">
              {isAdmin && (
                <Button asChild variant="secondary" size="sm">
                  <Link to="/settings/ai/models">{t_i18n("Open in Settings")}</Link>
                </Button>
              )}
              {canLink && (
                <Button variant="ghost" size="sm" className="gap-1.5" onClick={onUnlink} disabled={busy}>
                  <Unlink className="h-3.5 w-3.5" />
                  {t_i18n("Unlink")}
                </Button>
              )}
            </div>
          </div>
        ) : canLink ? (
          <div className="space-y-3">
            <p className="content-compact text-default-secondary">
              {t_i18n("Offer the models you want below, then create the provider. It keeps a copy of this integration's API key, replaced whenever the key is replaced here.")}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" className="gap-1.5" onClick={onCreate} disabled={busy || selectionSize === 0}>
                <Bot className="h-3.5 w-3.5" />
                {selectionSize === 0
                  ? t_i18n("Offer at least one model first")
                  : t_i18n("{count, plural, one {Create the provider with # model} other {Create the provider with # models}}", { count: selectionSize })}
              </Button>
              {adoptable.length > 0 && (
                <>
                  <span className="content-compact text-default-secondary">{t_i18n("or feed an existing one:")}</span>
                  <SearchableSelect
                    aria-label={t_i18n("Pick an OpenRouter provider")}
                    className="w-60"
                    options={adoptable.map((option) => ({ value: option.id, label: option.display_name || "OpenRouter" }))}
                    value={adoptId}
                    onValueChange={setAdoptId}
                    placeholder={t_i18n("Pick an OpenRouter provider")}
                  />
                  <Button variant="secondary" size="sm" className="gap-1.5" disabled={!adoptId || busy} onClick={() => onAdopt(adoptId)}>
                    <Link2 className="h-3.5 w-3.5" />
                    {t_i18n("Link")}
                  </Button>
                </>
              )}
            </div>
            {adoptable.length > 0 && (
              <p className="content-compact text-default-secondary">
                {t_i18n("Linking an existing provider replaces its API key with this integration's and keeps its models.")}
              </p>
            )}
          </div>
        ) : (
          <p className="content-compact text-default-secondary">
            {isAdmin
              ? linkRefusal
              : t_i18n("Only an administrator can feed the platform LLM provider from {name}.", { name: integration.name })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// OpenRouter models - sub-page under /integrations/:id/models: the catalog the
// integration's key may use, each model's providers, and the models the
// platform OpenRouter LLM provider offers.
// ---------------------------------------------------------------------------
export function OpenRouterModelsTab({ integration }: { integration: Integration }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [ConfirmEl, confirm] = useConfirm();
  const aiOff = useAiActionDisabled();
  const [kind, setKind] = useState<OpenRouterCatalogKind>("chat");
  const overview = useOpenRouterOverview(integration.id, !aiOff);
  const catalog = useOpenRouterModels(integration.id, kind, !aiOff);
  const link = useLinkOpenRouterProvider(integration.id);
  const updateModels = useUpdateOpenRouterProviderModels(integration.id);
  const unlink = useUnlinkOpenRouterProvider(integration.id);
  const { data: announcementStatus, refetch: refetchAnnouncementStatus } = useModelAnnouncementStatus(isAdmin);
  const [catalogChange, setCatalogChange] = useState<ModelCatalogChange | null>(null);

  const provider = overview.data?.llm_providers[0];
  const canLink = !!overview.data?.can_link_provider;
  const editable = canLink && !provider?.models_env_locked;
  const savedModels = useMemo(() => provider?.available_models ?? [], [provider]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [defaultModel, setDefaultModel] = useState<string | null>(null);
  // The selection starts from what the provider offers, and starts again
  // from it after every save (the provider answer replaces it).
  useEffect(() => {
    setSelected(new Set(savedModels));
    setDefaultModel(provider?.default_model ?? null);
  }, [savedModels, provider?.default_model]);

  const [expanded, setExpanded] = useState<string | null>(null);

  const models = useMemo(() => catalog.data?.models ?? [], [catalog.data]);
  const authors = useMemo(() => catalogAuthors(models), [models]);
  const dirty = (!!provider && selectionChanged(savedModels, selected)) || (!!provider && defaultModel !== (provider.default_model ?? null));

  const offerCatalogChange = async (change: ModelCatalogChange | null | undefined) => {
    if (!isAdmin || !change || !canEmailUsers(announcementStatus)) return;
    await refetchAnnouncementStatus();
    setCatalogChange(change);
  };

  const toggle = (modelId: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(modelId);
      else next.delete(modelId);
      return next;
    });
    if (!on && defaultModel === modelId) setDefaultModel(null);
  };

  const failed = (title: string) => (error: Error) => toast({ title: t_i18n(title), description: error.message, variant: "error" });
  const order = () => orderedSelection(savedModels, selected, models.map((m) => m.id));

  const save = () => {
    if (!provider) return;
    updateModels.mutate(
      { configId: provider.id, available_models: order(), default_model: defaultModel },
      {
        onSuccess: (saved) => {
          toast({ title: t_i18n("Models saved"), variant: "success" });
          void offerCatalogChange(saved.model_catalog_change);
        },
        onError: failed("The models could not be saved"),
      },
    );
  };

  const create = () => {
    const availableModels = order();
    link.mutate(
      { available_models: availableModels, default_model: defaultModel ?? availableModels[0] ?? null },
      {
        onSuccess: (created) => {
          toast({ title: t_i18n("OpenRouter LLM provider created"), variant: "success" });
          void offerCatalogChange(created.model_catalog_change);
        },
        onError: failed("The provider could not be created"),
      },
    );
  };

  const adopt = async (configId: string) => {
    const confirmed = await confirm({
      title: "Feed this provider from the integration?",
      description: "Its API key is replaced with this integration's, now and whenever the key is replaced here. Its models are kept.",
      confirmLabel: "Link",
      variant: "warning",
    });
    if (!confirmed) return;
    link.mutate(
      { provider_config_id: configId },
      {
        onSuccess: () => toast({ title: t_i18n("Provider linked"), variant: "success" }),
        onError: failed("The provider could not be linked"),
      },
    );
  };

  const doUnlink = async () => {
    if (!provider) return;
    const confirmed = await confirm({
      title: "Unlink the provider?",
      description: "The provider keeps running on the API key and the models it has; it no longer follows this integration's key.",
      confirmLabel: "Unlink",
      variant: "warning",
    });
    if (!confirmed) return;
    unlink.mutate(provider.id, {
      onSuccess: () => toast({ title: t_i18n("Provider unlinked"), variant: "success" }),
      onError: failed("The provider could not be unlinked"),
    });
  };

  if (aiOff) return <AiDisabledNotice />;
  if (overview.isLoading) return <PageSkeleton cards={2} />;
  if (overview.error || !overview.data) {
    return <ConsoleErrorNote error={(overview.error as Error) ?? { message: t_i18n("The OpenRouter account could not be read.") }} />;
  }

  const busy = link.isPending || updateModels.isPending || unlink.isPending;
  // Toggles are live while the provider is fed from here, or about to be created.
  const rowsEditable = editable;
  const perMillion = (model: OpenRouterModel, value: number | null) =>
    model.variable_pricing ? <span className="text-default-secondary">{t_i18n("Varies")}</span> : formatPerMillion(value);

  const modelColumns: DataTableColumn<OpenRouterModel>[] = [
    {
      key: "name",
      header: "Name",
      width: MODEL_COLUMN_WIDTHS.name,
      interactive: true,
      cell: (model) => {
        const open = expanded === model.id;
        const Chevron = open ? ChevronDown : ChevronRight;
        return (
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <button
              type="button"
              onClick={() => setExpanded(open ? null : model.id)}
              aria-expanded={open}
              aria-label={open ? t_i18n("Hide providers") : t_i18n("Show providers")}
              className="shrink-0 text-default-secondary hover:text-default-primary"
            >
              <Chevron className="h-4 w-4" />
            </button>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-1.5">
                <Tip content={model.name}>
                  <p className="truncate font-medium">{model.name}</p>
                </Tip>
                {model.expiration_date && (
                  <Badge variant="warning" className="shrink-0 whitespace-nowrap">
                    {t_i18n("Retired on {date}", { date: model.expiration_date })}
                  </Badge>
                )}
              </div>
              <p className="truncate font-mono-plex content-caption text-default-secondary">{model.id}</p>
            </div>
          </div>
        );
      },
      sortValue: (model) => model.name.toLowerCase(),
      firstSort: "asc",
      csv: (model) => model.name,
    },
    { key: "id", header: "Model", exportOnly: true, cell: (model) => model.id, csv: (model) => model.id },
    {
      key: "capabilities",
      header: "Capabilities",
      width: MODEL_COLUMN_WIDTHS.capabilities,
      hideable: true,
      cell: (model) => {
        const capabilities = capabilitiesOf(model);
        if (capabilities.length === 0) return <span className="text-default-secondary">—</span>;
        const icons = { tools: Wrench, reasoning: Brain, images: Eye } as const;
        return (
          <div className="flex items-center gap-1" data-testid="openrouter-model-capabilities">
            {capabilities.map((capability) => {
              if (capability === "free") {
                return <Badge key={capability} variant="success" className="whitespace-nowrap">{t_i18n("Free")}</Badge>;
              }
              const Icon = icons[capability];
              return (
                <Tip key={capability} content={t_i18n(CAPABILITY_LABELS[capability])}>
                  <span className="inline-flex">
                    <Icon role="img" aria-label={t_i18n(CAPABILITY_LABELS[capability])} className="h-3.5 w-3.5 text-default-secondary" />
                  </span>
                </Tip>
              );
            })}
          </div>
        );
      },
      sortValue: (model) => capabilitiesOf(model).length,
      csv: (model) => capabilitiesOf(model).map((capability) => CAPABILITY_LABELS[capability]).join(", "),
    },
    {
      key: "context",
      header: "Context",
      align: "right",
      width: MODEL_COLUMN_WIDTHS.figure,
      hideable: true,
      cell: (model) => formatTokens(model.context_length),
      sortValue: (model) => model.context_length,
      csv: (model) => model.context_length,
    },
    {
      key: "input",
      header: "Input / 1M",
      align: "right",
      width: MODEL_COLUMN_WIDTHS.figure,
      hideable: true,
      cell: (model) => perMillion(model, model.pricing.prompt),
      // A variable price sorts last whatever the direction.
      sortValue: (model) => (model.variable_pricing ? null : model.pricing.prompt),
      firstSort: "asc",
      csv: (model) => (model.variable_pricing ? null : model.pricing.prompt),
    },
    {
      key: "output",
      header: "Output / 1M",
      align: "right",
      width: MODEL_COLUMN_WIDTHS.figure,
      hideable: true,
      cell: (model) => perMillion(model, model.pricing.completion),
      sortValue: (model) => (model.variable_pricing ? null : model.pricing.completion),
      firstSort: "asc",
      csv: (model) => (model.variable_pricing ? null : model.pricing.completion),
    },
    {
      key: "created",
      header: "Created",
      width: MODEL_COLUMN_WIDTHS.date,
      hideable: true,
      cell: (model) => {
        const at = model.created ? Date.parse(model.created) : Number.NaN;
        return Number.isNaN(at) ? (
          <span className="text-default-secondary">—</span>
        ) : (
          <span className="whitespace-nowrap text-default-secondary">{intl.formatDate(at, { dateStyle: "medium" })}</span>
        );
      },
      sortValue: (model) => model.created,
      firstSort: "desc",
      csv: (model) => model.created,
    },
    {
      key: "offered",
      header: "Offered",
      width: MODEL_COLUMN_WIDTHS.offered,
      interactive: rowsEditable,
      cell: (model) => {
        const on = selected.has(model.id);
        if (!rowsEditable) {
          return on ? <Badge variant="cyan" className="whitespace-nowrap">{t_i18n("Offered")}</Badge> : <span className="text-default-secondary">—</span>;
        }
        const isDefault = defaultModel === model.id;
        return (
          <>
            <Switch checked={on} onCheckedChange={(next) => toggle(model.id, next)} aria-label={t_i18n("Offer this model on the platform")} />
            <Tip content={isDefault ? t_i18n("Default model of the provider") : t_i18n("Make it the provider's default model")}>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => setDefaultModel(model.id)}
                disabled={!on}
                aria-pressed={isDefault}
                aria-label={t_i18n("Default model of the provider")}
                className={isDefault ? "text-feedback-warning-primary" : "text-default-secondary"}
              >
                <Star className={cn("h-4 w-4", isDefault && "fill-feedback-alert-primary")} />
              </Button>
            </Tip>
          </>
        );
      },
      sortValue: (model) => selected.has(model.id),
      csv: (model) => selected.has(model.id),
    },
  ];

  const modelFilters: DataTableFilter<OpenRouterModel>[] = [
    {
      key: "author",
      label: "Model author",
      values: (model) => [model.author],
      options: authors.map((author) => ({ value: author, label: author })),
    },
    {
      key: "capability",
      label: "Capabilities",
      values: capabilitiesOf,
      options: (Object.keys(CAPABILITY_LABELS) as ModelCapability[]).map((capability) => ({
        value: capability,
        label: t_i18n(CAPABILITY_LABELS[capability]),
      })),
    },
    {
      key: "offered",
      label: "Offered",
      values: (model) => [selected.has(model.id) ? "offered" : "not_offered"],
      options: (Object.keys(OFFER_LABELS) as (keyof typeof OFFER_LABELS)[]).map((value) => ({ value, label: t_i18n(OFFER_LABELS[value]) })),
    },
  ];

  return (
    <div className="space-y-6">
      <ProviderCard
        integration={integration}
        provider={provider}
        canLink={canLink}
        linkRefusal={overview.data.link_refusal}
        adoptable={overview.data.adoptable_providers}
        selectionSize={selected.size}
        onCreate={create}
        onAdopt={(id) => void adopt(id)}
        onUnlink={() => void doUnlink()}
        busy={busy}
      />

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="text-base">{t_i18n("Catalog")}</CardTitle>
              <CardDescription>
                {t_i18n("The models this integration's key may use, with their prices per million tokens. Open a model to see the providers serving it.")}
              </CardDescription>
            </div>
            <SegmentedFilter
              options={[
                { key: "chat", label: "Text models" },
                { key: "embeddings", label: "Embedding models" },
              ]}
              value={kind}
              onChange={setKind}
              ariaLabel={t_i18n("Catalog")}
            />
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {!catalog.error && (
            <p className="content-compact text-default-secondary">
              {t_i18n("{count, plural, one {# model offered} other {# models offered}}", { count: selected.size })}
            </p>
          )}
          {catalog.error ? (
            <ConsoleErrorNote error={catalog.error as Error} />
          ) : (
            <DataTable
              rows={models}
              columns={modelColumns}
              layout="fixed"
              rowKey={(model) => model.id}
              defaultSort={{ key: "name", direction: "asc" }}
              defaultPageSize={25}
              searchFields={(model) => [model.id, model.name]}
              searchPlaceholder={t_i18n("Search models...")}
              filters={modelFilters}
              exportName={`openrouter-${kind}-models`}
              urlKey="orm_"
              storageKey="openrouter-models"
              rowLines={2}
              isLoading={catalog.isLoading}
              emptyLabel={t_i18n("No models available yet")}
              rowTestId="openrouter-model-row"
              rowClassName={(model) => (selected.has(model.id) ? "bg-accent/[0.04]" : undefined)}
              onRowClick={(model) => setExpanded((current) => (current === model.id ? null : model.id))}
              toggleInCell
              expandedRow={(model) =>
                expanded === model.id ? (
                  <div className="space-y-3">
                    {model.description && <p className="whitespace-pre-line content-compact text-default-secondary">{model.description}</p>}
                    <Endpoints integrationId={integration.id} modelId={model.id} />
                  </div>
                ) : null
              }
            />
          )}
        </CardContent>
      </Card>

      {provider && dirty && editable && (
        <div className="sticky bottom-4 z-10 flex items-center justify-between gap-3 rounded-sm border border-accent/30 layer-1 bg-elevation-default px-4 py-3 shadow-overlay">
          <p className="content-base">
            {t_i18n("{count, plural, one {# model offered} other {# models offered}}", { count: selected.size })}
            <span className="text-default-secondary"> · {t_i18n("Not saved yet")}</span>
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setSelected(new Set(savedModels));
                setDefaultModel(provider.default_model ?? null);
              }}
              disabled={busy}
            >
              {t_i18n("Discard")}
            </Button>
            <Button size="sm" onClick={save} disabled={busy}>
              {updateModels.isPending ? t_i18n("Saving...") : t_i18n("Save models")}
            </Button>
          </div>
        </div>
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
      {ConfirmEl}
    </div>
  );
}
