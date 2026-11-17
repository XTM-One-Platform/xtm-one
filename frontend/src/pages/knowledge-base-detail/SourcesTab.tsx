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

import { IconButton, Spinner } from "@filigran/design-system";
import { useMemo, useState } from "react";
import {
  Clock,
  CheckCircle2,
  XCircle,
  Globe,
  Pencil,
  Power,
  Trash2,
  Plus,
  RefreshCw,
  Info,
  Lock,
} from "lucide-react";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TagInput } from "@/components/ui/tag-input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import {
  useKBSyncSources, useCreateKBSyncSource, useUpdateKBSyncSource, useDeleteKBSyncSource, useTriggerKBSyncSource, useForceResyncKBSyncSource,
} from "@/hooks/api/useKnowledgeBaseApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsCommunityEdition } from "@/hooks/usePlatformConfig";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { Tip } from "@/components/ui/tooltip";
import { AiActionGate } from "@/components/ui/ai-disabled";
import type { KnowledgeBase, KBSyncSource } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { SYNC_SOURCES, sourceLabel } from "./sync-sources";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Source sync configuration
// ---------------------------------------------------------------------------

/** Return a disabled reason when a lower-priority Notion field is shadowed. */
function notionFieldDisabledReason(
  fieldKey: string,
  config: Record<string, string>,
): string | null {
  if (fieldKey === "database_id" && config.page_id?.trim()) {
    return "A Page ID is set: this field is ignored";
  }
  if (fieldKey === "search_query") {
    if (config.page_id?.trim()) return "A Page ID is set: this field is ignored";
    if (config.database_id?.trim()) return "A Database ID is set: this field is ignored";
  }
  return null;
}

/** A list field is stored as one joined string in the source config; the form edits it as tags. */
function storedList(value: string | undefined): string[] {
  return (value ?? "").split(",").map((item) => item.trim()).filter(Boolean);
}

// ---------------------------------------------------------------------------
// Sync interval labels & helpers
// ---------------------------------------------------------------------------
const SYNC_INTERVAL_OPTIONS = [
  { value: "manual", label: "Manual only" },
  { value: "6h", label: "Every 6 hours" },
  { value: "12h", label: "Every 12 hours" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
];

function syncIntervalLabel(interval: string): string {
  return SYNC_INTERVAL_OPTIONS.find((o) => o.value === interval)?.label || interval;
}

function sourceIcon(sourceType: string): string | null {
  return SYNC_SOURCES.find((s) => s.key === sourceType)?.icon ?? null;
}

function sourceUsesLucide(sourceType: string): boolean {
  return SYNC_SOURCES.find((s) => s.key === sourceType)?.lucideIcon != null;
}

function sourceConfigSummary(sourceType: string, config: Record<string, string>): string {
  if (sourceType === "website") return config.url || "No URL";
  if (sourceType === "github" || sourceType === "github_public") return [config.repo, config.branch, config.path].filter(Boolean).join(" / ");
  if (sourceType === "notion") {
    const suffix = config.recursive === "true" ? " (recursive)" : "";
    if (config.page_id) return `Page: ${config.page_id.slice(0, 12)}…${suffix}`;
    if (config.database_id) return `Database: ${config.database_id.slice(0, 12)}…${suffix}`;
    return (config.search_query || "All pages") + suffix;
  }
  if (sourceType === "microsoft-drive") return config.folder_path || "/";
  if (sourceType === "google-drive") return config.folder_id || "root";
  if (sourceType === "drata") return config.statuses || "ACTIVE";
  return Object.values(config).filter(Boolean).join(", ") || "—";
}

// ---------------------------------------------------------------------------
// Sources Tab (sync from external sources)
// ---------------------------------------------------------------------------
export function SourcesTab({ kb, canManage }: { kb: KnowledgeBase; canManage: boolean }) {
  const aiOff = useAiActionDisabled();
  const { t_i18n, intl } = useFormatter();
  const { data: syncSources, isLoading: loadingSources } = useKBSyncSources(kb.id);
  // The viewer's integrations plus the ones this KB's sources sync through, so
  // an admin editing a colleague's source keeps its integration.
  const { data: visibleIntegrations } = useIntegrations();
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const integrations = useMemo(
    () => withBoundEntities(
      visibleIntegrations,
      allIntegrations,
      (syncSources ?? []).flatMap((source) => (source.integration_id ? [source.integration_id] : [])),
    ),
    [visibleIntegrations, allIntegrations, syncSources],
  );
  const { isAdmin } = useAuth();
  // Automated KB import sources are an Enterprise capability in XTM One mode:
  // Community Edition (no EE product attached, no Filigran license) is read-only.
  // Copilot mode (edition "") is unaffected.
  const eeBlocked = useIsCommunityEdition();
  const editable = canManage && !eeBlocked;
  const createSource = useCreateKBSyncSource();
  const updateSource = useUpdateKBSyncSource();
  const deleteSource = useDeleteKBSyncSource();
  const triggerSource = useTriggerKBSyncSource();
  const forceResync = useForceResyncKBSyncSource();

  // Build a set of providers that the user has a connected integration for
  const connectedProviders = new Set(
    integrations?.filter((i) => i.status === "connected").map((i) => i.provider) || [],
  );

  // While a source is syncing, KnowledgeBaseDetail already polls the shared
  // sync-sources query — no local poller needed here.
  const { toast } = useToast();
  const [adding, setAdding] = useState<string | null>(null);
  const [syncConfig, setSyncConfig] = useState<Record<string, string>>({});
  const [syncInterval, setSyncInterval] = useState("manual");
  const [selectedIntegrationId, setSelectedIntegrationId] = useState<string>("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editConfig, setEditConfig] = useState<Record<string, string>>({});
  const [editInterval, setEditInterval] = useState("manual");
  const [editIntegrationId, setEditIntegrationId] = useState<string>("");
  const [ConfirmEl, confirm] = useConfirm();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();

  /** One confirmation when an import source lends a credential to the
   *  knowledge base's readers. */
  const confirmSourceIntegration = (integrationIds: (string | null | undefined)[]) =>
    confirmSharing(
      {
        entity_type: "knowledge_base",
        entity_id: kb.id,
        integration_ids: integrationIds.filter((id): id is string => !!id),
      },
      { confirmLabel: t_i18n("Save and share"), unshareConfirmLabel: t_i18n("Save and stop sharing") },
    );

  const handleCreate = async (sourceType: string) => {
    const chosen = (selectedIntegrationId && selectedIntegrationId !== "auto") ? selectedIntegrationId : undefined;
    if (chosen && !(await confirmSourceIntegration([...(syncSources ?? []).map((s) => s.integration_id), chosen]))) return;
    createSource.mutate(
      { kbId: kb.id, source_type: sourceType, config: { ...syncConfig }, sync_interval: syncInterval, integration_id: (selectedIntegrationId && selectedIntegrationId !== "auto") ? selectedIntegrationId : undefined },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Sync source added"), variant: "success" });
          setAdding(null);
          setSyncConfig({});
          setSyncInterval("manual");
          setSelectedIntegrationId("");
        },
      },
    );
  };

  const handleTrigger = (source: KBSyncSource) => {
    triggerSource.mutate(
      { kbId: kb.id, sourceId: source.id },
      { onSuccess: () => toast({ title: t_i18n("Sync started in background"), variant: "success" }) },
    );
  };

  const handleForceResync = async (source: KBSyncSource) => {
    if (
      await confirm({
        title: "Force re-sync?",
        description:
          "This will reset the stuck sync status, delete all documents previously imported by this source, and start a fresh sync from scratch.",
        confirmLabel: "Force Re-sync",
        variant: "danger",
      })
    ) {
      forceResync.mutate(
        { kbId: kb.id, sourceId: source.id },
        {
          onSuccess: (data) =>
            toast({
              title: t_i18n("Force re-sync started ({count} documents purged)", { count: data.purged_documents }),
              variant: "success",
            }),
        },
      );
    }
  };

  const startEditingSource = (source: KBSyncSource) => {
    setEditingId(source.id);
    setEditConfig(source.config || {});
    setEditInterval(source.sync_interval);
    setEditIntegrationId(source.integration_id || "auto");
  };

  const handleSaveSource = async (source: KBSyncSource) => {
    const payload: { kbId: string; sourceId: string; config?: Record<string, string>; sync_interval?: string; integration_id?: string } = {
      kbId: kb.id,
      sourceId: source.id,
    };
    // Only send changed fields
    if (JSON.stringify(editConfig) !== JSON.stringify(source.config || {})) {
      payload.config = editConfig;
    }
    if (editInterval !== source.sync_interval) {
      payload.sync_interval = editInterval;
    }
    const effectiveIntId = editIntegrationId === "auto" ? "" : editIntegrationId;
    const currentIntId = source.integration_id || "";
    if (effectiveIntId !== currentIntId) {
      payload.integration_id = effectiveIntId || "";
      if (effectiveIntId && !(await confirmSourceIntegration([
        ...(syncSources ?? []).filter((s) => s.id !== source.id).map((s) => s.integration_id),
        effectiveIntId,
      ]))) return;
    }
    updateSource.mutate(payload, {
      onSuccess: () => { toast({ title: t_i18n("Source updated"), variant: "success" }); setEditingId(null); },
    });
  };

  const handleToggle = (source: KBSyncSource) => {
    updateSource.mutate(
      { kbId: kb.id, sourceId: source.id, enabled: !source.enabled },
      { onSuccess: () => toast({ title: source.enabled ? t_i18n("Sync disabled") : t_i18n("Sync enabled"), variant: "success" }) },
    );
  };

  const handleDelete = async (source: KBSyncSource) => {
    if (await confirm({ title: "Remove sync source?", description: `This will stop syncing from ${sourceLabel(source.source_type)}. Existing documents will not be removed.`, confirmLabel: "Remove", variant: "danger" })) {
      deleteSource.mutate(
        { kbId: kb.id, sourceId: source.id },
        { onSuccess: () => toast({ title: t_i18n("Sync source removed"), variant: "success" }) },
      );
    }
  };

  const availableSources = SYNC_SOURCES;

  return (
    <div className="space-y-6">
      {confirmSharingElement}
      {/* Built-in KB import sources are editable — clarify seeding behavior */}
      {kb.is_builtin && editable && (
        <div className="flex items-start gap-2 rounded-sm border border-accent/20 bg-accent/5 px-3 py-2.5">
          <Info className="h-4 w-4 text-accent mt-0.5 shrink-0" />
          <p className="content-compact text-default-secondary">
            {kb.sync_sources_customized
              ? t_i18n("Import sources are customized. Platform updates will no longer overwrite the sources of this built-in knowledge base.")
              : t_i18n("This is a built-in knowledge base. You can still customize its import sources (e.g. point to internal mirrors on airgapped deployments). Once you add, edit, or remove a source, platform updates will no longer overwrite them.")}
          </p>
        </div>
      )}

      {/* Configured sync sources */}
      {(syncSources && syncSources.length > 0) && (
        <div className="space-y-3">
          <h3 className="content-base font-semibold">{t_i18n("Configured Sources")}</h3>
          {syncSources.map((source) => (
            <Card key={source.id} className={cn("transition-all", !source.enabled && "opacity-60")}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex items-start gap-3 min-w-0 flex-1">
                    {sourceUsesLucide(source.source_type)
                      ? <Globe className="h-6 w-6 mt-0.5 shrink-0 text-accent" />
                      : <img src={sourceIcon(source.source_type) ?? ""} alt={source.source_type} className="h-6 w-6 mt-0.5 shrink-0" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="content-base font-semibold">{t_i18n(sourceLabel(source.source_type))}</p>
                        {source.enabled && source.sync_interval !== "manual" ? (
                          <Badge variant="cyan" className="content-caption gap-1"><Clock className="h-2.5 w-2.5" />{t_i18n(syncIntervalLabel(source.sync_interval))}</Badge>
                        ) : source.sync_interval === "manual" ? (
                          <Badge variant="outline" className="content-caption">{t_i18n("Manual")}</Badge>
                        ) : (
                          <Badge variant="outline" className="content-caption text-default-secondary">{t_i18n("Paused")}</Badge>
                        )}
                        {source.last_sync_status === "syncing" && <Badge variant="cyan" className="content-caption gap-0.5"><Spinner size="sm" tone="inherit" />{t_i18n("Syncing")}</Badge>}
                        {source.last_sync_status === "success" && <Badge variant="success" className="content-caption gap-0.5"><CheckCircle2 className="h-2.5 w-2.5" />{t_i18n("OK")}</Badge>}
                        {source.last_sync_status === "error" && <Badge variant="destructive" className="content-caption gap-0.5"><XCircle className="h-2.5 w-2.5" />{t_i18n("Error")}</Badge>}
                      </div>
                      <p className="content-caption text-default-secondary mt-0.5 truncate">{sourceConfigSummary(source.source_type, source.config)}</p>
                      <div className="flex items-center gap-3 mt-1 content-caption text-default-secondary">
                        {source.last_synced_at && <span>{t_i18n("Last synced {time} · {count, plural, one {# doc} other {# docs}}", { time: formatTimeAgo(intl, source.last_synced_at), count: source.last_sync_documents })}</span>}
                        {!source.last_synced_at && <span>{t_i18n("Never synced")}</span>}
                        {isAdmin && source.integration_name && (
                          <span className="text-accent/70">{t_i18n("via {name}", { name: source.integration_name })}</span>
                        )}
                      </div>
                      {source.last_sync_status === "error" && source.last_sync_error && (
                        <p className="content-caption text-feedback-error-primary mt-1 line-clamp-2">{source.last_sync_error}</p>
                      )}

                      {/* Inline full editor */}
                      {editingId === source.id && (() => {
                        const srcDef = SYNC_SOURCES.find((s) => s.key === source.source_type);
                        const providerIntegrations = srcDef?.requiredProvider
                          ? (integrations?.filter((i) => i.provider === srcDef.requiredProvider && i.status === "connected") || [])
                          : [];
                        return (
                          <div className="mt-3 rounded-sm bg-accent/5 p-3 space-y-2.5">
                            {srcDef?.fields.map((field) => {
                              const disabledReason = source.source_type === "notion" ? notionFieldDisabledReason(field.key, editConfig) : null;
                              return "type" in field && field.type === "switch" ? (
                                <div key={field.key} className="flex items-center justify-between gap-3 py-0.5">
                                  <div>
                                    <Label>{t_i18n(field.label)}</Label>
                                    {"hint" in field && field.hint && <p className="content-compact text-default-secondary">{t_i18n(field.hint)}</p>}
                                  </div>
                                  <Switch aria-label={t_i18n(field.label)} checked={editConfig[field.key] === "true"} onCheckedChange={(checked) => setEditConfig({ ...editConfig, [field.key]: checked ? "true" : "" })} />
                                </div>
                              ) : (
                                <div key={field.key} className="space-y-1.5">
                                  <Label>{t_i18n(field.label)}</Label>
                                  {/* Only the control fades when disabled: the reason under it stays at full contrast. */}
                                  <div className={cn("transition-opacity", disabledReason && "opacity-50")}>
                                    {"type" in field && field.type === "tags" ? (
                                      <TagInput aria-label={t_i18n(field.label)} aria-describedby={`kb-src-help-${source.id}-${field.key}`} preserveCase suggestFromPlatformTags={false} placeholder={t_i18n(field.placeholder)} value={storedList(editConfig[field.key])} disabled={!!disabledReason} onChange={(tags) => setEditConfig({ ...editConfig, [field.key]: tags.join(",") })} />
                                    ) : (
                                      <Input aria-label={t_i18n(field.label)} aria-describedby={`kb-src-help-${source.id}-${field.key}`} placeholder={"placeholder" in field && field.placeholder ? t_i18n(field.placeholder) : undefined} value={editConfig[field.key] || ""} disabled={!!disabledReason} onChange={(e) => setEditConfig({ ...editConfig, [field.key]: e.target.value })} />
                                    )}
                                  </div>
                                  {disabledReason
                                    ? <p id={`kb-src-help-${source.id}-${field.key}`} className="content-compact text-feedback-warning-primary">{t_i18n(disabledReason)}</p>
                                    : "hint" in field && field.hint && <p id={`kb-src-help-${source.id}-${field.key}`} className="content-compact text-default-secondary">{t_i18n(field.hint)}</p>}
                                </div>
                              );
                            })}
                            {providerIntegrations.length > 0 && (
                              <div className="space-y-1.5">
                                <Label>{t_i18n("Integration to use")}</Label>
                                <SearchableSelect
                                  aria-label={t_i18n("Integration to use")}
                                  aria-describedby={`kb-src-integration-help-${source.id}`}
                                  options={providerIntegrations.map((int) => ({
                                    value: int.id,
                                    label: int.name,
                                    description: int.is_company_managed ? t_i18n("(company)") : undefined,
                                  }))}
                                  pinned={[{ value: "auto", label: t_i18n("Auto (your default)") }]}
                                  value={editIntegrationId || "auto"}
                                  onValueChange={setEditIntegrationId}
                                />
                                <p id={`kb-src-integration-help-${source.id}`} className="content-compact text-default-secondary">{t_i18n("Credentials from this integration will be used for syncing.")}</p>
                              </div>
                            )}
                            <div className="space-y-1.5">
                              <Label htmlFor={`kb-sync-schedule-${source.id}`}>{t_i18n("Sync Schedule")}</Label>
                              <Select value={editInterval} onValueChange={setEditInterval}>
                                <SelectTrigger id={`kb-sync-schedule-${source.id}`} aria-describedby={`kb-sync-schedule-help-${source.id}`} className="w-full"><SelectValue /></SelectTrigger>
                                <SelectContent>
                                  {SYNC_INTERVAL_OPTIONS.map((opt) => (
                                    <SelectItem key={opt.value} value={opt.value}>{t_i18n(opt.label)}</SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                              <p id={`kb-sync-schedule-help-${source.id}`} className="content-compact text-default-secondary">{t_i18n("How often the source is synced again; Manual only syncs when you run it.")}</p>
                            </div>
                            <div className="flex justify-end gap-2 pt-1">
                              <Button variant="ghost" onClick={() => setEditingId(null)}>{t_i18n("Cancel")}</Button>
                              <Button onClick={() => handleSaveSource(source)} disabled={updateSource.isPending || aiOff} aria-busy={updateSource.isPending || undefined}>
                                {updateSource.isPending ? <><Spinner size="sm" tone="inherit" /> {t_i18n("Saving...")}</> : t_i18n("Save changes")}
                              </Button>
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                  {editable && (
                    <div className="flex items-center gap-1 shrink-0">
                      {/* Sync embeds, so it needs a provider (FR-018). */}
                      {source.last_sync_status === "syncing" || source.last_sync_status === "error" ? (
                        <AiActionGate>
                          <Tip content={t_i18n("Force re-sync (reset + purge + fresh start)")}>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-feedback-warning-primary hover:text-feedback-warning-primary" onClick={() => handleForceResync(source)} disabled={forceResync.isPending} aria-busy={forceResync.isPending || undefined}>
                              {forceResync.isPending && <Spinner size="sm" tone="inherit" />}{!forceResync.isPending && <RefreshCw className="h-3.5 w-3.5" />}
                            </Button>
                          </Tip>
                        </AiActionGate>
                      ) : (
                        <AiActionGate>
                          <Tip content={t_i18n("Sync now")}>
                            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => handleTrigger(source)} disabled={triggerSource.isPending} aria-busy={triggerSource.isPending || undefined}>
                              {triggerSource.isPending && <Spinner size="sm" tone="inherit" />}{!triggerSource.isPending && <RefreshCw className="h-3.5 w-3.5" />}
                            </Button>
                          </Tip>
                        </AiActionGate>
                      )}
                      <Tip content={t_i18n("Edit source")}>
                        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Edit source")} icon={<Pencil className="h-3.5 w-3.5" />} className="h-7 w-7" onClick={() => startEditingSource(source)} />
                      </Tip>
                      <Tip content={source.enabled ? t_i18n("Disable") : t_i18n("Enable")}>
                        <Button aria-label={source.enabled ? t_i18n("Disable") : t_i18n("Enable")} variant="ghost" size="icon" className={cn("h-7 w-7", source.enabled ? "text-feedback-success-primary" : "text-default-secondary")} onClick={() => handleToggle(source)} disabled={aiOff}>
                          <Power className="h-3.5 w-3.5" />
                        </Button>
                      </Tip>
                      <Tip content={t_i18n("Remove")}>
                        <Button aria-label={t_i18n("Remove")} variant="ghost" size="icon" className="h-7 w-7 text-feedback-error-primary hover:text-feedback-error-primary" onClick={() => handleDelete(source)} disabled={aiOff}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </Tip>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Read-only notice (CE-only lockdown, or non-admin on a company KB) so
          the tab is never blank. Configured sources above still render. */}
      {!editable && !loadingSources && kb.is_package_origin && (
        <CompanyManagedBanner entityLabel="knowledge base" isPackageOrigin />
      )}
      {!editable && !loadingSources && !kb.is_package_origin && (
        <Card className="border-dashed border-elevation-subtle-soft">
          <CardContent className="p-4 flex items-start gap-3">
            <Lock className="h-4 w-4 text-default-secondary mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="content-base font-medium">
                {eeBlocked
                  ? t_i18n("Automated import sources are an Enterprise capability")
                  : t_i18n("Import sources are managed by an administrator")}
              </p>
              <p className="content-compact text-default-secondary mt-0.5">
                {eeBlocked
                  ? t_i18n("Connect at least one Enterprise product (OpenCTI or OpenAEV EE) to configure automated syncing for this knowledge base. Any existing sources remain visible above.")
                  : syncSources && syncSources.length > 0
                    ? t_i18n("These sources are configured by an administrator - you can view them but not change them.")
                    : t_i18n("No import sources are configured for this knowledge base yet.")}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Add new source (editable only) */}
      {editable && availableSources.length > 0 && (
        <div className="space-y-3">
          <h3 className="content-base font-semibold">{syncSources && syncSources.length > 0 ? t_i18n("Add Another Source") : t_i18n("Import Sources")}</h3>
          <p className="content-compact text-default-secondary">{t_i18n("Connect your integrations first, then configure a sync source with optional scheduling.")}</p>

          <div className="grid gap-4 sm:grid-cols-2">
            {availableSources.map((src) => {
              const hasIntegration = !src.requiredProvider || connectedProviders.has(src.requiredProvider);
              return (
                <Card key={src.key} className={cn("transition-all", adding === src.key && "border-accent/30", !hasIntegration && "opacity-60")}>
                  <CardContent className="pt-5 space-y-3">
                    <div className="flex items-center gap-3">
                      {src.lucideIcon ? <Globe className="h-6 w-6 text-accent" /> : <img src={src.icon ?? ""} alt={src.label} className="h-6 w-6" />}
                      <div>
                        <p className="content-base font-semibold">{t_i18n(src.label)}</p>
                        <p className="content-compact text-default-secondary">{t_i18n(src.description)}</p>
                      </div>
                    </div>

                    {!hasIntegration ? (
                      <Tip content={t_i18n("To enable this source, configure a {name} integration first.", { name: t_i18n(src.label) })}>
                        <div>
                          <Button variant="secondary" size="sm" className="w-full gap-1.5" disabled>
                            <Plus className="h-3.5 w-3.5" /> {t_i18n("Configure Source")}
                          </Button>
                        <p className="content-compact text-feedback-warning-primary mt-1.5 text-center">{t_i18n("Requires a connected {name} integration", { name: t_i18n(src.label) })}</p>
                        </div>
                      </Tip>
                    ) : adding === src.key ? (
                      <div className="space-y-2">
                        {src.fields.map((field) => {
                          const disabledReason = src.key === "notion" ? notionFieldDisabledReason(field.key, syncConfig) : null;
                          return "type" in field && field.type === "switch" ? (
                            <div key={field.key} className="flex items-center justify-between gap-3 py-1">
                              <div>
                                <Label>{t_i18n(field.label)}</Label>
                                {"hint" in field && field.hint && <p className="content-compact text-default-secondary">{t_i18n(field.hint)}</p>}
                              </div>
                              <Switch aria-label={t_i18n(field.label)} checked={syncConfig[field.key] === "true"} onCheckedChange={(checked) => setSyncConfig({ ...syncConfig, [field.key]: checked ? "true" : "" })} />
                            </div>
                          ) : (
                            <div key={field.key} className="space-y-1.5">
                              <Label>{t_i18n(field.label)}</Label>
                              {/* Only the control fades when disabled: the reason under it stays at full contrast. */}
                              <div className={cn("transition-opacity", disabledReason && "opacity-50")}>
                                {"type" in field && field.type === "tags" ? (
                                  <TagInput aria-label={t_i18n(field.label)} aria-describedby={`kb-src-help-new-${src.key}-${field.key}`} preserveCase suggestFromPlatformTags={false} placeholder={t_i18n(field.placeholder)} value={storedList(syncConfig[field.key])} disabled={!!disabledReason} onChange={(tags) => setSyncConfig({ ...syncConfig, [field.key]: tags.join(",") })} />
                                ) : (
                                  <Input aria-label={t_i18n(field.label)} aria-describedby={`kb-src-help-new-${src.key}-${field.key}`} placeholder={"placeholder" in field && field.placeholder ? t_i18n(field.placeholder) : undefined} value={syncConfig[field.key] || ""} disabled={!!disabledReason} onChange={(e) => setSyncConfig({ ...syncConfig, [field.key]: e.target.value })} />
                                )}
                              </div>
                              {disabledReason
                                ? <p id={`kb-src-help-new-${src.key}-${field.key}`} className="content-compact text-feedback-warning-primary">{t_i18n(disabledReason)}</p>
                                : "hint" in field && field.hint && <p id={`kb-src-help-new-${src.key}-${field.key}`} className="content-compact text-default-secondary">{t_i18n(field.hint)}</p>}
                            </div>
                          );
                        })}
                        {/* Integration picker — select which integration to use for syncing */}
                        {src.requiredProvider && (() => {
                          const providerIntegrations = integrations?.filter(
                            (i) => i.provider === src.requiredProvider && i.status === "connected",
                          ) || [];
                          return providerIntegrations.length > 0 ? (
                            <div className="space-y-1.5">
                              <Label>{t_i18n("Integration to use")}</Label>
                              <SearchableSelect
                                aria-label={t_i18n("Integration to use")}
                                aria-describedby={`kb-src-integration-help-new-${src.key}`}
                                options={providerIntegrations.map((int) => ({
                                  value: int.id,
                                  label: int.name,
                                  description: int.is_company_managed ? t_i18n("(company)") : undefined,
                                }))}
                                pinned={[{ value: "auto", label: t_i18n("Auto (your default)") }]}
                                value={selectedIntegrationId || "auto"}
                                onValueChange={setSelectedIntegrationId}
                              />
                              <p id={`kb-src-integration-help-new-${src.key}`} className="content-compact text-default-secondary">{t_i18n("Credentials from this integration will be used for syncing.")}</p>
                            </div>
                          ) : null;
                        })()}
                        <div className="space-y-1.5">
                          <Label htmlFor={`kb-sync-schedule-new-${src.key}`}>{t_i18n("Sync Schedule")}</Label>
                          <Select value={syncInterval} onValueChange={setSyncInterval}>
                            <SelectTrigger id={`kb-sync-schedule-new-${src.key}`} aria-describedby={`kb-sync-schedule-help-new-${src.key}`} className="w-full"><SelectValue /></SelectTrigger>
                            <SelectContent>
                              {SYNC_INTERVAL_OPTIONS.map((opt) => (
                                <SelectItem key={opt.value} value={opt.value}>{t_i18n(opt.label)}</SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <p id={`kb-sync-schedule-help-new-${src.key}`} className="content-compact text-default-secondary">{t_i18n("How often the source is synced again; Manual only syncs when you run it.")}</p>
                        </div>
                        <div className="flex justify-end gap-2 pt-1">
                          <Button variant="ghost" onClick={() => { setAdding(null); setSyncConfig({}); setSyncInterval("manual"); setSelectedIntegrationId(""); }}>{t_i18n("Cancel")}</Button>
                          <Button className="gap-1.5" onClick={() => handleCreate(src.key)} disabled={createSource.isPending || aiOff}>
                            {createSource.isPending ? <><Spinner size="sm" tone="inherit" /> {t_i18n("Saving...")}</> : <><Plus className="h-4 w-4" /> {t_i18n("Add Source")}</>}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <Button variant="secondary" size="sm" className="w-full gap-1.5" onClick={() => { setAdding(src.key); setSyncConfig({}); setSyncInterval("manual"); setSelectedIntegrationId(""); }}>
                        <Plus className="h-3.5 w-3.5" /> {t_i18n("Configure Source")}
                      </Button>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </div>
      )}

      {loadingSources && <div className="flex justify-center py-8"><Spinner size="lg" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} /></div>}

      {/* Empty state if all sources configured */}
      {availableSources.length === 0 && syncSources && syncSources.length > 0 && (
        <p className="content-compact text-default-secondary text-center py-4">{t_i18n("All available source types are configured.")}</p>
      )}

      {ConfirmEl}
    </div>
  );
}
