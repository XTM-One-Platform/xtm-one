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
 * Workspaces - the Tasks `workspaces` tab (`/tasks/workspaces`, #775).
 * A workspace is the boundary the other Operations tabs (work items,
 * shared state, worksets) are scoped by.
 *
 * Scope of this page is deliberately "create and pick one". The full
 * management surface — per-workspace instructions, skills pinning,
 * export/import, a `WorkspacesUsingCard` — is specified separately, and building
 * it here would duplicate that design.
 *
 * Two behaviours are worth knowing before reading the code:
 *
 * * **The default workspace is created by listing.** The backend has no
 *   backfill, so the first render of this page is what materialises it. A
 *   user therefore always sees at least one row and never an empty state on
 *   a healthy platform.
 * * **Exactly one default exists, always.** It is a partial unique index in
 *   Postgres, not a convention — so there is no "two defaults" or "no
 *   default" branch to write. The default cannot be archived or deleted; the
 *   API refuses both, and its actions menu (`WorkspaceActionsMenu`, shared
 *   with the chat sidebar) disables them rather than offering them and then
 *   failing. So is every action the caller's right does not cover
 *   (`can_manage`, and "Make default" on a workspace they do not own).
 */
import { workspacePath } from "@/lib/task-routes";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Boxes, FolderKanban, Hash, LayoutGrid, Plus, Sparkles, User } from "lucide-react";

import {
  AgenticEEBadgeForButton,
  AgenticGateDialog,
  AgenticGateDialogControlled,
} from "@/components/ui/license-gate-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsHoverCard, OpsListSkeleton, OpsMoreToken, OpsRowGrid, OpsToken, OpsUpdated,
  type OpsFact,
} from "@/components/ui/operations-list";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { useAuth } from "@/hooks/useAuth";
import { useAgenticAIEnabled, usePlatformConfig } from "@/hooks/usePlatformConfig";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useViewMode } from "@/hooks/useViewMode";
import { useFormatter } from "@/i18n";
import { canManageEntity, cn, isEntityShared } from "@/lib/utils";
import { WorkspaceActionsMenu } from "@/components/workspaces/WorkspaceActionsMenu";
import { WorkspaceDialog } from "@/components/workspaces/WorkspaceDialog";
import { hostedFleetCount } from "@/lib/workspace-hosted-fleets";
import type { Workspace, WorkspaceContents } from "@/types";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/** The URL prefix of the Workspaces table: its Status filter is `ws_f_status`. */
const URL_KEY = "ws_";

/**
 * "New workspace", the primary action of the tab, drawn in the Operations page header
 * (data-tables.md: the primary action stays in the page header, UX r1 B2 on #11288). It
 * writes `?new=1`, which the tab consumes to open the create dialog, as the hub card does.
 * Gated on the agentic LICENCE, not on AI being enabled, as the backend does
 * (`require_agentic_ai_licensed`): naming a container invokes no model.
 */
export function NewWorkspaceButton() {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const open = () => {
    const next = new URLSearchParams(searchParams);
    next.set("new", "1");
    setSearchParams(next, { replace: true });
  };
  return (
    <AgenticGateDialog feature="Creating workspaces" requiresAi={false}>
      <Button className="gap-1.5" onClick={open} data-testid="workspace-new">
        <Plus className="h-4 w-4" /> {t_i18n("New workspace")}
        <AgenticEEBadgeForButton />
      </Button>
    </AgenticGateDialog>
  );
}

export default function Workspaces() {
  const { t_i18n } = useFormatter();
  const [viewMode, setViewMode] = useViewMode("operations-workspaces");
  // `?new=1` opens the create dialog, so the hub card can deep-link it the
  // way the Variables card does. The param is consumed on arrival, or a
  // reload after closing the dialog would reopen it.
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(
    () => searchParams.get("new") === "1",
  );

  useEffect(() => {
    if (searchParams.get("new") !== "1") return;
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    setSearchParams(next, { replace: true });
    setCreateOpen(true);
  }, [searchParams, setSearchParams]);

  const { user, isAdmin } = useAuth();
  const userId = user?.id;

  /* GET /workspaces is refused without the agentic licence. Left ungated,
     that refusal reached the route error boundary and replaced the whole
     Operations page (title and tabs included) on Community Edition. Gated
     on the licence, the request is not sent there and the tab shows its own
     Enterprise Edition state instead. A failure on a licensed deployment
     still reaches the error boundary: rendering it as an empty list would
     tell the user they have no workspace when the list did not load. */
  const licensed = useAgenticAIEnabled();
  // The licence answer fails closed while the config loads: wait for it, or a
  // licensed deployment shows the Enterprise Edition card for a frame.
  const configLoaded = usePlatformConfig() !== null;
  const [gateOpen, setGateOpen] = useState(false);
  // One control for archived workspaces (UX r1 B1 on #11288): the Status filter. Choosing
  // "Archived" there loads them; with no Status filter the list holds the live ones.
  const showArchived = searchParams.getAll(`${URL_KEY}f_status`).includes("Archived");
  const { data, isLoading } = useWorkspaces({
    includeArchived: showArchived,
    withContents: true,
    enabled: licensed,
  });

  // Your default first (pinned by the table), then archived last, then
  // alphabetical: the order a picker wants, and this list doubles as one. A
  // colleague's default shared with you is not yours, so it sorts with the
  // rest. A column sort chosen in the toolbar replaces it.
  const workspaces = useMemo(
    () =>
      [...(data ?? [])].sort((a, b) => {
        if (a.is_archived !== b.is_archived) return a.is_archived ? 1 : -1;
        return a.name.localeCompare(b.name);
      }),
    [data],
  );
  const columns = useMemo(() => workspaceColumns(userId, t_i18n), [userId, t_i18n]);
  const filters = useMemo(() => workspaceFilters(userId, t_i18n), [userId, t_i18n]);

  // Until the licence is read the list is drawn loading on its own table (#11345): most
  // readers hold the licence, so their rows land in place.
  if (configLoaded && !licensed) {
    return (
      <div className="space-y-4">
        <EmptyState
          icon={LayoutGrid}
          title={t_i18n("Workspaces are part of Enterprise Edition")}
          description={t_i18n(
            "A workspace scopes what agents share: point two agents at one and they read each other's state.",
          )}
          actionLabel={t_i18n("Enterprise Edition")}
          actionIcon={Sparkles}
          actionVariant="default"
          onAction={() => setGateOpen(true)}
        />
        <AgenticGateDialogControlled
          open={gateOpen}
          onOpenChange={setGateOpen}
          feature="Creating workspaces"
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* One records list on the shared data table (dev-docs/reference/data-tables.md,
          slice 8): search, Owner and Status filters, sort, Columns, total, page size and URL
          state are the table's. The grid view keeps each row on the Operations row contract
          (OpsRowGrid) as its card; the list view draws the columns, chosen per user. */}
      <DataTable<Workspace>
        rows={workspaces}
        columns={columns}
        rowKey={(w) => w.id}
        urlKey={URL_KEY}
        storageKey="operations-workspaces"
        searchFields={(w) => [w.name, w.slug, w.description, w.owner_name]}
        searchPlaceholder={t_i18n("Search workspaces...")}
        filters={filters}
        pinFirst={(w) => w.is_default && w.user_id === userId}
        exportName="workspaces"
        isLoading={isLoading || !configLoaded}
        emptyLabel={t_i18n("No workspaces yet")}
        noResultsLabel={t_i18n("No workspace matches")}
        emptyContent={
          <EmptyState
            icon={LayoutGrid}
            title={t_i18n("No workspaces yet")}
            description={t_i18n(
              "A workspace scopes what agents share: point two agents at one and they read each other's state.",
            )}
            actionLabel={t_i18n("New workspace")}
            onAction={() => setCreateOpen(true)}
          />
        }
        rowHref={(w) => workspacePath(w.id)}
        rowClassName={(w) => (w.is_archived ? "opacity-60" : undefined)}
        rowActions={(w) => <WorkspaceActionsMenu workspace={w} className="size-7" />}
        rowActionsWidth={64}
        view={{ value: viewMode, onChange: setViewMode }}
        // One scrollbar in grid view too: the rows scroll inside, the pager stays in the list (UX r1 on #785, B1).
        fitGrid
        renderGrid={
          viewMode === "grid"
            ? (rows) => (
                <div className="space-y-2" data-testid="workspace-list">
                  {rows.map((w) => (
                    <WorkspaceRow key={w.id} workspace={w} userId={userId} isAdmin={isAdmin} />
                  ))}
                </div>
              )
            : undefined
        }
        renderGridSkeleton={(pageSize) => (
          <OpsListSkeleton label={t_i18n("Loading...")} pageSize={pageSize} actionsClassName={ACTIONS_COLUMN} actions={1} lead={1} />
        )}
      />

      {createOpen && (
        <WorkspaceDialog
          workspace={null}
          open
          onClose={() => setCreateOpen(false)}
        />
      )}
    </div>
  );
}

/** The actions column of Workspaces: the one actions menu, one width on every row. */
const ACTIONS_COLUMN = "w-16";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * The columns of the Workspaces list: the cells of the list view (Columns chooses them
 * per user), the toolbar sort of the grid view and the CSV export.
 */
function workspaceColumns(userId: string | undefined, t_i18n: Translate): DataTableColumn<Workspace>[] {
  return [
    { key: "name", header: "Name", cell: (w) => w.name, sortValue: (w) => w.name.toLowerCase(), csv: (w) => w.name },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      cell: (w) => workspaceOwner(w, userId, t_i18n) ?? "",
      sortValue: (w) => (workspaceOwner(w, userId, t_i18n) ?? "").toLowerCase(),
      csv: (w) => w.owner_name ?? "",
    },
    { key: "slug", header: "Slug", hideable: true, cell: (w) => w.slug, sortValue: (w) => w.slug, csv: (w) => w.slug },
    {
      key: "status",
      header: "Status",
      hideable: true,
      cell: (w) => workspaceStatus(w, userId).map((s) => t_i18n(s)).join(", "),
      csv: (w) => workspaceStatus(w, userId).join(", "),
    },
    {
      key: "updated",
      header: "Updated",
      hideable: true,
      align: "right",
      cell: (w) => <OpsUpdated at={w.updated_at} />,
      sortValue: (w) => w.updated_at,
      csv: (w) => w.updated_at,
    },
  ];
}

/** The status values of a workspace (English, the filter's values): your default, archived, else active. */
function workspaceStatus(w: Workspace, userId: string | undefined): string[] {
  const status = [w.is_default && w.user_id === userId ? "Default" : null, w.is_archived ? "Archived" : "Active"];
  return status.filter((s): s is string => !!s);
}

/** The filter pills of the Workspaces list: Owner (you first) and Status. */
function workspaceFilters(userId: string | undefined, t_i18n: Translate): DataTableFilter<Workspace>[] {
  return [
    { key: "owner", label: "Owner", values: (w) => [workspaceOwner(w, userId, t_i18n)] },
    {
      key: "status",
      label: "Status",
      values: (w) => workspaceStatus(w, userId),
      options: ["Default", "Active", "Archived"].map((value) => ({ value, label: t_i18n(value) })),
    },
  ];
}

/**
 * What a workspace currently holds, as the parts of one line, or null.
 *
 * The question somebody opens a management view with is "what is in here",
 * and it is also the question that decides whether deleting one is safe: the
 * work-item foreign key is RESTRICT, so a single item, open or closed,
 * refuses the delete, and a row that looked empty would make that refusal
 * baffling.
 *
 * Null when the counts are ABSENT, which is not the same as every count being
 * zero: absent means nobody counted, and inventing "empty" from it would be
 * an overstatement. An empty workspace says so in words ("Nothing filed here
 * yet.") instead of showing six zeroes, unless it hosts a fleet: that is its
 * coordination, never "nothing filed". Live work is given apart (`open`): the
 * TOTAL is what blocks a delete, "3 of them still open" is what tells
 * somebody to look before archiving the workspace out from under it.
 */
export function workspaceContentsLine(
  contents: WorkspaceContents | null | undefined,
  hostsFleet: boolean,
  t_i18n: Translate,
): { text: string; open: string | null } | null {
  if (!contents) return null;
  const parts: string[] = [];
  const add = (n: number, one: string, many: string) => {
    if (n > 0) parts.push(n === 1 ? t_i18n(one, { n }) : t_i18n(many, { n }));
  };
  add(contents.conversations, "{n} conversation", "{n} conversations");
  add(contents.work_items, "{n} work item", "{n} work items");
  add(contents.shared_states, "{n} shared state", "{n} shared states");
  add(contents.worksets, "{n} workset", "{n} worksets");
  add(contents.knowledge_bases, "{n} knowledge base", "{n} knowledge bases");
  if (!parts.length) return hostsFleet ? null : { text: t_i18n("Nothing filed here yet."), open: null };
  return {
    text: parts.join(" · "),
    open: contents.open_work_items > 0 ? t_i18n("({n} still open)", { n: contents.open_work_items }) : null,
  };
}

/**
 * Every fact of a workspace, in the contract's order: who owns it, the fleet
 * it hosts, the state facts (default, archived, access), what it is for and
 * what it holds, the slug, then the timestamps. Pure: the hover card and the
 * tests read it.
 */
export function workspaceFacts(
  w: Workspace,
  viewer: { id?: string; isAdmin: boolean },
  t_i18n: Translate,
  formatTime: (iso: string) => string,
): OpsFact[] {
  const facts: OpsFact[] = [];
  const owner = workspaceOwner(w, viewer.id, t_i18n);
  if (owner) facts.push({ label: t_i18n("Owner"), value: owner });
  const fleets = w.hosted_fleets ?? [];
  const total = hostedFleetCount(w);
  if (total > 0) {
    const names = fleets.map((f) => (f.is_archived ? `${f.name} (${t_i18n("archived")})` : f.name));
    const unseen = total - fleets.length;
    if (unseen === 1) names.push(t_i18n("and 1 fleet you cannot see"));
    else if (unseen > 1) names.push(t_i18n("and {n} fleets you cannot see", { n: unseen }));
    facts.push({ label: total === 1 ? t_i18n("Fleet") : t_i18n("Fleets"), value: names.join(", ") });
  }
  const state = [
    w.is_default && w.user_id === viewer.id ? t_i18n("Default") : null,
    w.is_archived ? t_i18n("Archived") : null,
  ].filter((s): s is string => !!s);
  if (state.length) facts.push({ label: t_i18n("Status"), value: state.join(", ") });
  const access = workspaceAccess(w, viewer, t_i18n);
  if (access) facts.push({ label: t_i18n("Access"), value: access });
  if (w.description) facts.push({ label: t_i18n("Description"), value: w.description });
  const contents = workspaceContentsLine(w.contents, total > 0, t_i18n);
  if (contents) {
    facts.push({
      label: t_i18n("Contents (what it holds)"),
      value: contents.open ? `${contents.text} ${contents.open}` : contents.text,
      tone: contents.open ? "warning" : undefined,
    });
  }
  facts.push({ label: t_i18n("Slug"), value: <span className="font-mono">{w.slug}</span> });
  facts.push({ label: t_i18n("Created"), value: formatTime(w.created_at) });
  facts.push({ label: t_i18n("Updated"), value: formatTime(w.updated_at) });
  return facts;
}

/** Who owns a workspace, as the row names it: you, the owner, or nobody named. */
function workspaceOwner(w: Workspace, viewerId: string | undefined, t_i18n: Translate): string | null {
  if (w.user_id && w.user_id === viewerId) return t_i18n("You");
  return w.owner_name || null;
}

/** The access a viewer has, in one phrase, or null for a private workspace of their own. */
function workspaceAccess(w: Workspace, viewer: { id?: string; isAdmin: boolean }, t_i18n: Translate): string | null {
  if (w.is_company_managed) return t_i18n("Only an administrator can manage a company-managed entry.");
  const parts: string[] = [];
  if (isEntityShared(w, viewer.id)) parts.push(t_i18n("Shared with you"));
  else if (w.user_id === viewer.id && (!!w.shared_group_ids?.length || !!w.shared_user_ids?.length)) parts.push(t_i18n("Shared"));
  if (!canManageEntity(w, viewer.id, viewer.isAdmin)) parts.push(t_i18n("Read only"));
  return parts.length ? parts.join(", ") : null;
}

/**
 * One workspace on the Operations row contract: the name and its chips on
 * line 1, what it holds on line 2, the owner and the slug in the meta column,
 * the updated cell, the actions menu. A click opens the workspace page.
 */
function WorkspaceRow({ workspace: w, userId, isAdmin }: { workspace: Workspace; userId?: string; isAdmin: boolean }) {
  const { t_i18n, locale } = useFormatter();
  const { user } = useAuth();
  const navigate = useNavigate();
  const zone = viewerTimeZone(user?.timezone);
  const formatTime = (iso: string) => formatInZone(iso, zone, locale);
  const facts = workspaceFacts(w, { id: userId, isAdmin }, t_i18n, formatTime);
  const owner = workspaceOwner(w, userId, t_i18n);
  const total = hostedFleetCount(w);
  const fleets = w.hosted_fleets ?? [];
  const contents = workspaceContentsLine(w.contents, total > 0, t_i18n);
  const isMyDefault = w.is_default && w.user_id === userId;
  const line2 = contents?.text ?? w.description;
  // The row shows the owner, the slug, line 2 (contents, else the
  // description), the state chips, the access chips and the fleet chip;
  // "+N" counts what only the hover card holds.
  const shown =
    (owner ? 1 : 0) +
    1 +
    (line2 ? 1 : 0) +
    (isMyDefault || w.is_archived ? 1 : 0) +
    (workspaceAccess(w, { id: userId, isAdmin }, t_i18n) ? 1 : 0) +
    (total > 0 ? 1 : 0);
  const hiddenFacts = Math.max(0, facts.length - shown);
  const open = () => navigate(workspacePath(w.id));

  return (
    <Card
      data-testid="workspace-row"
      onClick={open}
      className={cn(OPS_ROW_CARD, "group cursor-pointer overflow-hidden transition-all hover:border-elevation-default", w.is_archived && "opacity-60")}
    >
      <CardContent className="p-0">
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="workspace-line">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="workspace-actions"
            lead={<FolderKanban className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />}
            main={
              <>
                <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap" data-testid="workspace-line1">
                  <OpsHoverCard title={w.name} facts={facts}>
                    <Link
                      to={workspacePath(w.id)}
                      className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
                      data-testid="workspace-title"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {w.name}
                    </Link>
                  </OpsHoverCard>
                  {/* The state chips a row never loses: the default is its OWNER's
                      (a colleague's default shared with you is not yours). */}
                  {(isMyDefault || w.is_archived) && (
                    <span className="flex shrink-0 items-center gap-1.5" data-testid="workspace-state">
                      {isMyDefault && (
                        <Badge variant="cyan" className="content-compact">
                          {t_i18n("Default")}
                        </Badge>
                      )}
                      {w.is_archived && (
                        <Badge variant="outline" className="content-compact">
                          {t_i18n("Archived")}
                        </Badge>
                      )}
                    </span>
                  )}
                  <OpsChipStrip testId="workspace-chips">
                    {total > 0 && (
                      <Badge variant="outline" className="content-compact gap-1 text-accent" data-testid="workspace-fleet">
                        <Boxes className="h-3 w-3" aria-hidden="true" />
                        {total === 1 && fleets.length === 1 ? fleets[0].name : t_i18n("Hosts {n} fleets", { n: total })}
                      </Badge>
                    )}
                    {w.is_company_managed && <CompanyManagedBadge size="sm" />}
                    {isEntityShared(w, userId) ? (
                      <SharedBadge ownerName={w.owner_name} size="default" />
                    ) : (
                      !w.is_company_managed &&
                      w.user_id === userId &&
                      (!!w.shared_group_ids?.length || !!w.shared_user_ids?.length) && <SharedBadge size="default" />
                    )}
                    {/* Company-managed already says it: only an admin changes it. */}
                    {!w.is_company_managed && !canManageEntity(w, userId, isAdmin) && <ReadOnlyBadge size="default" />}
                  </OpsChipStrip>
                </div>
                {/* Line 2, one line: what it holds (the delete question), else what it is for. */}
                <div className="flex min-w-0 items-center gap-1 overflow-hidden whitespace-nowrap content-compact text-default-secondary" data-testid="workspace-summary">
                  <span className="min-w-0 truncate">{line2}</span>
                  {contents?.open && <span className="shrink-0 text-feedback-warning-primary">{contents.open}</span>}
                </div>
              </>
            }
            meta={
              <>
                {owner ? (
                  <OpsToken icon={User} full={owner} testId="workspace-owner">{owner}</OpsToken>
                ) : (
                  <span aria-hidden="true" className="h-4" />
                )}
                <span className="flex min-w-0 items-center gap-2">
                  <OpsToken icon={Hash} tone="muted" full={w.slug} copy={w.slug} copyLabel={t_i18n("Copy")} testId="workspace-slug" className="font-mono">
                    {w.slug}
                  </OpsToken>
                  <OpsMoreToken count={hiddenFacts} title={w.name} facts={facts} onOpen={open} testId="workspace-more" />
                </span>
              </>
            }
            updated={<OpsUpdated at={w.updated_at} testId="workspace-updated" />}
            actions={<WorkspaceActionsMenu workspace={w} className="size-7" />}
          />
        </div>
      </CardContent>
    </Card>
  );
}
