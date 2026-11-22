// SPDX-License-Identifier: Apache-2.0

/**
 * One asset of CTEM > Inventory `/ctem/inventory/assets/:id` (#1178, dev-docs/asset-model.md section 9): the
 * recap of everything known on one real thing. Its fields with the source that set each one, the source records
 * merged into it and why, and its software (the version `unknown` when no source knows it, the bundling
 * component when an SBOM gave it).
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { NotFoundState } from "@/components/ui/not-found-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useCanonicalAsset, type AssetSoftware, type AssetSourceRecord } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { INVENTORY_LABEL, INVENTORY_PATH, assetLabel, sourceLabel } from "./ctem/inventory";

const SOFTWARE_PAGE = 100;

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const AssetGlyph = ({ className }: { className?: string }) => <Icon name="server" size={16} className={className} />;

/** The fields of the header band, in the order people read them. */
const HEADER_FIELDS = ["category", "criticality", "internet_facing", "owner"] as const;
const HEADER_LABELS: Record<(typeof HEADER_FIELDS)[number], string> = {
  category: "Category",
  criticality: "Criticality",
  internet_facing: "Internet facing",
  owner: "Owner",
};

export default function CtemAssetDetail() {
  const { t_i18n, intl } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [softwarePage, setSoftwarePage] = useState(0);
  const { data, isLoading, isError, refetch } = useCanonicalAsset(id, {
    offset: softwarePage * SOFTWARE_PAGE,
    limit: SOFTWARE_PAGE,
  });

  if (isError && !data) {
    return (
      <NotFoundState
        icon={AssetGlyph}
        title="Asset not found"
        description="This asset does not exist any more: a newer sync, a merge or a split may have replaced it."
        actionLabel="Back to Inventory"
        onAction={() => navigate(INVENTORY_PATH)}
        backTo={INVENTORY_PATH}
        backLabel="Back to Inventory"
      />
    );
  }

  const origin = (field: string) => {
    const source = data?.fields?.[field]?.source;
    return source ? t_i18n("From {source}", { source: sourceLabel(String(source)) }) : undefined;
  };
  const date = (value: string | null) => (value ? intl.formatDate(value, { dateStyle: "medium" }) : "");

  const softwareColumns: DataTableColumn<AssetSoftware>[] = [
    {
      key: "product",
      header: "Software",
      cell: (s) => <TruncatedText text={[s.vendor, s.product].filter(Boolean).join(" ")} className="max-w-[18rem] font-medium" />,
      csv: (s) => [s.vendor, s.product].filter(Boolean).join(" "),
      // The store pages the software by vendor and product: the page reads in that order.
      sortValue: (s) => `${s.vendor} ${s.product}`.toLowerCase(),
    },
    {
      key: "version",
      header: "Version",
      // `unknown` is said as such, never drawn empty (dev-docs/asset-model.md 9.4).
      cell: (s) => (s.version === "unknown" ? <span className="text-default-secondary">{t_i18n("Unknown")}</span> : s.version),
      csv: (s) => s.version,
    },
    {
      key: "bundled",
      header: "Bundled in",
      cell: (s) => <TruncatedText text={s.bundled_in ?? ""} className="max-w-[14rem]" />,
      csv: (s) => s.bundled_in,
      hideable: true,
    },
    {
      key: "sources",
      header: "Sources",
      cell: (s) => s.sources.map(sourceLabel).join(", "),
      csv: (s) => s.sources.join(" "),
      hideable: true,
    },
    {
      key: "last_seen",
      header: "Last seen",
      cell: (s) => date(s.last_seen),
      csv: (s) => s.last_seen,
      hideable: true,
      hideBelow: "4xl",
    },
  ];

  const recordColumns: DataTableColumn<AssetSourceRecord>[] = [
    { key: "source", header: "Source", cell: (r) => sourceLabel(r.source), csv: (r) => r.source, sortValue: (r) => r.source },
    {
      key: "external_id",
      header: "Its id there",
      cell: (r) => <TruncatedText text={r.external_id} className="max-w-[18rem] font-mono" />,
      csv: (r) => r.external_id,
    },
    {
      key: "last_seen",
      header: "Last seen",
      cell: (r) => date(r.last_seen),
      csv: (r) => r.last_seen,
      sortValue: (r) => r.last_seen,
      firstSort: "desc",
      hideable: true,
    },
  ];

  return (
    <div className="space-y-4">
      <CtemPageHeader
        back={{ to: INVENTORY_PATH, label: t_i18n(INVENTORY_LABEL) }}
        title={data?.name ?? ""}
        hint={data?.explanation || undefined}
        hintLabel={t_i18n("How this asset was matched")}
        stats={HEADER_FIELDS.map((field) => ({
          id: field,
          label: t_i18n(HEADER_LABELS[field]),
          value: assetLabel(String(data?.fields?.[field]?.value ?? "")) || "-",
          title: origin(field),
        }))}
      />

      <section aria-labelledby="asset-software" className="space-y-2">
        <h2 id="asset-software" className="text-base font-semibold">{t_i18n("Software")}</h2>
        <DataTable<AssetSoftware>
          rows={data?.software.items ?? []}
          columns={softwareColumns}
          rowKey={(s) => `${s.vendor}|${s.product}|${s.version}`}
          server={{ total: data?.software.total ?? 0 }}
          onStateChange={(state) => setSoftwarePage(state.page)}
          defaultPageSize={SOFTWARE_PAGE}
          noFilters="One asset's software, paged on the server."
          exportName="ctem-asset-software"
          isLoading={isLoading && !data}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No software known on this asset yet.")}
          rowTestId="ctem-asset-software-row"
        />
      </section>

      <section aria-labelledby="asset-sources" className="space-y-2">
        <h2 id="asset-sources" className="text-base font-semibold">{t_i18n("Sources")}</h2>
        <DataTable<AssetSourceRecord>
          rows={data?.records ?? []}
          columns={recordColumns}
          rowKey={(r) => `${r.source}|${r.external_id}`}
          defaultSort={{ key: "source", direction: "asc" }}
          defaultPageSize={25}
          searchFields={(r) => [r.source, sourceLabel(r.source), r.external_id]}
          searchPlaceholder={t_i18n("Search source or id")}
          noFilters="The few records merged into one asset."
          exportName="ctem-asset-sources"
          isLoading={isLoading && !data}
          emptyLabel={t_i18n("No source record.")}
          rowTestId="ctem-asset-record-row"
        />
      </section>
    </div>
  );
}
