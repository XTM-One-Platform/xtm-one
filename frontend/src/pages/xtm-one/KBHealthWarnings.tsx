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

import { Icon, Spinner } from "@filigran/design-system";
import { Fragment, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { KB_DOCUMENT_ERROR_LABELS } from "@/lib/constants";
import { cn, timeUntil } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { KnowledgeBase, KnowledgeDocumentErrorCode } from "@/types";
import { useKnowledgeBases, useReprocessAllStale } from "@/hooks/api/useKnowledgeBaseApi";
import { useAvailableModels, useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

export function KBHealthWarnings() {
  const aiOff = useAiActionDisabled();
  const { t_i18n, locale } = useFormatter();
  const navigate = useNavigate();
  const { data: kbs } = useKnowledgeBases();
  // The "reasoning provider required" banner is a setup step for a
  // deployment whose agents CAN run. On Community Edition it would send an
  // administrator to configure a provider the edition cannot use and then
  // block them anyway - the upsell notice says the honest thing instead, so
  // the models are neither fetched nor warned about until AI is licensed.
  const licensed = useAgenticAIEnabled();
  const { isAdmin } = useAuth();
  // An admin is told whether any provider is configured; anyone else whether
  // one is open to them, since a provider can be restricted to groups and
  // users they are not part of.
  const { data: availableModels } = useAvailableModels({ enabled: licensed, scope: "all" });
  const { data: gaConfig } = useGeneralAssistantConfig();
  const [staleDebugOpen, setStaleDebugOpen] = useState(false);
  const reprocessAllStale = useReprocessAllStale();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const errorKbs = (kbs ?? []).filter((kb) => (kb.error_count ?? 0) > 0);
  const totalErrors = errorKbs.reduce((sum, kb) => sum + (kb.error_count ?? 0), 0);
  // Earliest scheduled automatic re-embed across all erroring KBs — failed
  // docs self-heal via the recover_failed_documents sweeper, so the banner
  // should say WHEN instead of looking permanently stuck.
  const nextAutoRetryAt = errorKbs
    .map((kb) => kb.next_auto_retry_at)
    .filter((v): v is string => !!v)
    .sort()[0] ?? null;
  // Structural failures (no extractable text, empty content) are never
  // auto-retried — only claim "no action needed" when EVERY failed doc has a
  // retry scheduled, otherwise some docs still need a manual re-embed.
  const totalRetryable = errorKbs.reduce(
    (sum, kb) => sum + kb.retryable_error_count,
    0
  );
  const nonRetryableErrors = Math.max(0, totalErrors - totalRetryable);
  // Failures confined to KBs a skill package installed cannot be re-embedded
  // by hand (their reprocess routes refuse it): the notice names the
  // package as the remedy instead of a re-embed.
  const packageOnlyErrors = errorKbs.length > 0 && errorKbs.every((kb) => kb.is_package_origin);
  // The classified cause of each failing KB's latest error, once per
  // cause, with the platform sentence it stands for as its tooltip. A code
  // this build does not know is left out rather than shown raw.
  const latestCauses = errorKbs.reduce<{ code: KnowledgeDocumentErrorCode; sentence: string | null }[]>(
    (causes, kb) => {
      const code = kb.latest_error_code;
      if (code && code in KB_DOCUMENT_ERROR_LABELS && !causes.some((cause) => cause.code === code)) {
        causes.push({ code, sentence: kb.latest_error_message });
      }
      return causes;
    },
    [],
  );
  const processingKbs = (kbs ?? []).filter((kb) => (kb.pending_count ?? 0) > 0);
  const totalPending = processingKbs.reduce((sum, kb) => sum + (kb.pending_count ?? 0), 0);

  // Stale detection is derived from stored embedding tuples (provider/model/dimension)
  // computed server-side from the embeddings table.
  const configuredModel = gaConfig?.embedding_model ?? "";
  const configuredDimensions = gaConfig?.embedding_dimensions ?? null;
  // Whether embedding can actually run — keyed on a configured embedding
  // *provider*, NOT on model + dimension. The backend embeds with provider
  // defaults: a model-optional provider (e.g. TEI) runs with no model, and
  // OpenAI runs with no explicit dimension (native size). Requiring model +
  // dimension here raised a false "Indexing stalled" banner in those
  // perfectly-indexable states. When no embedding provider is configured,
  // queued documents can never be indexed and "Re-indexing in progress" would
  // otherwise spin forever with no way out.
  const embeddingConfigured = !!(gaConfig?.embedding_provider || gaConfig?.embedding_provider_id);
  // A KB is stale when it has at least one tuple that doesn't match the platform target.
  // KBs that also have up-to-date tuples alongside stale ones are still included —
  // the stale_chunk_count only counts the mismatching tuples, not the whole KB.
  const modelMismatchKbs = (kbs ?? []).filter((kb) => (kb.stale_chunk_count ?? 0) > 0);
  const totalStaleChunks = modelMismatchKbs.reduce(
    (sum, kb) => sum + (kb.stale_chunk_count ?? 0),
    0
  );
  // "Re-embed all" skips the knowledge bases a skill package installed: the
  // server refuses to re-embed them, while background
  // auto-remediation still re-indexes them. Count and offer only the others.
  const reembeddableStaleKbs = modelMismatchKbs.filter((kb) => !kb.is_package_origin);
  const reembeddableStaleChunks = reembeddableStaleKbs.reduce(
    (sum, kb) => sum + (kb.stale_chunk_count ?? 0),
    0
  );
  const canReembedAll = reembeddableStaleKbs.length > 0;
  const modelMismatchKbIds = modelMismatchKbs.map((kb) => kb.id).join(",");
  const processingKbIds = processingKbs.map((kb) => kb.id).join(",");
  const errorKbIds = errorKbs.map((kb) => kb.id).join(",");
  // Show the stale warning + relaunch control whenever any KB has stale stored
  // embeddings — independent of whether other KBs are erroring or processing.
  // (Previously this was suppressed when ANY KB had an error or when docs were
  // processing, which made the "Re-embed all" relaunch button disappear in
  // exactly the states where the user needed it — "impossible to relaunch".)
  const hasDetectableStaleEmbeddings = modelMismatchKbs.length > 0;
  const hasProcessing = processingKbs.length > 0;
  // Documents are queued/processing but no embedding provider is configured —
  // nothing can ever progress. Surface this distinctly instead of an endless
  // "Re-indexing in progress" banner. Only once AI is licensed: on Community
  // Edition configuring a provider changes nothing, so the notice would be a
  // dead end under the upsell notice, which is the one call to action there.
  const indexingStalled = licensed && hasProcessing && !embeddingConfigured;
  const hasReasoningModels = (availableModels ?? []).length > 0;
  const showMissingReasoningWarning =
    licensed && Array.isArray(availableModels) && !hasReasoningModels;

  const staleColumns = useMemo<DataTableColumn<KnowledgeBase>[]>(
    () => [
      {
        key: "name",
        header: "Knowledge base",
        className: "w-full max-w-0",
        cell: (kb) => (
          <Tip content={kb.name}>
            <span
              tabIndex={0}
              className="block truncate rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {kb.name}
            </span>
          </Tip>
        ),
        sortValue: (kb) => kb.name,
        csv: (kb) => kb.name,
      },
      {
        key: "tuples",
        hideable: true,
        header: "Stale tuples",
        cell: (kb) => kb.stale_tuple_count ?? 0,
        sortValue: (kb) => kb.stale_tuple_count ?? 0,
        csv: (kb) => kb.stale_tuple_count ?? 0,
        align: "right",
      },
      {
        key: "chunks",
        hideable: true,
        header: "Stale chunks",
        cell: (kb) => kb.stale_chunk_count ?? 0,
        sortValue: (kb) => kb.stale_chunk_count ?? 0,
        csv: (kb) => kb.stale_chunk_count ?? 0,
        align: "right",
      },
    ],
    [],
  );

  const handleReembedAll = async () => {
    const ok = await confirm({
      title: "Re-embed all stale knowledge bases?",
      description: t_i18n(
        "This will reset and re-queue all documents in {kbCount, plural, one {# knowledge base} other {# knowledge bases}} ({chunkCount, plural, one {# stale chunk} other {# stale chunks}}) for re-embedding with the current platform model. Documents will be temporarily unavailable for search while processing.",
        { kbCount: reembeddableStaleKbs.length, chunkCount: reembeddableStaleChunks },
      ),
      confirmLabel: "Re-embed all",
      variant: "default",
    });
    if (!ok) return;
    reprocessAllStale.mutate(undefined, {
      onSuccess: (data) => toast({ title: data.message, variant: "success" }),
      onError: (e) => toast({ title: t_i18n("Re-embed failed"), description: e.message, variant: "error" }),
    });
  };

  if (!showMissingReasoningWarning && (!kbs || kbs.length === 0)) return null;
  if (
    !showMissingReasoningWarning
    && errorKbs.length === 0
    && !hasDetectableStaleEmbeddings
    && !indexingStalled
    && !(hasProcessing && embeddingConfigured)
  ) {
    return null;
  }

  return (
    <div className="space-y-3">
      {ConfirmEl}
      {showMissingReasoningWarning && isAdmin && (
        <DashboardNotice
          variant="error"
          title={t_i18n("Reasoning provider required")}
          actions={
            <NoticeAction variant="error" onClick={() => navigate("/settings/ai/models")}>
              {t_i18n("Configure AI Models")} <Icon name="arrow-right" size={16} />
            </NoticeAction>
          }
        >
          {t_i18n("No provider is configured for Reasoning Models & Routing. This is a blocking setup state: chat, agent reasoning, and assignments will not work until at least one reasoning provider is configured.")}
        </DashboardNotice>
      )}

      {showMissingReasoningWarning && !isAdmin && (
        <DashboardNotice variant="error" title={t_i18n("No AI model available to you")}>
          {t_i18n("No AI provider is open to you, so chat, agent reasoning and assignments run on your behalf will not work. Contact your administrator to configure a provider or give you access to one.")}
        </DashboardNotice>
      )}

      {indexingStalled && (
        <DashboardNotice
          variant="error"
          title={t_i18n("Indexing stalled: no embedding provider configured")}
          actions={
            <NoticeAction variant="error" onClick={() => navigate("/settings/ai/models")}>
              {t_i18n("Configure AI Models")} <Icon name="arrow-right" size={16} />
            </NoticeAction>
          }
        >
          {t_i18n(
            "{docCount, plural, one {# document is} other {# documents are}} waiting to be indexed across {kbCount, plural, one {# knowledge base} other {# knowledge bases}}, but no embedding provider is configured, so indexing cannot run and these documents will stay pending. Configure an embedding provider to resume; documents recover automatically once it is set.",
            { docCount: totalPending, kbCount: processingKbs.length },
          )}
        </DashboardNotice>
      )}

      {hasProcessing && embeddingConfigured && (
        <DashboardNotice
          variant="progress"
          spin
          title={
            hasDetectableStaleEmbeddings
              ? t_i18n("Re-indexing in progress (auto-remediation active)")
              : t_i18n("Re-indexing in progress")
          }
          actions={
            <NoticeAction
              variant="progress"
              onClick={() =>
                navigate(
                  `/knowledge/documents?status=pending,processing&kb_ids=${encodeURIComponent(processingKbIds)}`
                )
              }
            >
              {t_i18n("View Docs")} <Icon name="arrow-right" size={16} />
            </NoticeAction>
          }
        >
          {t_i18n(
            "{docCount, plural, one {# document is} other {# documents are}} currently being re-embedded across {kbCount, plural, one {# knowledge base} other {# knowledge bases}}.",
            { docCount: totalPending, kbCount: processingKbs.length },
          )}{" "}
          {hasDetectableStaleEmbeddings
            ? t_i18n("When background auto-remediation is enabled, the platform re-indexes knowledge bases with stale embeddings using the current model.") + " "
            : ""}
          {t_i18n("Results may evolve until indexing completes.")}
        </DashboardNotice>
      )}

      {errorKbs.length > 0 && (
        <DashboardNotice
          variant="warning"
          title={
            errorKbs.length === 1
              ? t_i18n("A knowledge base needs attention")
              : t_i18n("{count} knowledge bases need attention", { count: errorKbs.length })
          }
          actions={
            <NoticeAction
              variant="warning"
              onClick={() =>
                navigate(
                  `/knowledge/documents?status=error&kb_ids=${encodeURIComponent(errorKbIds)}`
                )
              }
            >
              {t_i18n("View Docs")} <Icon name="arrow-right" size={16} />
            </NoticeAction>
          }
        >
          {t_i18n("{count, plural, one {# document} other {# documents}} failed during re-indexing across", { count: totalErrors })}{" "}
          {errorKbs.length === 1 ? (
            <span className="font-medium">{errorKbs[0].name}</span>
          ) : (
            t_i18n("{count} knowledge bases", { count: errorKbs.length })
          )}
          .{" "}
          {latestCauses.length > 0 && (
            <>
              {t_i18n("{count, plural, one {Latest cause:} other {Latest causes:}}", { count: latestCauses.length })}{" "}
              {latestCauses.map((cause, index) => (
                <Fragment key={cause.code}>
                  {index > 0 && ", "}
                  <Tip content={cause.sentence ?? undefined}>
                    <span
                      tabIndex={cause.sentence ? 0 : undefined}
                      className={cn(
                        "font-medium",
                        cause.sentence && "cursor-help rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus",
                      )}
                    >
                      {t_i18n(KB_DOCUMENT_ERROR_LABELS[cause.code])}
                    </span>
                  </Tip>
                </Fragment>
              ))}
              .{" "}
            </>
          )}
          {packageOnlyErrors ? (
            t_i18n("{count, plural, one {This knowledge base was} other {These knowledge bases were}} installed by a skill package: update or remove the skill package to rebuild {count, plural, one {it} other {them}}.", { count: errorKbs.length })
          ) : (
            <>
              {t_i18n("Open the impacted KB to inspect the error and retry re-embedding.")}{" "}
              {nextAutoRetryAt ? (
                nonRetryableErrors === 0 ? (
                  totalRetryable === 1 ? (
                    <>
                      {t_i18n("An automatic retry is scheduled")}{" "}
                      <span className="font-medium">{timeUntil(nextAutoRetryAt, locale)}</span>
                      {t_i18n("; no action is needed unless you want to re-embed sooner.")}
                    </>
                  ) : (
                    <>
                      {t_i18n("All {count} failed documents will retry automatically (next retry", { count: totalRetryable })}{" "}
                      <span className="font-medium">{timeUntil(nextAutoRetryAt, locale)}</span>
                      {t_i18n("). No action is needed unless you want to re-embed sooner.")}
                    </>
                  )
                ) : (
                  <>
                    {t_i18n("{retryable} of {total, plural, one {# failed document} other {# failed documents}} will retry automatically (next retry", { retryable: totalRetryable, total: totalErrors })}{" "}
                    <span className="font-medium">{timeUntil(nextAutoRetryAt, locale)}</span>
                    {t_i18n("); {count, plural, one {the other one needs} other {the other # need}} a manual re-embed.", { count: nonRetryableErrors })}
                  </>
                )
              ) : (
                t_i18n("No automatic retry is scheduled for these documents. Check the document list for details, then re-embed them manually.")
              )}
            </>
          )}
        </DashboardNotice>
      )}

      {hasDetectableStaleEmbeddings && (
        <DashboardNotice
          variant="warning"
          title={t_i18n("Knowledge bases have stale embeddings")}
          footer={
            staleDebugOpen ? (
              // Below the notice row, across its whole width: the stale list
              // never shares the copy column with the actions (UX r1 B1, #10233).
              <div
                data-testid="kb-stale-debug"
                className="rounded-sm border border-feedback-alert-primary/20 p-3 space-y-2 content-compact font-mono-plex"
              >
                <div className="text-feedback-warning-primary">
                  <span className="text-feedback-warning-primary">{t_i18n("Platform model:")}</span>{" "}
                  <span className="text-default-primary">{configuredModel || <em className="text-feedback-error-primary">{t_i18n("not set (empty string)")}</em>}</span>
                </div>
                <div className="text-feedback-warning-primary">
                  <span className="text-feedback-warning-primary">{t_i18n("Platform dimensions:")}</span>{" "}
                  <span className="text-default-primary">{configuredDimensions ?? <em className="text-feedback-error-primary">{t_i18n("not set")}</em>}</span>
                </div>
                <div className="text-feedback-warning-primary">
                  <span className="text-feedback-warning-primary">{t_i18n("KBs with stale stored tuples:")}</span>
                </div>
                {/* Table text in the default color, not the notice warning tone (UX r1 B2: 2.98:1 in light). */}
                <div data-testid="kb-stale-table" className="font-sans text-default-primary">
                  <DataTable
                    rows={modelMismatchKbs}
                    columns={staleColumns}
                    rowKey={(kb) => kb.id}
                    defaultSort={{ key: "chunks", direction: "desc" }}
                    searchFields={(kb) => [kb.name]}
                    searchPlaceholder={t_i18n("Search knowledge bases...")}
                    noFilters="One row per knowledge base with stale embeddings, a name and two counts: the search is the only narrowing it needs."
                    exportName="stale-knowledge-bases"
                    urlKey="kbs_"
                    storageKey="kb-health-stale"
                    emptyLabel={t_i18n("No knowledge base has stale embeddings.")}
                  />
                </div>
              </div>
            ) : undefined
          }
          actions={
            <>
              <NoticeAction
                variant="warning"
                onClick={() =>
                  navigate(
                    `/knowledge/documents?kb_ids=${encodeURIComponent(modelMismatchKbIds)}`
                  )
                }
              >
                {t_i18n("View Docs")} <Icon name="arrow-right" size={16} />
              </NoticeAction>
              {canReembedAll && (
                <AiDisabledTip><NoticeAction variant="warning" disabled={reprocessAllStale.isPending || aiOff} busy={reprocessAllStale.isPending} onClick={handleReembedAll}>
                  {reprocessAllStale.isPending && <Spinner size="sm" tone="inherit" />}
                  {!reprocessAllStale.isPending && <Icon name="refresh-cw" size={16} />}
                  {t_i18n("Re-embed all")}
                </NoticeAction></AiDisabledTip>
              )}
            </>
          }
        >
          {t_i18n("{count, plural, one {# knowledge base contains} other {# knowledge bases contain}} stored embedding tuples that differ from the platform target", { count: modelMismatchKbs.length })}
          {configuredModel ? ` (${configuredModel})` : ""}
          {configuredDimensions != null ? ` ${t_i18n("at {dims} dims", { dims: configuredDimensions })}` : ""}
          . {t_i18n("{count, plural, one {# chunk is} other {# chunks are}} affected.", { count: totalStaleChunks })}{" "}
          {canReembedAll ? (
            <>
              {t_i18n("When background auto-remediation is enabled, the platform re-indexes these automatically; use")}{" "}
              <span className="font-medium">{t_i18n("Re-embed all")}</span> {t_i18n("to start (or force) a fresh pass now.")}
            </>
          ) : (
            t_i18n("When background auto-remediation is enabled, the platform re-indexes knowledge bases with stale embeddings using the current model.")
          )}

          {/* Debug toggle */}
          <button
            className="mt-2 flex items-center gap-1 content-compact text-feedback-warning-primary hover:text-feedback-warning-primary transition-colors"
            onClick={() => setStaleDebugOpen((v) => !v)}
          >
            <Icon name="bug" size={16} />
            {t_i18n("Debug: why is this showing?")}
            <Icon name={staleDebugOpen ? "chevron-down" : "chevron-right"} size={16} />
          </button>

        </DashboardNotice>
      )}
    </div>
  );
}
