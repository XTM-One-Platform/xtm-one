// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Boxes, Bot, BookOpen, FileText, GraduationCap, Server, Wrench, Building2, User,
  Link2, Presentation, LayoutTemplate, MonitorSmartphone, Workflow, Database, FlaskConical,
  Braces, Radio, Clapperboard, ClipboardList, Play, MessagesSquare, ScrollText, File,
  FolderKanban, Library, ListChecks, Activity, Globe,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAdminObjects } from "@/hooks/api/useMiscApi";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { useFeatureFlag, useFeatureFlagResolved } from "@/hooks/useFeatureFlag";
import { useCustomVariablesEnabled } from "@/hooks/usePlatformConfig";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { objectValueText } from "@/lib/admin-object-labels";
import { readTableState, storedPageSize, writeTableState, type DataTableDefaults, type DataTableSort } from "@/lib/data-table-state";
import { adminObjectsLoading, adminObjectsTypeFilter } from "@/lib/desktop";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { PlatformObjectItem, PlatformObjectType } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

/** The table's URL keys carry no prefix: it is the page's only table (`?q=`, `?f_type=`, `?sort=`). */
const URL_KEY = "";
/** Per-user page size and hidden columns (localStorage). */
const STORAGE_KEY = "admin-objects";
const PAGE_SIZE = 25;
const SORT_KEYS = ["created_at", "updated_at", "name", "object_type", "owner", "status"] as const;
type ObjectSort = (typeof SORT_KEYS)[number];
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };

/** Column widths in px under the fixed layout: the name takes what is left (no sideways scroll at 1080). */
const COLUMN_WIDTHS = {
  /** Fits the longest type label of en/fr/es/ja (ja "ブラウザープロファイル", 179 px with its padding); a longer one truncates with a tooltip. */
  object_type: 184,
  owner: 200,
  status: 208,
  created_at: 144,
  updated_at: 144,
} as const;

// One entry per key of the backend's `ENTITY_MAP` (`api/v1/admin_objects.py`):
// the browser is where an admin finds a colleague's private object (the library
// pages list only own + shared + company-managed, admins included), so
// a type missing here is a type an admin cannot reach. Every `route` (`:id`
// replaced by the row's id) is a page whose loader keeps the admin bypass, so
// the row always opens. A `null` route is a type with no page an admin can
// open another person's object on: its rows are listed for oversight
// and open nothing.
const OBJECT_TYPE_CONFIG: Record<PlatformObjectType, { label: string; icon: React.ElementType; route: string | null }> = {
  agent: { label: "Agent", icon: Bot, route: "/agents/:id" },
  knowledge_base: { label: "Knowledge Base", icon: BookOpen, route: "/knowledge/:id" },
  prompt: { label: "Prompt", icon: FileText, route: "/prompts/:id" },
  skill: { label: "Skill", icon: GraduationCap, route: "/skills/:id" },
  mcp_server: { label: "MCP Server", icon: Server, route: "/mcp-servers/:id" },
  custom_tool: { label: "Tool", icon: Wrench, route: "/tools/:id" },
  integration: { label: "Integration", icon: Link2, route: "/integrations/:id" },
  presentation: { label: "Presentation", icon: Presentation, route: "/presentations/:id" },
  video: { label: "Video", icon: Clapperboard, route: "/videos/:id" },
  template_container: { label: "Template", icon: LayoutTemplate, route: "/templates/:id" },
  // An enrolled machine - an XTM One Desktop install or a headless runner.
  // The Desktop page lists only YOUR fleet; this is where an
  // admin sees every machine, whoever enrolled it.
  runner: { label: "Machine", icon: MonitorSmartphone, route: "/desktop/runners/:id" },
  flow: { label: "Flow", icon: Workflow, route: "/flows/library/:id" },
  dataset: { label: "Dataset", icon: Database, route: "/evaluations/datasets/:id" },
  evaluation: { label: "Evaluation", icon: FlaskConical, route: "/evaluations/:id" },
  variable: { label: "Variable", icon: Braces, route: "/connections/variables/:id" },
  // Channel bots are configured on one Settings tab, which scrolls to this one.
  channel_bot: { label: "Channel Bot", icon: Radio, route: "/settings/integrations/channels?bot=:id" },
  assignment: { label: "Assignment", icon: ClipboardList, route: "/agents/assignments/:id" },
  assignment_run: { label: "Assignment Run", icon: Play, route: "/operations/executions/:id" },
  report: { label: "Report", icon: ScrollText, route: "/reports/:id" },
  conversation: { label: "Conversation", icon: MessagesSquare, route: null },
  file: { label: "File", icon: File, route: null },
  workspace: { label: "Workspace", icon: FolderKanban, route: null },
  workset: { label: "Workset", icon: Library, route: null },
  work_item: { label: "Work Item", icon: ListChecks, route: null },
  flow_run: { label: "Flow Run", icon: Activity, route: null },
  browser_profile: { label: "Browser Profile", icon: Globe, route: null },
  // A team of agents; archived fleets are listed too, their status says so.
  fleet: { label: "Fleet", icon: Boxes, route: "/fleets/:id" },
};

/** The Type pill's options; none picked lists the configuration types (the backend's default union). */
const OBJECT_TYPES: { value: PlatformObjectType; label: string }[] = [
  { value: "agent", label: "Agents" },
  { value: "knowledge_base", label: "Knowledge Bases" },
  { value: "prompt", label: "Prompts" },
  { value: "skill", label: "Skills" },
  { value: "mcp_server", label: "MCP Servers" },
  { value: "custom_tool", label: "Tools" },
  { value: "integration", label: "Integrations" },
  { value: "presentation", label: "Presentations" },
  { value: "video", label: "Videos" },
  { value: "template_container", label: "Templates" },
  { value: "runner", label: "Machines" },
  { value: "flow", label: "Flows" },
  { value: "dataset", label: "Datasets" },
  { value: "evaluation", label: "Evaluations" },
  { value: "variable", label: "Variables" },
  { value: "channel_bot", label: "Channel Bots" },
  { value: "assignment", label: "Assignments" },
  { value: "assignment_run", label: "Assignment Runs" },
  { value: "conversation", label: "Conversations" },
  { value: "report", label: "Reports" },
  { value: "file", label: "Files" },
  { value: "workspace", label: "Workspaces" },
  { value: "workset", label: "Worksets" },
  { value: "work_item", label: "Work Items" },
  { value: "flow_run", label: "Flow Runs" },
  { value: "browser_profile", label: "Browser Profiles" },
  { value: "fleet", label: "Fleets" },
];

/**
 * The Status pill's options: the states the object types carry (a machine's
 * connection, an assignment's switch, a fleet's archive, a run's or a work
 * item's progress, a browser session). The backend matches the raw value.
 */
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "online", label: "Online" },
  { value: "offline", label: "Offline" },
  { value: "revoked", label: "Revoked" },
  { value: "enabled", label: "Enabled" },
  { value: "paused", label: "Paused" },
  { value: "archived", label: "Archived" },
  { value: "pending", label: "Pending" },
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "awaiting_human", label: "Awaiting input" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "skipped", label: "Skipped" },
  { value: "cancelled", label: "Cancelled" },
  { value: "open", label: "Open (status)" },
  { value: "claimed", label: "Claimed" },
  { value: "in_progress", label: "In progress" },
  { value: "blocked", label: "Blocked" },
  { value: "done", label: "Done" },
  { value: "active", label: "Active" },
  { value: "resolved", label: "Resolved" },
  { value: "expired", label: "Expired" },
];

/** Scope pill values: company-managed or personal; both or none is no filter. */
const SCOPE_COMPANY = "company";
const SCOPE_PERSONAL = "personal";

// The live status of a machine, colored like the Desktop page's status dot.
const MACHINE_STATUS_CLASS: Record<string, { text: string; dot: string }> = {
  online: { text: "text-feedback-success-primary", dot: "bg-feedback-success-primary" },
  offline: { text: "text-default-secondary", dot: "bg-[var(--text-default-secondary)]" },
  revoked: { text: "text-feedback-error-primary", dot: "bg-feedback-error-primary" },
};

/**
 * What a machine is at a glance: the OS family it reported, whether it
 * is connected right now - the backend derives it from `last_seen_at` recency,
 * the same reading as the Desktop page, never a cached flag - when it was last
 * seen, and the runner build it runs. "Deployed" means connected and current,
 * not "a row exists". Rendered on runner rows only; the other types carry
 * null in these positions.
 */
function MachineDetails({ obj }: { obj: PlatformObjectItem }) {
  const { t_i18n, intl } = useFormatter();
  const platform = obj.subtype ? RUNNER_PLATFORM_CONFIG[obj.subtype] : undefined;
  const status = obj.status ? MACHINE_STATUS_CLASS[obj.status] : undefined;
  return (
    <span className="flex min-w-0 items-center gap-2 whitespace-nowrap">
      {obj.subtype && (
        <Tip content={platform?.label ?? obj.subtype}>
          <span className="inline-flex shrink-0 items-center">
            <ProviderLogo provider={obj.subtype} logo={platform?.logo} className="h-3 w-3" />
          </span>
        </Tip>
      )}
      {obj.status && (
        <span className={cn("inline-flex min-w-0 items-center gap-1", status?.text ?? "text-default-secondary")}>
          <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", status?.dot ?? "bg-[var(--text-default-secondary)]")} aria-hidden />
          <TruncatedText
            text={
              obj.status === "online"
                ? t_i18n("Online")
                : obj.status === "revoked"
                  ? t_i18n("Revoked")
                  : obj.last_seen_at
                    ? t_i18n("Offline - last seen {time}", { time: formatTimeAgo(intl, obj.last_seen_at) })
                    : t_i18n("Never connected")
            }
          />
        </span>
      )}
      {obj.version && (
        <Tip content={t_i18n("Runner version")}>
          <span className="shrink-0 font-mono-plex text-default-secondary">v{obj.version}</span>
        </Tip>
      )}
    </span>
  );
}

/**
 * The status cell: a machine's connection, a fleet's archive, else the state
 * and its kind in words (`objectValueText`: the catalog label, else a file's
 * format, else the code humanised; never a raw code).
 */
function ObjectStatus({ obj }: { obj: PlatformObjectItem }) {
  const { t_i18n } = useFormatter();
  if (obj.object_type === "runner") return <MachineDetails obj={obj} />;
  if (obj.object_type === "fleet") {
    return obj.status === "archived" ? <Badge variant="outline">{t_i18n("Archived")}</Badge> : <span>-</span>;
  }
  const status = obj.status ? objectValueText(obj.status, t_i18n) : null;
  const subtype = obj.subtype ? objectValueText(obj.subtype, t_i18n) : null;
  const detail = [status, subtype].filter(Boolean).join(" / ");
  if (!detail) return <span className="text-default-secondary">-</span>;
  return <TruncatedText text={detail} className="text-default-secondary" />;
}

/** The owner cell: the person (email in the tooltip), else the company for a company-managed object. */
function ObjectOwner({ obj }: { obj: PlatformObjectItem }) {
  const { t_i18n } = useFormatter();
  if (obj.user_email) {
    return (
      <Tip content={obj.user_email}>
        <span className="flex min-w-0 items-center gap-1 text-default-secondary">
          <User className="h-3 w-3 shrink-0" />
          <span className="truncate">{obj.user_name || obj.user_email}</span>
        </span>
      </Tip>
    );
  }
  if (obj.is_company_managed) {
    return (
      <span className="flex min-w-0 items-center gap-1 text-default-secondary">
        <Building2 className="h-3 w-3 shrink-0" />
        <span className="truncate">{t_i18n("Company-managed")}</span>
      </span>
    );
  }
  return <span className="text-default-secondary">-</span>;
}

/** Where a row opens, with the way back to this page; `null` for a type with no admin page. */
function objectRoute(obj: PlatformObjectItem): string | null {
  const route = OBJECT_TYPE_CONFIG[obj.object_type]?.route;
  return route ? route.replace(":id", encodeURIComponent(obj.id)) : null;
}

/** The detail pages that read it (agents, machines) send the admin back HERE, not to a library page. */
const BACK_TO_OBJECTS = { backTo: "/objects", backLabel: "Back to Objects" };

export default function AdminObjects() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const defaults = useMemo<DataTableDefaults>(
    () => ({ sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) }),
    [],
  );

  // Older links (`?type=runner&user=<id>`) become the table's filter keys once.
  useEffect(() => {
    if (!searchParams.has("type") && !searchParams.has("user")) return;
    setSearchParams(
      (previous) => {
        const out = new URLSearchParams(previous);
        const type = out.get("type");
        const user = out.get("user");
        out.delete("type");
        out.delete("user");
        out.delete("page");
        const current = readTableState(out, URL_KEY, defaults);
        const filters = { ...current.filters };
        if (type && type !== "all") filters.type = [type];
        if (user && user !== "all") filters.owner = [user];
        return writeTableState(out, URL_KEY, { ...current, filters, page: 0 }, defaults);
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams, defaults]);

  // A type whose pages this deployment does not offer (`RUNNERS_ENABLED=false`,
  // the `FLOW_BUILDER` flag off, evaluation off,
  // `DISABLE_CUSTOM_VARIABLES`) is not offered here either; the backend drops
  // the same arms from its union (`active_entity_arms`). While `/auth/config`
  // loads, `Machines` stays offered and a list naming machines waits for the
  // answer (`adminObjectsTypeFilter`).
  const runnersEnabled = useRunnersEnabled();
  const flowBuilderEnabled = useFeatureFlag("FLOW_BUILDER");
  // Offered until the flag is known, so a deep-linked `?f_type=evaluation` survives the load.
  const evaluationFlag = useFeatureFlagResolved("EVALUATION");
  const evaluationsOffered = !evaluationFlag.isResolved || evaluationFlag.enabled;
  const variablesEnabled = useCustomVariablesEnabled();

  const state = readTableState(searchParams, URL_KEY, defaults);
  const typeFilter = adminObjectsTypeFilter(state.filters.type ?? [], runnersEnabled);
  const hiddenTypes = new Set<string>([
    ...(typeFilter.offerRunners ? [] : ["runner"]),
    ...(flowBuilderEnabled ? [] : ["flow", "flow_run"]),
    ...(evaluationsOffered ? [] : ["dataset", "evaluation"]),
    ...(variablesEnabled ? [] : ["variable"]),
  ]);
  // A type this deployment does not offer (a bookmark, a link from another
  // install) is dropped: the pill has no such entry and the backend ignores it.
  const objectTypes = typeFilter.objectTypes.filter((type) => !hiddenTypes.has(type));
  const owners = state.filters.owner ?? [];
  const statuses = state.filters.status ?? [];
  const scopes = state.filters.scope ?? [];
  const companyManaged =
    scopes.includes(SCOPE_COMPANY) === scopes.includes(SCOPE_PERSONAL) ? undefined : scopes.includes(SCOPE_COMPANY);
  const sortKey: ObjectSort = (SORT_KEYS as readonly string[]).includes(state.sort?.key ?? "")
    ? (state.sort?.key as ObjectSort)
    : "created_at";

  const { data: users } = useUsers();

  const { data, isLoading, isError, refetch } = useAdminObjects({
    search: state.search.trim() || undefined,
    object_type: objectTypes.length ? objectTypes.join(",") : undefined,
    user_id: owners.length ? owners.join(",") : undefined,
    status: statuses.length ? statuses.join(",") : undefined,
    is_company_managed: companyManaged,
    sort: sortKey,
    direction: state.sort?.direction ?? "desc",
    limit: state.pageSize,
    offset: state.page * state.pageSize,
  }, { enabled: !typeFilter.waitForRunners });

  const filters = useMemo<DataTableFilter<PlatformObjectItem>[]>(
    () => [
      {
        key: "type",
        label: "Type",
        options: OBJECT_TYPES.filter((type) => !hiddenTypes.has(type.value)).map((type) => ({
          value: type.value,
          label: t_i18n(type.label),
        })),
      },
      {
        key: "owner",
        label: "Owner",
        options: (users ?? []).map((user) => ({ value: user.id, label: user.full_name?.trim() || user.email })),
      },
      { key: "status", label: "Status", options: STATUS_OPTIONS.map((status) => ({ value: status.value, label: t_i18n(status.label) })) },
      {
        key: "scope",
        label: "Scope",
        options: [
          { value: SCOPE_COMPANY, label: t_i18n("Company-managed") },
          { value: SCOPE_PERSONAL, label: t_i18n("Personal") },
        ],
      },
    ],
    // hiddenTypes is rebuilt each render from the flags it reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t_i18n, users, typeFilter.offerRunners, flowBuilderEnabled, evaluationsOffered, variablesEnabled],
  );

  // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
  const sortable = { sortValue: () => null };
  const columns = useMemo<DataTableColumn<PlatformObjectItem>[]>(
    () => [
      {
        key: "name",
        header: "Name",
        className: "max-w-0",
        cell: (obj) => {
          const config = OBJECT_TYPE_CONFIG[obj.object_type];
          const Icon = config?.icon ?? Boxes;
          return (
            <span className="flex min-w-0 items-center gap-2">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/10">
                <Icon className="h-3.5 w-3.5 text-accent" />
              </span>
              {/* The row opens the object (#12070): the name is plain text in every row. */}
              <TruncatedText text={obj.name} className="font-medium" />
              {obj.is_company_managed && <CompanyManagedIcon />}
            </span>
          );
        },
        csv: (obj) => obj.name,
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "object_type",
        header: "Type",
        width: COLUMN_WIDTHS.object_type,
        className: "max-w-0 whitespace-nowrap",
        cell: (obj) => {
          const config = OBJECT_TYPE_CONFIG[obj.object_type];
          return (
            <Badge variant="outline" className="min-w-0 max-w-full">
              <TruncatedText text={config?.label ? t_i18n(config.label) : obj.object_type} />
            </Badge>
          );
        },
        csv: (obj) => obj.object_type,
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "owner",
        header: "Owner",
        width: COLUMN_WIDTHS.owner,
        hideable: true,
        className: "max-w-0",
        cell: (obj) => <ObjectOwner obj={obj} />,
        csv: (obj) => obj.user_email ?? (obj.is_company_managed ? "company-managed" : ""),
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "status",
        header: "Status",
        width: COLUMN_WIDTHS.status,
        hideable: true,
        hideBelow: "4xl",
        className: "max-w-0",
        cell: (obj) => <ObjectStatus obj={obj} />,
        csv: (obj) => obj.status ?? "",
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "created_at",
        header: "Created",
        width: COLUMN_WIDTHS.created_at,
        className: "whitespace-nowrap text-default-secondary",
        cell: (obj) => <DateTimeCell value={obj.created_at} />,
        csv: (obj) => obj.created_at,
        ...sortable,
        firstSort: "desc",
      },
      {
        key: "updated_at",
        header: "Updated",
        width: COLUMN_WIDTHS.updated_at,
        hideable: true,
        defaultHidden: true,
        hideBelow: "6xl",
        className: "whitespace-nowrap text-default-secondary",
        cell: (obj) => <DateTimeCell value={obj.updated_at} />,
        csv: (obj) => obj.updated_at,
        ...sortable,
        firstSort: "desc",
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t_i18n],
  );

  // The header and the toolbar stay mounted while the list loads; only the rows
  // load on their skeleton (#11345). A list naming machines waits for the flag.
  const listLoading = adminObjectsLoading(typeFilter, { isLoading, data });

  return (
    <div className="space-y-6">
      {/* One header row (#10215, criterion 5): the page's help is the title's "i". */}
      <ListPageHeader
        title={t_i18n("Objects")}
        infoLabel={t_i18n("About objects")}
        info={t_i18n("Browse all platform objects across users for troubleshooting and administration.")}
      />

      <DataTable
        rows={listLoading ? [] : (data?.items ?? [])}
        columns={columns}
        rowKey={(obj) => `${obj.object_type}-${obj.id}`}
        server={{ total: data?.total ?? 0 }}
        urlKey={URL_KEY}
        storageKey={STORAGE_KEY}
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        searchPlaceholder={t_i18n("Search by name...")}
        filters={filters}
        exportName="platform-objects"
        layout="fixed"
        isLoading={listLoading}
        isError={isError}
        onRetry={() => void refetch()}
        onRowClick={(obj) => {
          const route = objectRoute(obj);
          if (route) navigate(route, { state: BACK_TO_OBJECTS });
        }}
        rowOpens={(obj) => objectRoute(obj) !== null}
        emptyLabel={t_i18n("There are no objects on the platform yet.")}
        noResultsLabel={t_i18n("No objects match the current filters.")}
        rowTestId="admin-object-row"
      />
    </div>
  );
}
