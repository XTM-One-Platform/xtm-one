// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM > Inventory `/ctem/inventory/:tab?` (#1178, Sam 22 Nov 2026): the organization's assets, read from the
 * canonical asset store (one asset per real thing across sources, dev-docs/asset-model.md section 9), and the
 * tech stack built from their software. Every list is paginated on the server, so 10,000 assets read as fast as
 * ten.
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { useToast } from "@/components/ui/toast";
import { DetailTabs } from "@/components/ui/detail-tabs";
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
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { formatCount } from "@/lib/number-format";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { CategoryMark, OsMark, SourceLogo } from "./ctem/inventory-icons";
import { AiSystemsTab, AssetGroupsTab } from "./ctem/InventoryProfileTabs";
import { AssetMatchingCard, InventorySourcesCard } from "./ctem/InventorySourcesCard";
import { INVENTORY_LABEL, INVENTORY_PATH, assetCategory, assetCategoryLabel, assetFieldLabel, sourceLabel } from "./ctem/inventory";

type InventoryTab = "assets" | "asset-groups" | "tech-stack" | "ai-systems" | "sources";

const ASSETS_URL_KEY = "a_";
const ASSETS_STORAGE_KEY = "ctem-inventory-assets";
const TECH_URL_KEY = "t_";
const TECH_STORAGE_KEY = "ctem-inventory-tech-stack";
const PAGE_SIZE = 50;
const NAME_SORT = { key: "name", direction: "asc" } as const;

/** The server filters of the asset list, in the order the toolbar shows them. */
const ASSET_FILTERS: { key: AssetFilterKey; label: string }[] = [
  { key: "category", label: "Category" },
  { key: "criticality", label: "Criticality" },
  { key: "internet_facing", label: "Internet facing" },
  { key: "source", label: "Source" },
];

/** The query parameter of the administrator's edit view: the list and its edit cards never share the screen. */
const EDIT_PARAM = "edit";

/** The query parameter of the Asset matching view of the Sources tab: nothing renders under the syncs table (UX round 2, B7). */
const MATCHING_PARAM = "matching";

/** The most records one merge names (organization_asset_store.MAX_MERGE_RECORDS). */
const MAX_MERGE_RECORDS = 50;

function AssetsTab() {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, ASSETS_URL_KEY, {
    sort: NAME_SORT,
    pageSize: storedPageSize(ASSETS_STORAGE_KEY, PAGE_SIZE),
  });
  const picked = Object.fromEntries(ASSET_FILTERS.map((f) => [f.key, table.filters[f.key] ?? []]));
  const { data, isLoading, isError, refetch } = useCanonicalAssets({
    q: table.search,
    offset: table.page * table.pageSize,
    limit: table.pageSize,
    filters: picked,
  });
  const facets = useAssetFacets();
  const decide = useRecordAssetDecision();
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmMerge, setConfirmMerge] = useState(false);

  const filters: DataTableFilter<CanonicalAssetSummary>[] = ASSET_FILTERS.map(({ key, label }) => ({
    key,
    label,
    options: (facets.data?.[key] ?? []).map((value) => ({
      value,
      label: key === "source" ? sourceLabel(t_i18n, value) : assetFieldLabel(t_i18n, key, value),
    })),
  }));

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
      cell: (a) => <TruncatedText text={a.name} className="max-w-[18rem] font-medium" />,
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
    },
    {
      key: "sources",
      header: "Sources",
      // The logo of each source, its name beside the first and on hover (#1367, point 8a).
      cell: (a) => (
        <span className="inline-flex min-w-0 items-center gap-1.5" title={a.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")}>
          {a.sources.map((source) => (
            <SourceLogo key={source} source={source} />
          ))}
          <TruncatedText text={a.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[12rem]" />
        </span>
      ),
      csv: (a) => a.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <>
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
        filters={filters}
        exportName="ctem-inventory-assets"
        isLoading={isLoading && !data}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No asset yet. Add a source to the inventory to fill it.")}
        noResultsLabel={t_i18n("No asset matches this search")}
        rowTestId="ctem-inventory-asset-row"
        selection={
          isAdmin
            ? {
                selected,
                onChange: setSelected,
                actions: (
                  <Button size="sm" variant="secondary" disabled={!canMerge || decide.isPending} onClick={() => setConfirmMerge(true)}>
                    {t_i18n("Merge into one asset")}
                  </Button>
                ),
              }
            : undefined
        }
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
 * The profile cards an administrator edits on each tab (#1178 slice 2b): the same cards, per-card edit and Save as
 * Settings > Organization had, now here; Settings keeps the business profile.
 */
const EDIT_CARDS = {
  assets: ["assets_exposure", "prioritization_policy", "outside_lens"],
  "asset-groups": ["asset_groups"],
  "tech-stack": ["tech_stack"],
  "ai-systems": ["ai_systems"],
} as const satisfies Record<Exclude<InventoryTab, "sources">, readonly string[]>;

export default function CtemInventory() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<InventoryTab>("assets");
  // Editing the profile stays administrator-only, as in Settings; everyone else reads the tables.
  const { isAdmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  // The Sources tab (slice 3) holds the inventory syncs and asset matching; it embeds no profile card.
  const editCards = tab === "sources" ? null : (EDIT_CARDS[tab] ?? EDIT_CARDS.assets);
  // One list per screen, nothing below it (UX round 1, B2 and B3): an administrator edits in a view of its own,
  // opened from the header, and every reader, administrators included, sees the same list.
  const editing = isAdmin && Boolean(editCards) && searchParams.get(EDIT_PARAM) === "1";
  // Asset matching is read by everyone, edited by administrators inside its own card.
  const matching = tab === "sources" && searchParams.get(MATCHING_PARAM) === "1";
  const toggleParam = (param: string, on: boolean) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (on) next.set(param, "1");
        else next.delete(param);
        return next;
      },
      { replace: true },
    );
  const setEditing = (on: boolean) => toggleParam(EDIT_PARAM, on);
  const setMatching = (on: boolean) => toggleParam(MATCHING_PARAM, on);
  return (
    <div className="space-y-4">
      {/* The app's standard list header (#1367, Sam 23 Nov 2026: no band with a surface and a border). */}
      <ListPageHeader
        title={t_i18n(INVENTORY_LABEL)}
        info={t_i18n("One asset per real thing across your sources: the records of several sources on one thing are merged on their identifiers. The tech stack is built from the software the assets run.")}
        infoLabel={t_i18n("About the inventory")}
        actions={
          tab === "sources" ? (
            matching ? (
              <Button variant="secondary" className="gap-1.5" onClick={() => setMatching(false)} data-testid="ctem-inventory-matching-done">
                <Icon name="check" size={16} aria-hidden /> {t_i18n("Done")}
              </Button>
            ) : (
              <Button variant="secondary" className="gap-1.5" onClick={() => setMatching(true)} data-testid="ctem-inventory-matching">
                <Icon name="settings" size={16} aria-hidden /> {t_i18n("Asset matching")}
              </Button>
            )
          ) : isAdmin && editCards ? (
            editing ? (
              <Button variant="secondary" className="gap-1.5" onClick={() => setEditing(false)} data-testid="ctem-inventory-edit-done">
                <Icon name="check" size={16} aria-hidden /> {t_i18n("Done")}
              </Button>
            ) : (
              <Button variant="secondary" className="gap-1.5" onClick={() => setEditing(true)} data-testid="ctem-inventory-edit">
                <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
              </Button>
            )
          ) : undefined
        }
      />
      <DetailTabs<InventoryTab>
        ariaLabel={t_i18n("Inventory sections")}
        active={tab}
        tabs={[
          { key: "assets", label: "Assets" },
          { key: "asset-groups", label: "Asset groups" },
          { key: "tech-stack", label: "Tech stack" },
          { key: "ai-systems", label: "AI systems" },
          { key: "sources", label: "Sources" },
        ]}
      />
      {editing && editCards ? (
        <OrganizationSection key={tab} embed={{ cards: editCards }} />
      ) : tab === "sources" ? (
        matching ? <AssetMatchingCard /> : <InventorySourcesCard />
      ) : tab === "asset-groups" ? (
        <AssetGroupsTab />
      ) : tab === "tech-stack" ? (
        <TechStackTab />
      ) : tab === "ai-systems" ? (
        <AiSystemsTab />
      ) : (
        <AssetsTab />
      )}
    </div>
  );
}
