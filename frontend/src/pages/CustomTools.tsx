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

import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMemo, useState } from "react";
import { Plus, Wrench, Code2, Sparkles } from "lucide-react";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { cn, pluralize, isEntityShared, formatCompactNumber } from "@/lib/utils";
import { isCoordinationBuiltinTool } from "@/lib/constants";
import { CompanyManagedBadge, CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { SharedBadge, SharedIcon } from "@/components/ui/shared-badge";
import { useAuth } from "@/hooks/useAuth";
import { useBuiltinTools, type BuiltinToolOption } from "@/hooks/useBuiltinTools";
import { useAgenticAIEnabled, useCustomToolsEnabled } from "@/hooks/usePlatformConfig";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useFormatter } from "@/i18n";
import { ToolGateBadge } from "@/pages/tools/ToolGateBadge";
import { useToolGateSummary } from "@/pages/tools/useToolGateSummary";
import type { CustomTool } from "@/types";
import { ResourceSharingWaysBadge } from "@/components/ui/sharing-ways-badge";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import { formatTimeAgo } from "@/lib/relative-time";
import { ToolSourcesTab } from "@/pages/tools/ToolSourcesTab";
import { Icon as DsIcon } from "@filigran/design-system";

/** The Repositories tab's icon: the library glyph, as a `DetailTabs` icon component. */
function RepositoriesIcon({ className }: { className?: string }) {
  return <DsIcon name="folder-open" size={16} className={className} />;
}

/** Which list the page is showing. `builtin` is the default and has no path
 *  segment, so `/tools` stays the plain URL; `custom` is `/tools/custom`,
 *  `sources` (the repositories custom tools are synced from) `/tools/sources`. */
type ToolsTab = "builtin" | "custom" | "sources";

/** The address of a Tools tab (#10203: the path says where you are). */
function toolsTabPath(tab: ToolsTab): string {
  return tab === "builtin" ? "/tools" : `/tools/${tab}`;
}

/** `90` → `1m 30s`, `45` → `45s`. */
function formatTimeout(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const rest = seconds % 60;
  return `${Math.floor(seconds / 60)}m${rest ? ` ${rest}s` : ""}`;
}

function BuiltinToolCard({ tool }: { tool: BuiltinToolOption }) {
  const { t_i18n } = useFormatter();
  const Icon = tool.icon;
  // Coordination tools (work items / shared state) get a subtle violet accent
  // so they read as a distinct cross-run coordination category.
  const isCoord = isCoordinationBuiltinTool(tool.id);
  /* A builtin row covers several tool names, so it answers in aggregate. */
  const { byGroup } = useToolGateSummary();
  const gate = byGroup.get(tool.id.replace(/^builtin-/, ""));

  return (
    <Link to={`/tools/builtin-${tool.id}`} className="block">
      <Card data-testid="tool-card" className="transition-all cursor-pointer hover:bg-elevation-hover border-elevation-subtle">
        <CardContent className="p-4">
          <div className="flex items-center gap-3">
            <div className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-sm",
              isCoord ? "bg-filigran-ia-primary/10 light:bg-filigran-ia-primary/10" : "bg-accent/10",
            )}>
              <Icon className={cn("h-5 w-5", isCoord ? "text-filigran-ia-primary light:text-filigran-ia-primary" : "text-accent")} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium truncate">{tool.label}</p>
              <p className="content-caption text-default-secondary truncate">{tool.description}</p>
            </div>
            {/* Icon-only company-managed mark: the text chip ran past the card's
                right edge at 1024 px beside the gate and Enabled chips. */}
            <div className="flex items-center gap-2 shrink-0">
              {gate && <ToolGateBadge gating={gate.gating} total={gate.total} />}
              <Badge variant="success" className="content-caption">{t_i18n("Enabled")}</Badge>
              <CompanyManagedIcon />
            </div>
          </div>

          {/* Metadata */}
          <div className="flex flex-wrap items-center gap-3 mt-3 pt-3 border-t border-elevation-subtle-soft">
            <Badge variant="cyan" className="content-caption gap-1">
              <Sparkles className="h-2.5 w-2.5" /> {t_i18n("Built-in")}
            </Badge>
            <Badge
              variant="outline"
              className={cn(
                "content-caption px-1.5 py-0",
                isCoord && "border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary light:border-filigran-ia-primary/40 light:bg-filigran-ia-primary/10 light:text-filigran-ia-primary",
              )}
            >
              {tool.tag}
            </Badge>
            {/* How many functions the suite grants on this deployment. */}
            {tool.functions.length > 0 && (
              <span data-testid={`builtin-tool-functions-${tool.id}`} className="content-caption text-default-secondary">
                {t_i18n("{count, plural, one {# function} other {# functions}}", { count: tool.functions.length })}
              </span>
            )}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

function ToolCard({ tool, userId }: { tool: CustomTool; userId?: string }) {
  const { t_i18n } = useFormatter();
  const isShared = isEntityShared(tool, userId);
  const paramCount = tool.input_schema
    ? Object.keys(tool.input_schema.properties ?? {}).length
    : 0;
  const { byName } = useToolGateSummary();
  const gates = byName.get(tool.name);

  return (
    <Link to={`/tools/${tool.id}`} className="block h-full">
      <Card
        data-testid="tool-card"
        className={cn(
          "transition-all cursor-pointer hover:bg-elevation-hover h-full",
          tool.enabled ? "border-elevation-subtle" : "border-elevation-subtle-soft opacity-60",
        )}
      >
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              {tool.icon_url ? (
                <img src={tool.icon_url} alt="" className="h-5 w-5 object-contain" />
              ) : (
                <Wrench className="h-5 w-5 text-accent" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium truncate">{tool.name}</p>
              <p className="content-caption text-default-secondary truncate">{tool.description || t_i18n("No description")}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {gates !== undefined && <ToolGateBadge gating={gates ? 1 : 0} />}
              <Badge variant={tool.enabled ? "success" : "outline"} className="content-caption">
                {tool.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
              </Badge>
              {tool.is_company_managed && <CompanyManagedIcon />}
              {isShared && <SharedIcon ownerName={tool.owner_name} />}
              <ResourceSharingWaysBadge kind="custom_tool" id={tool.id} />
            </div>
          </div>

          {/* Metadata — pushed to bottom */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            <Badge variant="outline" className="content-caption gap-1">
              <Code2 className="h-2.5 w-2.5" /> {tool.language === "python" ? "Python" : "JavaScript"}
            </Badge>
            <span className="content-caption text-default-secondary">
              {t_i18n("{duration} timeout", { duration: formatTimeout(tool.timeout) })}
            </span>
            {paramCount > 0 && (
              <span className="content-caption text-default-secondary">{paramCount} {t_i18n(pluralize(paramCount, "parameter"))}</span>
            )}
            <TagList tags={tool.tags} className="ml-auto" />
            {(!tool.tags || tool.tags.length === 0) && tool.js_function && (
              <span className="content-caption text-default-secondary ml-auto">{t_i18n("{count} lines", { count: tool.js_function.split("\n").length })}</span>
            )}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// List views
// ---------------------------------------------------------------------------

/** List-view columns for the built-in tool catalog — name, category, status. */
function useBuiltinToolColumns(): EntityTableColumn<BuiltinToolOption>[] {
  const { t_i18n } = useFormatter();
  const { byGroup, active } = useToolGateSummary();
  return useMemo(() => withoutApprovalColumn([
    {
      key: "name",
      header: "Name",
      sortValue: (tool) => tool.label,
      cell: (tool) => {
        const Icon = tool.icon;
        const isCoord = isCoordinationBuiltinTool(tool.id);
        return (
          <EntityNameCell
            icon={<Icon className={cn("h-4 w-4", isCoord ? "text-filigran-ia-primary light:text-filigran-ia-primary" : "text-accent")} />}
            name={tool.label}
            description={tool.description}
            badges={<CompanyManagedBadge />}
          />
        );
      },
    },
    {
      key: "status",
      header: "Status",
      cell: () => <Badge variant="success" className="content-caption">{t_i18n("Enabled")}</Badge>,
    },
    {
      key: "kind",
      header: "Kind",
      className: "whitespace-nowrap",
      cell: () => (
        <Badge variant="cyan" className="content-caption gap-1">
          <Sparkles className="h-2.5 w-2.5" /> {t_i18n("Built-in")}
        </Badge>
      ),
    },
    {
      key: "approval",
      header: "Approval",
      className: "whitespace-nowrap",
      cell: (tool) => {
        const gate = byGroup.get(tool.id.replace(/^builtin-/, ""));
        return gate ? <ToolGateBadge gating={gate.gating} total={gate.total} /> : null;
      },
    },
    {
      key: "category",
      header: "Category",
      className: "whitespace-nowrap",
      sortValue: (tool) => tool.tag,
      cell: (tool) => {
        const isCoord = isCoordinationBuiltinTool(tool.id);
        return (
          <Badge
            variant="outline"
            className={cn(
              "content-caption px-1.5 py-0",
              isCoord && "border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary light:border-filigran-ia-primary/40 light:bg-filigran-ia-primary/10 light:text-filigran-ia-primary",
            )}
          >
            {tool.tag}
          </Badge>
        );
      },
    },
  ], active), [t_i18n, byGroup, active]);
}

/** The Approval column only while approvals are in use: with no agent asking
 *  before running tools, it would describe a behaviour nobody gets. */
function withoutApprovalColumn<T>(columns: EntityTableColumn<T>[], active: boolean): EntityTableColumn<T>[] {
  return active ? columns : columns.filter((column) => column.key !== "approval");
}

/** List-view columns for custom tools — name, status, runtime, limits, usage. */
function useCustomToolColumns(userId?: string): EntityTableColumn<CustomTool>[] {
  const { t_i18n, intl } = useFormatter();
  const { byName, active } = useToolGateSummary();
  return useMemo(() => withoutApprovalColumn([
    {
      key: "name",
      header: "Name",
      sortValue: (tool) => tool.name,
      cell: (tool) => (
        <EntityNameCell
          icon={tool.icon_url
            ? <img src={tool.icon_url} alt="" className="h-4 w-4 object-contain" />
            : <Wrench className="h-4 w-4 text-accent" />}
          name={tool.name}
          description={tool.description || t_i18n("No description")}
          muted={!tool.enabled}
          badges={
            <>
              {tool.is_company_managed && <CompanyManagedBadge />}
              {isEntityShared(tool, userId) && <SharedBadge ownerName={tool.owner_name} />}
              <ResourceSharingWaysBadge kind="custom_tool" id={tool.id} />
            </>
          }
        />
      ),
    },
    {
      key: "status",
      header: "Status",
      sortValue: (tool) => tool.enabled,
      cell: (tool) => (
        <Badge variant={tool.enabled ? "success" : "outline"} className="content-caption">
          {tool.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
        </Badge>
      ),
    },
    {
      key: "approval",
      header: "Approval",
      className: "whitespace-nowrap",
      sortValue: (tool) => !!byName.get(tool.name),
      cell: (tool) => {
        const gates = byName.get(tool.name);
        return gates === undefined ? null : <ToolGateBadge gating={gates ? 1 : 0} />;
      },
    },
    {
      key: "language",
      header: "Language",
      className: "whitespace-nowrap",
      sortValue: (tool) => tool.language,
      cell: (tool) => (
        <Badge variant="outline" className="content-caption gap-1">
          <Code2 className="h-2.5 w-2.5" /> {tool.language === "python" ? "Python" : "JavaScript"}
        </Badge>
      ),
    },
    {
      key: "parameters",
      header: "Parameters",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (tool) => Object.keys(tool.input_schema?.properties ?? {}).length,
      cell: (tool) => Object.keys(tool.input_schema?.properties ?? {}).length,
    },
    {
      key: "timeout",
      header: "Timeout",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (tool) => tool.timeout,
      cell: (tool) => formatTimeout(tool.timeout),
    },
    {
      key: "calls",
      header: "Calls",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (tool) => tool.call_count,
      cell: (tool) => formatCompactNumber(tool.call_count),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (tool) => <TagList tags={tool.tags} />,
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (tool) => tool.updated_at,
      cell: (tool) => formatTimeAgo(intl, tool.updated_at),
    },
  ], active), [t_i18n, intl, userId, byName, active]);
}

export default function CustomTools() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { data: tools, isLoading } = useCustomTools();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const customToolsEnabled = useCustomToolsEnabled();
  const {
    offered: offeredBuiltinTools,
    isReady: builtinToolsReady,
    isError: builtinToolsFailed,
  } = useBuiltinTools();
  const builtinTools = useMemo(() => offeredBuiltinTools("agent"), [offeredBuiltinTools]);
  /* A path segment (#10203): `/tools/custom` is a static route declared before
     `/tools/:id/:tab?`, so it is never read as a tool whose id is "custom".
     The former tab and gate query parameters are not read: no redirect (#797). */
  const [params] = useSearchParams();
  const { pathname } = useLocation();
  const trimmedPath = pathname.replace(/\/+$/, "");
  const requestedTab = trimmedPath === "/tools/custom" ? "custom" : trimmedPath === "/tools/sources" ? "sources" : null;
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allTools, allTags, filtered } =
    useEntityFilter({ items: tools, userId: user?.id });
  // The two sections are independent lists with different columns, so each
  // remembers its own grid/list choice.
  const [builtinView, setBuiltinView] = useViewMode("tools-builtin");
  const [customView, setCustomView] = useViewMode("tools-custom");
  const builtinColumns = useBuiltinToolColumns();
  // The built-in list is the registry's, searched by name and description in the page.
  const [builtinSearch, setBuiltinSearch] = useState("");
  const builtinShown = useMemo(() => {
    const needle = builtinSearch.trim().toLowerCase();
    if (!needle) return builtinTools;
    return builtinTools.filter((tool) => `${tool.label} ${tool.id} ${tool.description}`.toLowerCase().includes(needle));
  }, [builtinTools, builtinSearch]);
  const customColumns = useCustomToolColumns(user?.id);

  // A tab is an address: back and forward move between tabs, the list's query is kept.
  const setTab = (next: ToolsTab) => {
    const q = params.toString();
    navigate(`${toolsTabPath(next)}${q ? `?${q}` : ""}`);
  };
  const tabs = useMemo(
    () =>
      [
        { key: "builtin" as const, label: "Built-in Tools", icon: Sparkles },
        ...(customToolsEnabled
          ? [
              { key: "custom" as const, label: "Custom Tools", icon: Code2 },
              { key: "sources" as const, label: "Repositories", icon: RepositoriesIcon },
            ]
          : []),
      ],
    [customToolsEnabled],
  );

  /* Resolved against the tabs that EXIST, not against the names. A
     custom tab address on a deployment with custom tools disabled otherwise left
     `DetailTabs` highlighting nothing above an empty body — reachable from the
     admin-objects link, and briefly on every refresh of that URL, since
     `useCustomToolsEnabled` reads false until the platform config resolves. */
  const tab: ToolsTab = tabs.some((t) => t.key === requestedTab)
    ? (requestedTab as ToolsTab)
    : "builtin";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Tools")}</h1>
          <p className="mt-2 text-default-secondary">
            {customToolsEnabled
              ? t_i18n("Built-in platform tools and custom JavaScript tools available to your agents.")
              : t_i18n("Built-in platform tools available to your agents.")}
          </p>
        </div>
        {customToolsEnabled && (
          <div className="flex items-center gap-3">
            <AgenticGateDialog feature="Creating custom tools" requiresAi={false}>  {/* a Custom tool is a sandboxed script (T077) */}
              <Button className="gap-1.5" onClick={() => navigate("/tools/new")}>
                <Plus className="h-4 w-4" /> {t_i18n("Add Tool")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
          </div>
        )}
      </div>

      <DetailTabs tabs={tabs} active={tab} onChange={setTab} />

      {/* Built-in tools */}
      {tab === "builtin" && (
      <div className="space-y-3">
        {/* No heading: the tab above says which list this is. Before the registry
            answers the list is unknown: a skeleton while it loads, an inline error
            line if it failed, never a blank area. Standard v2 once loaded: the
            shared toolbar (search, Clear all | total, Columns, page size, view). */}
        {!builtinToolsReady ? (
          <BuiltinToolsPending
            isError={builtinToolsFailed}
            gridClassName="gap-4 md:grid-cols-2 lg:grid-cols-3"
            testId="tools-builtin"
          />
        ) : (
          <EntityTable
            items={builtinShown}
            columns={builtinColumns}
            rowKey={(tool) => tool.id}
            rowHref={(tool) => `/tools/builtin-${tool.id}`}
            defaultSortKey="name"
            storageKey="tools-builtin"
            search={{ value: builtinSearch, onChange: setBuiltinSearch, placeholder: t_i18n("Search tools...") }}
            view={{ value: builtinView, onChange: setBuiltinView }}
            renderGrid={
              builtinView === "grid"
                ? (page) => (
                    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                      {page.map((tool) => (
                        <BuiltinToolCard key={tool.id} tool={tool} />
                      ))}
                    </div>
                  )
                : undefined
            }
            totalAll={builtinTools.length}
            onClearAll={() => setBuiltinSearch("")}
            noResultsLabel={t_i18n("No tools matching your filters")}
          />
        )}
      </div>
      )}

      {/* Repositories custom tools are synced from — absent when custom tools are disabled */}
      {tab === "sources" && customToolsEnabled && <ToolSourcesTab />}

      {/* Custom tools — the tab is absent when disabled, so this cannot show */}
      {tab === "custom" && customToolsEnabled && (
      <div className="space-y-3">
        {!isLoading && allTools.length === 0 ? (
            /* Deliberately no `requiresAi`: a Custom tool is a sandboxed
               script and creating one works with AI off (T077), so saying the
               area is empty *because* AI is disabled would be false - and it
               hides the create affordance on the only screen this renders on. */
            <EmptyState
              icon={Wrench}
              title="No custom tools yet"
              description="Create your first custom tool to give your agents custom capabilities with JavaScript."
              actionLabel={agenticAIEnabled ? "Add Tool" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/tools/new") : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope,
            // tags, Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={customColumns}
              rowKey={(tool) => tool.id}
              rowHref={(tool) => `/tools/${tool.id}`}
              rowClassName={(tool) => tool.enabled ? undefined : "opacity-60"}
              defaultSortKey="name"
              storageKey="tools-custom"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search tools...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              view={{ value: customView, onChange: setCustomView }}
              renderGrid={
                customView === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((tool) => (
                          <ToolCard key={tool.id} tool={tool} userId={user?.id} />
                        ))}
                      </div>
                    )
                  : undefined
              }
              totalAll={allTools.length}
              externalFilterCount={selectedTags.length}
              onClearAll={() => clearAll(undefined, { keepScope: true })}
              noResultsLabel={t_i18n("No tools matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No tools matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )
        }
      </div>
      )}
    </div>
  );
}
