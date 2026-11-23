// SPDX-License-Identifier: Apache-2.0

/**
 * The Asset groups tab of CTEM > Inventory (#1178; the AI systems tab retired by #1501: an AI system is an asset of
 * category AI target). The list is a section of the organization profile, read from the same API the agents read;
 * nothing is stored twice. The list is short (the profile caps it), so search, filters and export run in the page.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useSaveOrganizationContext, type OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import type { AssetGroupRow } from "@/lib/organization-profile";
import { assetFieldLabel, sourceLabel } from "./inventory";
import { deleteRows, isSyncedRow } from "./inventory-entries";
import { InventoryEntryDialog, type EntryDialogMode } from "./InventoryEntryDialog";

/** The page header's Add of the Assets and Asset groups tabs (UX round 1 on #1375, B1): one add request the tab answers. */
export type AddRequest = { open: boolean; onClose: () => void };

/** A bulk action of the selection bar: the table toolbar's control height (UX round 1 on #1375, B1). */
export const BULK_ACTION = `${TOOLBAR_CONTROL_HEIGHT} px-3`;

/** An asset group with its index in the profile's list (what the entry dialog and a delete address). */
type IndexedGroup = AssetGroupRow & { index: number };

/** The exposure of an asset group, as the Settings table words it. @i18n-keys */
const GROUP_EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;

/** A known value's label (translated by the caller), else the value as stored. */
const label = (labels: Record<string, string>, value: string) => labels[value] ?? value;

export function AssetGroupsTab({ add }: { add: AddRequest }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { data, isLoading, isError, refetch } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  const stored = (data?.profile as { asset_groups?: AssetGroupRow[] } | undefined)?.asset_groups ?? [];
  const groups: IndexedGroup[] = stored.map((g, index) => ({ ...g, index })).filter((g) => g.name.trim());
  const [selected, setSelected] = useState<string[]>([]);
  const [dialog, setDialog] = useState<EntryDialogMode | null>(null);
  const [toDelete, setToDelete] = useState<number[] | null>(null);
  const keyOf = (g: IndexedGroup) => String(g.index);
  const selectedIndexes = selected.map(Number);

  const onDelete = () => {
    if (!data || !toDelete) return;
    const out = deleteRows(stored, toDelete);
    save.mutate(
      { profile: { ...data.profile, asset_groups: out.rows } as unknown as OrganizationProfile, source: "form" },
      {
        onSuccess: () => {
          setSelected([]);
          toast({ title: t_i18n("{count, plural, one {# entry deleted} other {# entries deleted}}", { count: out.changed }), variant: "success" });
        },
        onError: (e) => toast({ title: t_i18n("Nothing was deleted"), description: e.message, variant: "error" }),
      },
    );
  };
  const syncedReason = t_i18n("Synced from its source: exclude it from the sync to remove it.");

  const columns: DataTableColumn<IndexedGroup>[] = [
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
      key: "tech",
      header: "Tech stack",
      align: "right",
      cell: (g) => formatCount(intl.locale, g.tech_stack.length),
      csv: (g) => g.tech_stack.join(" "),
      sortValue: (g) => g.tech_stack.length,
      hideable: true,
    },
    {
      key: "source",
      header: "Source",
      cell: (g) => sourceLabel(t_i18n, g.source || "manual"),
      csv: (g) => g.source || "manual",
      hideable: true,
      hideBelow: "6xl",
    },
  ];

  return (
    <>
    <DataTable<IndexedGroup>
      rows={groups}
      columns={columns}
      rowKey={keyOf}
      rowActions={
        isAdmin
          ? (g) => (
              <OverflowMenu
                label={t_i18n("Actions on {name}", { name: g.name })}
                className="h-7 w-7"
                items={[
                  { id: "update", label: t_i18n("Update"), glyph: "pencil", onSelect: () => setDialog({ kind: "update", index: g.index }) },
                  {
                    id: "delete",
                    label: t_i18n("Delete"),
                    destructive: true,
                    disabled: isSyncedRow(g),
                    disabledReason: isSyncedRow(g) ? syncedReason : undefined,
                    onSelect: () => setToDelete([g.index]),
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
                  <Button size="sm" variant="secondary" className={BULK_ACTION} onClick={() => setDialog({ kind: "bulk", indexes: selectedIndexes })}>
                    {t_i18n("Update")}
                  </Button>
                  <Button size="sm" variant="secondary" className={BULK_ACTION} onClick={() => setToDelete(selectedIndexes)} disabled={save.isPending}>
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
        { key: "exposure", label: "Exposure", values: (g: AssetGroupRow) => [t_i18n(label(GROUP_EXPOSURE_LABELS, g.internet_facing))] },
        { key: "environment", label: "Environment", values: (g: AssetGroupRow) => (g.environment ? [assetFieldLabel(t_i18n, "environment", g.environment)] : []) },
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
