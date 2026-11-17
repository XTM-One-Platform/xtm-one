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

import {
  BookOpen,
  Bot,
  Database,
  FileText,
  Globe,
  Search,
  ShieldAlert,
  Tag,
  Trash2,
  Upload,
} from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { IconButton } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { StatCard } from "@/components/ui/stat-card";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { Badge } from "@/components/ui/badge";
import { AgentsUsingCard } from "@/components/ui/agents-using-card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useDeleteEmbeddingsByDimension,
  useKBEmbeddingStats,
} from "@/hooks/api/useKnowledgeBaseApi";
import { formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { KnowledgeBase, KnowledgeDocument } from "@/types";
import { withBasePath } from "@/lib/base-path";

const SOURCE_TYPE_ICONS: Record<
  string,
  { icon?: string; lucide?: React.ElementType; label: string }
> = {
  upload: { lucide: Upload, label: "Upload" },
  text: { lucide: FileText, label: "Text" },
  markdown: { lucide: FileText, label: "Markdown" },
  url: { lucide: Globe, label: "URL" },
  assignment: { lucide: Bot, label: "Assignment" },
  website: { lucide: Globe, label: "Website" },
  notion: { icon: withBasePath("/logos/notion.svg"), label: "Notion" },
  "microsoft-drive": { icon: withBasePath("/logos/microsoft.svg"), label: "OneDrive" },
  "google-drive": { icon: withBasePath("/logos/google.svg"), label: "Google Drive" },
  github: { icon: withBasePath("/logos/github.svg"), label: "GitHub" },
  drata: { icon: withBasePath("/logos/drata.svg"), label: "Drata" },
};

export function OverviewTab({
  kb,
  docs,
  isAdmin,
  configuredDimensions,
}: {
  kb: KnowledgeBase;
  docs?: KnowledgeDocument[];
  isAdmin?: boolean;
  configuredDimensions?: number | null;
}) {
  const { t_i18n } = useFormatter();
  const readyDocs = kb.ready_count ?? 0;
  const pendingDocs = kb.pending_count ?? 0;
  const errorDocs = kb.error_count ?? 0;

  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.knowledge_base_ids?.includes(kb.id)) || [];

  const { data: embeddingStats } = useKBEmbeddingStats(isAdmin ? kb.id : "");
  const deleteDim = useDeleteEmbeddingsByDimension();
  // The dimension-delete route refuses a KB a skill package installed:
  // its stored dimensions are shown, never offered for deletion.
  const canDeleteDimensions = !kb.is_package_origin;
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const sourceTypes =
    docs?.reduce(
      (acc, d) => {
        acc[d.source_type] = (acc[d.source_type] || 0) + 1;
        return acc;
      },
      {} as Record<string, number>
    ) || {};
  const sourceEntries = Object.entries(sourceTypes);

  return (
    <div className="space-y-6">
      {ConfirmEl}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={FileText}
          label="Documents"
          value={kb.document_count}
          subtitle={
            errorDocs > 0
              ? t_i18n("{ready} ready, {pending} processing, {failed} failed", {
                  ready: formatCompactNumber(readyDocs),
                  pending: formatCompactNumber(pendingDocs),
                  failed: formatCompactNumber(errorDocs),
                })
              : t_i18n("{ready} ready, {pending} processing", {
                  ready: formatCompactNumber(readyDocs),
                  pending: formatCompactNumber(pendingDocs),
                })
          }
        />
        <StatCard
          icon={Database}
          label="Chunks"
          value={kb.chunk_count}
          subtitle="embedded vectors"
        />
        <StatCard
          icon={Search}
          label="KB Queries"
          value={kb.query_count}
          subtitle={kb.query_count > 0 ? "total searches" : "no queries yet"}
        />
        <Card>
          <CardContent className="pt-4 pb-3 px-4">
            <div className="flex items-center gap-2 mb-1">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">
                {t_i18n("Source Types")}
              </p>
            </div>
            <p className="text-2xl font-bold mt-1">{sourceEntries.length}</p>
            {sourceEntries.length > 0 ? (
              <div className="flex items-center mt-1">
                <div className="flex items-center -space-x-1">
                  {sourceEntries.slice(0, 6).map(([type]) => {
                    const cfg = SOURCE_TYPE_ICONS[type];
                    if (cfg?.icon) {
                      return (
                        <Tip key={type} content={`${t_i18n(cfg.label)} (${sourceTypes[type]})`}>
                          <img
                            src={cfg.icon}
                            alt={type}
                            className="h-4 w-4 rounded-full bg-elevation-hover p-0.5 ring-1 ring-focus"
                          />
                        </Tip>
                      );
                    }
                    const LIcon = cfg?.lucide ?? FileText;
                    return (
                      <Tip
                        key={type}
                        content={`${cfg?.label ? t_i18n(cfg.label) : type} (${sourceTypes[type]})`}
                      >
                        <div className="flex h-4 w-4 items-center justify-center rounded-full bg-elevation-hover ring-1 ring-focus">
                          <LIcon className="h-2.5 w-2.5 text-default-secondary" />
                        </div>
                      </Tip>
                    );
                  })}
                </div>
                {sourceEntries.length > 6 && (
                  <span className="content-caption text-default-secondary ml-2">
                    +{sourceEntries.length - 6}
                  </span>
                )}
              </div>
            ) : (
              <p className="content-caption text-default-secondary mt-0.5">{t_i18n("no documents yet")}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <BookOpen className="h-4 w-4 text-accent" />
            {t_i18n("Overview")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {kb.description && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <div className="content-compact font-medium text-default-secondary">{t_i18n("Description")}</div>
              <p className="content-base text-default-secondary">{kb.description}</p>
            </div>
          )}
          {kb.tags && kb.tags.length > 0 && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <Tag className="h-3 w-3" />
                {t_i18n("Tags")}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {kb.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="content-caption">
                    {tag}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          <TimestampGrid createdAt={kb.created_at} updatedAt={kb.updated_at} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="content-base">{t_i18n("Recent Documents")}</CardTitle>
        </CardHeader>
        <CardContent>
          {docs && docs.length > 0 ? (
            <div className="space-y-1.5">
              {docs.slice(0, 10).map((doc) => (
                <div
                  key={doc.id}
                  className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2 content-compact"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileText className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
                    <span className="truncate">{doc.filename}</span>
                    <Badge
                      variant={
                        doc.status === "ready"
                          ? "success"
                          : doc.status === "error"
                            ? "destructive"
                            : "outline"
                      }
                      className="text-[8px] shrink-0"
                    >
                      {doc.status}
                    </Badge>
                    {doc.chunk_count > 0 && (
                      <span className="text-[9px] text-default-secondary shrink-0">
                        {t_i18n("{count} chunks", { count: formatCompactNumber(doc.chunk_count) })}
                      </span>
                    )}
                  </div>
                  <span className="text-[9px] text-default-secondary shrink-0">
                    {doc.source_type}
                  </span>
                </div>
              ))}
              {docs.length > 10 && (
                <p className="content-caption text-default-secondary text-center pt-1">
                  {t_i18n("+ {count} more", { count: formatCompactNumber(docs.length - 10) })}
                </p>
              )}
            </div>
          ) : (
            <div className="text-center py-6">
              <FileText className="h-8 w-8 text-default-disabled mx-auto mb-2" />
              <p className="content-base text-default-secondary">{t_i18n("No documents yet")}</p>
            </div>
          )}
        </CardContent>
      </Card>

      {isAdmin && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="content-base flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-feedback-warning-primary" />
              {t_i18n("Stored Dimensions")}
              <Badge
                variant="outline"
                className="text-[9px] ml-1 text-feedback-warning-primary border-feedback-alert-primary/40"
              >
                {t_i18n("Admin")}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!embeddingStats ? (
              <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>
            ) : embeddingStats.length === 0 ? (
              <p className="content-compact text-default-secondary">{t_i18n("No embeddings stored yet.")}</p>
            ) : (
              <div className="space-y-2">
                {canDeleteDimensions ? (
                  <p className="content-caption text-default-secondary mb-3">
                    {t_i18n("Deleting a dimension purges matching vectors and re-queues affected documents for re-embedding with the current platform model.")}
                  </p>
                ) : (
                  <div className="mb-3">
                    <CompanyManagedBanner entityLabel="knowledge base" isPackageOrigin />
                  </div>
                )}
                {embeddingStats.map((s) => {
                  const isMismatch = configuredDimensions != null && s.dimension !== configuredDimensions;
                  const staleLabel = (
                    <span className="content-caption text-feedback-warning-primary font-medium">{t_i18n("stale")}</span>
                  );
                  return (
                  <div
                    key={s.dimension}
                    className={`flex items-center justify-between rounded-sm px-3 py-2 ${
                      isMismatch
                        ? "bg-feedback-alert-primary/5 border border-feedback-alert-primary/20"
                        : "bg-elevation-hover"
                    }`}
                  >
                    <div className="flex items-center gap-3">
                      <code className={`content-compact font-mono-plex ${isMismatch ? "text-feedback-warning-primary" : "text-accent"}`}>{s.dimension}</code>
                      <span className="content-caption text-default-secondary">{t_i18n("dims")}</span>
                      <span className="content-compact text-default-secondary">
                        {t_i18n("{count} chunks", { count: formatCompactNumber(s.chunk_count) })}
                      </span>
                      {isMismatch && (canDeleteDimensions ? (
                        <Tip content={t_i18n("Configured target is {dims} dims: delete this to re-embed at the correct dimension", { dims: configuredDimensions ?? 0 })}>
                          {staleLabel}
                        </Tip>
                      ) : staleLabel)}
                    </div>
                    {canDeleteDimensions && (
                      <IconButton
                        variant="default"
                        priority="tertiary"
                        aria-label={t_i18n("Delete dimension")}
                        icon={<Trash2 className="h-3.5 w-3.5" />}
                        className="h-7 w-7 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                        disabled={deleteDim.isPending}
                        onClick={async () => {
                          const ok = await confirm({
                            title: `Delete ${s.dimension}-dim embeddings?`,
                            description: `This will delete all ${formatCompactNumber(
                              s.chunk_count
                            )} chunks at ${s.dimension} dimensions and re-queue affected documents for re-embedding. This cannot be undone.`,
                            confirmLabel: "Delete & Re-queue",
                            variant: "danger",
                          });
                          if (!ok) {
                            return;
                          }
                          deleteDim.mutate(
                            { kbId: kb.id, dim: s.dimension },
                            {
                              onSuccess: () =>
                                toast({
                                  title: t_i18n("{dims}-dim embeddings deleted", { dims: s.dimension }),
                                  variant: "success",
                                }),
                              onError: (e) =>
                                toast({
                                  title: t_i18n("Failed to delete embeddings"),
                                  description: e.message,
                                  variant: "error",
                                }),
                            }
                          );
                        }}
                      />
                    )}
                  </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <AgentsUsingCard
        agents={boundAgents}
        entityLabel="Knowledge Base"
        emptyHint="Bind this knowledge base to an agent in the agent's settings."
      />
    </div>
  );
}
