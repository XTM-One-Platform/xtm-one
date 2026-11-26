// SPDX-License-Identifier: Apache-2.0

/**
 * The Asset groups tab of CTEM > Inventory (#1178). The asset groups are rows of their own table since #1543 (`GET /asset-groups`, written one
 * row API call per group). The list is short, so search, filters and export run in the page. A row opens the group's
 * page (#1556): its fields and its member assets; the Assets column counts them from the membership index.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAssetGroups, useDeleteAssetGroups, type AssetGroupRecord } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { assetFieldLabel, assetGroupPath, sourceLabel } from "./inventory";
import { deletableRows, isSyncedRow } from "./inventory-entries";
import { InventoryEntryDialog, type EntryDialogMode } from "./InventoryEntryDialog";
import { SourceLogo } from "./inventory-icons";

/** The page header's Add of the Assets and Asset groups tabs (UX round 1 on #1375, B1): one add request the tab answers. */
export type AddRequest = { open: boolean; onClose: () => void };

/** A bulk action of the selection bar: the table toolbar's control height (UX round 1 on #1375, B1). */
export const BULK_ACTION = `${TOOLBAR_CONTROL_HEIGHT} px-3`;

/** The exposure of an asset group, as the Settings table words it. @i18n-keys */
const GROUP_EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;

/** A known value's label (translated by the caller), else the value as stored. */
const label = (labels: Record<string, string>, value: string) => labels[value] ?? value;

export function AssetGroupsTab({ add }: { add: AddRequest }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { data, isLoading, isError, refetch } = useAssetGroups();
  const remove = useDeleteAssetGroups();
  const groups = (data?.items ?? []).filter((g) => g.name.trim());
  const [selected, setSelected] = useState<string[]>([]);
  const [dialog, setDialog] = useState<EntryDialogMode | null>(null);
  const [toDelete, setToDelete] = useState<string[] | null>(null);
  const keyOf = (g: AssetGroupRecord) => g.id;
  const selectedGroups = groups.filter((g) => selected.includes(g.id));

  const onDelete = () => {
    if (!toDelete) return;
    remove.mutate(
      toDelete,
      {
        onSuccess: (out) => {
          setSelected([]);
          if (out.refused.length > 0) {
            toast({ title: t_i18n("Nothing was deleted"), description: out.refused.map((r) => r.reason).join("\n"), variant: "error" });
            if (out.written === 0) return;
          }
          toast({ title: t_i18n("{count, plural, one {# entry deleted} other {# entries deleted}}", { count: out.written }), variant: "success" });
        },
        onError: (e) => toast({ title: t_i18n("Nothing was deleted"), description: e.message, variant: "error" }),
      },
    );
  };
  const syncedReason = t_i18n("Synced from its source: exclude it from the sync to remove it.");

  const columns: DataTableColumn<AssetGroupRecord>[] = [
    {
      key: "name",
      header: "Asset group",
      cell: (g) => <TruncatedText text={g.name} className="max-w-[18rem] font-medium" />,
      csv: (g) => g.name,
      sortValue: (g) => g.name.toLowerCase(),
    },
    {
      key: "environment",
      header: "Environment",
      cell: (g) => assetFieldLabel(t_i18n, "environment", g.environment),
      csv: (g) => g.environment,
      sortValue: (g) => g.environment,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Exposure",
      cell: (g) => t_i18n(label(GROUP_EXPOSURE_LABELS, g.internet_facing)),
      csv: (g) => g.internet_facing,
      sortValue: (g) => Object.keys(GROUP_EXPOSURE_LABELS).indexOf(g.internet_facing),
      hideable: true,
    },
    {
      key: "owner",
      header: "Owner",
      cell: (g) => <TruncatedText text={g.owner} className="max-w-[12rem]" />,
      csv: (g) => g.owner,
      sortValue: (g) => g.owner.toLowerCase(),
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      // The distinct assets in the group; the members its source named that the inventory lacks are said beside.
      cell: (g) => (
        <span className="whitespace-nowrap tabular-nums" data-unresolved={g.unresolved_members ?? 0}>
          {formatCount(intl.locale, g.member_count)}
          {(g.unresolved_members ?? 0) > 0 && (
            <span className="ml-1.5 text-default-secondary">
              {t_i18n("{count, plural, one {+# not in the inventory} other {+# not in the inventory}}", { count: g.unresolved_members ?? 0 })}
            </span>
          )}
        </span>
      ),
      csv: (g) => g.member_count,
      sortValue: (g) => g.member_count,
    },
    {
      key: "tech",
      header: "Software",
      align: "right",
      cell: (g) => formatCount(intl.locale, g.products.length),
      csv: (g) => g.products.join(" "),
      sortValue: (g) => g.products.length,
      hideable: true,
    },
    {
      key: "source",
      header: "Source",
      // The source's logo and name, as the Assets and Software lists draw them (#1729).
      cell: (g) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <SourceLogo source={g.source || "manual"} />
          <TruncatedText text={sourceLabel(t_i18n, g.source || "manual")} className="max-w-[10rem]" />
        </span>
      ),
      csv: (g) => g.source || "manual",
      hideable: true,
      hideBelow: "6xl",
    },
  ];

  return (
    <>
    <DataTable<AssetGroupRecord>
      rows={groups}
      columns={columns}
      rowKey={keyOf}
      rowHref={(g) => assetGroupPath(g.id)}
      rowActions={
        isAdmin
          ? (g) => (
              <OverflowMenu
                label={t_i18n("Actions on {name}", { name: g.name })}
                className="h-7 w-7"
                items={[
                  { id: "update", label: t_i18n("Update"), glyph: "pencil", onSelect: () => setDialog({ kind: "update", id: g.id }) },
                  {
                    id: "delete",
                    label: t_i18n("Delete"),
                    destructive: true,
                    disabled: isSyncedRow(g),
                    disabledReason: isSyncedRow(g) ? syncedReason : undefined,
                    onSelect: () => setToDelete([g.id]),
                  },
                ]}
              />
            )
          : undefined
      }
      rowActionsWidth={48}
      selection={
        isAdmin
          ? {
              selected,
              onChange: setSelected,
              actions: (
                <>
                  <Button size="sm" variant="secondary" className={BULK_ACTION} onClick={() => setDialog({ kind: "bulk", rows: selectedGroups })}>
                    {t_i18n("Update")}
                  </Button>
                  <Button size="sm" variant="secondary" className={BULK_ACTION} onClick={() => setToDelete(deletableRows(selectedGroups).ids)}
                    disabled={remove.isPending || deletableRows(selectedGroups).ids.length === 0}
                  >
                    {t_i18n("Delete")}
                  </Button>
                </>
              ),
            }
          : undefined
      }
      searchFields={(g) => [g.name, g.description, g.owner, ...g.tags]}
      searchPlaceholder={t_i18n("Search...")}
      filters={[
        { key: "exposure", label: "Exposure", values: (g: AssetGroupRecord) => [t_i18n(label(GROUP_EXPOSURE_LABELS, g.internet_facing))] },
        { key: "environment", label: "Environment", values: (g: AssetGroupRecord) => (g.environment ? [assetFieldLabel(t_i18n, "environment", g.environment)] : []) },
      ]}
      urlKey="g_"
      storageKey="ctem-inventory-asset-groups"
      exportName="ctem-inventory-asset-groups"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No asset group yet. Add a source to the inventory to fill it.")}
      rowTestId="ctem-inventory-group-row"
    />
    <InventoryEntryDialog
      section="asset_groups"
      mode={dialog ?? (add.open ? { kind: "add" } : null)}
      onOpenChange={(o) => {
        if (o) return;
        setDialog(null);
        add.onClose();
      }}
      onSaved={() => setSelected([])}
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
