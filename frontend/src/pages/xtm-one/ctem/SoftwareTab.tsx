// SPDX-License-Identifier: Apache-2.0

/**
 * The Software tab of CTEM > Inventory (#1495 slice 2, replaces the Tech stack tab): one row per software entity
 * (vendor and product across every asset and SBOM) with its type, its version and asset counts, its CPE and how sure
 * it is, and its sources; each row opens the software's page. Paged and filtered on the server, the row pattern of
 * the Assets tab (#1476).
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { useToast } from "@/components/ui/toast";
import { TruncatedText } from "@/components/ui/truncated-text";
import { type SoftwareFilterKey, type SoftwareSummary, useDeleteSoftware, useSoftwareList } from "@/hooks/api/useOrganizationAssetsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { formatCount } from "@/lib/number-format";
import { CPE_STATE_LABELS, SOFTWARE_ORIGIN_LABELS, SOFTWARE_TYPE_LABELS, assetFieldLabel, softwarePath, sourceLabel } from "./inventory";
import { SourceLogo } from "./inventory-icons";
import { BULK_ACTION } from "./InventoryProfileTabs";
import { softwareDeletable } from "./inventory-entries";

const SOFTWARE_URL_KEY = "s_";
const SOFTWARE_STORAGE_KEY = "ctem-inventory-software";
const PAGE_SIZE = 50;
const NAME_SORT = { key: "name", direction: "asc" } as const;

/** The server filters of the software list, in the order the toolbar shows them, each with its stored values. */
const SOFTWARE_FILTERS: { key: SoftwareFilterKey; label: string; field: string; values: Record<string, string> }[] = [
  { key: "type", label: "Type", field: "software_type", values: SOFTWARE_TYPE_LABELS },
  { key: "origin", label: "Origin", field: "origin", values: SOFTWARE_ORIGIN_LABELS },
  { key: "cpe_state", label: "CPE", field: "cpe_state", values: CPE_STATE_LABELS },
];

export function SoftwareTab() {
  const { t_i18n, intl } = useFormatter();
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, SOFTWARE_URL_KEY, {
    sort: NAME_SORT,
    pageSize: storedPageSize(SOFTWARE_STORAGE_KEY, PAGE_SIZE),
  });
  const picked = Object.fromEntries(SOFTWARE_FILTERS.map((f) => [f.key, table.filters[f.key] ?? []]));
  const { data, isLoading, isError, refetch } = useSoftwareList({
    q: table.search,
    offset: table.page * table.pageSize,
    limit: table.pageSize,
    filters: picked,
  });
  // Selection and bulk Delete (#1729), the Asset groups pattern: a synced software is skipped and counted.
  const { isAdmin } = useAuth();
  const { toast } = useToast();
  const remove = useDeleteSoftware();
  const [selected, setSelected] = useState<string[]>([]);
  const [toDelete, setToDelete] = useState<string[] | null>(null);
  const picks = softwareDeletable((data?.items ?? []).filter((s) => selected.includes(s.id)));
  const onDelete = () => {
    if (!toDelete) return;
    remove.mutate(toDelete, {
      onSuccess: (out) => {
        setSelected([]);
        if (out.refused.length > 0) {
          toast({ title: t_i18n("Nothing was deleted"), description: out.refused.map((r) => r.reason).join("\n"), variant: "error" });
          if (out.written === 0) return;
        }
        toast({ title: t_i18n("{count, plural, one {# entry deleted} other {# entries deleted}}", { count: out.written }), variant: "success" });
      },
      onError: (e) => toast({ title: t_i18n("Nothing was deleted"), description: e.message, variant: "error" }),
    });
  };

  const filters: DataTableFilter<SoftwareSummary>[] = SOFTWARE_FILTERS.map(({ key, label, field, values }) => ({
    key,
    label,
    options: Object.keys(values).map((value) => ({ value, label: assetFieldLabel(t_i18n, field, value) })),
  }));

  const columns: DataTableColumn<SoftwareSummary>[] = [
    {
      key: "name",
      header: "Software",
      cell: (s) => <TruncatedText text={s.name} className="max-w-[18rem] font-medium" />,
      csv: (s) => s.name,
      // The store answers by name: the one server sort.
      sortValue: (s) => s.name.toLowerCase(),
    },
    {
      key: "type",
      header: "Type",
      cell: (s) => assetFieldLabel(t_i18n, "software_type", s.type),
      csv: (s) => s.type,
      hideable: true,
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      hint: t_i18n("The number of assets running the software, each counted once whatever its versions."),
      cell: (s) => formatCount(intl.locale, s.asset_count),
      csv: (s) => s.asset_count,
    },
    {
      key: "versions",
      header: "Versions",
      align: "right",
      // A software with a version no source knows says so beside the count, never an empty cell.
      cell: (s) => (
        <span className="inline-flex items-center gap-1.5">
          {formatCount(intl.locale, s.version_count)}
          {s.has_unknown_version && <span className="content-caption text-default-secondary">{t_i18n("incl. unknown")}</span>}
        </span>
      ),
      csv: (s) => s.version_count,
      hideable: true,
    },
    {
      key: "cpe",
      header: "CPE",
      cell: (s) =>
        s.cpe ? (
          <TruncatedText text={s.cpe} className="max-w-[16rem] font-mono" />
        ) : (
          <span className="text-default-secondary">{assetFieldLabel(t_i18n, "cpe_state", s.cpe_state)}</span>
        ),
      csv: (s) => s.cpe,
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "sources",
      header: "Sources",
      // The logo of each source and their names, as the Assets tab draws them (#1367, point 8a).
      cell: (s) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {s.sources.map((source) => (
            <SourceLogo key={source} source={source} />
          ))}
          <TruncatedText text={s.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[10rem]" />
        </span>
      ),
      csv: (s) => s.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <>
    <DataTable<SoftwareSummary>
      rows={data?.items ?? []}
      columns={columns}
      rowKey={(s) => s.id}
      rowHref={(s) => softwarePath(s.id)}
      selection={
        isAdmin
          ? {
              selected,
              onChange: setSelected,
              actions: (
                <Button
                  size="sm"
                  variant="secondary"
                  className={BULK_ACTION}
                  onClick={() => setToDelete(picks.ids)}
                  disabled={remove.isPending || picks.ids.length === 0}
                  data-testid="ctem-software-bulk-delete"
                >
                  {t_i18n("Delete")}
                </Button>
              ),
            }
          : undefined
      }
      server={{ total: data?.total ?? 0 }}
      urlKey={SOFTWARE_URL_KEY}
      storageKey={SOFTWARE_STORAGE_KEY}
      defaultSort={NAME_SORT}
      defaultPageSize={PAGE_SIZE}
      searchPlaceholder={t_i18n("Search software by name or vendor")}
      filters={filters}
      exportName="ctem-inventory-software"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No software yet. It fills from the software your sources report and the software you add.")}
      noResultsLabel={t_i18n("No software matches this search")}
      rowTestId="ctem-inventory-software-row"
    />
    <ConfirmDialog
      open={toDelete !== null}
      onOpenChange={(o) => !o && setToDelete(null)}
      variant="danger"
      title={t_i18n("Delete these entries?")}
      description={t_i18n("The entries entered by hand are deleted. A synced entry stays: its sync owns it.")}
      confirmLabel={t_i18n("Delete")}
      onConfirm={onDelete}
    />
    </>
  );
}
