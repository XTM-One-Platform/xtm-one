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

import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Plus, FileText, AlignLeft,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { Button } from "@/components/ui/button";
import { usePrompts } from "@/hooks/api/usePromptApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { cn, isEntityShared } from "@/lib/utils";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { useAuth } from "@/hooks/useAuth";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { Prompt } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

function PromptCard({ prompt, userId }: { prompt: Prompt; userId?: string }) {
  const { t_i18n } = useFormatter();
  const isShared = isEntityShared(prompt, userId);
  const contentPreview = prompt.content
    ? prompt.content.length > 120
      ? prompt.content.slice(0, 120) + "…"
      : prompt.content
    : null;

  return (
    <Link to={`/prompts/${prompt.id}`} className="block h-full">
      <Card
        className={cn(
          "transition-all cursor-pointer hover:bg-elevation-hover border-elevation-subtle h-full",
        )}
      >
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <FileText className="h-5 w-5 text-accent" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium truncate">{prompt.name}</p>
              <p className="content-caption text-default-secondary truncate">{prompt.description || t_i18n("No description")}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {prompt.is_company_managed && <CompanyManagedBadge />}
              {isShared && <SharedBadge ownerName={prompt.owner_name} />}
            </div>
          </div>

          {/* Metadata — pushed to bottom */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            {contentPreview && (
              <span className="content-caption text-default-secondary truncate flex items-center gap-1">
                <AlignLeft className="h-2.5 w-2.5 shrink-0" />
                {contentPreview}
              </span>
            )}
            {!contentPreview && (
              <span className="content-caption text-default-secondary italic">{t_i18n("No content yet")}</span>
            )}
            <TagList tags={prompt.tags} className="ml-auto" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

/** List-view columns — name, content preview, tags, last modified. */
function usePromptColumns(userId?: string): EntityTableColumn<Prompt>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (p) => p.name,
      cell: (p) => (
        <EntityNameCell
          icon={<FileText className="h-4 w-4 text-accent" />}
          name={p.name}
          description={p.description || null}
          badges={
            <>
              {p.is_company_managed && <CompanyManagedBadge />}
              {isEntityShared(p, userId) && <SharedBadge ownerName={p.owner_name} />}
            </>
          }
        />
      ),
    },
    {
      key: "content",
      header: "Content",
      cell: (p) => p.content ? (
        <span className="text-[11px] text-default-secondary line-clamp-2 max-w-[420px]">{p.content}</span>
      ) : (
        <span className="text-[11px] text-default-secondary italic">{t_i18n("No content yet")}</span>
      ),
    },
    {
      key: "length",
      header: "Length",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (p) => p.content?.length ?? 0,
      cell: (p) => t_i18n("{count} chars", { count: p.content?.length ?? 0 }),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (p) => <TagList tags={p.tags} />,
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (p) => p.updated_at,
      cell: (p) => formatTimeAgo(intl, p.updated_at),
    },
  ], [t_i18n, intl, userId]);
}

export default function Prompts() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { data: prompts, isLoading } = usePrompts();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const [viewMode, setViewMode] = useViewMode("prompts");
  const columns = usePromptColumns(user?.id);
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allPrompts, allTags, filtered } =
    useEntityFilter({ items: prompts, userId: user?.id });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Prompts")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Reusable prompt templates for your agents and chat conversations.")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <AgenticGateDialog feature={t_i18n("Creating prompts")}>
            <Button className="gap-1.5" onClick={() => navigate("/prompts/new")}>
              <Plus className="h-4 w-4" /> {t_i18n("Add Prompt")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
        </div>
      </div>

        <>
          {!isLoading && allPrompts.length === 0 ? (
            <EmptyState
              icon={FileText}
              title="No prompts yet"
              requiresAi
              description="Create your first prompt template to reuse across agents and chat conversations."
              actionLabel={agenticAIEnabled ? "Add Prompt" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/prompts/new") : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope, tags,
            // Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(p) => p.id}
              rowHref={(p) => `/prompts/${p.id}`}
              defaultSortKey="name"
              storageKey="prompts"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search prompts...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              view={{ value: viewMode, onChange: setViewMode }}
              // The first read draws this page's own card: no tag row, its footer at its own height (#11345).
              renderGridSkeleton={(pageSize) => <CardGridSkeleton count={cardSkeletonCount(pageSize)} label={t_i18n("Loading")} tags={false} meta="tags" />}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((prompt) => <PromptCard key={prompt.id} prompt={prompt} userId={user?.id} />)}
                      </div>
                    )
                  : undefined
              }
              totalAll={allPrompts.length}
              externalFilterCount={selectedTags.length}
              onClearAll={() => clearAll(undefined, { keepScope: true })}
              noResultsLabel={t_i18n("No prompts matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No prompts matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}
        </>
    </div>
  );
}
