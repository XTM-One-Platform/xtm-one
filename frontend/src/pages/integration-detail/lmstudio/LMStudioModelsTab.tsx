import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bot,
  Brain,
  Download,
  Eye,
  Link2,
  MemoryStick,
  Play,
  RefreshCw,
  Server,
  Star,
  Unlink,
  Wrench,
  X,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SearchInput, TOOLBAR_SEARCH_HEIGHT } from "@/components/ui/search-input";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { PageSkeleton } from "@/components/ui/skeleton";
import { RelayRouteBadge } from "@/components/ui/relay-route-badge";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useModelAnnouncementStatus } from "@/hooks/api/useSettingsApi";
import {
  useLMStudioDownload,
  useLMStudioModels,
  useLMStudioOverview,
  useLinkLMStudioProvider,
  useLoadLMStudioModel,
  useStartLMStudioDownload,
  useUnlinkLMStudioProvider,
  useUnloadLMStudioModel,
  useUpdateLMStudioProviderModels,
} from "@/hooks/api/useLMStudioApi";
import { useFormatter } from "@/i18n";
import { canEmailUsers } from "@/lib/model-announcements";
import { cn } from "@/lib/utils";
import { ModelChangesDialog } from "@/pages/settings/ModelAnnouncements";
import type { Integration, LLMProvider, LMStudioModel, LMStudioModelType, ModelCatalogChange } from "@/types";
import { formatTokens, orderedSelection, selectionChanged } from "../openrouter/openrouter-console";
import { ConsoleErrorNote } from "../openrouter/OpenRouterUsageTab";
import {
  DEFAULT_SERVER_MODEL_FILTER,
  downloadFinished,
  downloadShare,
  filterServerModels,
  formatBytes,
  isDownloadSource,
  parseContextLength,
  type ServerModelFilter,
} from "./lmstudio-console";
import { Spinner } from "@filigran/design-system";

/** A model's type, as a label; every value is a catalog key `validate-i18n` checks. @i18n-keys */
const TYPE_LABELS = {
  llm: "LLM",
  embedding: "Embedding",
  decision: "Decision",
} as const satisfies Record<LMStudioModelType, string>;

/** A download's state, as a label; every value is a catalog key `validate-i18n` checks. @i18n-keys */
const DOWNLOAD_LABELS = {
  downloading: "Downloading",
  paused: "Paused",
  completed: "Downloaded",
  failed: "Download failed",
  already_downloaded: "Already on the server",
} as const;

type TypeFilter = "all" | LMStudioModelType;

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

function ServerCard({
  integration, baseUrl, relayGroupId, refusal, onRefresh, refreshing,
}: {
  integration: Integration;
  baseUrl: string | null;
  relayGroupId: string | null;
  refusal: string | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <MemoryStick className="h-4 w-4 text-accent" />
          {t_i18n("LM Studio server")}
        </CardTitle>
        <CardDescription>
          {relayGroupId
            ? t_i18n("Reached from the relay installed in its network.")
            : t_i18n("Reached directly from the platform.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center justify-between gap-3 content-base">
        <div className="min-w-0 space-y-1">
          <p className="truncate font-mono-plex content-compact">{baseUrl ?? "—"}</p>
          {relayGroupId ? <RelayRouteBadge groupId={relayGroupId} size="default" /> : null}
          {refusal && <p className="content-compact text-feedback-warning-primary">{refusal}</p>}
        </div>
        <div className="flex items-center gap-2">
          {refusal ? (
            <Button asChild variant="secondary" size="sm">
              <Link to={`/integrations/${integration.id}/config`}>{t_i18n("Open Configuration")}</Link>
            </Button>
          ) : (
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={onRefresh} disabled={refreshing}>
              <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
              {t_i18n("Refresh")}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Download
// ---------------------------------------------------------------------------

function DownloadCard({ integrationId }: { integrationId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [source, setSource] = useState("");
  const [quantization, setQuantization] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const start = useStartLMStudioDownload(integrationId);
  const job = useLMStudioDownload(integrationId, jobId);
  const fromHuggingFace = source.trim().startsWith("https://");
  const valid = isDownloadSource(source);

  const submit = () => {
    start.mutate(
      { model: source.trim(), ...(fromHuggingFace && quantization.trim() ? { quantization: quantization.trim() } : {}) },
      {
        onSuccess: (started) => {
          if (started.job_id) setJobId(started.job_id);
          toast({
            title: started.status === "already_downloaded" ? t_i18n("Already on the server") : t_i18n("Download started"),
            variant: "success",
          });
        },
        onError: (error: Error) => toast({ title: t_i18n("The download could not start"), description: error.message, variant: "error" }),
      },
    );
  };

  const current = job.data;
  const share = current ? downloadShare(current) : null;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Download className="h-4 w-4 text-accent" />
          {t_i18n("Download a model")}
        </CardTitle>
        <CardDescription>
          {t_i18n("An LM Studio catalog id (openai/gpt-oss-20b) or a Hugging Face link. The server downloads it itself, from its own network.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label={t_i18n("Model to download")}
            className="min-w-64 flex-1"
            value={source}
            maxLength={500}
            placeholder="openai/gpt-oss-20b"
            onChange={(e) => setSource(e.target.value)}
          />
          {fromHuggingFace && (
            <Input
              aria-label={t_i18n("Quantization")}
              className="w-36"
              value={quantization}
              maxLength={40}
              placeholder="Q4_K_M"
              onChange={(e) => setQuantization(e.target.value)}
            />
          )}
          <Button size="sm" className="gap-1.5" onClick={submit} disabled={!valid || start.isPending} aria-busy={start.isPending || undefined}>
            {start.isPending && <Spinner size="sm" tone="inherit" />}{!start.isPending && <Download className="h-3.5 w-3.5" />}
            {t_i18n("Download")}
          </Button>
        </div>
        {source.trim() && !valid && (
          <p className="content-compact text-feedback-warning-primary">
            {t_i18n("Enter a catalog id (owner/model) or an https link to huggingface.co.")}
          </p>
        )}
        {jobId && job.error ? <ConsoleErrorNote error={job.error as Error} /> : null}
        {current && (
          <div className="space-y-1.5 rounded-sm border border-elevation-subtle-soft px-3 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2 content-compact">
              <span className="flex items-center gap-1.5 font-medium">
                {!downloadFinished(current) && <Spinner size="sm" tone="inherit" />}
                {t_i18n(DOWNLOAD_LABELS[current.status])}
              </span>
              <span className="text-default-secondary">
                {formatBytes(current.downloaded_bytes)} / {formatBytes(current.total_size_bytes)}
                {current.status === "downloading" && current.bytes_per_second
                  ? ` · ${formatBytes(current.bytes_per_second)}/s`
                  : ""}
              </span>
            </div>
            {share !== null && (
              <div
                className="h-1.5 overflow-hidden rounded-full bg-elevation-hover"
                role="progressbar"
                aria-label={t_i18n("Download progress")}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(share)}
              >
                <div
                  className={cn("h-full", current.status === "failed" ? "bg-feedback-error-primary" : "bg-accent")}
                  style={{ width: `${share}%` }}
                />
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

function LoadDialog({
  integrationId, model, onClose,
}: {
  integrationId: string;
  model: LMStudioModel | null;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const load = useLoadLMStudioModel(integrationId);
  const [contextLength, setContextLength] = useState("");
  const [flashAttention, setFlashAttention] = useState(false);
  const [offloadKvCache, setOffloadKvCache] = useState(true);
  useEffect(() => {
    setContextLength("");
    setFlashAttention(false);
    setOffloadKvCache(true);
  }, [model?.key]);

  if (!model) return null;
  const parsed = contextLength.trim() ? parseContextLength(contextLength, model.max_context_length) : null;
  const invalid = contextLength.trim() !== "" && parsed === null;
  const isChat = model.type === "llm";

  const submit = () => {
    load.mutate(
      {
        model: model.key,
        ...(parsed !== null ? { context_length: parsed } : {}),
        ...(isChat ? { flash_attention: flashAttention, offload_kv_cache_to_gpu: offloadKvCache } : {}),
      },
      {
        onSuccess: (loaded) => {
          toast({
            title: t_i18n("Model loaded"),
            description: loaded.load_time_seconds !== null
              ? t_i18n("{model} loaded in {seconds} s.", { model: loaded.instance_id, seconds: loaded.load_time_seconds.toFixed(1) })
              : loaded.instance_id,
            variant: "success",
          });
          onClose();
        },
        onError: (error: Error) => toast({ title: t_i18n("The model could not be loaded"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open onOpenChange={(next) => !next && !load.isPending && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Load {model}", { model: model.display_name })}</DialogTitle>
          <DialogDescription>
            {t_i18n("The model takes the server's memory until it is unloaded. A large model can take minutes to load.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <label className="content-compact font-medium" htmlFor="lmstudio-context-length">{t_i18n("Context length (tokens)")}</label>
            <Input
              aria-label={t_i18n("Context length (tokens)")}
              id="lmstudio-context-length"
              inputMode="numeric"
              value={contextLength}
              placeholder={model.max_context_length ? t_i18n("LM Studio's default, up to {max}", { max: formatTokens(model.max_context_length) }) : t_i18n("LM Studio's default")}
              onChange={(e) => setContextLength(e.target.value)}
              aria-invalid={invalid}
            />
            {invalid && (
              <p className="content-compact text-feedback-warning-primary">
                {model.max_context_length
                  ? t_i18n("A whole number of tokens from 256 to {max}.", { max: model.max_context_length })
                  : t_i18n("A whole number of tokens, 256 or more.")}
              </p>
            )}
            <p className="content-compact text-default-secondary">
              {t_i18n("The platform offers this model with the context it is loaded with.")}
            </p>
          </div>
          {isChat && (
            <>
              <label className="flex items-center justify-between gap-3 content-compact">
                {t_i18n("Flash attention")}
                <Switch checked={flashAttention} onCheckedChange={setFlashAttention} />
              </label>
              <label className="flex items-center justify-between gap-3 content-compact">
                {t_i18n("Keep the KV cache on the GPU")}
                <Switch checked={offloadKvCache} onCheckedChange={setOffloadKvCache} />
              </label>
            </>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={load.isPending}>{t_i18n("Cancel")}</Button>
          <Button size="sm" className="gap-1.5" onClick={submit} disabled={invalid || load.isPending} aria-busy={load.isPending || undefined}>
            {load.isPending && <Spinner size="sm" tone="inherit" />}{!load.isPending && <Play className="h-3.5 w-3.5" />}
            {load.isPending ? t_i18n("Loading...") : t_i18n("Load")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

function ModelRow({
  model, canManageServer, offerable, selected, isDefault, onToggle, onDefault, onLoad, onUnload, unloading,
}: {
  model: LMStudioModel;
  canManageServer: boolean;
  offerable: boolean;
  selected: boolean;
  isDefault: boolean;
  onToggle: (on: boolean) => void;
  onDefault: () => void;
  onLoad: () => void;
  onUnload: (instanceId: string) => void;
  unloading: string | null;
}) {
  const { t_i18n } = useFormatter();
  const loaded = model.loaded_instances.length > 0;
  return (
    <div className={cn("rounded-sm border px-3 py-2.5 transition-colors", selected ? "border-accent/30 bg-accent/[0.04]" : "border-elevation-subtle-soft")}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-48">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate content-base font-medium">{model.display_name}</span>
            <Badge variant="outline" className="content-caption">{t_i18n(TYPE_LABELS[model.type])}</Badge>
            {loaded && <Badge variant="success" className="content-caption">{t_i18n("Loaded")}</Badge>}
            {model.tool_use && (
              <Tip content={t_i18n("Calls tools")}><Badge variant="outline" className="content-caption"><Wrench className="h-2.5 w-2.5" /></Badge></Tip>
            )}
            {model.reasoning_options.length > 0 && (
              <Tip content={t_i18n("Reasons before answering")}><Badge variant="outline" className="content-caption"><Brain className="h-2.5 w-2.5" /></Badge></Tip>
            )}
            {model.vision && (
              <Tip content={t_i18n("Reads images")}><Badge variant="outline" className="content-caption"><Eye className="h-2.5 w-2.5" /></Badge></Tip>
            )}
          </div>
          <p className="truncate font-mono-plex text-[11px] text-default-secondary">{model.key}</p>
        </div>
        <div className="hidden shrink-0 grid-cols-3 gap-4 text-right content-compact sm:grid">
          <div>
            <p className="text-default-secondary">{t_i18n("Size")}</p>
            <p>{formatBytes(model.size_bytes)}</p>
          </div>
          <div>
            <p className="text-default-secondary">{t_i18n("Quantization")}</p>
            <p>{model.quantization ?? "—"}</p>
          </div>
          <div>
            <p className="text-default-secondary">{t_i18n("Max context")}</p>
            <p>{formatTokens(model.max_context_length)}</p>
          </div>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {canManageServer && (
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={onLoad}>
              <Play className="h-3.5 w-3.5" />
              {loaded ? t_i18n("Load again") : t_i18n("Load")}
            </Button>
          )}
          {offerable && (
            <>
              <Tip content={isDefault ? t_i18n("Default model of the provider") : t_i18n("Make it the provider's default model")}>
                <button
                  type="button"
                  onClick={onDefault}
                  disabled={!selected}
                  aria-pressed={isDefault}
                  aria-label={t_i18n("Default model of the provider")}
                  className={cn("disabled:opacity-30", isDefault ? "text-feedback-warning-primary" : "text-default-secondary hover:text-default-primary")}
                >
                  <Star className={cn("h-4 w-4", isDefault && "fill-feedback-alert-primary")} />
                </button>
              </Tip>
              <Switch checked={selected} onCheckedChange={onToggle} aria-label={t_i18n("Offer this model on the platform")} />
            </>
          )}
        </div>
      </div>
      {loaded && (
        <div className="mt-2 flex flex-wrap gap-2 border-t border-elevation-subtle-soft pt-2">
          {model.loaded_instances.map((instance) => (
            <span key={instance.id} className="flex items-center gap-1.5 rounded-sm bg-elevation-hover px-2 py-1 content-caption">
              <span className="font-mono-plex">{instance.id}</span>
              {instance.context_length ? (
                <span className="text-default-secondary">{t_i18n("{tokens} context", { tokens: formatTokens(instance.context_length) })}</span>
              ) : null}
              {canManageServer && (
                <Tip content={t_i18n("Unload this instance")}>
                  <button
                    type="button"
                    onClick={() => onUnload(instance.id)}
                    disabled={unloading === instance.id}
                    aria-label={t_i18n("Unload this instance")}
                    className="text-default-secondary hover:text-feedback-error-primary disabled:opacity-40"
                  >
                    {unloading === instance.id && <Spinner size="sm" tone="inherit" />}{!(unloading === instance.id) && <X className="h-3 w-3" />}
                  </button>
                </Tip>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Platform provider
// ---------------------------------------------------------------------------

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
          {t_i18n("The LM Studio provider every agent and the chat can run on, reached through this integration, with the models you offer below.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {provider ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <p className="font-medium">{provider.display_name || "LM Studio"}</p>
              <p className="content-compact text-default-secondary">
                {t_i18n("{count, plural, one {# model offered} other {# models offered}}", { count: provider.available_models?.length ?? 0 })}
                {provider.default_model ? ` · ${t_i18n("Default: {model}", { model: provider.default_model })}` : ""}
              </p>
              <p className="content-compact text-default-secondary">
                {t_i18n("It holds no URL or token: it reads them, and the route, from this integration on every request.")}
              </p>
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
              {t_i18n("Offer the models you want below, then create the provider. Its requests follow this integration's route: its relay group, or a direct connection.")}
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
                    aria-label={t_i18n("Pick an LM Studio provider")}
                    className="w-60"
                    options={adoptable.map((option) => ({ value: option.id, label: option.display_name || "LM Studio" }))}
                    value={adoptId}
                    onValueChange={setAdoptId}
                    placeholder={t_i18n("Pick an LM Studio provider")}
                  />
                  <Button variant="secondary" size="sm" className="gap-1.5" disabled={!adoptId || busy} onClick={() => onAdopt(adoptId)}>
                    <Link2 className="h-3.5 w-3.5" />
                    {t_i18n("Link")}
                  </Button>
                </>
              )}
            </div>
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
// LM Studio models - sub-page under /integrations/:id/models: the server's
// models and their loaded instances, loading, unloading and downloading
// them, and the models the platform LM Studio LLM provider offers.
// ---------------------------------------------------------------------------
export function LMStudioModelsTab({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [ConfirmEl, confirm] = useConfirm();
  const aiOff = useAiActionDisabled();
  const overview = useLMStudioOverview(integration.id, !aiOff);
  const serverReady = !!overview.data && overview.data.server_refusal === null;
  const listing = useLMStudioModels(integration.id, !aiOff && serverReady);
  const link = useLinkLMStudioProvider(integration.id);
  const updateModels = useUpdateLMStudioProviderModels(integration.id);
  const unlink = useUnlinkLMStudioProvider(integration.id);
  const unload = useUnloadLMStudioModel(integration.id);
  const { data: announcementStatus, refetch: refetchAnnouncementStatus } = useModelAnnouncementStatus(isAdmin);
  const [catalogChange, setCatalogChange] = useState<ModelCatalogChange | null>(null);
  const [loading, setLoading] = useState<LMStudioModel | null>(null);
  const [unloading, setUnloading] = useState<string | null>(null);

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

  const [filter, setFilter] = useState<ServerModelFilter>(DEFAULT_SERVER_MODEL_FILTER);
  const models = useMemo(() => listing.data?.models ?? [], [listing.data]);
  const shown = useMemo(() => filterServerModels(models, filter, selected), [models, filter, selected]);
  const loadedCount = models.filter((m) => m.loaded_instances.length > 0).length;
  const dirty = !!provider && (selectionChanged(savedModels, selected) || defaultModel !== (provider.default_model ?? null));

  const offerCatalogChange = async (change: ModelCatalogChange | null | undefined) => {
    if (!isAdmin || !change || !canEmailUsers(announcementStatus)) return;
    await refetchAnnouncementStatus();
    setCatalogChange(change);
  };

  const toggle = (modelKey: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(modelKey);
      else next.delete(modelKey);
      return next;
    });
    if (!on && defaultModel === modelKey) setDefaultModel(null);
  };

  const failed = (title: string) => (error: Error) => toast({ title: t_i18n(title), description: error.message, variant: "error" });
  const order = () => orderedSelection(savedModels, selected, models.map((m) => m.key));

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
    const ordered = order();
    const chosenDefault = defaultModel ?? ordered[0] ?? null;
    // The default first: a first provider seeds the platform's default model
    // with its first model, which must be the one the admin starred.
    const availableModels = chosenDefault ? [chosenDefault, ...ordered.filter((m) => m !== chosenDefault)] : ordered;
    link.mutate(
      { available_models: availableModels, default_model: chosenDefault },
      {
        onSuccess: (created) => {
          toast({ title: t_i18n("LM Studio LLM provider created"), variant: "success" });
          void offerCatalogChange(created.model_catalog_change);
        },
        onError: failed("The provider could not be created"),
      },
    );
  };

  const adopt = async (configId: string) => {
    const confirmed = await confirm({
      title: "Feed this provider from the integration?",
      description: "Its requests then go to this integration's server, through its relay group or directly. Its models are kept.",
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
      description: "An LM Studio provider runs only through its integration: once unlinked, every request to its models is refused until an integration feeds it again.",
      confirmLabel: "Unlink",
      variant: "warning",
    });
    if (!confirmed) return;
    unlink.mutate(provider.id, {
      onSuccess: () => toast({ title: t_i18n("Provider unlinked"), variant: "success" }),
      onError: failed("The provider could not be unlinked"),
    });
  };

  const doUnload = async (instanceId: string) => {
    const confirmed = await confirm({
      title: "Unload this instance?",
      description: "Requests to it fail until it is loaded again, or until LM Studio loads it on demand.",
      confirmLabel: "Unload",
      variant: "warning",
    });
    if (!confirmed) return;
    setUnloading(instanceId);
    unload.mutate(instanceId, {
      onSuccess: () => toast({ title: t_i18n("Instance unloaded"), variant: "success" }),
      onError: failed("The instance could not be unloaded"),
      onSettled: () => setUnloading(null),
    });
  };

  if (aiOff) return <AiDisabledNotice />;
  if (overview.isLoading) return <PageSkeleton cards={2} />;
  if (overview.error || !overview.data) {
    return <ConsoleErrorNote error={(overview.error as Error) ?? { message: t_i18n("The LM Studio integration could not be read.") }} />;
  }

  const data = overview.data;
  const busy = link.isPending || updateModels.isPending || unlink.isPending;
  const typeOptions: { key: TypeFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "llm", label: TYPE_LABELS.llm },
    { key: "embedding", label: TYPE_LABELS.embedding },
    { key: "decision", label: TYPE_LABELS.decision },
  ];

  return (
    <div className="space-y-6">
      <ServerCard
        integration={integration}
        baseUrl={data.base_url}
        relayGroupId={data.relay_group_id}
        refusal={data.server_refusal}
        onRefresh={() => void listing.refetch()}
        refreshing={listing.isFetching}
      />

      <ProviderCard
            integration={integration}
            provider={provider}
            canLink={canLink}
            linkRefusal={data.link_refusal}
            adoptable={data.adoptable_providers}
            selectionSize={selected.size}
            onCreate={create}
            onAdopt={(id) => void adopt(id)}
            onUnlink={() => void doUnlink()}
            busy={busy}
          />

      {serverReady && (
        <>
          <Card>
            <CardHeader className="pb-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <CardTitle className="text-base">{t_i18n("Models on the server")}</CardTitle>
                  <CardDescription>
                    {t_i18n("Every model LM Studio holds, loaded or not. A model that is not loaded is loaded on demand when LM Studio allows it.")}
                  </CardDescription>
                </div>
                <SegmentedFilter
                  options={typeOptions}
                  value={filter.type ?? "all"}
                  onChange={(value) => setFilter((f) => ({ ...f, type: value === "all" ? null : value }))}
                  ariaLabel={t_i18n("Model type")}
                />
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <SearchInput
                  inputClassName={TOOLBAR_SEARCH_HEIGHT}
                  value={filter.search}
                  onChange={(search) => setFilter((f) => ({ ...f, search }))}
                  placeholder={t_i18n("Search models...")}
                  className="w-64"
                />
                <label className="flex items-center gap-2 content-compact text-default-secondary">
                  <Switch checked={filter.loadedOnly} onCheckedChange={(loadedOnly) => setFilter((f) => ({ ...f, loadedOnly }))} />
                  {t_i18n("Loaded only")}
                </label>
                <label className="flex items-center gap-2 content-compact text-default-secondary">
                  <Switch checked={filter.offeredOnly} onCheckedChange={(offeredOnly) => setFilter((f) => ({ ...f, offeredOnly }))} />
                  {t_i18n("Offered only")}
                </label>
              </div>

              {listing.isLoading ? (
                <PageSkeleton cards={3} />
              ) : listing.error ? (
                <ConsoleErrorNote error={listing.error as Error} />
              ) : models.length === 0 ? (
                <p className="content-compact text-default-secondary">{t_i18n("The server holds no model yet: download one below.")}</p>
              ) : (
                <>
                  <p className="content-compact text-default-secondary">
                    {t_i18n("{shown} of {total} models · {loaded} loaded · {offered} offered", {
                      shown: shown.length, total: models.length, loaded: loadedCount, offered: selected.size,
                    })}
                  </p>
                  <div className="space-y-2">
                    {shown.map((model) => (
                      <ModelRow
                        key={model.key}
                        model={model}
                        canManageServer={data.can_manage_server}
                        offerable={editable}
                        selected={selected.has(model.key)}
                        isDefault={defaultModel === model.key}
                        onToggle={(on) => toggle(model.key, on)}
                        onDefault={() => setDefaultModel(model.key)}
                        onLoad={() => setLoading(model)}
                        onUnload={(instanceId) => void doUnload(instanceId)}
                        unloading={unloading}
                      />
                    ))}
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {data.can_manage_server && <DownloadCard integrationId={integration.id} />}
        </>
      )}

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
      <LoadDialog integrationId={integration.id} model={loading} onClose={() => setLoading(null)} />
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
