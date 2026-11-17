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

import { useMemo, useState } from "react";
import { useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  LayoutTemplate, Plus, Layers, Moon, Paperclip, Presentation as PresentationIcon, Ratio, SlidersHorizontal,
  Sparkles, Sun, Upload,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useToast } from "@/components/ui/toast";
import {
  useImportTemplateContainer,
  useTemplateContainers,
} from "@/hooks/api/useTemplateContainerApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useCompactListToolbar } from "@/hooks/useCompactToolbar";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { TagFilter } from "@/components/ui/tag-filter";
import { VisibilityEmptyHint, VisibilityFilter } from "@/components/ui/visibility-filter";
import { SearchInput } from "@/components/ui/search-input";
import { EmptyState } from "@/components/ui/empty-state";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { TagList } from "@/components/ui/tag-list";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { cn, isEntityShared, pluralize } from "@/lib/utils";
import { ASSET_LIBRARY_KIND, templateKindLabel } from "@/lib/constants";
import { useAuth } from "@/hooks/useAuth";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import type { TemplateContainer } from "@/types";

/** Sentinel for "no kind filter" — an empty string is not a valid Radix
 *  SelectItem value, and a legacy row may carry any stored kind. */
const ALL_KINDS = "__all__";

function TemplateCard({ template, userId }: { template: TemplateContainer; userId?: string }) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const isShared = isEntityShared(template, userId);
  const layoutCount = template.layouts?.length ?? 0;

  return (
    <Link to={`/templates/${template.id}`} className="block h-full">
      <Card className="transition-all cursor-pointer hover:bg-elevation-hover border-elevation-subtle h-full">
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <LayoutTemplate className="h-5 w-5 text-accent" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium truncate">{template.name}</p>
              <p className="content-caption text-default-secondary truncate">
                {template.description || t_i18n("No description")}
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {/* A built-in is company-managed too; one badge says both. */}
              {template.is_builtin ? (
                <BuiltinBadge size="sm" />
              ) : (
                template.is_company_managed && <CompanyManagedBadge />
              )}
              {isShared && <SharedBadge ownerName={template.owner_name} />}
            </div>
          </div>

          {/* What makes a template pickable at a glance — the same attributes
              agents filter on through search_templates, plus the themes it
              declares it supports: a dark-only brand shows
              "Dark" alone. */}
          <div className="flex items-center gap-1.5 mt-3 flex-wrap">
            <Badge variant="cyan" className="content-caption px-1.5 py-0">{t_i18n(templateKindLabel(template.kind))}</Badge>
            {template.supported_schemes.map((scheme) => (
              <Badge key={scheme} variant="outline" className="content-caption px-1.5 py-0 gap-1">
                {scheme === "dark" ? <Moon className="h-2.5 w-2.5" /> : <Sun className="h-2.5 w-2.5" />}
                {schemeLabel(scheme)}
              </Badge>
            ))}
            {template.aspect_ratio && (
              <Badge variant="outline" className="content-caption px-1.5 py-0 gap-1">
                <Ratio className="h-2.5 w-2.5" />
                {template.aspect_ratio}
              </Badge>
            )}
          </div>

          {/* Metadata — pushed to bottom. A legacy asset library (a kind that
              can no longer be created) has no layout catalog by design:
              "0 layouts" would read as a gap. The counts never shrink or wrap:
              the TagList takes the squeeze and folds into +N. */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            {template.kind !== ASSET_LIBRARY_KIND && (
              <span className="content-caption text-default-secondary flex shrink-0 items-center gap-1 whitespace-nowrap">
                <Layers className="h-2.5 w-2.5 shrink-0" />
                {layoutCount} {t_i18n(pluralize(layoutCount, "layout"))}
              </span>
            )}
            <span className="content-caption text-default-secondary flex shrink-0 items-center gap-1 whitespace-nowrap">
              <Paperclip className="h-2.5 w-2.5 shrink-0" />
              {template.asset_count} {t_i18n(pluralize(template.asset_count, "asset"))}
            </span>
            <TagList tags={template.tags} className="ml-auto" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

export default function Templates() {
  const compactToolbar = useCompactListToolbar();
  const navigate = useNavigate();
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const importTemplate = useImportTemplateContainer();

  // A hidden file input rather than a dialog: importing a package is a
  // single-file, no-options action, so a review step would be ceremony. The
  // backend rejects a malformed package with a readable reason, which is what
  // the error toast surfaces.
  const handleImport = async (file: File | undefined) => {
    if (!file) return;
    try {
      const imported = await importTemplate.mutateAsync(file);
      toast({
        title: t_i18n("Template imported"),
        description: imported.name,
      });
      navigate(`/templates/${imported.id}`);
    } catch (err) {
      toast({
        title: t_i18n("Import failed"),
        description: err instanceof Error ? err.message : t_i18n("Import failed"),
        variant: "error",
      });
    }
  };
  const { t_i18n } = useFormatter();
  // Fetched unfiltered: search / tag / visibility filtering is client-side via
  // useEntityFilter, consistent with every other resource list. The server-side
  // structured filters exist for the agent tool, whose result set is capped.
  const { data: templates, isLoading } = useTemplateContainers();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const {
    search, setSearch, selectedTags, setSelectedTags,
    visibilityFilter, setVisibilityFilter, visibilityCounts, allItems: allTemplates, allTags, filtered,
  } = useEntityFilter({ items: templates, userId: user?.id });

  // `kind` is exposed as its own filter because it is the coarsest axis an
  // agent narrows on (`search_templates(kind=…)` is an exact match), so the
  // list should let a human reproduce what the agent sees. Options are derived
  // from the data rather than from the closed choice so a legacy row (a kind
  // that can no longer be created) stays reachable under its own label.
  const [kindFilter, setKindFilter] = useState<string>(ALL_KINDS);
  const kinds = useMemo(
    () => [...new Set(allTemplates.map((t) => t.kind).filter(Boolean))].sort(),
    [allTemplates],
  );
  const visible = useMemo(
    () => (kindFilter === ALL_KINDS ? filtered : filtered.filter((t) => t.kind === kindFilter)),
    [filtered, kindFilter],
  );

  return (
    <div className="space-y-6">
      {/* One header row (#9301 N5, UX A2 of #10508): the shared ListPageHeader, the description in its "i". */}
      <ListPageHeader
        title={t_i18n("Templates")}
        infoLabel={t_i18n("About templates")}
        info={t_i18n("A template is a design your presentations wear: a palette per theme, fonts, logos and predefined assets.")}
        actions={
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip,.json,application/zip,application/json"
              className="hidden"
              onChange={(event) => {
                void handleImport(event.target.files?.[0]);
                // Reset so re-picking the same file fires `change` again.
                event.target.value = "";
              }}
            />
            <AgenticGateDialog feature={t_i18n("Importing templates")}>
              <Button
                variant="outline"
                className="gap-1.5"
                disabled={importTemplate.isPending}
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="h-4 w-4" /> {t_i18n("Import")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
            <AgenticGateDialog feature={t_i18n("Creating templates")}>
              <Button variant="outline" className="gap-1.5" onClick={() => navigate("/templates/new?generate=1")}>
                <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("Generate with AI")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
            <AgenticGateDialog feature={t_i18n("Creating templates")}>
              <Button className="gap-1.5" onClick={() => navigate("/templates/new")}>
                <Plus className="h-4 w-4" /> {t_i18n("Add Template")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
          </>
        }
      />

      {/* How a template is USED — the three places it enters a deck's
          life, stated once here so nobody has to guess that "using" a template
          means naming it to an agent. */}
      <div className="grid gap-3 md:grid-cols-3">
        {[
          {
            icon: LayoutTemplate,
            title: t_i18n("1. Design it"),
            text: t_i18n("Choose the themes it supports (Dark, Light or both), give each its palette, add fonts, logos and the assets decks may place."),
          },
          {
            icon: PresentationIcon,
            title: t_i18n("2. Choose it when you create a presentation"),
            text: t_i18n("New presentation asks which template to use. In a chat, an assistant asks you the same question when you have several."),
          },
          {
            icon: SlidersHorizontal,
            title: t_i18n("3. Change it anytime"),
            text: t_i18n("A deck's Design panel applies another template, or none. The deck keeps its content; the theme is limited to what the template supports."),
          },
        ].map((step) => (
          <div key={step.title} className="flex gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3 backdrop-blur-sm">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <step.icon className="h-4 w-4 text-accent" />
            </div>
            <div className="min-w-0">
              <p className="content-compact font-medium">{step.title}</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-default-secondary">{step.text}</p>
            </div>
          </div>
        ))}
      </div>

      {isLoading ? (
        <CardGridSkeleton count={cardSkeletonCount()} label={t_i18n("Loading")} />
      ) : (
        <>
          {/* Below 1280 the row never wraps: the search gives its width back down to
              its 128 px floor first, so the kind picker, Tags and the count stay on the
              one 32 px row at 1024 in every locale (UX r2 drift note on #10207);
              list-toolbar-compact.test.ts keeps it. */}
          <div data-testid="templates-toolbar" className={cn("flex items-center gap-3", compactToolbar ? "flex-nowrap" : "flex-wrap")}>
            <SearchInput toolbar compact={compactToolbar} placeholder={t_i18n("Search templates...")} value={search} onChange={setSearch} />
            <VisibilityFilter
              value={visibilityFilter}
              onChange={setVisibilityFilter}
              counts={visibilityCounts}
              iconOnly={compactToolbar}
            />
            {kinds.length > 1 && (
              <SearchableSelect
                aria-label={t_i18n("Kind")}
                searchable="auto"
                className="h-8 w-48 shrink-0"
                options={kinds.map((kind) => ({ value: kind, label: t_i18n(templateKindLabel(kind)) }))}
                pinned={[{ value: ALL_KINDS, label: t_i18n("All kinds") }]}
                value={kindFilter}
                onValueChange={setKindFilter}
              />
            )}
            {/* The tags sit behind one pill so the toolbar stays one row at 1024. */}
            <TagFilter collapsed allTags={allTags} selected={selectedTags} onChange={setSelectedTags} />
            <span className="ml-auto shrink-0 rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary">
              {allTemplates.length}
            </span>
          </div>

          {allTemplates.length === 0 ? (
            <EmptyState
              icon={LayoutTemplate}
              title="No templates yet"
              requiresAi
              description="Create a template to hold the palettes, fonts, logos and assets your presentations wear. Without one, decks use the plain built-in look."
              actionLabel={agenticAIEnabled ? "Add Template" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/templates/new") : undefined}
            />
          ) : visible.length > 0 ? (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {visible.map((template) => (
                <TemplateCard key={template.id} template={template} userId={user?.id} />
              ))}
            </div>
          ) : (
            <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
              <p className="content-base text-default-secondary py-8 text-center">
                {t_i18n("No templates matching your filters")}
              </p>
            </VisibilityEmptyHint>
          )}
        </>
      )}
    </div>
  );
}
