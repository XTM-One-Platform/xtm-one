// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM > Inventory `/ctem/inventory/:tab?` (#1178, Sam 22 Nov 2026): the organization's assets, read from the
 * canonical asset store (one asset per real thing across sources, dev-docs/asset-model.md section 9), and the
 * tech stack built from their software. Every list is paginated on the server, so 10,000 assets read as fast as
 * ten. The old `/ctem/assets` links redirect here.
 */
import { useSearchParams } from "react-router-dom";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  useCanonicalAssets,
  useTechStackInventory,
  type CanonicalAssetSummary,
  type TechStackItem,
} from "@/hooks/api/useOrganizationAssetsApi";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { formatCount } from "@/lib/number-format";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { INVENTORY_LABEL, INVENTORY_PATH, assetLabel, sourceLabel } from "./ctem/inventory";

type InventoryTab = "assets" | "tech-stack";

const ASSETS_URL_KEY = "a_";
const ASSETS_STORAGE_KEY = "ctem-inventory-assets";
const TECH_URL_KEY = "t_";
const TECH_STORAGE_KEY = "ctem-inventory-tech-stack";
const PAGE_SIZE = 50;
const NAME_SORT = { key: "name", direction: "asc" } as const;

function AssetsTab() {
  const { t_i18n, intl } = useFormatter();
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, ASSETS_URL_KEY, {
    sort: NAME_SORT,
    pageSize: storedPageSize(ASSETS_STORAGE_KEY, PAGE_SIZE),
  });
  const { data, isLoading, isError, refetch } = useCanonicalAssets({
    q: table.search,
    offset: table.page * table.pageSize,
    limit: table.pageSize,
  });

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
      cell: (a) => assetLabel(a.subcategory ?? a.category),
      csv: (a) => a.subcategory ?? a.category,
      hideable: true,
    },
    {
      key: "criticality",
      header: "Criticality",
      cell: (a) => assetLabel(a.criticality),
      csv: (a) => a.criticality,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Internet facing",
      cell: (a) => assetLabel(a.internet_facing),
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
      cell: (a) => [a.os_family, a.os_version].filter(Boolean).join(" "),
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
      cell: (a) => <TruncatedText text={a.sources.map(sourceLabel).join(", ")} className="max-w-[14rem]" />,
      csv: (a) => a.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <DataTable<CanonicalAssetSummary>
      rows={data?.items ?? []}
      columns={columns}
      rowKey={(a) => a.id}
      rowHref={(a) => `${INVENTORY_PATH}/assets/${a.id}`}
      server={{ total: data?.total ?? 0 }}
      urlKey={ASSETS_URL_KEY}
      storageKey={ASSETS_STORAGE_KEY}
      defaultSort={NAME_SORT}
      defaultPageSize={PAGE_SIZE}
      searchPlaceholder={t_i18n("Search assets by name")}
      noFilters="The store answers a name search only; the category, criticality and source filters come with the server filters of the asset list."
      exportName="ctem-inventory-assets"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No asset yet. Add a source to the inventory to fill it.")}
      noResultsLabel={t_i18n("No asset matches this search")}
      rowTestId="ctem-inventory-asset-row"
    />
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
      cell: (i) => assetLabel(i.kind),
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
      cell: (i) => <TruncatedText text={i.sources.map(sourceLabel).join(", ")} className="max-w-[14rem]" />,
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

export default function CtemInventory() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<InventoryTab>("assets");
  return (
    <div className="space-y-4">
      <CtemPageHeader
        title={t_i18n(INVENTORY_LABEL)}
        hint={t_i18n("One asset per real thing across your sources: the records of several sources on one thing are merged on their identifiers. The tech stack is built from the software the assets run.")}
        hintLabel={t_i18n("About the inventory")}
      />
      <DetailTabs<InventoryTab>
        ariaLabel={t_i18n("Inventory sections")}
        active={tab}
        tabs={[
          { key: "assets", label: "Assets" },
          { key: "tech-stack", label: "Tech stack" },
        ]}
      />
      {tab === "tech-stack" ? <TechStackTab /> : <AssetsTab />}
    </div>
  );
}
