// SPDX-License-Identifier: Apache-2.0

import { useNavigate, useSearchParams } from "react-router-dom";
import { UsersRound, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { useGroupsCount, useGroupsPage, type GroupTableQuery } from "@/hooks/api/useGroupApi";
import { GROUP_KIND_LABELS } from "@/lib/group-kinds";
import { readTableState, storedPageSize, type DataTableDefaults } from "@/lib/data-table-state";
import { useFormatter } from "@/i18n";
import type { Group, GroupKind } from "@/types";

/** The table's keys in the URL (`g_q`, `g_sort`, `g_page`, `g_f_members`...), unchanged since #10581. */
const URL_KEY = "g_";
const STORAGE_KEY = "groups";
const PAGE_SIZE = 25;
const DEFAULT_SORT = { key: "name", direction: "asc" } as const;

/** A filter's picked values as the API takes them: joined by commas, or nothing. */
function joined(values: readonly string[] | undefined): string | undefined {
  return values && values.length > 0 ? values.join(",") : undefined;
}

/**
 * The groups on the shared DataTable in server mode (#9378 slice 6z, #11940): GET /groups
 * searches, filters, sorts and pages them and GET /groups/count gives the total under the
 * same filters. The state stays in the URL under the `g_` keys it has had since #10581.
 */
export default function Groups() {
  const { t_i18n, intl } = useFormatter();
  const [searchParams] = useSearchParams();
  // The size the user kept (DataTable storageKey) is the size the API is asked with.
  const defaults: DataTableDefaults = { sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) };
  const table = readTableState(searchParams, URL_KEY, defaults);
  const filtersQuery: GroupTableQuery = {
    search: table.search.trim() || undefined,
    kind: joined(table.filters.kind),
    members: joined(table.filters.members),
  };
  const sort = table.sort ?? DEFAULT_SORT;
  const { data: groups, isLoading, isError, refetch } = useGroupsPage({
    ...filtersQuery,
    sort: sort.key,
    direction: sort.direction,
    limit: table.pageSize,
    offset: table.page * table.pageSize,
  });
  const { data: counted } = useGroupsCount(filtersQuery);
  // Every group under no filter: a filtered total reads "n of N" (UX r1 B1 on #11941).
  const { data: countedAll } = useGroupsCount({});
  const navigate = useNavigate();

  const columns: DataTableColumn<Group>[] = [
    {
      key: "name",
      header: "Name",
      cell: (g) => (
        <span className="flex min-w-0 items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/10">
            <UsersRound className="h-3.5 w-3.5 text-accent" aria-hidden />
          </span>
          <TruncatedText text={g.name} className="min-w-[240px] font-medium" />
          {g.auto_join && (
            <Badge variant="outline" className="shrink-0 whitespace-nowrap" title={t_i18n("Single sign-on users join this group")}>
              {t_i18n("Auto join")}
            </Badge>
          )}
        </span>
      ),
      sortValue: (g) => g.name.toLowerCase(),
      csv: (g) => g.name,
    },
    {
      key: "description",
      header: "Description",
      cell: (g) => <TruncatedText text={g.description ?? ""} className="max-w-[28rem] text-default-secondary" />,
      sortValue: (g) => (g.description ?? "").toLowerCase(),
      csv: (g) => g.description ?? "",
      hideBelow: "3xl",
      hideable: true,
    },
    {
      key: "kind",
      header: "Kind",
      cell: (g) => <span className="whitespace-nowrap text-default-secondary">{t_i18n(GROUP_KIND_LABELS[g.kind ?? "auto"])}</span>,
      sortValue: (g) => g.kind ?? "auto",
      csv: (g) => g.kind ?? "auto",
      hideBelow: "4xl",
      hideable: true,
    },
    {
      key: "members",
      header: "Members",
      align: "right",
      cell: (g) => g.member_count,
      sortValue: (g) => g.member_count,
      csv: (g) => g.member_count,
    },
    {
      key: "created",
      header: "Created",
      cell: (g) => <span className="whitespace-nowrap text-default-secondary">{intl.formatDate(g.created_at, { dateStyle: "medium", timeStyle: "short" })}</span>,
      sortValue: (g) => g.created_at,
      csv: (g) => g.created_at,
      firstSort: "desc",
      hideBelow: "xl",
    },
  ];

  return (
    <div className="space-y-6">
      {/* The Users and groups family header (#9301 N3b): its h1, the page's
          help in the "i" (#10215), and one tab row Users / Groups. */}
      <NavFamilyHeader
        to="/users-and-groups/groups"
        title="Groups"
        infoLabel={t_i18n("About groups")}
        info={t_i18n("Manage user groups for sharing agents, knowledge bases, and other resources.")}
        actions={
          <Button className="gap-1.5" onClick={() => navigate("/users-and-groups/groups/new")}>
            <Plus className="h-4 w-4" /> {t_i18n("New Group")}
          </Button>
        }
      />

      <DataTable
        rows={groups ?? []}
        columns={columns}
        rowKey={(g) => g.id}
        rowHref={(g) => `/users-and-groups/groups/${g.id}`}
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        server={{ total: counted?.count ?? groups?.length ?? 0 }}
        totalAll={countedAll?.count}
        searchPlaceholder={t_i18n("Search groups by name or description")}
        filters={[
          {
            key: "kind",
            label: "Kind",
            options: (Object.keys(GROUP_KIND_LABELS) as GroupKind[]).map((value) => ({
              value,
              label: t_i18n(GROUP_KIND_LABELS[value]),
            })),
          },
          {
            key: "members",
            label: "Members",
            options: [
              { value: "some", label: t_i18n("Has members") },
              { value: "empty", label: t_i18n("Empty") },
            ],
          },
        ]}
        exportName="groups"
        urlKey={URL_KEY}
        storageKey={STORAGE_KEY}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No groups yet")}
        rowTestId="groups-row"
      />
    </div>
  );
}
