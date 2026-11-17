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

import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  FileText, Trash2, Database,
  RefreshCw, BarChart3, Settings2, Activity,
  Globe, Search, AlertTriangle, Download, Archive, Lock,
} from "lucide-react";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { ManagedByWorkspaceBadge } from "@/components/workspaces/ManagedByWorkspaceBadge";
import { useToast } from "@/components/ui/toast";
import {
  useKnowledgeBase, useKnowledgeDocuments, useKBSyncSources,
  useDeleteKnowledgeBase, useReprocessDocuments,
  useKBEmbeddingStats, useExportKnowledgeBase, useKBExportPackages,
} from "@/hooks/api/useKnowledgeBaseApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { getToken } from "@/lib/api";
import { downloadErrorDescription } from "@/lib/download-error";
import { withBasePath } from "@/lib/base-path";
import { REFETCH_INTERVAL_FAST, REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { canManageEntity, cn, isEntityShared, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { Tip } from "@/components/ui/tooltip";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { OverviewTab } from "./knowledge-base-detail/OverviewTab";
import { DocumentsTab } from "./knowledge-base-detail/DocumentsTab";
import { SourcesTab } from "./knowledge-base-detail/SourcesTab";
import { ConfigTab } from "./knowledge-base-detail/ConfigTab";
import { ActivityTab } from "./knowledge-base-detail/ActivityTab";
import { TestSearchPanel } from "./knowledge-base-detail/TestSearchPanel";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";

type Tab = "overview" | "documents" | "sources" | "config" | "activity";

/**
 * Download an export package through the authenticated API endpoint. The app
 * authenticates with a bearer token (not cookies), so a plain `window.open`
 * cannot carry credentials and would 401; fetch with the Authorization header,
 * then save the streamed blob. Throws on failure so callers can surface a toast;
 * the error carries the HTTP `status`, which `downloadErrorDescription` turns
 * into a translated description.
 */
async function downloadExportPackage(url: string, filename: string): Promise<void> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(withBasePath(url), { headers });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.clone().json();
      if (body && typeof body === "object" && "detail" in body) {
        const raw = (body as { detail: unknown }).detail;
        if (typeof raw === "string") detail = raw;
      }
    } catch {
      // Non-JSON error body — fall back to the status code.
    }
    throw Object.assign(
      new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`),
      { status: res.status },
    );
  }
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(blobUrl);
}

export default function KnowledgeBaseDetail() {
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason() ?? undefined;
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { data: kb, isLoading, refetch: refetchKb } = useKnowledgeBase(id || "");
  const { data: docs, refetch: refetchDocs } = useKnowledgeDocuments(id || "");
  const { data: syncSources, refetch: refetchSources } = useKBSyncSources(id || "");
  const { user, isAdmin } = useAuth();
  const remove = useDeleteKnowledgeBase();
  const reprocessDocs = useReprocessDocuments();
  const exportKnowledgeBase = useExportKnowledgeBase();
  const { data: exportPackages, refetch: refetchExportPackages } = useKBExportPackages(id || "");
  const { data: gaConfig } = useGeneralAssistantConfig();
  const { data: embeddingStats } = useKBEmbeddingStats(isAdmin && id ? id : "");
  const [tab] = useTabParam<Tab>("overview");
  const [showTestSearch, setShowTestSearch] = useState(false);
  const [showExportPackages, setShowExportPackages] = useState(false);
  const [exportPackagesNowMs, setExportPackagesNowMs] = useState(0);
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const displayedPackages = exportPackages ?? [];

  // Derived busy states
  const isProcessing = kb != null && kb.pending_count > 0;
  const anySyncing = syncSources?.some((s) => s.last_sync_status === "syncing") ?? false;
  const isBusy = isProcessing || anySyncing || reprocessDocs.isPending;

  // Auto-poll KB stats + documents + sync sources every 5s while busy
  useEffect(() => {
    if (!isProcessing && !anySyncing) return;
    const interval = setInterval(() => { refetchKb(); refetchDocs(); refetchSources(); }, REFETCH_INTERVAL_FAST);
    return () => clearInterval(interval);
  }, [isProcessing, anySyncing, refetchKb, refetchDocs, refetchSources]);

  // Keep render pure: capture "now" in an effect instead of calling Date.now() in JSX.
  // While the dialog stays open, refresh "now" on an interval so a package that
  // crosses its expiry is reflected (download disabled + "Expired") without the
  // user having to reopen the dialog.
  useEffect(() => {
    if (!showExportPackages) return;
    setExportPackagesNowMs(Date.now());
    const interval = setInterval(
      () => setExportPackagesNowMs(Date.now()),
      REFETCH_INTERVAL_SLOW,
    );
    return () => clearInterval(interval);
  }, [showExportPackages, displayedPackages.length]);

  if (isLoading) return <DetailPageSkeleton backTo="/knowledge" backLabel="Back to Knowledge Bases" tabs={5} descriptionLines={1} actions={2} />;
  if (!kb) return (
    <NotFoundState
      icon={Database}
      title="Knowledge base not found"
      description="This knowledge base may have been deleted or you don't have access."
      actionLabel="View All Knowledge Bases"
      onAction={() => navigate("/knowledge")}
      backTo="/knowledge"
      backLabel="Back to Knowledge Bases"
    />
  );

  // A KB a skill package installed is refused by every write route (config,
  // documents, sources, re-embed, delete), whoever asks.
  const canManage = kb.is_system_managed || kb.is_package_origin ? false : canManageEntity(kb, user?.id, isAdmin);
  const isShared = isEntityShared(kb, user?.id);

  // Dirty state warnings
  const hasEmbeddingErrors = (kb.error_count ?? 0) > 0;
  const configuredDim = gaConfig?.embedding_dimensions ?? null;
  const hasDimMismatch =
    isAdmin &&
    configuredDim !== null &&
    embeddingStats !== undefined &&
    embeddingStats.length > 0 &&
    embeddingStats.some((s) => s.dimension !== configuredDim);
  const isDirty = hasEmbeddingErrors || hasDimMismatch;

  const exportKb = () => {
    exportKnowledgeBase.mutate(kb.id, {
      onSuccess: (data) => {
        setShowExportPackages(true);
        void refetchExportPackages();
        if (data.packages.length === 1) {
          const pkg = data.packages[0];
          void downloadExportPackage(pkg.download_url, pkg.filename).catch((err) => {
            toast({
              title: t_i18n("Download failed"),
              description: downloadErrorDescription(err, pkg.filename, t_i18n),
              variant: "error",
            });
          });
        }
        toast({
          title: t_i18n("Export ready"),
          description:
            data.packages.length === 1
              ? t_i18n("Your knowledge base package is ready.")
              : t_i18n("{count} package parts are ready for download.", { count: data.packages.length }),
          variant: "success",
        });
      },
      onError: (error) => {
        toast({
          title: t_i18n("Export failed"),
          description: error.message,
          variant: "error",
        });
      },
    });
  };
  // A locked knowledge base cannot be deleted either: the server refuses it,
  // so no delete is offered. Unlock in Configuration first, which makes
  // destroying ground truth a deliberate act.
  const canDelete = !kb.is_system_managed && !kb.is_builtin && !kb.is_read_only && canManage;
  const deleteKb = async () => {
    if (await confirmSharing({ entity_type: "knowledge_base", entity_id: kb.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete knowledge base?", description: "All documents and embeddings in this knowledge base will be permanently removed." } })) remove.mutate(kb.id, { onSuccess: () => { toast({ title: t_i18n("Deleted"), variant: "success" }); navigate("/knowledge"); } });
  };
  // Below 1280 px Export, Packages and Delete are rows of the
  // "More actions" menu; Test Search and Re-embed stay in the row.
  const moreActions: OverflowMenuItem[] = [
    { id: "export", label: t_i18n("Export"), icon: Download, onSelect: exportKb, disabled: exportKnowledgeBase.isPending },
    ...(displayedPackages.length > 0
      ? [{ id: "packages", label: t_i18n("Packages"), icon: Archive, onSelect: () => setShowExportPackages((v) => !v) }]
      : []),
    ...(canDelete
      ? [{ id: "delete", label: t_i18n("Delete knowledge base"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void deleteKb(), disabled: aiOff, disabledReason: aiOff ? aiOffReason : undefined }]
      : []),
  ];

  const showSourcesTab = !kb.is_system_managed;
  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = showSourcesTab
    ? [
        { key: "overview", label: "Overview", icon: BarChart3 },
        { key: "documents", label: "Documents", icon: FileText },
        { key: "sources", label: "Import Sources", icon: Globe },
        { key: "config", label: "Configuration", icon: Settings2 },
        { key: "activity", label: "Activity", icon: Activity },
      ]
    : [
        { key: "overview", label: "Overview", icon: BarChart3 },
        { key: "documents", label: "Documents", icon: FileText },
        { key: "config", label: "Configuration", icon: Settings2 },
        { key: "activity", label: "Activity", icon: Activity },
      ];

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo="/knowledge"
        backLabel="Back to Knowledge Bases"
        icon={Database}
        title={kb.name}
        description={kb.description}
        badges={
              <>
                {kb.is_system_managed && <Badge variant="cyan" className="content-caption">{t_i18n("System managed")}</Badge>}
                {kb.managed_by_workspace && (
                  <ManagedByWorkspaceBadge workspace={kb.managed_by_workspace} link size="default" />
                )}
                {kb.is_company_managed && <CompanyManagedBadge size="default" />}
                {isShared && <SharedBadge size="default" ownerName={kb.owner_name} />}
                {/* Content lock — distinct from ReadOnlyBadge below, which is
                    about who may ADMINISTER the entity, not about its content. */}
                {kb.is_read_only && (
                  <Tip content={t_i18n("Ground truth: still fully searchable, but agents cannot write to it and its documents cannot be added, edited or deleted.")}>
                    <Badge variant="warning" className="content-caption gap-1"><Lock className="h-2.5 w-2.5" />{t_i18n("Content locked")}</Badge>
                  </Tip>
                )}
                {(kb.is_company_managed || isShared || kb.is_package_origin) && !canManage && <ReadOnlyBadge />}
              </>
        }
        actions={
          <>
            <EntityHeaderWideOnly>
            <Tip content={t_i18n("Export as ZIP package (multi-part for large KBs)")}>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5"
                disabled={exportKnowledgeBase.isPending}
                onClick={exportKb}
              >
                {exportKnowledgeBase.isPending ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Download className="h-4 w-4" />
                )}
                {t_i18n("Export")}
              </Button>
            </Tip>
            {displayedPackages.length > 0 && (
              <Tip content={t_i18n("View active export packages")}>
                <Button
                  variant={showExportPackages ? "secondary" : "ghost"}
                  size="sm"
                  className="gap-1.5"
                  onClick={() => setShowExportPackages((v) => !v)}
                >
                  <Archive className="h-4 w-4" />
                  {t_i18n("Packages")}
                </Button>
              </Tip>
            )}
            </EntityHeaderWideOnly>
            <Tip content={kb.chunk_count === 0 ? t_i18n("No chunks to search: add documents first") : t_i18n("Test RAG search")}>
              <Button
                variant={showTestSearch ? "secondary" : "ghost"}
                size="sm"
                className="gap-1.5"
                onClick={() => setShowTestSearch((v) => !v)}
                disabled={kb.chunk_count === 0}
              >
                <Search className="h-4 w-4" />
                {t_i18n("Test Search")}
              </Button>
            </Tip>
            {canManage && (
              <Button variant="ghost" size="sm" className="gap-1.5" disabled={isBusy || aiOff}
                onClick={() =>
                  reprocessDocs.mutate(
                    { kbId: kb.id, includeReady: true },
                    { onSuccess: (data) => toast({ title: data.message, variant: "success" }) }
                  )
                }>
                <RefreshCw className={cn("h-4 w-4", isBusy && "animate-spin")} />
                {reprocessDocs.isPending ? t_i18n("Re-embedding...") : anySyncing ? t_i18n("Syncing...") : isProcessing ? t_i18n("Processing...") : t_i18n("Re-embed")}
              </Button>
            )}
            {canDelete && (
              <EntityHeaderWideOnly>
              <Button aria-label={t_i18n("Delete knowledge base")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={() => void deleteKb()} disabled={aiOff}>
                <Trash2 className="h-4 w-4" />
              </Button>
              </EntityHeaderWideOnly>
            )}
            <EntityHeaderMoreActions items={moreActions} />
          </>
        }
      />

      {/* Dirty-state warning banner */}
      {isDirty && (
        <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-4 py-3 flex items-start gap-3">
          <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0 space-y-1">
            {/* Each remedy is named only where this page offers its control:
                Re-embed renders for `canManage`, and the Overview tab's
                dimension delete is refused on a package-installed KB. */}
            {hasEmbeddingErrors && (
              <p className="content-compact text-feedback-warning-primary">
                <span className="font-medium">{t_i18n("{count} {unit} failed embedding.", { count: kb.error_count ?? 0, unit: t_i18n(pluralize(kb.error_count ?? 0, "document")) })}</span>
                {canManage && (
                  <>
                    {" "}{t_i18n("Use")} <span className="font-medium">{t_i18n("Re-embed")}</span> {t_i18n("to retry.")}
                  </>
                )}
              </p>
            )}
            {hasDimMismatch && (
              <p className="content-compact text-feedback-warning-primary">
                <span className="font-medium">{t_i18n("Dimension mismatch:")}</span> {t_i18n("some stored vectors use dimensions other than the configured")}{" "}
                <span className="font-mono-plex">{configuredDim}</span>.
                {!kb.is_package_origin && (
                  <>
                    {" "}{t_i18n("Open the")} <span className="font-medium">{t_i18n("Overview")}</span> {t_i18n("tab to delete stale dimensions and re-embed.")}
                  </>
                )}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Test Search Panel */}
      {showTestSearch && <TestSearchPanel kb={kb} onClose={() => setShowTestSearch(false)} />}

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Tab content */}
      {tab === "overview" && <OverviewTab kb={kb} docs={docs} isAdmin={isAdmin} configuredDimensions={configuredDim} />}
      {tab === "documents" && <DocumentsTab kb={kb} canManage={canManage} />}
      {tab === "sources" && <SourcesTab kb={kb} canManage={canManage} />}
      {tab === "config" && <ConfigTab kb={kb} canManage={canManage} />}
      {tab === "activity" && <ActivityTab kb={kb} />}

      <Dialog open={showExportPackages} onOpenChange={setShowExportPackages}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t_i18n("Export Packages")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Download links stay available for 24 hours, then are automatically deleted.")}
            </DialogDescription>
          </DialogHeader>

          {displayedPackages.length === 0 ? (
            <p className="content-base text-default-secondary">{t_i18n("No export packages available yet.")}</p>
          ) : (
            <div className="space-y-2 max-h-[50vh] overflow-y-auto pr-1">
              {displayedPackages.map((pkg) => {
                const expiresAt = new Date(pkg.expires_at);
                const expired = Number.isNaN(expiresAt.getTime()) ? false : expiresAt.getTime() <= exportPackagesNowMs;
                return (
                  <div
                    key={pkg.id}
                    className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 flex items-center justify-between gap-3"
                  >
                    <div className="min-w-0">
                      <p className="content-base font-medium truncate">{pkg.filename}</p>
                      <p className="content-compact text-default-secondary">
                        {t_i18n("Part {part}/{total} · {size} · Expires {date}", { part: pkg.part_number, total: pkg.total_parts, size: formatBytes(pkg.file_size), date: expiresAt.toLocaleString() })}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={expired}
                      onClick={() => {
                        void downloadExportPackage(pkg.download_url, pkg.filename).catch((err) => {
                          toast({
                            title: t_i18n("Download failed"),
                            description: downloadErrorDescription(err, pkg.filename, t_i18n),
                            variant: "error",
                          });
                        });
                      }}
                    >
                      <Download className="h-3.5 w-3.5 mr-1" />
                      {expired ? t_i18n("Expired") : t_i18n("Download")}
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </DialogContent>
      </Dialog>
      {ConsentEl}
    </div>
  );
}
