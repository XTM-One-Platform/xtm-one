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

/**
 * Custom dashboards - `/dashboards`: the pages of widgets the person's agents
 * built for them, most recently refreshed first.
 *
 * A dashboard is asked of the assistant (the button puts a starting request in
 * its composer, for the person to adapt and send), and an assignment can keep it
 * up to date; with the Dashboard Studio, one is also created empty or imported
 * from a file (an XTM One pack, an OpenCTI or OpenAEV export), then opened in
 * the studio.
 *
 * Cards or a table (sorted by any column), searched, filtered by whose and by
 * tag; each dashboard duplicated into a private copy, tagged by those who may
 * edit it, and those its owner selected deleted together.
 */
import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Boxes, Building2, Copy, Globe, House, LayoutGrid, Sparkles, Tags, Trash2, Upload, UsersRound } from "lucide-react";

import { useAskAssistant } from "@/lib/ask-assistant";
import { ImportDashboardDialog } from "@/components/custom-dashboard/ImportDashboardDialog";
import { NewDashboardDialog } from "@/components/custom-dashboard/NewDashboardDialog";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { PublicLinksDialog } from "@/components/custom-dashboard/PublicLinksDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EntityNameCell, EntityTable, type EntityTableColumn } from "@/components/ui/entity-table";
import { ListControlBar, SelectionBox } from "@/components/ui/list-control-bar";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { TagInput } from "@/components/ui/tag-input";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { useMe } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";
import {
  useCustomDashboards,
  useDeleteCustomDashboard,
  useDuplicateCustomDashboard,
  useSetDashboardTags,
} from "@/hooks/api/useCustomDashboardApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { useToast } from "@/components/ui/toast";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import type { CustomDashboardSummary } from "@/types";

/** A dashboard as the list filters it (`useEntityFilter`): its title is its name, "shared" is a grant on another's. */
type Row = CustomDashboardSummary & { name: string; shared_user_ids: string[] };

const asRow = (dashboard: CustomDashboardSummary): Row => ({
  ...dashboard,
  name: dashboard.title,
  shared_user_ids: dashboard.is_shared ? [dashboard.user_id ?? "shared"] : [],
});

/** Who a dashboard is to the person, in words. @i18n-keys */
const ACCESS_LABELS = {
  company: "Company",
  manage: "Shared with you to manage",
  edit: "Shared with you to edit",
  view: "Shared with you to view",
  shared: "Shared",
  yours: "Yours",
} as const;
type Access = keyof typeof ACCESS_LABELS;

/** Its owner deletes it, and those it is shared with to manage. */
const canDelete = (dashboard: CustomDashboardSummary) => dashboard.permission === "owner" || dashboard.permission === "manage";

function accessOf(dashboard: CustomDashboardSummary): Access {
  if (dashboard.is_company_managed) return "company";
  if (dashboard.permission === "manage") return "manage";
  if (dashboard.permission === "config") return "edit";
  if (dashboard.permission === "read_only") return "view";
  return dashboard.is_shared ? "shared" : "yours";
}

function AccessBadge({ dashboard }: { dashboard: CustomDashboardSummary }) {
  const { t_i18n } = useFormatter();
  const access = accessOf(dashboard);
  if (access === "yours") return null;
  const Icon = access === "company" ? Building2 : UsersRound;
  return (
    <Badge variant="outline" className="gap-1">
      <Icon className="h-3 w-3" aria-hidden="true" /> {t_i18n(ACCESS_LABELS[access])}
    </Badge>
  );
}

function DashboardTile({ dashboard, isHome, actions }: { dashboard: CustomDashboardSummary; isHome: boolean; actions: OverflowMenuItem[] }) {
  const { t_i18n, intl } = useFormatter();
  return (
    <div className="relative">
      <Link
        to={`/dashboards/${dashboard.id}`}
        className="group block h-full rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <Card className="flex h-full flex-col gap-3 p-5 transition-colors group-hover:bg-elevation-hover">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <LayoutGrid className="h-4 w-4 text-accent" aria-hidden="true" />
            </span>
            <span className="min-w-0 truncate content-base font-semibold text-default-primary">{dashboard.title}</span>
            {isHome && (
              <Badge variant="cyan" className="ml-auto shrink-0 gap-1">
                <House className="h-3 w-3" aria-hidden="true" /> {t_i18n("Your dashboard")}
              </Badge>
            )}
          </div>
          {(accessOf(dashboard) !== "yours" || dashboard.tags.length > 0) && (
            <div className="flex flex-wrap items-center gap-1.5">
              <AccessBadge dashboard={dashboard} />
              <TagList tags={dashboard.tags} />
            </div>
          )}
          <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pr-8 content-caption text-default-secondary">
            <span>{t_i18n("{count} widgets", { count: dashboard.widget_count })}</span>
            {dashboard.updated_at && <span>{t_i18n("Updated {when}", { when: formatTimeAgo(intl, dashboard.updated_at) })}</span>}
            {dashboard.assignment_run_id && <span className="text-accent">{t_i18n("Kept up to date by a run")}</span>}
          </div>
        </Card>
      </Link>
      <OverflowMenu items={actions} label={t_i18n("Actions on {name}", { name: dashboard.title })} className="absolute bottom-3 right-3 h-7 w-7" />
    </div>
  );
}

function useDashboardColumns({
  homeId,
  selected,
  toggle,
  actionsOf,
}: {
  homeId: string | null | undefined;
  selected: Set<string>;
  toggle: (id: string) => void;
  actionsOf: (dashboard: Row) => OverflowMenuItem[];
}): EntityTableColumn<Row>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(
    () => [
      {
        key: "select",
        header: "",
        interactive: true,
        className: "w-8",
        cell: (dashboard) =>
          canDelete(dashboard) ? (
            <SelectionBox
              checked={selected.has(dashboard.id)}
              label={t_i18n("Select {name}", { name: dashboard.title })}
              onToggle={() => toggle(dashboard.id)}
            />
          ) : null,
      },
      {
        key: "title",
        header: "Name",
        sortValue: (dashboard) => dashboard.title.toLowerCase(),
        cell: (dashboard) => (
          <EntityNameCell
            icon={<LayoutGrid className="h-4 w-4 text-accent" aria-hidden="true" />}
            name={dashboard.title}
            badges={
              dashboard.id === homeId ? (
                <Badge variant="cyan" className="gap-1">
                  <House className="h-3 w-3" aria-hidden="true" /> {t_i18n("Your dashboard")}
                </Badge>
              ) : undefined
            }
          />
        ),
      },
      { key: "tags", header: "Tags", sortValue: (dashboard) => dashboard.tags[0] ?? "", cell: (dashboard) => <TagList tags={dashboard.tags} /> },
      {
        key: "access",
        header: "Access",
        sortValue: (dashboard) => accessOf(dashboard),
        cell: (dashboard) => <span className="content-compact text-default-secondary">{t_i18n(ACCESS_LABELS[accessOf(dashboard)])}</span>,
      },
      { key: "widgets", header: "Widgets", align: "right", sortValue: (dashboard) => dashboard.widget_count, cell: (dashboard) => dashboard.widget_count },
      {
        key: "updated",
        header: "Updated",
        align: "right",
        className: "whitespace-nowrap",
        sortValue: (dashboard) => (dashboard.updated_at ? Date.parse(dashboard.updated_at) : null),
        cell: (dashboard) => (dashboard.updated_at ? formatTimeAgo(intl, dashboard.updated_at) : ""),
      },
      {
        key: "actions",
        header: "",
        interactive: true,
        className: "w-10",
        cell: (dashboard) => <OverflowMenu items={actionsOf(dashboard)} label={t_i18n("Actions on {name}", { name: dashboard.title })} className="h-7 w-7" />,
      },
    ],
    [homeId, selected, toggle, actionsOf, t_i18n, intl],
  );
}

/** A dashboard's tags, edited as a whole list. */
function TagsDialog({ dashboard, onClose }: { dashboard: CustomDashboardSummary | null; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const save = useSetDashboardTags();
  const [tags, setTags] = useState<string[]>(dashboard?.tags ?? []);
  if (!dashboard) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-w-md flex-col gap-4" data-testid="dashboards.tags.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Tags className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Tags")}
          </DialogTitle>
          <DialogDescription>{t_i18n("What {name} is filed under, to find it in the list.", { name: dashboard.title })}</DialogDescription>
        </DialogHeader>
        <TagInput value={tags} onChange={setTags} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            disabled={save.isPending}
            onClick={() =>
              save.mutate(
                { id: dashboard.id, tags },
                {
                  onSuccess: onClose,
                  onError: (error) => toast({ title: t_i18n("The dashboard could not be changed"), description: error.message, variant: "error" }),
                },
              )
            }
          >
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function CustomDashboards() {
  const { t_i18n } = useFormatter();
  const ask = useAskAssistant();
  const { data, isLoading } = useCustomDashboards();
  const { data: me } = useMe();
  const studio = useFeatureFlag("DASHBOARD_STUDIO");
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const { isAdmin } = useAuth();
  const [importing, setImporting] = useState(false);
  const duplicate = useDuplicateCustomDashboard();
  const remove = useDeleteCustomDashboard();
  const [confirmDialog, confirm] = useConfirm();
  const [tagging, setTagging] = useState<CustomDashboardSummary | null>(null);
  const [viewMode, setViewMode] = useViewMode("dashboards");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const rows = useMemo(() => (data ?? []).map(asRow), [data]);
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allTags, filtered } = useEntityFilter({
    items: rows,
    userId: me?.id,
    searchFields: (dashboard) => dashboard.tags,
  });
  // The list's order: most recently updated first (the table sorts by any column).
  const visible = useMemo(() => [...filtered].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? "")), [filtered]);

  const deleteDashboards = useCallback(
    async (targets: CustomDashboardSummary[]) => {
      const one = targets.length === 1;
      const confirmed = await confirm({
        title: one ? t_i18n("Delete {name}?", { name: targets[0].title }) : t_i18n("Delete {count} dashboards?", { count: targets.length }),
        description: t_i18n("Deleted dashboards cannot be restored."),
        confirmLabel: t_i18n("Delete"),
        variant: "danger",
      });
      if (!confirmed) return;
      const results = await Promise.allSettled(targets.map((dashboard) => remove.mutateAsync(dashboard.id)));
      const failed = results.filter((result) => result.status === "rejected").length;
      setSelected(new Set());
      if (failed) toast({ title: t_i18n("{count, plural, one {# dashboard could not be deleted} other {# dashboards could not be deleted}}", { count: failed }), variant: "error" });
    },
    [confirm, remove, t_i18n, toast],
  );

  const actionsOf = useCallback(
    (dashboard: CustomDashboardSummary): OverflowMenuItem[] => [
      {
        id: "duplicate",
        label: t_i18n("Duplicate"),
        icon: Copy,
        onSelect: () =>
          duplicate.mutate(dashboard.id, {
            onSuccess: (copy) => toast({ title: t_i18n("Duplicated as {name}", { name: copy.title }), variant: "success" }),
            onError: (error) => toast({ title: t_i18n("The dashboard could not be duplicated"), description: error.message, variant: "error" }),
          }),
      },
      ...(dashboard.permission !== "read_only" ? [{ id: "tags", label: t_i18n("Edit tags"), icon: Tags, onSelect: () => setTagging(dashboard) }] : []),
      ...(canDelete(dashboard)
        ? [{ id: "delete", label: t_i18n("Delete"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void deleteDashboards([dashboard]) }]
        : []),
    ],
    [duplicate, deleteDashboards, t_i18n, toast],
  );

  const toggle = useCallback(
    (id: string) =>
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [],
  );
  const columns = useDashboardColumns({ homeId: me?.home_dashboard_id, selected, toggle, actionsOf });
  const selectable = visible.filter(canDelete);
  const chosen = selectable.filter((dashboard) => selected.has(dashboard.id));
  const allChosen = selectable.length > 0 && chosen.length === selectable.length;

  // With the studio, the create dialog: a brief, then the studio builds it in view.
  // Without it, the chat, where the dashboard lands as a card.
  const buildWithAssistant = () =>
    studio ? setCreating(true) : ask(t_i18n("Build me a dashboard of what I should look at first this morning."));

  return (
    <div className="space-y-6">
      {/* One header row, as Videos and Presentations: the page's description is the title's "i",
          and one main button, whose dialog builds with the assistant or starts empty. */}
      <ListPageHeader
        title={t_i18n("Dashboards")}
        infoLabel={t_i18n("About dashboards")}
        info={t_i18n("Pages of widgets your agents build and keep up to date, yours and those shared with you, on the web and on your phone.")}
        actions={
          <>
            {studio && (
              <>
                {isAdmin && (
                  <Button variant="outline" className="gap-1.5" onClick={() => setReviewing(true)}>
                    <Globe className="h-4 w-4" /> {t_i18n("Public links")}
                  </Button>
                )}
                <Button variant="outline" className="gap-1.5" asChild>
                  <Link to="/dashboards/packs">
                    <Boxes className="h-4 w-4" /> {t_i18n("Widget packs")}
                  </Link>
                </Button>
                <Button variant="outline" className="gap-1.5" onClick={() => setImporting(true)}>
                  <Upload className="h-4 w-4" /> {t_i18n("Import")}
                </Button>
              </>
            )}
            <Button className="gap-1.5" onClick={buildWithAssistant}>
              <Sparkles className="h-4 w-4" /> {t_i18n("New dashboard")}
            </Button>
          </>
        }
      />

      {studio && <NewDashboardDialog open={creating} onOpenChange={setCreating} />}
      {studio && <ImportDashboardDialog open={importing} onOpenChange={setImporting} />}
      {studio && isAdmin && <PublicLinksDialog open={reviewing} onOpenChange={setReviewing} />}
      <TagsDialog key={tagging?.id ?? "none"} dashboard={tagging} onClose={() => setTagging(null)} />
      {confirmDialog}

      {!isLoading && rows.length === 0 ? (
        <EmptyState
          icon={LayoutGrid}
          title="No dashboards yet"
          requiresAi
          description="Ask your assistant for a dashboard: a morning brief, an exposure overview, the status of your agents. It lands here, and an assignment can keep it up to date."
          actionLabel="Build with the assistant"
          actionIcon={Sparkles}
          actionVariant="default"
          onAction={buildWithAssistant}
        />
      ) : (
        // Standard v2: the shared toolbar of the records table (search, scope, tags,
        // Clear all | total, Columns, page size, view), in list and grid; the bulk
        // selection bar stays above the list with select-all and bulk delete only:
        // the table's toolbar and footer carry the one count and pager.
        <div className="space-y-3">
          {viewMode === "list" && selectable.length > 0 && (
            <ListControlBar
              paging="footer"
              page={0}
              pageSize={Math.max(1, visible.length)}
              total={visible.length}
              onPageChange={() => undefined}
              selection={{
                hasVisibleRows: true,
                allVisibleSelected: allChosen,
                selectedCount: chosen.length,
                allMatchingSelected: false,
                totalMatching: selectable.length,
                canSelectAllMatching: false,
                onToggleVisible: () => setSelected(allChosen ? new Set() : new Set(selectable.map((dashboard) => dashboard.id))),
                onSelectAllMatching: () => undefined,
                onClear: () => setSelected(new Set()),
                onBulkDelete: () => void deleteDashboards(chosen),
                bulkDeleting: remove.isPending,
              }}
            />
          )}
          <EntityTable
            isLoading={isLoading}
            items={visible}
            columns={columns}
            rowKey={(dashboard) => dashboard.id}
            rowHref={(dashboard) => `/dashboards/${dashboard.id}`}
            defaultSortKey="updated"
            defaultSortDirection="desc"
            storageKey="dashboards"
            search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search dashboards...") }}
            scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
            tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
            view={{ value: viewMode, onChange: setViewMode }}
            gridClassName="grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
            renderGrid={
              viewMode === "grid"
                ? (page) => (
                    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                      {page.map((dashboard) => (
                        <DashboardTile key={dashboard.id} dashboard={dashboard} isHome={dashboard.id === me?.home_dashboard_id} actions={actionsOf(dashboard)} />
                      ))}
                    </div>
                  )
                : undefined
            }
            totalAll={rows.length}
            externalFilterCount={selectedTags.length}
            onClearAll={() => clearAll(undefined, { keepScope: true })}
            noResultsLabel={t_i18n("No dashboards matching your search")}
          />
        </div>
      )}
    </div>
  );
}
