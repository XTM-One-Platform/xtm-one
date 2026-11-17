// SPDX-License-Identifier: Apache-2.0

import { useId, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Calculator,
  ChevronsUpDown,
  CheckCircle2,
  CircleAlert,
  ExternalLink,
  Download,
  Edit3,
  FileJson,
  List,
  Plus,
  RefreshCw,
  Save,
  Search,
  Upload,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import {
  useCreateModelPricingRow,
  useImportModelPricingPayload,
  useModelPricingRows,
  useModelsWithoutPricingRows,
  useObservabilityConfig,
  useModelPricingStatus,
  useRepriceNullCostTraces,
  useSyncModelPricingFromSeed,
  useUpdateModelPricingRow,
} from "@/hooks/api/useSettingsApi";
import type { ModelPricingRepriceResult, ModelPricingRow, ModelPricingSyncResult } from "@/types";
import { useDebounce } from "@/hooks/useDebounce";
import { formatDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { Spinner } from "@filigran/design-system";

type PriceEditorState = {
  id: string | null;
  provider: string;
  model_id: string;
  model_pattern: string;
  input_price_per_1m: string;
  output_price_per_1m: string;
  cache_read_price_per_1m: string;
  cache_write_price_per_1m: string;
  effective_from: string;
  is_custom: boolean;
};

const EMPTY_EDITOR: PriceEditorState = {
  id: null,
  provider: "",
  model_id: "",
  model_pattern: "",
  input_price_per_1m: "",
  output_price_per_1m: "",
  cache_read_price_per_1m: "",
  cache_write_price_per_1m: "",
  effective_from: "",
  is_custom: true,
};

function toEditorState(row: ModelPricingRow): PriceEditorState {
  return {
    id: row.id,
    provider: row.provider,
    model_id: row.model_id,
    model_pattern: row.model_pattern,
    input_price_per_1m: String(row.input_price_per_1m),
    output_price_per_1m: String(row.output_price_per_1m),
    cache_read_price_per_1m: row.cache_read_price_per_1m === null ? "" : String(row.cache_read_price_per_1m),
    cache_write_price_per_1m: row.cache_write_price_per_1m === null ? "" : String(row.cache_write_price_per_1m),
    effective_from: row.effective_from,
    is_custom: row.is_custom,
  };
}

function toEditorStateFromModel(model: string): PriceEditorState {
  return {
    ...EMPTY_EDITOR,
    model_id: model,
  };
}

type PriceEditorField = "provider" | "model" | "input" | "output" | "cache-read" | "cache-write";

/** Field order of the form: the first invalid one takes the focus. */
const PRICE_EDITOR_FIELDS: PriceEditorField[] = ["provider", "model", "input", "output", "cache-read", "cache-write"];

function isPrice(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0;
}

function parseOptionalPrice(value: string): number | null {
  return value.trim() ? Number(value.trim()) : null;
}

export function ModelPricingSection() {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const { data: status, isLoading } = useModelPricingStatus();
  const syncFromSeed = useSyncModelPricingFromSeed();
  const importPayload = useImportModelPricingPayload();
  const repriceNullCosts = useRepriceNullCostTraces();
  const [RepriceConfirmEl, confirmReprice] = useConfirm();
  const createRow = useCreateModelPricingRow();
  const updateRow = useUpdateModelPricingRow();
  const { data: observability } = useObservabilityConfig();

  const [selectedFileName, setSelectedFileName] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<ModelPricingSyncResult | null>(null);
  const [lastRepriceResult, setLastRepriceResult] = useState<ModelPricingRepriceResult | null>(null);
  // A price dated by its administrator applies from that date: the
  // traces before it keep no cost rather than an anachronistic one.
  const [fromEffectiveDate, setFromEffectiveDate] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [editor, setEditor] = useState<PriceEditorState>(EMPTY_EDITOR);
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const [providerSuggestionsOpen, setProviderSuggestionsOpen] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<PriceEditorField, string>>>({});

  const { data: rowsData, isLoading: loadingRows } = useModelPricingRows(debouncedSearch);
  const { data: unpricedData, isLoading: loadingUnpriced } = useModelsWithoutPricingRows();

  const providers = useMemo(() => {
    const values = new Set<string>();
    for (const row of rowsData?.items ?? []) {
      if (row.provider) values.add(row.provider);
    }
    return Array.from(values).sort((a, b) => a.localeCompare(b));
  }, [rowsData?.items]);

  const filteredProviders = useMemo(() => {
    const query = editor.provider.trim().toLowerCase();
    if (!query) return providers.slice(0, 30);
    return providers
      .filter((provider) => provider.toLowerCase().includes(query))
      .slice(0, 30);
  }, [editor.provider, providers]);

  const inFlight = syncFromSeed.isPending || importPayload.isPending || repriceNullCosts.isPending;
  const savingRow = createRow.isPending || updateRow.isPending;

  const handleSyncDetectedSeed = () => {
    syncFromSeed.mutate(undefined, {
      onSuccess: (result) => {
        setLastResult(result);
        toast({ title: t_i18n("Model pricing synchronized"), description: result.message, variant: "success" });
      },
      onError: (error) => {
        toast({ title: t_i18n("Model pricing sync failed"), description: error.message, variant: "error" });
      },
    });
  };

  const handleSyncExample = (path: string) => {
    syncFromSeed.mutate(
      { source_path: path },
      {
        onSuccess: (result) => {
          setLastResult(result);
          toast({ title: t_i18n("Model pricing synchronized"), description: result.message, variant: "success" });
        },
        onError: (error) => {
          toast({ title: t_i18n("Model pricing sync failed"), description: error.message, variant: "error" });
        },
      },
    );
  };

  const runReprice = () => {
    repriceNullCosts.mutate({ enforce_effective_from: fromEffectiveDate }, {
      onSuccess: (result) => {
        setLastRepriceResult(result);
        toast({
          title: t_i18n("Traces repriced"),
          description: result.message,
          variant: "success",
        });
      },
      onError: (error) => {
        toast({ title: t_i18n("Trace repricing failed"), description: error.message, variant: "error" });
      },
    });
  };

  // A mass write, so it is previewed first: the backend's dry run
  // counts what would change without writing, and only a confirmed preview
  // runs the real reprice.
  const handleRepriceNullCosts = () => {
    repriceNullCosts.mutate(
      { dry_run: true, enforce_effective_from: fromEffectiveDate },
      {
        onSuccess: async (preview) => {
          if (preview.traces_updated === 0 && preview.spans_updated === 0) {
            toast({ title: t_i18n("Nothing to reprice"), description: preview.message, variant: "info" });
            return;
          }
          const confirmed = await confirmReprice({
            title: "Reprice $0 traces?",
            description: t_i18n(
              "{traces, plural, one {# trace} other {# traces}} and {spans, plural, one {# span} other {# spans}} without a cost will be priced at the current tariff. Costs already recorded are never changed.",
              { traces: preview.traces_updated, spans: preview.spans_updated },
            ),
            confirmLabel: "Reprice",
            variant: "warning",
          });
          if (confirmed) runReprice();
        },
        onError: (error) => {
          toast({ title: t_i18n("Trace repricing failed"), description: error.message, variant: "error" });
        },
      },
    );
  };

  const handlePickFile = () => {
    fileInputRef.current?.click();
  };

  const handleImportFile: React.ChangeEventHandler<HTMLInputElement> = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    setSelectedFileName(file.name);

    try {
      const rawText = await file.text();
      const parsed = JSON.parse(rawText) as Record<string, unknown>;
      importPayload.mutate(
        { payload: parsed },
        {
          onSuccess: (result) => {
            setLastResult(result);
            toast({
              title: t_i18n("Model pricing imported"),
              description: result.message,
              variant: "success",
            });
            if (fileInputRef.current) fileInputRef.current.value = "";
          },
          onError: (error) => {
            toast({ title: t_i18n("Model pricing import failed"), description: error.message, variant: "error" });
          },
        },
      );
    } catch {
      toast({
        title: t_i18n("Invalid JSON file"),
        description: t_i18n("The selected file is not a valid JSON payload."),
        variant: "error",
      });
      if (fileInputRef.current) fileInputRef.current.value = "";
      setSelectedFileName(null);
    }
  };

  const resetEditor = () => {
    setEditor(EMPTY_EDITOR);
    setFieldErrors({});
    setProviderSuggestionsOpen(false);
  };

  const closeEditor = () => {
    resetEditor();
    setIsEditorOpen(false);
  };

  const handleCreateNewRow = () => {
    resetEditor();
    setIsEditorOpen(true);
    setProviderSuggestionsOpen(true);
  };

  const handleEditRow = (row: ModelPricingRow) => {
    setFieldErrors({});
    setEditor(toEditorState(row));
    setIsEditorOpen(true);
    setProviderSuggestionsOpen(false);
  };

  const handleQuickCreate = (model: string) => {
    setFieldErrors({});
    setEditor(toEditorStateFromModel(model));
    setIsEditorOpen(true);
    setProviderSuggestionsOpen(true);
  };

  // Field errors are a pure function of the values and stay inline under
  // each field (aria-invalid, focus on the first one), never in a toast.
  const validateEditor = (): Partial<Record<PriceEditorField, string>> => {
    const errors: Partial<Record<PriceEditorField, string>> = {};
    if (!editor.provider.trim()) errors.provider = t_i18n("Enter the provider, e.g. openai.");
    if (!editor.model_id.trim()) errors.model = t_i18n("Enter the model id, e.g. gpt-5.");
    if (!isPrice(editor.input_price_per_1m)) errors.input = t_i18n("Enter a price of 0 or more, e.g. 1.25.");
    if (!isPrice(editor.output_price_per_1m)) errors.output = t_i18n("Enter a price of 0 or more, e.g. 1.25.");
    if (editor.cache_read_price_per_1m.trim() && !isPrice(editor.cache_read_price_per_1m)) {
      errors["cache-read"] = t_i18n("Leave it empty or enter a price of 0 or more.");
    }
    if (editor.cache_write_price_per_1m.trim() && !isPrice(editor.cache_write_price_per_1m)) {
      errors["cache-write"] = t_i18n("Leave it empty or enter a price of 0 or more.");
    }
    return errors;
  };

  const handleSaveRow = () => {
    const errors = validateEditor();
    setFieldErrors(errors);
    const firstInvalid = PRICE_EDITOR_FIELDS.find((field) => errors[field]);
    if (firstInvalid) {
      document.getElementById(`${fieldId}-${firstInvalid}`)?.focus();
      return;
    }
    // Validated above: every price parses.
    {
      const payload = {
        provider: editor.provider.trim().toLowerCase(),
        model_id: editor.model_id.trim(),
        model_pattern: editor.model_pattern.trim() || null,
        input_price_per_1m: Number(editor.input_price_per_1m.trim()),
        output_price_per_1m: Number(editor.output_price_per_1m.trim()),
        cache_read_price_per_1m: parseOptionalPrice(editor.cache_read_price_per_1m),
        cache_write_price_per_1m: parseOptionalPrice(editor.cache_write_price_per_1m),
        effective_from: editor.effective_from.trim() || null,
        is_custom: editor.is_custom,
      };

      const onSuccess = (saved: ModelPricingRow) => {
        setEditor(toEditorState(saved));
        toast({
          title: editor.id ? t_i18n("Pricing row updated") : t_i18n("Pricing row created"),
          description: `${saved.provider}/${saved.model_id}`,
          variant: "success",
        });
      };

      const onError = (error: Error) => {
        toast({ title: t_i18n("Failed to save pricing row"), description: error.message, variant: "error" });
      };

      if (editor.id) {
        updateRow.mutate({ id: editor.id, body: payload }, { onSuccess, onError });
      } else {
        createRow.mutate(payload, { onSuccess, onError });
      }
    }
  };

  return (
    <>
      {/* FR-022: an AI settings section renders read-only with the reason
          stated, rather than disappearing. */}
      <AiDisabledNotice />
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Model Pricing")}</p>
              <p className="content-compact text-default-secondary">
                {t_i18n("Configure pricing seeds, import JSON examples, and manage provider/model prices.")}
              </p>
            </div>
            {isLoading ? (
              <Badge variant="outline" className="content-caption">{t_i18n("Loading")}</Badge>
            ) : status?.source_exists && status?.source_readable ? (
              <Badge variant="success" className="content-caption">
                <CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Seed detected")}
              </Badge>
            ) : (
              <Badge variant="outline" className="content-caption">{t_i18n("Seed not detected")}</Badge>
            )}
          </div>

          <div className="rounded-sm bg-elevation-default p-4 space-y-2 content-compact">
            <p className="text-default-secondary">{t_i18n("Last pricing sync")}</p>
            {observability?.pricing_last_updated_at ? (
              <>
                <p className="text-default-secondary">{formatDateTime(observability.pricing_last_updated_at)}</p>
                {observability.pricing_staleness_days !== null ? (
                  <p className="text-default-secondary">
                    {t_i18n("{count, plural, =0 {today} one {# day ago} other {# days ago}}", { count: observability.pricing_staleness_days })}
                  </p>
                ) : null}
              </>
            ) : (
              <p className="text-default-secondary">{t_i18n("No sync recorded yet")}</p>
            )}
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              onClick={handleSyncDetectedSeed}
              disabled={inFlight || !status?.source_exists || aiOff}
              className="gap-1.5" aria-busy={syncFromSeed.isPending || undefined}>
              {syncFromSeed.isPending && <Spinner size="sm" tone="inherit" />}{!syncFromSeed.isPending && <RefreshCw className="h-3.5 w-3.5" />}
              {t_i18n("Sync detected seed")}
            </Button>

            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handlePickFile}
              disabled={inFlight}
              className="gap-1.5" aria-busy={importPayload.isPending || undefined}>
              {importPayload.isPending && <Spinner size="sm" tone="inherit" />}{!importPayload.isPending && <Upload className="h-3.5 w-3.5" />}
              {t_i18n("Import JSON file")}
            </Button>

            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => setDrawerOpen(true)}
              className="gap-1.5"
            >
              <List className="h-3.5 w-3.5" />
              {t_i18n("Manage model prices")}
            </Button>

            {RepriceConfirmEl}
            <Tip content={t_i18n("Backfill costs for traces whose model had no pricing entry when they ran. Only traces without a cost are updated, at the current tariff.")}>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleRepriceNullCosts}
                disabled={inFlight || aiOff}
                className="gap-1.5" aria-busy={repriceNullCosts.isPending || undefined}>
                {repriceNullCosts.isPending && <Spinner size="sm" tone="inherit" />}{!repriceNullCosts.isPending && <Calculator className="h-3.5 w-3.5" />}
                {t_i18n("Reprice $0 traces")}
              </Button>
            </Tip>
            <span className="flex items-center gap-2 content-compact text-default-secondary">
              <Switch
                aria-label={t_i18n("Only from the price's effective date")}
                checked={fromEffectiveDate}
                onCheckedChange={setFromEffectiveDate}
                disabled={inFlight || aiOff}
              />
              {t_i18n("Only from the price's effective date")}
            </span>

            <input
              ref={fileInputRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={handleImportFile}
            />
          </div>

          {selectedFileName ? (
            <p className="content-compact text-default-secondary inline-flex items-center gap-1.5">
              <FileJson className="h-3.5 w-3.5" /> {t_i18n("Selected file: {name}", { name: selectedFileName })}
            </p>
          ) : null}

          {status?.examples && status.examples.length > 0 ? (
            <div className="space-y-2">
              <p className="content-compact font-medium text-default-secondary">{t_i18n("Example files")}</p>
              <div className="space-y-2">
                {status.examples.map((example) => (
                  <div
                    key={example.key}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2"
                  >
                    <div className="space-y-1">
                      <p className="content-compact font-medium">{example.label}</p>
                      <p className="content-caption text-default-secondary">{example.path}</p>
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={inFlight || !example.exists || aiOff}
                      onClick={() => handleSyncExample(example.path)}
                      className="gap-1.5"
                    >
                      <Download className="h-3.5 w-3.5" />
                      {t_i18n("Sync with this file")}
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {lastResult ? (
            <div className="rounded-sm border border-feedback-success-primary/30 bg-feedback-success-primary/10 p-3 content-compact text-feedback-success-primary">
              <p className="font-medium text-feedback-success-primary">{t_i18n("Model pricing saved")}</p>
              <p className="mt-1">{lastResult.message}</p>
              <p className="mt-1">
                {t_i18n("Synced: {count}", { count: lastResult.synced_count })} | {t_i18n("Custom skipped: {count}", { count: lastResult.skipped_custom_count })}
              </p>
            </div>
          ) : null}

          {lastRepriceResult ? (
            <div className="rounded-sm border border-feedback-success-primary/30 bg-feedback-success-primary/10 p-3 content-compact text-feedback-success-primary">
              <p className="font-medium text-feedback-success-primary">{t_i18n("Traces repriced")}</p>
              <p className="mt-1">{lastRepriceResult.message}</p>
              <p className="mt-1">
                {t_i18n("Traces: {count}", { count: lastRepriceResult.traces_updated })} | {t_i18n("Spans: {count}", { count: lastRepriceResult.spans_updated })} | {t_i18n("Days re-aggregated: {count}", { count: lastRepriceResult.days_reaggregated })}
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Dialog
        open={drawerOpen}
        onOpenChange={(open) => {
          setDrawerOpen(open);
          if (!open) {
            setIsEditorOpen(false);
            resetEditor();
            queryClient.invalidateQueries({ queryKey: ["observability-config"] });
            queryClient.invalidateQueries({ queryKey: ["model-pricing-status"] });
            queryClient.invalidateQueries({ queryKey: ["model-pricing-unpriced-models"] });
          }
        }}
      >
        <DialogContent className="left-auto right-0 top-0 flex h-dvh max-h-dvh w-[min(92vw,1480px)] max-w-none translate-x-0 translate-y-0 flex-col gap-0 rounded-none border-l border-elevation-subtle p-0 data-[state=closed]:slide-out-to-right-full data-[state=open]:slide-in-from-right-full data-[state=closed]:zoom-out-100 data-[state=open]:zoom-in-100 overflow-hidden">
          <DialogHeader className="px-6 pt-6 pb-3 border-b border-elevation-subtle-soft">
            <DialogTitle className="text-base">{t_i18n("Model prices")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Edit existing prices, create new provider/model rows, and bootstrap rows from unresolved models.")}
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 grid grid-cols-12 gap-0">
            <section className="col-span-8 border-r border-elevation-subtle-soft min-h-0 flex flex-col">
              <div className="px-6 py-4 border-b border-elevation-subtle-soft flex flex-wrap items-center gap-2">
                <div className="relative min-w-[280px] flex-1">
                  <Search className="h-4 w-4 absolute left-2 top-2.5 text-default-secondary" />
                  <Input aria-label={t_i18n("Search provider, model id, or regex pattern")}
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder={t_i18n("Search provider, model id, or regex pattern")}
                    className="h-9 pl-8"
                  />
                </div>
                <Button type="button" size="sm" variant="outline" className="gap-1.5" onClick={handleCreateNewRow}>
                  <Plus className="h-3.5 w-3.5" />
                  {t_i18n("New row")}
                </Button>
              </div>

              <div className="px-6 py-3 content-compact text-default-secondary border-b border-elevation-subtle-soft">
                {loadingRows ? t_i18n("Loading pricing rows...") : t_i18n("{count, plural, one {# pricing row} other {# pricing rows}}", { count: rowsData?.total ?? 0 })}
              </div>

              <div className="min-h-0 flex-1 overflow-auto px-6 pb-6">
                <div className="mt-3 space-y-2">
                  {(rowsData?.items ?? []).map((row) => (
                    <div
                      key={row.id}
                      className="rounded-sm border border-elevation-subtle-soft bg-elevation-default px-3 py-3 flex items-start justify-between gap-3"
                    >
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="outline" className="content-caption">{row.provider}</Badge>
                          <p className="content-compact font-medium break-all">{row.model_id}</p>
                          {row.is_custom ? (
                            <Badge variant="success" className="content-caption">{t_i18n("Custom")}</Badge>
                          ) : (
                            <Badge variant="outline" className="content-caption">{t_i18n("Seed")}</Badge>
                          )}
                        </div>
                        <p className="content-caption text-default-secondary">
                          input ${row.input_price_per_1m}/1M | output ${row.output_price_per_1m}/1M
                        </p>
                        {(row.cache_read_price_per_1m !== null || row.cache_write_price_per_1m !== null) ? (
                          <p className="content-caption text-default-secondary">
                            cache read {row.cache_read_price_per_1m ?? "—"} | cache write {row.cache_write_price_per_1m ?? "—"}
                          </p>
                        ) : null}
                        <p className="content-caption text-default-secondary inline-flex items-center gap-1">
                          <ChevronsUpDown className="h-3 w-3" /> {t_i18n("effective from {date}", { date: row.effective_from })}
                        </p>
                        {row.model_pattern ? (
                          <p className="content-caption text-default-secondary break-all">regex: {row.model_pattern}</p>
                        ) : null}
                      </div>

                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="gap-1.5 shrink-0"
                        onClick={() => handleEditRow(row)}
                      >
                        <Edit3 className="h-3.5 w-3.5" />
                        {t_i18n("Edit")}
                      </Button>
                    </div>
                  ))}

                  {!loadingRows && (rowsData?.items?.length ?? 0) === 0 ? (
                    <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-5 text-center content-compact text-default-secondary">
                      {t_i18n("No pricing rows found for this filter.")}
                    </div>
                  ) : null}
                </div>
              </div>
            </section>

            <section className="col-span-4 min-h-0 flex flex-col">
              <div className="px-6 py-4 border-b border-elevation-subtle-soft">
                <p className="content-base font-medium">
                  {isEditorOpen ? (editor.id ? t_i18n("Edit pricing row") : t_i18n("Create pricing row")) : t_i18n("Pricing editor")}
                </p>
                <p className="content-compact text-default-secondary mt-1">
                  {isEditorOpen
                    ? t_i18n("Add a provider/model price or quickly create a row from unresolved models.")
                    : t_i18n("Click + New row to open the creation form, or Edit on an existing row.")}
                </p>
              </div>

              <div className="min-h-0 flex-1 overflow-auto px-6 py-4 space-y-3">
                {isEditorOpen ? (
                  <>
                    <SettingsField
                      id={`${fieldId}-provider`}
                      label={t_i18n("Provider")}
                      helper={t_i18n("The provider that bills the model, as traces name it, e.g. openai. Custom for a model of your own.")}
                      info={t_i18n("The provider name on the traces this price applies to, e.g. openai or anthropic. Cost analytics match a trace on provider and model id. Choose Custom for a self-hosted or private model.")}
                      error={fieldErrors.provider}
                      required
                    >
                      <div className="flex items-center gap-2">
                        <div className="relative flex-1">
                        <Input
                          id={`${fieldId}-provider`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-provider`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-provider`)}
                          aria-required
                          aria-invalid={fieldErrors.provider ? true : undefined}
                          value={editor.provider}
                          onFocus={() => setProviderSuggestionsOpen(true)}
                          onBlur={() => {
                            window.setTimeout(() => {
                              setProviderSuggestionsOpen(false);
                            }, 120);
                          }}
                          onChange={(event) => {
                            setEditor((prev) => ({ ...prev, provider: event.target.value }));
                            setProviderSuggestionsOpen(true);
                          }}
                          placeholder="openai"
                          className="h-8 content-compact"
                        />

                        {providerSuggestionsOpen ? (
                          <div className="absolute left-0 top-[calc(100%+4px)] z-20 w-full rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-1 shadow-overlay max-h-44 overflow-auto">
                            {filteredProviders.length > 0 ? (
                              filteredProviders.map((provider) => (
                                <button
                                  key={provider}
                                  type="button"
                                  onMouseDown={(event) => {
                                    event.preventDefault();
                                    setEditor((prev) => ({ ...prev, provider }));
                                    setProviderSuggestionsOpen(false);
                                  }}
                                  className="w-full text-left content-compact rounded-sm px-2 py-1.5 text-default-secondary hover:bg-elevation-hover"
                                >
                                  {provider}
                                </button>
                              ))
                            ) : (
                              <p className="px-2 py-1.5 content-compact text-default-secondary">
                                {t_i18n("No provider matches your search")}
                              </p>
                            )}
                          </div>
                        ) : null}
                        </div>

                        <Button
                          type="button"
                          variant="outline"
                          className="shrink-0"
                          onMouseDown={(event) => event.preventDefault()}
                          onClick={() => {
                            setEditor((prev) => ({ ...prev, provider: "custom" }));
                            setProviderSuggestionsOpen(false);
                          }}
                        >
                          {t_i18n("Custom")}
                        </Button>
                      </div>
                    </SettingsField>

                    <SettingsField
                      id={`${fieldId}-model`}
                      label={t_i18n("Model id")}
                      helper={t_i18n("The model name exactly as traces report it, e.g. gpt-5.")}
                      info={t_i18n("The exact model name the provider reports on each trace, e.g. gpt-5 or claude-sonnet-4. Cost analytics price every trace of this model with this row. A dated variant needs its own row or a regex pattern.")}
                      error={fieldErrors.model}
                      required
                    >
                      <Input
                        id={`${fieldId}-model`}
                        aria-labelledby={settingsFieldLabelId(`${fieldId}-model`)}
                        aria-describedby={settingsFieldHintId(`${fieldId}-model`)}
                        aria-required
                        aria-invalid={fieldErrors.model ? true : undefined}
                        value={editor.model_id}
                        onChange={(event) => setEditor((prev) => ({ ...prev, model_id: event.target.value }))}
                        placeholder="gpt-5"
                        className="h-8 content-compact"
                      />
                    </SettingsField>

                    <SettingsField
                      id={`${fieldId}-pattern`}
                      label={t_i18n("Model regex pattern (optional)")}
                      helper={t_i18n("Prices every model whose name matches, e.g. ^gpt-5-.*$. Empty: the model id only.")}
                      info={t_i18n("A regular expression tested against the model name of each trace. Use it for dated or suffixed variants of one model, e.g. ^gpt-5-.*$ for gpt-5-mini and gpt-5-2026-08.")}
                    >
                      <Input
                        id={`${fieldId}-pattern`}
                        aria-labelledby={settingsFieldLabelId(`${fieldId}-pattern`)}
                        aria-describedby={settingsFieldHintId(`${fieldId}-pattern`)}
                        value={editor.model_pattern}
                        onChange={(event) => setEditor((prev) => ({ ...prev, model_pattern: event.target.value }))}
                        placeholder="^gpt-5-.*$"
                        className="h-8 content-compact"
                      />
                    </SettingsField>

                    <div className="grid grid-cols-2 gap-2">
                      <SettingsField
                        id={`${fieldId}-input`}
                        label={t_i18n("Input $/1M")}
                        helper={t_i18n("Price in USD per 1M input tokens, e.g. 1.25")}
                        info={t_i18n("What the provider charges, in US dollars, for one million prompt tokens sent to the model, e.g. 1.25. Cost analytics multiply it by the input tokens of each trace. Enter 0 for a free model.")}
                        error={fieldErrors.input}
                        required
                      >
                        <Input
                          id={`${fieldId}-input`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-input`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-input`)}
                          aria-required
                          aria-invalid={fieldErrors.input ? true : undefined}
                          inputMode="decimal"
                          value={editor.input_price_per_1m}
                          onChange={(event) => setEditor((prev) => ({ ...prev, input_price_per_1m: event.target.value }))}
                          placeholder="1.25"
                          className="h-8 content-compact"
                        />
                      </SettingsField>
                      <SettingsField
                        id={`${fieldId}-output`}
                        label={t_i18n("Output $/1M")}
                        helper={t_i18n("Price in USD per 1M output tokens, e.g. 4.0")}
                        info={t_i18n("What the provider charges, in US dollars, for one million tokens the model generates, e.g. 4.0. Cost analytics multiply it by the output tokens of each trace. Enter 0 for a free model.")}
                        error={fieldErrors.output}
                        required
                      >
                        <Input
                          id={`${fieldId}-output`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-output`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-output`)}
                          aria-required
                          aria-invalid={fieldErrors.output ? true : undefined}
                          inputMode="decimal"
                          value={editor.output_price_per_1m}
                          onChange={(event) => setEditor((prev) => ({ ...prev, output_price_per_1m: event.target.value }))}
                          placeholder="4.0"
                          className="h-8 content-compact"
                        />
                      </SettingsField>
                    </div>

                    <div className="grid grid-cols-2 gap-2">
                      <SettingsField
                        id={`${fieldId}-cache-read`}
                        label={t_i18n("Cache read $/1M")}
                        helper={t_i18n("Price in USD per 1M cached input tokens read, e.g. 0.125. Empty: not billed.")}
                        info={t_i18n("What the provider charges, in US dollars, for one million prompt tokens served from its cache, e.g. 0.125. Cost analytics use it for the cached tokens of each trace. Empty: cached tokens cost nothing.")}
                        error={fieldErrors["cache-read"]}
                      >
                        <Input
                          id={`${fieldId}-cache-read`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-cache-read`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-cache-read`)}
                          aria-invalid={fieldErrors["cache-read"] ? true : undefined}
                          inputMode="decimal"
                          value={editor.cache_read_price_per_1m}
                          onChange={(event) => setEditor((prev) => ({ ...prev, cache_read_price_per_1m: event.target.value }))}
                          placeholder={t_i18n("Optional")}
                          className="h-8 content-compact"
                        />
                      </SettingsField>
                      <SettingsField
                        id={`${fieldId}-cache-write`}
                        label={t_i18n("Cache write $/1M")}
                        helper={t_i18n("Price in USD per 1M input tokens written to the cache, e.g. 1.5. Empty: not billed.")}
                        info={t_i18n("What the provider charges, in US dollars, for one million prompt tokens it stores in its cache, e.g. 1.5. Cost analytics use it for the cache writes of each trace. Empty: cache writes cost nothing.")}
                        error={fieldErrors["cache-write"]}
                      >
                        <Input
                          id={`${fieldId}-cache-write`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-cache-write`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-cache-write`)}
                          aria-invalid={fieldErrors["cache-write"] ? true : undefined}
                          inputMode="decimal"
                          value={editor.cache_write_price_per_1m}
                          onChange={(event) => setEditor((prev) => ({ ...prev, cache_write_price_per_1m: event.target.value }))}
                          placeholder={t_i18n("Optional")}
                          className="h-8 content-compact"
                        />
                      </SettingsField>
                    </div>

                    <SettingsField
                      id={`${fieldId}-effective`}
                      label={t_i18n("Effective from")}
                      helper={t_i18n("The first day this price applies, e.g. 2026-01-01. Empty: today.")}
                      info={t_i18n("Repricing with the effective date enforced skips the traces that started before this date: they stay without a cost rather than take a tariff that was not in force when they ran.")}
                    >
                      <div className="flex items-center gap-2">
                        <DateFilterInput
                          id={`${fieldId}-effective`}
                          aria-labelledby={settingsFieldLabelId(`${fieldId}-effective`)}
                          aria-describedby={settingsFieldHintId(`${fieldId}-effective`)}
                          value={editor.effective_from}
                          onChange={(effective_from) => setEditor((prev) => ({ ...prev, effective_from }))}
                        />
                        <Button
                          type="button"
                          variant="outline"
                          className="shrink-0"
                          onClick={() => {
                            const today = new Date().toISOString().slice(0, 10);
                            setEditor((prev) => ({ ...prev, effective_from: today }));
                          }}
                        >
                          {t_i18n("Today")}
                        </Button>
                      </div>
                    </SettingsField>
                  </>
                ) : (
                  <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-4 content-compact text-default-secondary">
                    {t_i18n("Pricing form is hidden. Use")} <span className="text-default-primary">{t_i18n("+ New row")}</span> {t_i18n("to create a row, or click")} <span className="text-default-primary">{t_i18n("Edit")}</span> {t_i18n("on an existing row.")}
                  </div>
                )}

                {!isEditorOpen ? (
                  <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <p className="content-compact font-medium">{t_i18n("Observed models without price")}</p>
                      <Badge variant="outline" className="content-caption">
                        {loadingUnpriced ? "..." : unpricedData?.total ?? 0}
                      </Badge>
                    </div>
                    <div className="max-h-[180px] overflow-auto space-y-1.5 pr-1">
                      {(unpricedData?.items ?? []).slice(0, 60).map((item) => (
                        <div key={item.model} className="flex items-center justify-between gap-2 rounded-sm border border-elevation-subtle-soft px-2 py-1.5">
                          <div className="min-w-0">
                            <Tip content={item.model}>
                              <p tabIndex={0} className={`content-caption font-medium truncate ${TIP_WRAP_FOCUS_RING}`}>{item.model}</p>
                            </Tip>
                            <p className="content-caption text-default-secondary">{t_i18n("{count, plural, one {# unresolved trace} other {# unresolved traces}}", { count: item.unresolved_count })}</p>
                          </div>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            onClick={() => handleQuickCreate(item.model)}
                          >
                            <Plus className="h-3 w-3" />
                            {t_i18n("Use")}
                          </Button>
                        </div>
                      ))}
                      {!loadingUnpriced && (unpricedData?.items?.length ?? 0) === 0 ? (
                        <p className="content-compact text-default-secondary">{t_i18n("No unresolved models detected.")}</p>
                      ) : null}
                    </div>
                  </div>
                ) : null}

                <div className="rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/10 p-2 content-caption text-feedback-warning-tertiary flex items-start gap-1.5">
                  <CircleAlert className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                  {t_i18n("Rows added here are intended as custom overrides for rapid pricing completion.")}
                </div>
              </div>

              <div className="px-6 py-3 border-t border-elevation-subtle-soft flex flex-wrap items-center justify-end gap-2">
                {isEditorOpen ? (
                  <>
                    <Button type="button" size="sm" variant="secondary" onClick={closeEditor}>{t_i18n("Cancel")}</Button>
                    <Button type="button" size="sm" onClick={handleSaveRow} disabled={savingRow || aiOff} className="gap-1.5" aria-busy={savingRow || undefined}>
                      {savingRow && <Spinner size="sm" tone="inherit" />}{!savingRow && <Save className="h-3.5 w-3.5" />}
                      {editor.id ? t_i18n("Update row") : t_i18n("Create row")}
                    </Button>
                  </>
                ) : (
                  <Button type="button" size="sm" onClick={handleCreateNewRow} className="ml-auto gap-1.5">
                    <Plus className="h-3.5 w-3.5" />
                    {t_i18n("New row")}
                  </Button>
                )}
              </div>
            </section>
          </div>

          <div className="px-6 py-2 border-t border-elevation-subtle-soft content-compact text-default-secondary flex items-center gap-1.5">
            <ExternalLink className="h-3 w-3" />
            {t_i18n("Tip: create a row from unresolved models to quickly clear unknown-cost analytics.")}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
