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

import { type ChangeEvent, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus, BookOpen, Database, FileText, AlertTriangle, List, Search, Upload, Lock } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { useGlobalKBSearch, useImportKnowledgeBase, useKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { ManagedByWorkspaceBadge } from "@/components/workspaces/ManagedByWorkspaceBadge";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { formatCompactNumber, pluralize, isEntityShared } from "@/lib/utils";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import type { GlobalRAGSearchResponse, GlobalRAGSearchResult, KnowledgeBase } from "@/types";
import { KBHealthWarnings } from "@/pages/xtm-one/KBHealthWarnings";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { formatTimeAgo } from "@/lib/relative-time";

function KBCard({ kb, userId }: { kb: KnowledgeBase; userId?: string }) {
  const { t_i18n } = useFormatter();
  const isShared = isEntityShared(kb, userId);
  return (
    <Link to={`/knowledge/${kb.id}`} className="block h-full">
      <Card className="transition-all cursor-pointer hover:bg-elevation-hover h-full">
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <Database className="h-5 w-5 text-accent" />
            </div>
            <div className="min-w-0 flex-1">
              <Tip content={kb.name}>
                <p className="content-base font-medium truncate">{kb.name}</p>
              </Tip>
              <p className="content-caption text-default-secondary line-clamp-1">{kb.description || "\u00A0"}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {kb.is_system_managed && <Badge variant="cyan" className="content-caption">{t_i18n("System")}</Badge>}
              {kb.is_read_only && (
                <Tip content={t_i18n("Read-only: locked as ground truth. Searchable, but nothing writes to it.")}>
                  <Badge variant="warning" className="content-caption gap-0.5"><Lock className="h-2 w-2" />{t_i18n("Locked")}</Badge>
                </Tip>
              )}
              {kb.managed_by_workspace && <ManagedByWorkspaceBadge workspace={kb.managed_by_workspace} />}
              {kb.is_company_managed && <CompanyManagedBadge />}
              {isShared && <SharedBadge ownerName={kb.owner_name} />}
            </div>
          </div>

          {/* Metadata — pushed to bottom */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            <Badge variant="outline" className="content-caption shrink-0 whitespace-nowrap">
              <FileText className="h-2.5 w-2.5 mr-0.5" />{formatCompactNumber(kb.document_count)} {t_i18n(pluralize(kb.document_count, "doc"))}
            </Badge>
            {kb.chunk_count > 0 && (
              <Badge variant="outline" className="content-caption shrink-0 whitespace-nowrap">{t_i18n("{count} chunks", { count: formatCompactNumber(kb.chunk_count) })}</Badge>
            )}
            {(kb.error_count ?? 0) > 0 && (
              <Tip content={t_i18n("{count} {unit} failed embedding: open the KB to re-embed", { count: kb.error_count ?? 0, unit: t_i18n(pluralize(kb.error_count ?? 0, "document")) })}>
                <Badge variant="warning" className="content-caption gap-1">
                  <AlertTriangle className="h-2.5 w-2.5" />
                </Badge>
              </Tip>
            )}
            <TagList tags={kb.tags} className="ml-auto" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

/** List-view columns — name, doc/chunk/query counts, embedding model, tags. */
function useKBColumns(userId?: string): EntityTableColumn<KnowledgeBase>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (kb) => kb.name,
      cell: (kb) => (
        <EntityNameCell
          icon={<Database className="h-4 w-4 text-accent" />}
          name={kb.name}
          description={kb.description || null}
          badges={
            <>
              {kb.is_system_managed && <Badge variant="cyan" className="content-caption">{t_i18n("System")}</Badge>}
              {kb.managed_by_workspace && <ManagedByWorkspaceBadge workspace={kb.managed_by_workspace} />}
              {kb.is_company_managed && <CompanyManagedBadge />}
              {isEntityShared(kb, userId) && <SharedBadge ownerName={kb.owner_name} />}
            </>
          }
        />
      ),
    },
    {
      key: "documents",
      header: "Documents",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (kb) => kb.document_count,
      cell: (kb) => formatCompactNumber(kb.document_count),
    },
    {
      key: "chunks",
      header: "Chunks",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (kb) => kb.chunk_count,
      cell: (kb) => formatCompactNumber(kb.chunk_count),
    },
    {
      key: "queries",
      header: "Queries",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (kb) => kb.query_count,
      cell: (kb) => formatCompactNumber(kb.query_count),
    },
    {
      key: "errors",
      header: "Errors",
      align: "right",
      className: "whitespace-nowrap",
      sortValue: (kb) => kb.error_count ?? 0,
      cell: (kb) => (kb.error_count ?? 0) > 0 ? (
        <Tip content={t_i18n("{count} {unit} failed embedding: open the KB to re-embed", { count: kb.error_count ?? 0, unit: t_i18n(pluralize(kb.error_count ?? 0, "document")) })}>
          <Badge variant="warning" className="content-caption gap-1 tabular-nums">
            <AlertTriangle className="h-2.5 w-2.5" />{kb.error_count}
          </Badge>
        </Tip>
      ) : (
        <span className="text-default-disabled">—</span>
      ),
    },
    {
      key: "embedding_model",
      header: "Embedding model",
      sortValue: (kb) => kb.embedding_model,
      cell: (kb) => (
        <span className="block max-w-[200px] truncate font-mono-plex content-caption text-default-secondary">
          {kb.embedding_model}
        </span>
      ),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (kb) => <TagList tags={kb.tags} />,
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (kb) => kb.updated_at,
      cell: (kb) => formatTimeAgo(intl, kb.updated_at),
    },
  ], [t_i18n, intl, userId]);
}

export default function KnowledgeBases() {
  const aiOff = useAiActionDisabled();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { data: kbs, isLoading } = useKnowledgeBases();
  const importKnowledgeBase = useImportKnowledgeBase();
  const globalSearch = useGlobalKBSearch();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [globalQuery, setGlobalQuery] = useState("");
  const [globalResults, setGlobalResults] = useState<GlobalRAGSearchResponse | null>(null);
  const [selectedChunk, setSelectedChunk] = useState<GlobalRAGSearchResult | null>(null);
  const [viewMode, setViewMode] = useViewMode("knowledge");
  const columns = useKBColumns(user?.id);
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allKbs, allTags, filtered } =
    useEntityFilter({ items: kbs, userId: user?.id });

  const filteredKbIds = filtered.map((kb) => kb.id);

  const handleGlobalSearch = () => {
    const query = globalQuery.trim();
    if (!query || filteredKbIds.length === 0) return;
    globalSearch.mutate(
      {
        query,
        top_k: 10,
        kb_ids: filteredKbIds,
      },
      {
        onSuccess: (data) => setGlobalResults(data),
      },
    );
  };

  const handleImportFile = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    // Ignore a second selection while an import is still in flight to avoid
    // concurrent imports and overlapping success/error toasts.
    if (importKnowledgeBase.isPending) {
      event.target.value = "";
      return;
    }
    importKnowledgeBase.mutate(files, {
      onSuccess: (kb) => {
        toast({
          title: t_i18n("Knowledge base imported"),
          description: t_i18n("The package has been imported successfully."),
          variant: "success",
        });
        navigate(`/knowledge/${kb.id}`);
      },
      onError: (error) => {
        toast({
          title: t_i18n("Import failed"),
          description: error.message,
          variant: "error",
        });
      },
    });
    event.target.value = "";
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Knowledge Bases")}</h1>
          <p className="mt-2 text-default-secondary">{t_i18n("Vectorized document collections your agents use for context and answers (RAG).")}</p>
        </div>
        <div className="flex items-center gap-3">
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip"
            multiple
            className="hidden"
            onChange={handleImportFile}
          />
          <Button variant="outline" className="gap-1.5" onClick={() => navigate("/knowledge/documents")}>
            <List className="h-4 w-4" /> {t_i18n("View Documents")}
          </Button>
          <AgenticGateDialog feature="Importing knowledge bases">
            <Button
              variant="outline"
              className="gap-1.5"
              disabled={importKnowledgeBase.isPending}
              aria-busy={importKnowledgeBase.isPending || undefined}
              onClick={() => fileInputRef.current?.click()}
            >
              {importKnowledgeBase.isPending && <Spinner size="sm" tone="inherit" />}
              {!importKnowledgeBase.isPending && <Upload className="h-4 w-4" />}
              {t_i18n("Import")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
          <AgenticGateDialog feature="Creating knowledge bases">
            <Button className="gap-1.5" onClick={() => navigate("/knowledge/new")}>
              <Plus className="h-4 w-4" /> {t_i18n("New Knowledge Base")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
        </div>
      </div>

        <>
          {/* Drawn during the first read too, at its loaded height, so the table below does
              not move when the knowledge bases land (#11345). */}
          {(isLoading || allKbs.length > 0) && (
            <div className="rounded-sm border border-accent/20 bg-accent/5 p-4 space-y-3">
              <div className="flex items-center gap-2">
                <Search className="h-4 w-4 text-accent" />
                <h2 className="content-base font-semibold">{t_i18n("Global Semantic Search")}</h2>
                {!isLoading && (
                  <Badge variant="outline" className="content-caption">
                    {t_i18n("{count} scoped KBs", { count: filteredKbIds.length })}
                  </Badge>
                )}
              </div>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Search runs only across knowledge bases matching active filters (name, visibility, tags), while preserving RBAC.")}
              </p>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-default-secondary" />
                  <Input aria-label={t_i18n("Search across filtered knowledge bases...")}
                    placeholder={t_i18n("Search across filtered knowledge bases...")}
                    value={globalQuery}
                    onChange={(e) => setGlobalQuery(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleGlobalSearch()}
                    className="pl-9"
                  />
                </div>
                <Button
                  className="gap-1.5"
                  onClick={handleGlobalSearch}
                  disabled={globalSearch.isPending || !globalQuery.trim() || filteredKbIds.length === 0 || aiOff}
                  aria-busy={globalSearch.isPending || undefined}
                >
                  {globalSearch.isPending && <Spinner size="sm" tone="inherit" />}
                  {!globalSearch.isPending && <Search className="h-3.5 w-3.5" />}
                  {t_i18n("Search")}
                </Button>
                {globalResults && (
                  <Button variant="ghost" onClick={() => setGlobalResults(null)}>
                    {t_i18n("Clear")}
                  </Button>
                )}
              </div>
            </div>
          )}

          {allKbs.length > 0 && globalSearch.isError && (
            <div className="rounded-sm bg-feedback-error-primary/5 p-3">
              <p className="content-compact text-feedback-error-primary">{globalSearch.error.message}</p>
            </div>
          )}

          {allKbs.length > 0 && globalResults && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <p className="content-compact text-default-secondary">
                  {globalResults.total === 0
                    ? t_i18n("No semantic matches in the current filtered scope")
                    : t_i18n("{count, plural, one {# result found in filtered scope} other {# results found in filtered scope}}", { count: globalResults.total })}
                </p>
              </div>
              {globalResults.results.length > 0 && (
                <div className="space-y-2">
                  {globalResults.results.map((result, i) => {
                    const pct = Math.round(result.score * 100);
                    return (
                      <div key={`${result.knowledge_base_id}-${i}`} className="rounded-sm bg-elevation-hover p-3 space-y-2 border border-elevation-subtle-soft">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-2 min-w-0">
                            <Badge variant="outline" className="content-caption font-mono-plex">{result.source_type}</Badge>
                            <Link to={`/knowledge/${result.knowledge_base_id}`}>
                              <Badge variant="secondary" className="content-caption truncate max-w-[240px] hover:bg-accent/20 hover:text-accent transition-colors">
                                {result.knowledge_base_name}
                              </Badge>
                            </Link>
                          </div>
                          <div className="flex flex-col items-end gap-1 shrink-0">
                            <Badge
                              variant={pct >= 80 ? "success" : pct >= 50 ? "cyan" : "outline"}
                              className="content-caption tabular-nums"
                            >
                              {t_i18n("{pct}% match", { pct })}
                            </Badge>
                            <button
                              type="button"
                              onClick={() => setSelectedChunk(result)}
                              className="inline-flex items-center rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-0.5 content-caption font-medium text-default-secondary hover:border-accent/40 hover:text-accent transition-colors"
                            >
                              {t_i18n("See chunk")}
                            </button>
                          </div>
                        </div>
                        <p className="content-compact text-default-secondary leading-relaxed whitespace-pre-wrap line-clamp-5">
                          {result.content}
                        </p>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {!isLoading && allKbs.length === 0 ? (
            <EmptyState
              icon={BookOpen}
              title="No knowledge bases yet"
              requiresAi
              description="Create a knowledge base from files, GitHub, Notion, or cloud storage."
              actionLabel={agenticAIEnabled ? "New Knowledge Base" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/knowledge/new") : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope, tags,
            // Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(kb) => kb.id}
              rowHref={(kb) => `/knowledge/${kb.id}`}
              defaultSortKey="name"
              storageKey="knowledge-bases"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Filter knowledge bases by name...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              view={{ value: viewMode, onChange: setViewMode }}
              gridClassName="grid gap-4 grid-cols-[repeat(auto-fill,minmax(22rem,1fr))]"
              // The first read draws the KBCard anatomy: no tag row, the badge footer (UX r2 B6 on #11349).
              renderGridSkeleton={(pageSize) => (
                <CardGridSkeleton
                  count={cardSkeletonCount(pageSize)}
                  label={t_i18n("Loading")}
                  className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(22rem,1fr))]"
                  tags={false}
                  meta="control"
                />
              )}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(22rem,1fr))]" data-testid="kb-card-grid">
                        {page.map((kb) => <KBCard key={kb.id} kb={kb} userId={user?.id} />)}
                      </div>
                    )
                  : undefined
              }
              totalAll={allKbs.length}
              externalFilterCount={selectedTags.length}
              onClearAll={() => clearAll(undefined, { keepScope: true })}
              noResultsLabel={t_i18n("No knowledge bases matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No knowledge bases matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}

          {/* Under the table: the notices mount once the knowledge bases are read, and above
              it they pushed the toolbar and the cards down by their height (#11345). */}
          <KBHealthWarnings />
        </>

      <Dialog open={!!selectedChunk} onOpenChange={(open) => { if (!open) setSelectedChunk(null); }}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t_i18n("Chunk Preview")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Full chunk content from {name}.", { name: selectedChunk?.knowledge_base_name ?? t_i18n("knowledge base") })}
            </DialogDescription>
          </DialogHeader>
          {selectedChunk && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="content-caption font-mono-plex">{selectedChunk.source_type}</Badge>
                <Link to={`/knowledge/${selectedChunk.knowledge_base_id}`} onClick={() => setSelectedChunk(null)}>
                  <Badge variant="secondary" className="content-caption hover:bg-accent/20 hover:text-accent transition-colors">
                    {selectedChunk.knowledge_base_name}
                  </Badge>
                </Link>
              </div>
              <div className="max-h-[60vh] overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <p className="content-compact leading-relaxed whitespace-pre-wrap break-words">
                  {selectedChunk.content}
                </p>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
