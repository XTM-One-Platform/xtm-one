// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Shield, User as UserIcon, Plus, Mail } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InviteUserDialog } from "@/components/InviteUserDialog";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { ssoProviderLabel } from "@/lib/constants";
import { roleLabelKey } from "@/lib/role-labels";
import { useUsersCount, useUsersPage, type UserTableQuery } from "@/hooks/api/useAuthApi";
import { useGroups } from "@/hooks/api/useGroupApi";
import { readTableState, storedPageSize, type DataTableDefaults } from "@/lib/data-table-state";
import { cn } from "@/lib/utils";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { useFormatter } from "@/i18n";
import type { UserListItem } from "@/types";

/** The "Created" filter: the API keeps the users created inside the widest window picked. */
const CREATED_WINDOWS = [
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
] as const;

/** The table's keys in the URL (`u_q`, `u_sort`, `u_page`, `u_f_role`...), unchanged since #10581. */
const URL_KEY = "u_";
const STORAGE_KEY = "users";
const PAGE_SIZE = 25;
const DEFAULT_SORT = { key: "name", direction: "asc" } as const;

/** A filter's picked values as the API takes them: joined by commas, or nothing. */
function joined(values: readonly string[] | undefined): string | undefined {
  return values && values.length > 0 ? values.join(",") : undefined;
}

/**
 * The platform users on the shared DataTable in server mode (#9378 slice 6z, #11940):
 * GET /users searches, filters, sorts and pages the directory and GET /users/count gives
 * the total under the same filters, so the page never loads every user to show 25. The
 * state stays in the URL under the `u_` keys it has had since #10581.
 */
export default function Users() {
  const { t_i18n, intl } = useFormatter();
  const [searchParams] = useSearchParams();
  // The size the user kept (DataTable storageKey) is the size the API is asked with.
  const defaults: DataTableDefaults = { sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) };
  const table = readTableState(searchParams, URL_KEY, defaults);
  const filtersQuery: UserTableQuery = {
    search: table.search.trim() || undefined,
    role: joined(table.filters.role),
    status: joined(table.filters.status),
    group: joined(table.filters.group),
    created: joined(table.filters.created),
  };
  const sort = table.sort ?? DEFAULT_SORT;
  const { data: users, isLoading, isError, refetch } = useUsersPage({
    ...filtersQuery,
    sort: sort.key,
    direction: sort.direction,
    limit: table.pageSize,
    offset: table.page * table.pageSize,
  });
  const { data: counted } = useUsersCount(filtersQuery);
  // The directory's size under no filter: a filtered total reads "n of N" (UX r1 B1 on #11941).
  const { data: countedAll } = useUsersCount({});
  const { data: groups } = useGroups();
  const navigate = useNavigate();
  const [inviteOpen, setInviteOpen] = useState(false);

  // Fixed layout (UX r3 to r5 on #10581): every secondary column has the width of its
  // longest label (fr included), sort glyph and padding, and Role fits both badges
  // ("Administrateur" + "Platform Admin"); Name and Email carry no width and share what
  // is left, so the table fits its card at every screen (834 px at 1080, where only Name,
  // Email, Role, Status and Created are drawn) instead of overflowing it. The four
  // resource counts start hidden (one click away in the Columns pill), so Name and Email
  // keep their room at 1728; a value still cut is named by the DS tooltip.
  const columns: DataTableColumn<UserListItem>[] = [
    {
      key: "name",
      header: "Name",
      cell: (u) => (
        <span className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              "flex h-6 w-6 shrink-0 items-center justify-center rounded-full",
              u.role === "admin" ? "bg-accent/10" : "bg-elevation-hover",
            )}
          >
            {u.role === "admin" ? <Shield className="h-3.5 w-3.5 text-accent" aria-hidden /> : <UserIcon className="h-3.5 w-3.5 text-default-secondary" aria-hidden />}
          </span>
          <TruncatedText text={u.full_name} className="font-medium" />
        </span>
      ),
      sortValue: (u) => u.full_name.toLowerCase(),
      csv: (u) => u.full_name,
    },
    {
      key: "email",
      header: "Email",
      cell: (u) => <TruncatedText text={u.email} className="text-default-secondary" />,
      sortValue: (u) => u.email.toLowerCase(),
      csv: (u) => u.email,
    },
    {
      key: "role",
      header: "Role",
      width: 224,
      cell: (u) => (
        <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
          <Badge variant={u.role === "admin" ? "cyan" : "outline"} className="whitespace-nowrap">{t_i18n(roleLabelKey(u.role))}</Badge>
          {u.is_platform_admin && <Badge variant="cyan" className="whitespace-nowrap">{t_i18n("Platform Admin")}</Badge>}
        </span>
      ),
      sortValue: (u) => `${u.is_platform_admin ? 0 : 1}${u.role}`,
      csv: (u) => (u.is_platform_admin ? `${u.role}, platform admin` : u.role),
    },
    {
      key: "status",
      header: "Status",
      width: 96,
      cell: (u) => (u.is_active ? <Badge variant="outline">{t_i18n("Active")}</Badge> : <Badge variant="destructive">{t_i18n("Disabled")}</Badge>),
      sortValue: (u) => (u.is_active ? 0 : 1),
      csv: (u) => (u.is_active ? "active" : "disabled"),
      hideBelow: "3xl",
      hideable: true,
    },
    {
      key: "sign_in",
      header: "Sign-in method",
      width: 176,
      cell: (u) => <span className="block truncate text-default-secondary">{u.sso_provider ? ssoProviderLabel(u.sso_provider) : t_i18n("Password")}</span>,
      sortValue: (u) => u.sso_provider ?? "",
      csv: (u) => u.sso_provider ?? "password",
      hideBelow: "4xl",
      hideable: true,
    },
    { key: "integrations", header: "Integrations", width: 112, align: "right", cell: (u) => u.integration_count, sortValue: (u) => u.integration_count, csv: (u) => u.integration_count, hideable: true, defaultHidden: true },
    { key: "mcp_servers", header: "MCP Servers", width: 128, align: "right", cell: (u) => u.mcp_server_count, sortValue: (u) => u.mcp_server_count, csv: (u) => u.mcp_server_count, hideable: true, defaultHidden: true },
    { key: "agents", header: "Agents", width: 96, align: "right", cell: (u) => u.agent_count, sortValue: (u) => u.agent_count, csv: (u) => u.agent_count, hideable: true, defaultHidden: true },
    { key: "knowledge_bases", header: "Knowledge Bases", width: 184, align: "right", cell: (u) => u.knowledge_base_count, sortValue: (u) => u.knowledge_base_count, csv: (u) => u.knowledge_base_count, hideable: true, defaultHidden: true },
    {
      key: "created",
      header: "Created",
      width: 160,
      cell: (u) => <span className="whitespace-nowrap text-default-secondary">{u.created_at ? intl.formatDate(u.created_at, { dateStyle: "medium", timeStyle: "short" }) : ""}</span>,
      sortValue: (u) => u.created_at ?? "",
      csv: (u) => u.created_at ?? "",
      firstSort: "desc",
      hideBelow: "xl",
    },
  ];

  return (
    <div className="space-y-6">
      {/* The Users and groups family header (#9301 N3b): its h1, the page's
          help in the "i" (#10215), and one tab row Users / Groups. */}
      <NavFamilyHeader
        to="/users-and-groups"
        title="Users"
        infoLabel={t_i18n("About users")}
        info={t_i18n("Manage users, view their resources, and control access.")}
        actions={
          <>
            <Button variant="outline" className="gap-1.5" onClick={() => setInviteOpen(true)}><Mail className="h-4 w-4" /> {t_i18n("Invite user")}</Button>
            <Button className="gap-1.5" onClick={() => navigate("/users-and-groups/users/new")}><Plus className="h-4 w-4" /> {t_i18n("New User")}</Button>
          </>
        }
      />

      <InviteUserDialog open={inviteOpen} onOpenChange={setInviteOpen} />

      <DataTable
        rows={users ?? []}
        columns={columns}
        rowKey={(u) => u.id}
        rowHref={(u) => `/users-and-groups/users/${u.id}`}
        layout="fixed"
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        server={{ total: counted?.count ?? users?.length ?? 0 }}
        totalAll={countedAll?.count}
        searchPlaceholder={t_i18n("Search users by name or email")}
        filters={[
          {
            key: "role",
            label: "Role",
            options: [
              { value: "admin", label: t_i18n(roleLabelKey("admin")) },
              { value: "user", label: t_i18n(roleLabelKey("user")) },
            ],
          },
          {
            key: "status",
            label: "Status",
            options: [
              { value: "active", label: t_i18n("Active") },
              { value: "disabled", label: t_i18n("Disabled") },
            ],
          },
          {
            key: "created",
            label: "Created",
            options: CREATED_WINDOWS.map((w) => ({ value: w.value, label: t_i18n(w.label) })),
          },
          {
            key: "group",
            label: "Groups",
            options: (groups ?? []).map((g) => ({ value: g.id, label: g.name })),
          },
        ]}
        exportName="users"
        urlKey={URL_KEY}
        storageKey={STORAGE_KEY}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No users yet.")}
        noResultsLabel={t_i18n("No users matching your filters.")}
        rowTestId="users-row"
      />
    </div>
  );
}
