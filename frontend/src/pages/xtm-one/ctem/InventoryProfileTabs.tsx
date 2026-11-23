// SPDX-License-Identifier: Apache-2.0

/**
 * The Asset groups and AI systems tabs of CTEM > Inventory (#1178, Sam 22 Nov 2026: four tabs Assets, Asset groups,
 * Tech stack, AI systems). The asset groups are rows of their own table since #1543 (`GET /asset-groups`, written one
 * row API call per group); the AI systems stay a section of the organization profile. Both lists are short, so
 * search, filters and export run in the page.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAssetGroups, useCanonicalAssets, useDeleteAssetGroups, type AssetGroupRecord } from "@/hooks/api/useOrganizationAssetsApi";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { INVENTORY_PATH, assetFieldLabel, sourceLabel } from "./inventory";
import { deletableRows, isSyncedRow } from "./inventory-entries";
import { InventoryEntryDialog, type EntryDialogMode } from "./InventoryEntryDialog";

/** The page header's Add of the Assets and Asset groups tabs (UX round 1 on #1375, B1): one add request the tab answers. */
export type AddRequest = { open: boolean; onClose: () => void };

/** A bulk action of the selection bar: the table toolbar's control height (UX round 1 on #1375, B1). */
export const BULK_ACTION = `${TOOLBAR_CONTROL_HEIGHT} px-3`;

/** The exposure of an asset group, as the Settings table words it. @i18n-keys */
const GROUP_EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;
/** @i18n-keys */
const AI_KIND_LABELS = { assistant: "Assistant", agent: "Agent", model_api: "Model API", embedded: "Embedded" } as const;
/** @i18n-keys */
const AI_EXPOSURE_LABELS = { internal: "Internal", partner: "Partner", public: "Public" } as const;

type AiSystemRow = { name: string; kind: string; owner: string; exposure: string; openaev_asset_id: string; source?: string };

/** A known value's label (translated by the caller), else the value as stored. */
const label = (labels: Record<string, string>, value: string) => labels[value] ?? value;

/** The Assets tab filtered on the AI target category: the AI systems the inventory's sources hold. */
const AI_TARGET_ASSETS_HREF = `${INVENTORY_PATH}?a_f_category=ai_target`;

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
    <DataTable<AssetGroupRecord>
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

export function AiSystemsTab() {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError, refetch } = useOrganizationContext();
  const systems = ((data?.profile.ai_systems ?? []) as AiSystemRow[]).filter((s) => s.name.trim());
  // The AI targets the Assets tab lists (LLM models, agents, MCP servers): the empty state never denies them
  // (UX round 2 on #1254, B9). One row read, the total is what counts.
  const aiTargets = useCanonicalAssets({ offset: 0, limit: 1, filters: { category: ["ai_target"] } });
  const aiTargetCount = aiTargets.data?.total ?? 0;

  const columns: DataTableColumn<AiSystemRow>[] = [
    {
      key: "name",
      header: "AI system",
      cell: (s) => <TruncatedText text={s.name} className="max-w-[18rem] font-medium" />,
      csv: (s) => s.name,
      sortValue: (s) => s.name.toLowerCase(),
    },
    {
      key: "kind",
      header: "Kind",
      cell: (s) => t_i18n(label(AI_KIND_LABELS, s.kind)),
      csv: (s) => s.kind,
      sortValue: (s) => s.kind,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Exposure",
      cell: (s) => t_i18n(label(AI_EXPOSURE_LABELS, s.exposure)),
      csv: (s) => s.exposure,
      sortValue: (s) => Object.keys(AI_EXPOSURE_LABELS).indexOf(s.exposure),
      hideable: true,
    },
    {
      key: "owner",
      header: "Owner",
      cell: (s) => <TruncatedText text={s.owner} className="max-w-[12rem]" />,
      csv: (s) => s.owner,
      sortValue: (s) => s.owner.toLowerCase(),
      hideable: true,
    },
    {
      key: "source",
      header: "Source",
      cell: (s) => sourceLabel(t_i18n, s.source || "manual"),
      csv: (s) => s.source || "manual",
      hideable: true,
      hideBelow: "6xl",
    },
  ];

  return (
    <DataTable<AiSystemRow>
      rows={systems}
      columns={columns}
      rowKey={(s) => s.name}
      searchFields={(s) => [s.name, s.owner]}
      searchPlaceholder={t_i18n("Search...")}
      filters={[
        { key: "kind", label: "Kind", values: (s: AiSystemRow) => [t_i18n(label(AI_KIND_LABELS, s.kind))] },
        { key: "exposure", label: "Exposure", values: (s: AiSystemRow) => [t_i18n(label(AI_EXPOSURE_LABELS, s.exposure))] },
      ]}
      urlKey="ai_"
      storageKey="ctem-inventory-ai-systems"
      exportName="ctem-inventory-ai-systems"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No AI system declared for the AI security fleet yet.")}
      emptyContent={
        aiTargetCount > 0 ? (
          <p className="py-10 text-center content-compact text-default-secondary" data-testid="ctem-inventory-ai-targets">
            {t_i18n("No AI system declared for the AI security fleet yet.")}{" "}
            <Link to={AI_TARGET_ASSETS_HREF} className="text-accent underline-offset-2 hover:underline">
              {t_i18n("{count, plural, one {# AI target asset is in the inventory} other {# AI target assets are in the inventory}}", {
                count: aiTargetCount,
              })}
            </Link>
          </p>
        ) : undefined
      }
      rowTestId="ctem-inventory-ai-row"
    />
  );
}
