// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM > Inventory `/ctem/inventory/:tab?` (#1178, Sam 22 Nov 2026): the organization's assets, read from the
 * canonical asset store (one asset per real thing across sources, dev-docs/asset-model.md section 9), and the
 * tech stack built from their software. Every list is paginated on the server, so 10,000 assets read as fast as
 * ten.
 */
import { useEffect, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { BackButton } from "@/components/ui/back-button";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { useToast } from "@/components/ui/toast";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { TruncatedText } from "@/components/ui/truncated-text";
import { Icon } from "@filigran/design-system";
import {
  useAssetFacets,
  useCanonicalAssets,
  useRecordAssetDecision,
  useTechStackInventory,
  type AssetFilterKey,
  type CanonicalAssetSummary,
  type TechStackItem,
} from "@/hooks/api/useOrganizationAssetsApi";
import { useTabParam } from "@/hooks/useTabParam";
import { useAuth } from "@/hooks/useAuth";
import { OrganizationSection } from "@/pages/settings/OrganizationSection";
import { useOrganizationContext, useSaveOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import type { AssetRow } from "@/lib/organization-profile";
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { formatCount } from "@/lib/number-format";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { CategoryMark, OsMark, SourceLogo } from "./ctem/inventory-icons";
import { AssetFacetSidebar, SIDEBAR_FACETS } from "./ctem/AssetFacetSidebar";
import { AssetGroupsTab, BULK_ACTION, type AddRequest } from "./ctem/InventoryProfileTabs";
import { assetRowIndexes, deleteRows, isSyncedRow } from "./ctem/inventory-entries";
import { InventoryEntryDialog, type EntryDialogMode } from "./ctem/InventoryEntryDialog";
import { AssetMatchingCard, InventorySourcesCard } from "./ctem/InventorySourcesCard";
import { useInventoryEditView } from "./ctem/inventory-edit-view";
import { INVENTORY_LABEL, INVENTORY_PATH, assetCategory, assetCategoryLabel, assetFieldLabel, sourceLabel } from "./ctem/inventory";

type InventoryTab = "assets" | "asset-groups" | "tech-stack" | "sources";

/**
 * The retired AI systems tab (#1501: an AI system is an asset of category AI target): its old link lands on the
 * Assets tab filtered on that category, so a bookmark or a fleet link keeps working.
 */
const RETIRED_AI_SYSTEMS_TAB = "ai-systems";

const ASSETS_URL_KEY = "a_";
const AI_TARGET_ASSETS_HREF = `${INVENTORY_PATH}?${ASSETS_URL_KEY}f_category=ai_target`;
const ASSETS_STORAGE_KEY = "ctem-inventory-assets";
const TECH_URL_KEY = "t_";
const TECH_STORAGE_KEY = "ctem-inventory-tech-stack";
const PAGE_SIZE = 50;
const NAME_SORT = { key: "name", direction: "asc" } as const;


/**
 * The query parameter of the former Asset matching view (UX round 2 on #1254, B7). Asset matching now sits under the syncs
 * table (Sam, 23 Nov 2026): an old link lands on the Sources page and scrolls to the card, the parameter dropped.
 */
const MATCHING_PARAM = "matching";

/** The most records one merge names (organization_asset_store.MAX_MERGE_RECORDS). */
const MAX_MERGE_RECORDS = 50;

function AssetsTab({ add }: { add: AddRequest }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, ASSETS_URL_KEY, {
    sort: NAME_SORT,
    pageSize: storedPageSize(ASSETS_STORAGE_KEY, PAGE_SIZE),
  });
  // The sidebar's facets (#1501): OR within one, AND across them; the counts follow the search and the other picks.
  const picked = Object.fromEntries(SIDEBAR_FACETS.map((key) => [key, table.filters[key] ?? []])) as Record<AssetFilterKey, string[]>;
  const { data, isLoading, isError, refetch } = useCanonicalAssets({
    q: table.search,
    offset: table.page * table.pageSize,
    limit: table.pageSize,
    filters: picked,
  });
  const facets = useAssetFacets({ q: table.search, filters: picked });
  const decide = useRecordAssetDecision();
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmMerge, setConfirmMerge] = useState(false);
  // Entity management (#1367, point 2): Add, the row menu and the bulk actions write the profile's asset rows.
  const { data: context } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  // The API type leaves the #11543 sections out; the profile carries them (lib/organization-profile ProfileForm).
  const profileRows = ((context?.profile as { assets_exposure?: AssetRow[] } | undefined)?.assets_exposure ?? []) as AssetRow[];
  const [dialog, setDialog] = useState<EntryDialogMode | null>(null);
  const [toDelete, setToDelete] = useState<number[] | null>(null);
  const indexesOf = (assets: readonly CanonicalAssetSummary[]) => assetRowIndexes(profileRows, assets.flatMap((a) => a.records ?? []));
  // An asset whose every record a sync owns cannot be deleted here.
  const handIndexes = (a: CanonicalAssetSummary) => indexesOf([a]).filter((i) => !isSyncedRow(profileRows[i]));
  const selectedAssets = (data?.items ?? []).filter((a) => selected.includes(a.id));
  const onDelete = () => {
    if (!context || !toDelete) return;
    const out = deleteRows(profileRows, toDelete);
    save.mutate(
      { profile: { ...context.profile, assets_exposure: out.rows } as typeof context.profile, source: "form" },
      {
        onSuccess: () => {
          setSelected([]);
          toast({ title: t_i18n("{count, plural, one {# entry deleted} other {# entries deleted}}", { count: out.changed }), variant: "success" });
        },
        onError: (error) => toast({ title: t_i18n("Nothing was deleted"), description: error.message, variant: "error" }),
      },
    );
  };
  const syncedReason = t_i18n("Synced from its source: exclude it from the sync to remove it.");

  const rows = data?.items ?? [];
  const mergeRecords = rows.filter((a) => selected.includes(a.id)).flatMap((a) => a.records ?? []);
  const canMerge = selected.length >= 2 && mergeRecords.length >= 2 && mergeRecords.length <= MAX_MERGE_RECORDS;
  const onMerge = () => {
    decide.mutate(
      { kind: "merge", records: mergeRecords },
      {
        onSuccess: () => {
          setSelected([]);
          toast({ title: t_i18n("Assets merged into one"), variant: "success" });
        },
        onError: (error) => toast({ title: t_i18n("The assets were not merged"), description: error.message, variant: "error" }),
      },
    );
  };

  const columns: DataTableColumn<CanonicalAssetSummary>[] = [
    {
      key: "name",
      header: "Asset",
      cell: (a) => <TruncatedText text={a.name} className="max-w-[16rem] font-medium" />,
      csv: (a) => a.name,
      // The store answers by name: the one server sort.
      sortValue: (a) => a.name.toLowerCase(),
    },
    {
      key: "category",
      header: "Category",
      cell: (a) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <CategoryMark category={a.category} subcategory={a.subcategory} />
          <TruncatedText text={assetCategoryLabel(t_i18n, a)} className="max-w-[16rem]" />
        </span>
      ),
      csv: (a) => assetCategory(a),
      hideable: true,
    },
    {
      key: "criticality",
      header: "Criticality",
      cell: (a) => assetFieldLabel(t_i18n, "criticality", a.criticality),
      csv: (a) => a.criticality,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Internet facing",
      cell: (a) => assetFieldLabel(t_i18n, "internet_facing", a.internet_facing),
      csv: (a) => a.internet_facing,
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "owner",
      header: "Owner",
      cell: (a) => <TruncatedText text={a.owner ?? ""} className="max-w-[12rem]" />,
      csv: (a) => a.owner,
      hideable: true,
      hideBelow: "6xl",
    },
    {
      key: "os",
      header: "OS",
      cell: (a) =>
        a.os_family || a.os_version ? (
          <span className="inline-flex min-w-0 items-center gap-1.5">
            <OsMark family={a.os_family} />
            <TruncatedText text={[a.os_family, a.os_version].filter(Boolean).join(" ")} className="max-w-[12rem]" />
          </span>
        ) : null,
      csv: (a) => [a.os_family, a.os_version].filter(Boolean).join(" "),
      hideable: true,
      hideBelow: "6xl",
    },
    {
      key: "software",
      header: "Software",
      align: "right",
      cell: (a) => formatCount(intl.locale, a.software_count),
      csv: (a) => a.software_count,
      hideable: true,
      // The table fits its pane at 1080 (UX round 1 on #1375, B3): the count steps aside first.
      hideBelow: "4xl",
    },
    {
      key: "sources",
      header: "Sources",
      // The logo of each source, its name beside the first and on hover (#1367, point 8a).
      cell: (a) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {a.sources.map((source) => (
            <SourceLogo key={source} source={source} />
          ))}
          <TruncatedText text={a.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[10rem]" />
        </span>
      ),
      csv: (a) => a.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <>
      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
      <AssetFacetSidebar facets={facets.data} picked={picked} prefix={ASSETS_URL_KEY} />
      <div className="min-w-0 flex-1">
      <DataTable<CanonicalAssetSummary>
        rows={rows}
        columns={columns}
        rowKey={(a) => a.id}
        rowHref={(a) => `${INVENTORY_PATH}/assets/${a.id}`}
        server={{ total: data?.total ?? 0 }}
        urlKey={ASSETS_URL_KEY}
        storageKey={ASSETS_STORAGE_KEY}
        defaultSort={NAME_SORT}
        defaultPageSize={PAGE_SIZE}
        searchPlaceholder={t_i18n("Search assets by name")}
        exportName="ctem-inventory-assets"
        isLoading={isLoading && !data}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No asset yet. Add a source to the inventory to fill it.")}
        noResultsLabel={t_i18n("No asset matches this search")}
        rowTestId="ctem-inventory-asset-row"
        rowActions={
          isAdmin
            ? (a) => {
                const indexes = indexesOf([a]);
                const deletable = handIndexes(a);
                return (
                  <OverflowMenu
                    label={t_i18n("Actions on {name}", { name: a.name })}
                    className="h-7 w-7"
                    items={[
                      {
                        id: "update",
                        label: t_i18n("Update"),
                        glyph: "pencil",
                        disabled: indexes.length === 0,
                        onSelect: () => setDialog(indexes.length === 1 ? { kind: "update", index: indexes[0] } : { kind: "bulk", indexes }),
                      },
                      {
                        id: "delete",
                        label: t_i18n("Delete"),
                        destructive: true,
                        disabled: deletable.length === 0,
                        disabledReason: deletable.length === 0 ? syncedReason : undefined,
                        onSelect: () => setToDelete(deletable),
                      },
                    ]}
                  />
                );
              }
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
                    <Button size="sm" variant="secondary" className={BULK_ACTION} onClick={() => setDialog({ kind: "bulk", indexes: indexesOf(selectedAssets) })}>
                      {t_i18n("Update")}
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      className={BULK_ACTION}
                      disabled={save.isPending || selectedAssets.every((a) => handIndexes(a).length === 0)}
                      onClick={() => setToDelete(selectedAssets.flatMap(handIndexes))}
                    >
                      {t_i18n("Delete")}
                    </Button>
                    <Button size="sm" variant="secondary" className={BULK_ACTION} disabled={!canMerge || decide.isPending} onClick={() => setConfirmMerge(true)}>
                      {t_i18n("Merge into one asset")}
                    </Button>
                  </>
                ),
              }
            : undefined
        }
      />
      </div>
      </div>
      <InventoryEntryDialog
        section="assets_exposure"
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
      <ConfirmDialog
        open={confirmMerge}
        onOpenChange={setConfirmMerge}
        title={t_i18n("Merge these assets?")}
        description={t_i18n(
          "Their source records become one asset, whatever their identifiers say. The merge is audited and lasts across syncs.",
        )}
        confirmLabel={t_i18n("Merge")}
        onConfirm={onMerge}
      />
    </>
  );
}

function TechStackTab() {
  const { t_i18n, intl } = useFormatter();
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, TECH_URL_KEY, {
    sort: { key: "product", direction: "asc" },
    pageSize: storedPageSize(TECH_STORAGE_KEY, PAGE_SIZE),
  });
  const { data, isLoading, isError, refetch } = useTechStackInventory({
    q: table.search,
    offset: table.page * table.pageSize,
    limit: table.pageSize,
  });

  const versions = (item: TechStackItem) =>
    item.versions.map((v) => `${v.version} (${formatCount(intl.locale, v.asset_count)})`).join(", ");

  const columns: DataTableColumn<TechStackItem>[] = [
    {
      key: "product",
      header: "Product",
      cell: (i) => <TruncatedText text={[i.vendor, i.product].filter(Boolean).join(" ")} className="max-w-[18rem] font-medium" />,
      csv: (i) => [i.vendor, i.product].filter(Boolean).join(" "),
      // The inventory answers by vendor and product: the one server sort.
      sortValue: (i) => `${i.vendor} ${i.product}`.toLowerCase(),
    },
    {
      key: "kind",
      header: "Kind",
      cell: (i) => assetFieldLabel(t_i18n, "kind", i.kind),
      csv: (i) => i.kind,
      hideable: true,
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      hint: t_i18n("The number of assets running the product, each counted once whatever its versions."),
      cell: (i) => formatCount(intl.locale, i.asset_count),
      csv: (i) => i.asset_count,
    },
    {
      key: "versions",
      header: "Versions in use",
      cell: (i) => <TruncatedText text={versions(i)} className="max-w-[18rem]" />,
      csv: (i) => versions(i),
      hideable: true,
    },
    {
      key: "cpe",
      header: "CPE",
      cell: (i) => <TruncatedText text={i.cpe ?? ""} className="max-w-[16rem] font-mono" />,
      csv: (i) => i.cpe,
      hideable: true,
      hideBelow: "6xl",
    },
    {
      key: "sources",
      header: "Sources",
      cell: (i) => <TruncatedText text={i.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[14rem]" />,
      csv: (i) => i.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <DataTable<TechStackItem>
      rows={data?.items ?? []}
      columns={columns}
      rowKey={(i) => `${i.vendor}|${i.product}`}
      server={{ total: data?.total ?? 0 }}
      urlKey={TECH_URL_KEY}
      storageKey={TECH_STORAGE_KEY}
      defaultSort={{ key: "product", direction: "asc" }}
      defaultPageSize={PAGE_SIZE}
      searchPlaceholder={t_i18n("Search vendor or product")}
      noFilters="The inventory answers a vendor or product search only."
      exportName="ctem-inventory-tech-stack"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No product yet. It fills from the software of your assets and the products you add.")}
      noResultsLabel={t_i18n("No product matches this search")}
      rowTestId="ctem-inventory-tech-row"
    />
  );
}

/**
 * The profile cards that are settings, not rows (#1178 slice 2b, kept by #1367): the rows of every tab are managed
 * in place (Add, row menu, bulk actions), so only the policy cards of the Assets tab keep an edit view, opened by a
 * link to the card (`?card=`, Turn on on the Signals sources, #1356) and closed by Done. No page-level Edit button.
 */
const EDIT_CARDS = {
  assets: ["prioritization_policy", "outside_lens", "evidence_health"],
} as const;
export default function CtemInventory() {
  const [tab] = useTabParam<InventoryTab>("assets");
  if ((tab as string) === RETIRED_AI_SYSTEMS_TAB) return <Navigate to={AI_TARGET_ASSETS_HREF} replace />;
  return <InventoryPage />;
}

function InventoryPage() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<InventoryTab>("assets");
  // No page-level Edit mode any more (#1367, point 2): each tab manages its rows (Add, row menu, bulk actions).
  const [searchParams, setSearchParams] = useSearchParams();
  const { isAdmin, isLoading: authLoading } = useAuth();
  const [adding, setAdding] = useState(false);
  const add: AddRequest = { open: adding, onClose: () => setAdding(false) };
  const editCards = tab === "assets" ? EDIT_CARDS.assets : null;
  // A link to one of these cards opens the edit view on it and keeps it open once the card parameter leaves
  // (UX rounds 1 and 2 on #1356, B3 then B1).
  const [editing, setEditing] = useInventoryEditView(editCards, isAdmin);
  // Asset matching is read by everyone, edited by administrators inside its own card, under the syncs table.
  const matchingLink = tab === "sources" && searchParams.get(MATCHING_PARAM) === "1";
  const navigate = useNavigate();
  useEffect(() => {
    if (!matchingLink || !isAdmin) return;
    document.querySelector('[data-testid="ctem-inventory.matching"]')?.scrollIntoView({ block: "start" });
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(MATCHING_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [matchingLink, isAdmin, setSearchParams]);
  // Sources is a page of its own, not a tab (#1367, point 4): the standard back link to the inventory, its title, the
  // syncs table, then Asset matching below it with no click (Sam, 23 Nov 2026: no header button).
  // The sources and syncs are read and written by administrators only (the API answers 403 to a reader): a reader
  // who opens the URL lands on the inventory (UX round 1 on #1476, B2).
  if (tab === "sources" && !isAdmin) {
    return authLoading ? null : <Navigate to={INVENTORY_PATH} replace />;
  }
  if (tab === "sources") {
    return (
      <div className="space-y-4" data-testid="ctem-inventory-sources-page">
        <BackButton to={INVENTORY_PATH} label="Back to Inventory" />
        <ListPageHeader
          title={t_i18n("Inventory sources")}
          info={t_i18n("The platforms each sync reads on a schedule, the agents and partners writing through the API, CSV imports and rows entered by hand. Asset matching merges the records of several sources into one asset.")}
          infoLabel={t_i18n("About the inventory sources")}
        />
        <InventorySourcesCard />
        <AssetMatchingCard />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {/* The app's standard list header (#1367, Sam 23 Nov 2026: no band with a surface and a border). */}
      <ListPageHeader
        title={t_i18n(INVENTORY_LABEL)}
        info={t_i18n("One asset per real thing across your sources: the records of several sources on one thing are merged on their identifiers. The tech stack is built from the software the assets run.")}
        infoLabel={t_i18n("About the inventory")}
        actions={
          editing ? (
            <Button variant="secondary" className="gap-1.5" onClick={() => setEditing(false)} data-testid="ctem-inventory-edit-done">
              <Icon name="check" size={16} aria-hidden /> {t_i18n("Done")}
            </Button>
          ) : (
            <span className="flex flex-wrap items-center gap-2">
              {/* Sources, a secondary action top right (#1367, point 4: a button, not a tab), for administrators only: the
                  sources API is admin-only (UX round 1 on #1476, B2). Secondary before the filled action, which stays at the
                  right end (design-system Page-title actions). */}
              {isAdmin && (
                <Button variant="secondary" className="gap-1.5" onClick={() => navigate(`${INVENTORY_PATH}/sources`)} data-testid="ctem-inventory-sources">
                  <Icon name="database" size={16} aria-hidden /> {t_i18n("Sources")}
                </Button>
              )}
              {isAdmin && (tab === "assets" || tab === "asset-groups") && (
                // The tab's add action, outside the table toolbar (UX round 1 on #1375, B1), the page's primary action
                // (Sam, 23 Nov 2026: Add filled, Sources outlined).
                <Button
                  className="gap-1.5"
                  onClick={() => setAdding(true)}
                  data-testid={tab === "assets" ? "ctem-inventory-asset-add" : "ctem-inventory-group-add"}
                >
                  <Icon name="plus" size={16} aria-hidden /> {t_i18n(tab === "assets" ? "Add an asset" : "Add an asset group")}
                </Button>
              )}
            </span>
          )
        }
      />
      <DetailTabs<InventoryTab>
        ariaLabel={t_i18n("Inventory sections")}
        active={tab}
        tabs={[
          { key: "assets", label: "Assets" },
          { key: "asset-groups", label: "Asset groups" },
          { key: "tech-stack", label: "Tech stack" },
        ]}
      />
      {editing && editCards ? (
        <OrganizationSection key={tab} embed={{ cards: editCards }} />
      ) : tab === "asset-groups" ? (
        <AssetGroupsTab add={add} />
      ) : tab === "tech-stack" ? (
        <TechStackTab />
      ) : (
        <AssetsTab add={add} />
      )}
    </div>
  );
}
