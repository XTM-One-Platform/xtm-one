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
 * LibraryTab — the flow library.
 *
 * The list side of the visual flow builder: every flow the user can see, with
 * the usual entity-index furniture (search, tag + visibility filters, card ⇄
 * list toggle) and the actions that do not need the canvas — open, duplicate,
 * export, delete — plus creation and JSON import.
 *
 * "New flow" is a POST followed by a navigate rather than a `/flows/new` route:
 * `/flows/:tab?` already owns that path segment, and the builder needs a real
 * flow id to save against anyway.
 */

import { useCallback, useMemo, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Plus,
  Workflow,
  Upload,
  Download,
  Copy,
  Trash2,
  Pencil,
  Boxes,
  CheckCircle2,
  CircleDashed,
  HelpCircle,
  Archive,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import {
  EntityTable,
  EntityNameCell,
  type EntityTableColumn,
} from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { IconMark } from "@/components/ui/icon-mark";
import { CompanyManagedBadge, CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { SharedBadge, SharedIcon } from "@/components/ui/shared-badge";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { OwnerDeactivatedBadge, OwnerDeactivatedIcon } from "@/components/ui/owner-deactivated-notice";
import { useToast } from "@/components/ui/toast";
import {
  AgenticGateDialog,
  AgenticEEBadgeForButton,
} from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { useAuth } from "@/hooks/useAuth";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import {
  useFlows,
  useCreateFlow,
  useDeleteFlow,
  useDuplicateFlow,
  useExportFlow,
  useImportFlow,
} from "@/hooks/api/useFlowApi";
import { cn, isEntityShared, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { FlowStatus, FlowSummary } from "@/types";
import { flowStatusDisplay } from "./flow-status";
import { formatTimeAgo } from "@/lib/relative-time";

const STATUS_STYLES: Record<string, string> = {
  draft: "border-elevation-subtle-soft text-default-secondary",
  active: "border-feedback-success-primary/40 text-feedback-success-primary",
  archived: "border-feedback-alert-primary/40 text-feedback-warning-primary",
};

function StatusBadge({ status }: { status: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge
      variant="outline"
      className={cn("content-caption capitalize", STATUS_STYLES[status])}
    >
      {t_i18n(status)}
    </Badge>
  );
}

const STATUS_ICONS: Record<FlowStatus, LucideIcon> = {
  draft: CircleDashed,
  active: CheckCircle2,
  archived: Archive,
};

/**
 * Icon-only status — the card counterpart to `StatusBadge`, for the tight icon
 * row where a text badge would eat the width the tags need. The tooltip and
 * the aria-label carry the wording. A status outside `FlowStatus` gets a
 * neutral icon and its raw value, never the draft look.
 */
function StatusIcon({ status }: { status: string }) {
  const { t_i18n } = useFormatter();
  const shown = flowStatusDisplay(status);
  const Icon = shown.status ? STATUS_ICONS[shown.status] : HelpCircle;
  const { className } = shown;
  const text = shown.status ? t_i18n(shown.label) : shown.label;
  return (
    <IconMark label={text}>
      <Icon className={cn("h-3.5 w-3.5 shrink-0", className)} aria-hidden="true" />
    </IconMark>
  );
}

export function FlowCard({ flow, userId }: { flow: FlowSummary; userId?: string }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const isShared = isEntityShared(flow, userId);
  const href = `/flows/library/${flow.id}`;

  return (
    <Card
      className="h-full cursor-pointer border-elevation-subtle transition-all hover:bg-elevation-hover"
      onClick={() => navigate(href)}
    >
      <CardContent className="flex h-full flex-col p-4">
        {/* The link wraps the icon tile, name and description only. The icon
            marks below are tab stops of their own (IconMark), and a focusable
            descendant inside an <a> is invalid markup whose names the link
            would absorb, so they sit outside it; the card's onClick keeps the
            whole surface clickable (same split as AgentCard). */}
        <Link
          to={href}
          className="flex min-w-0 items-center gap-3"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <Workflow className="h-5 w-5 text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate content-base font-medium">{flow.name}</p>
            <p className="truncate content-caption text-default-secondary">
              {flow.description || t_i18n("No description")}
            </p>
          </div>
        </Link>

        {/* Tags: fixed-height row for consistent card sizing. Sharing,
            status and company-management ride along on the right as icons
            rather than badges, so the row above keeps its full width for the
            flow name. The list view keeps the text badges: it has dedicated
            columns, so the width pressure is not there. */}
        <div className="mt-3 flex h-[18px] items-center gap-1">
          <TagList tags={flow.tags} className="min-w-0 overflow-hidden" />
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {isShared && <SharedIcon ownerName={flow.owner_name} />}
            {flow.is_company_managed && <CompanyManagedIcon label="Company-managed flow" />}
            {flow.paused_by_owner_deactivation && <OwnerDeactivatedIcon entity="flow" />}
            <StatusIcon status={flow.status} />
          </div>
        </div>

        <div className="mt-auto flex items-center gap-3 border-t border-elevation-subtle-soft pt-3">
          <span className="flex items-center gap-1 content-caption text-default-secondary">
            <Boxes className="h-2.5 w-2.5 shrink-0" />
            {t_i18n("{count} nodes", { count: flow.node_count })}
          </span>
          <span className="content-caption text-default-secondary">
            v{flow.version_number}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

/** List-view columns — name, status, size, tags, last modified, actions. */
function useFlowColumns(
  userId: string | undefined,
  renderActions: (flow: FlowSummary) => React.ReactNode,
): EntityTableColumn<FlowSummary>[] {
  const { intl } = useFormatter();
  return useMemo(
    () => [
      {
        key: "name",
        header: "Name",
        sortValue: (f) => f.name,
        cell: (f) => (
          <EntityNameCell
            icon={<Workflow className="h-4 w-4 text-accent" />}
            name={f.name}
            description={f.description || null}
            badges={
              <>
                {f.is_company_managed && <CompanyManagedBadge />}
                {isEntityShared(f, userId) && (
                  <SharedBadge ownerName={f.owner_name} />
                )}
              </>
            }
          />
        ),
      },
      {
        key: "status",
        header: "Status",
        sortValue: (f) => f.status,
        cell: (f) => (
          <div className="flex items-center gap-1.5">
            <StatusBadge status={f.status} />
            {f.paused_by_owner_deactivation && <OwnerDeactivatedBadge entity="flow" />}
          </div>
        ),
      },
      {
        key: "nodes",
        header: "Nodes",
        align: "right",
        className: "whitespace-nowrap tabular-nums text-default-secondary",
        sortValue: (f) => f.node_count,
        cell: (f) => f.node_count,
      },
      {
        key: "version",
        header: "Version",
        align: "right",
        className: "whitespace-nowrap tabular-nums text-default-secondary",
        sortValue: (f) => f.version_number,
        cell: (f) => `v${f.version_number}`,
      },
      { key: "tags", header: "Tags", cell: (f) => <TagList tags={f.tags} /> },
      {
        key: "updated",
        header: "Updated",
        align: "right",
        className: "whitespace-nowrap text-default-secondary",
        sortValue: (f) => f.updated_at,
        cell: (f) => formatTimeAgo(intl, f.updated_at),
      },
      {
        key: "actions",
        header: "",
        // The cell owns a menu button, so a click must not also open the row.
        interactive: true,
        cell: renderActions,
      },
    ],
    [userId, renderActions, intl],
  );
}

export default function LibraryTab() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const { data: flows, isLoading } = useFlows();
  // Creating, importing and duplicating a flow all sit behind `require_agentic_ai`
  // server-side. Without the licence the buttons must say so — an enabled button
  // that 403s silently is the worst of both worlds.
  const agenticAIEnabled = useAgenticAIEnabled();
  const [viewMode, setViewMode] = useViewMode("flows");

  const createFlow = useCreateFlow();
  // Flows are not new to the platform — agents already declare handovers, and
  // the Live Map has always drawn them. Reporting "no flows yet" on an install
  // full of live handovers would be a lie about the user's own platform.
  const deleteFlow = useDeleteFlow();
  const duplicateFlow = useDuplicateFlow();
  const exportFlow = useExportFlow();
  const importFlow = useImportFlow();

  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();
  const { mutateAsync: removeFlow } = deleteFlow;

  // One dialog: who loses what the flow lends through it - the
  // integrations, tools and knowledge bases its nodes wire, for everyone it is
  // shared with - or the plain confirmation when nobody else uses any of it.
  const handleDelete = useCallback(
    async (flow: FlowSummary) => {
      const confirmed = await confirmSharing(
        { entity_type: "flow", entity_id: flow.id, change: { delete: true } },
        {
          mode: "delete",
          fallback: {
            title: "Delete this flow?",
            description: `"${flow.name}" and its ${pluralize(flow.version_number, "saved version")} will be permanently deleted.`,
          },
        },
      );
      if (confirmed) await removeFlow(flow.id);
    },
    [confirmSharing, removeFlow],
  );
  const fileInputRef = useRef<HTMLInputElement>(null);

  const {
    search,
    setSearch,
    selectedTags,
    setSelectedTags,
    visibilityFilter,
    setVisibilityFilter,
    visibilityCounts,
    clearAll,
    allItems: allFlows,
    allTags,
    filtered,
  } = useEntityFilter({ items: flows, userId: user?.id });

  const handleCreate = async () => {
    try {
      const flow = await createFlow.mutateAsync({
        name: t_i18n("Untitled flow"),
        description: "",
      });
      navigate(`/flows/library/${flow.id}`);
    } catch (err) {
      toast({
        title: t_i18n("Could not create the flow"),
        description: (err as Error).message,
        variant: "error",
      });
    }
  };


  const handleExport = async (flow: FlowSummary) => {
    let document: Record<string, unknown>;
    try {
      document = await exportFlow.mutateAsync(flow.id);
    } catch (err) {
      toast({
        title: t_i18n("Export failed"),
        description: (err as Error).message,
        variant: "error",
      });
      return;
    }
    const blob = new Blob([JSON.stringify(document, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `${flow.name.replace(/[^\w.-]+/g, "-")}.flow.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const handleImportFile = async (file: File) => {
    let document: Record<string, unknown>;
    try {
      document = JSON.parse(await file.text());
    } catch {
      toast({
        title: t_i18n("Import failed"),
        description: t_i18n("That file is not valid JSON."),
        variant: "error",
      });
      return;
    }
    try {
      const { flow, report } = await importFlow.mutateAsync({ document });
      // An import is never silently partial — always say what did not survive.
      const notes: string[] = [];
      if (report.nodes_unresolved > 0) {
        notes.push(
          t_i18n("{count} nodes had no equivalent and need remapping", {
            count: report.nodes_unresolved,
          }),
        );
      }
      if (report.nodes_to_review > 0) {
        notes.push(
          t_i18n("{count} nodes need a review before they can run", {
            count: report.nodes_to_review,
          }),
        );
      }
      if (report.edges_dropped > 0) {
        notes.push(
          t_i18n("{count} connections could not be reconnected", {
            count: report.edges_dropped,
          }),
        );
      }
      toast({
        title: t_i18n("Flow imported"),
        description:
          notes.join(" · ") ||
          t_i18n("Everything in the file was imported cleanly."),
        variant: notes.length ? "info" : "success",
      });
      navigate(`/flows/library/${flow.id}`);
    } catch (err) {
      toast({
        title: t_i18n("Import failed"),
        description: (err as Error).message,
        variant: "error",
      });
    }
  };

  const renderActions = useMemo(
    () =>
      function FlowActions(flow: FlowSummary) {
        return (
          <OverflowMenu
            label={t_i18n("Flow actions")}
            items={[
              {
                id: "edit",
                label: t_i18n("Open in builder"),
                icon: Pencil,
                onSelect: () => navigate(`/flows/library/${flow.id}`),
              },
              {
                id: "duplicate",
                label: t_i18n("Duplicate"),
                icon: Copy,
                disabled: !agenticAIEnabled,
                onSelect: () => void duplicateFlow.mutateAsync(flow.id),
              },
              {
                id: "export",
                label: t_i18n("Export as JSON"),
                icon: Download,
                onSelect: () => void handleExport(flow),
              },
              {
                id: "delete",
                label: t_i18n("Delete"),
                icon: Trash2,
                destructive: true,
                onSelect: () => void handleDelete(flow),
              },
            ]}
          />
        );
      },
    // `handleExport` closes over stable mutation objects only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t_i18n, navigate, duplicateFlow, agenticAIEnabled, handleDelete],
  );

  const columns = useFlowColumns(user?.id, renderActions);

  return (
    <div className="space-y-6">
      {/* The page's actions; search, scope, tags, total and view are on the table's toolbar. */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="ml-auto flex items-center gap-3">
          <AgenticGateDialog feature={t_i18n("Importing flows")}>
            <Button
              variant="outline"
              className="gap-1.5"
              onClick={() => fileInputRef.current?.click()}
            >
              <Upload className="h-4 w-4" /> {t_i18n("Import")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
          <AgenticGateDialog feature={t_i18n("Creating flows")}>
            <Button
              className="gap-1.5"
              onClick={handleCreate}
              disabled={createFlow.isPending}
            >
              <Plus className="h-4 w-4" /> {t_i18n("New flow")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          // Reset so re-picking the same file fires `change` again.
          e.target.value = "";
          if (file) void handleImportFile(file);
        }}
      />


      {!isLoading && allFlows.length === 0 ? (
        <EmptyState
          icon={Workflow}
          title="No flows yet"
          description="Compose your first automation on the canvas: drag a trigger, wire it to an agent, and save. You can also import a flow exported from XTM One, LangFlow or n8n."
          actionLabel={agenticAIEnabled ? "New flow" : undefined}
          onAction={agenticAIEnabled ? handleCreate : undefined}
        />
      ) : (
        // Standard v2: the shared toolbar of the records table (search, scope, tags,
        // Clear all | total, Columns, page size, view), in list and grid.
        <EntityTable
          isLoading={isLoading}
          items={filtered}
          columns={columns}
          rowKey={(f) => f.id}
          rowHref={(f) => `/flows/library/${f.id}`}
          defaultSortKey="updated"
          storageKey="flows"
          search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search flows...") }}
          scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
          tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
          view={{ value: viewMode, onChange: setViewMode }}
          // The first read draws this page's own card: its footer at its own height (#11345).
          renderGridSkeleton={(pageSize) => <CardGridSkeleton count={cardSkeletonCount(pageSize)} label={t_i18n("Loading")} meta="caption" />}
          renderGrid={
            viewMode === "grid"
              ? (page) => (
                  <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {page.map((flow) => (
                      <FlowCard key={flow.id} flow={flow} userId={user?.id} />
                    ))}
                  </div>
                )
              : undefined
          }
          totalAll={allFlows.length}
          externalFilterCount={selectedTags.length}
          onClearAll={() => clearAll(undefined, { keepScope: true })}
          noResultsLabel={t_i18n("No flows matching your filters")}
          emptyContent={
            <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
              <p className="py-8 text-center content-base text-default-secondary">
                {t_i18n("No flows matching your filters")}
              </p>
            </VisibilityEmptyHint>
          }
        />
      )}

      {ConsentEl}
    </div>
  );
}
