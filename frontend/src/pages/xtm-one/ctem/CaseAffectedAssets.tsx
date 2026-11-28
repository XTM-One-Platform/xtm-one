// SPDX-License-Identifier: Apache-2.0

/**
 * "Affected assets" on a case page (#1176 slice 3b, dev-docs/ctem-cases.md
 * section 4.2quater): one row per asset the caller's visible issues name, with
 * its open issues, KEV and highest CVSS, and for an upgrade case the version of
 * the product it runs, read from the asset inventory (Intune, Defender, an
 * SBOM, OpenAEV), never typed in the software inventory. When the versions do not come
 * from the inventory the card says "Version source: none yet" and asks nothing
 * of the user. A row of an asset the inventory holds opens its overview
 * (CTEM > Inventory, `/ctem/inventory/assets/:id`: software, sources, SBOM);
 * an asset outside the inventory opens nothing.
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CASE_SECTION_ICON, CaseCardTitleText } from "./case-icons";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { InfoTip } from "@/components/ui/info-tip";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemCaseAffectedAsset, CtemCaseAffectedAssets } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { SourceMark, versionSourceName } from "./CaseRunningVersions";
import { INVENTORY_PATH } from "./inventory";

/** The asset overview of a row, only when the inventory holds the asset (CTEM-20-UI). */
function affectedAssetHref(asset: CtemCaseAffectedAsset): string | undefined {
  return asset.in_inventory && asset.asset_id ? `${INVENTORY_PATH}/assets/${encodeURIComponent(asset.asset_id)}` : undefined;
}

const STATUS_LABEL: Record<NonNullable<CtemCaseAffectedAsset["status"]>, string> = {
  below_fixed: "Below the fixed version",
  fixed: "On the fixed version or later",
  not_comparable: "Cannot be compared",
};

/**
 * Status steps aside below a 896 px card (the case page at 1080), back from the Columns pill:
 * every row keeps one height and the table never scrolls sideways (design standards s.3, UX r1
 * B2 on #1324). Sources stay: as 16 px marks they take about 60 px (UX r2 B1 on #1324). Status
 * is cut on one line with a tooltip.
 */
const SECONDARY_HIDE_BELOW = "4xl" as const;

/**
 * Asset and Running version from the same 4xl (896 px) table width: drawn whole on one line from
 * it (the paired half and the stacked card at 1728 and wider, UX r1 B2 on #2758), cut on one line
 * with the design-system tooltip below it (the stacked card at 1080), so a long FQDN or version
 * never scrolls the table sideways (UX r2 B4 on #2758). The DataTable box is the container.
 * A case paired with Running versions is never capped: pairVersionsAndAssets already bounds its
 * names and versions to what a 708 px half draws whole, so they also fit the stacked card.
 * Whole class names, so Tailwind sees them.
 */
const ASSET_NAME_CAP = "max-w-[14rem] @4xl:max-w-none";
const RUNNING_VERSION_CAP = "max-w-[10rem] @4xl:max-w-none";

/** The width rule of the Asset and Running version cells: none when paired, the 4xl cap otherwise. */
export function affectedAssetCaps(paired: boolean): { asset: string; version: string } {
  return paired
    ? { asset: "whitespace-nowrap", version: "whitespace-nowrap" }
    : { asset: ASSET_NAME_CAP, version: RUNNING_VERSION_CAP };
}

const STATUS_RANK: Record<NonNullable<CtemCaseAffectedAsset["status"]>, number> = { below_fixed: 0, not_comparable: 1, fixed: 2 };

/** The version cell of an asset: its versions, "Unknown" for an unknown one, "Not in the inventory" when no inventory asset runs the product. */
function assetVersionText(asset: CtemCaseAffectedAsset, t: (s: string) => string): string {
  if (!asset.in_inventory) return t("Not in the inventory");
  return (asset.versions ?? []).map((v) => (v === "unknown" ? t("Unknown") : v)).join(", ");
}

export function CaseAffectedAssets({ data, paired = false }: { data: CtemCaseAffectedAssets | undefined; paired?: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { asset: assetCap, version: versionCap } = affectedAssetCaps(paired);
  if (!data || data.asset_count === 0) return null;
  const fromInventory = data.version_source === "inventory";
  const withStatus = fromInventory && data.assets.some((a) => a.status);

  const columns: DataTableColumn<CtemCaseAffectedAsset>[] = [
    {
      key: "name",
      header: "Asset",
      // Whole from a 4xl table, cut with a tooltip below it: the trailing Sources column takes only the width left over.
      cell: (a) => <TruncatedText text={a.name} className={assetCap} />,
      sortValue: (a) => a.name.toLowerCase(),
      csv: (a) => a.name,
    },
    {
      key: "open",
      header: "Open issues",
      align: "right",
      cell: (a) => formatCount(intl.locale, a.open_issue_count),
      sortValue: (a) => a.open_issue_count,
      csv: (a) => a.open_issue_count,
      firstSort: "desc",
    },
    {
      key: "kev",
      header: "KEV",
      cell: (a) => (a.kev ? t_i18n("Yes") : t_i18n("No")),
      sortValue: (a) => (a.kev ? 1 : 0),
      csv: (a) => (a.kev ? "yes" : "no"),
      firstSort: "desc",
      hideable: true,
    },
    {
      key: "cvss",
      header: "Max CVSS",
      align: "right",
      cell: (a) => (a.max_cvss == null ? null : formatCount(intl.locale, a.max_cvss)),
      sortValue: (a) => a.max_cvss ?? -1,
      csv: (a) => a.max_cvss ?? "",
      firstSort: "desc",
      hideable: true,
    },
    ...(fromInventory
      ? [
          {
            key: "version",
            header: "Running version",
            cell: (a: CtemCaseAffectedAsset) => (
              <TruncatedText
                text={assetVersionText(a, t_i18n)}
                className={cn(versionCap, !a.in_inventory && "text-default-secondary")}
              />
            ),
            sortValue: (a: CtemCaseAffectedAsset) => (a.in_inventory ? (a.versions ?? []).join(",") : "~"),
            csv: (a: CtemCaseAffectedAsset) => (a.in_inventory ? (a.versions ?? []).join("; ") : ""),
          } satisfies DataTableColumn<CtemCaseAffectedAsset>,
          ...(withStatus
            ? [
                {
                  key: "status",
                  header: "Status",
                  cell: (a: CtemCaseAffectedAsset) =>
                    a.status ? <TruncatedText text={t_i18n(STATUS_LABEL[a.status])} className="max-w-[12rem]" /> : null,
                  sortValue: (a: CtemCaseAffectedAsset) => (a.status ? STATUS_RANK[a.status] : 9),
                  csv: (a: CtemCaseAffectedAsset) => a.status ?? "",
                  hideable: true,
                  hideBelow: SECONDARY_HIDE_BELOW,
                } satisfies DataTableColumn<CtemCaseAffectedAsset>,
              ]
            : []),
          {
            key: "sources",
            header: "Sources",
            // The same marks as the Running versions card next to it: one implementation per pattern (UX r2 B1 on #1324).
            cell: (a: CtemCaseAffectedAsset) => (
              <span className="flex items-center gap-1.5">
                {(a.sources ?? []).map((s) => (
                  <SourceMark key={s} source={s} />
                ))}
              </span>
            ),
            csv: (a: CtemCaseAffectedAsset) => (a.sources ?? []).map(versionSourceName).join("; "),
            hideable: true,
            // The last column takes the free width: the others hug their content (#2750).
            headerClassName: "w-full",
          } satisfies DataTableColumn<CtemCaseAffectedAsset>,
        ]
      : []),
  ];

  const filters: DataTableFilter<CtemCaseAffectedAsset>[] = [
    {
      key: "kev",
      label: "KEV",
      values: (a) => [a.kev ? "yes" : "no"],
      options: [
        { value: "yes", label: t_i18n("Yes") },
        { value: "no", label: t_i18n("No") },
      ],
    },
    ...(withStatus
      ? [
          {
            key: "status",
            label: "Status",
            values: (a: CtemCaseAffectedAsset) => [a.status],
            options: (Object.keys(STATUS_LABEL) as (keyof typeof STATUS_LABEL)[]).map((s) => ({ value: s, label: t_i18n(STATUS_LABEL[s]) })),
          } satisfies DataTableFilter<CtemCaseAffectedAsset>,
        ]
      : []),
  ];

  return (
    // A flex column, so next to Running versions both cards share the row's height and the pager sits at the bottom (#2750).
    <Card data-testid="ctem-case-affected-assets" className="flex flex-col">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.affected_assets}>{t_i18n("Affected assets")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3 content-base">
        {!fromInventory && (
          <p className="text-default-secondary" data-testid="ctem-case-affected-assets-no-version">
            {t_i18n("Version source: none yet")}
          </p>
        )}
        <DataTable
          fill
          framed={false}
          rows={data.assets}
          columns={columns}
          filters={filters}
          rowKey={(a) => a.name}
          rowHref={affectedAssetHref}
          searchFields={(a) => [a.name]}
          searchPlaceholder={t_i18n("Search asset")}
          defaultSort={{ key: "open", direction: "desc" }}
          defaultPageSize={10}
          exportName="ctem-case-affected-assets"
          urlKey="a_"
          totalAll={data.asset_count}
          toolbar={
            data.more_assets > 0 ? (
              <InfoTip
                label={t_i18n("Assets not listed")}
                content={t_i18n("{count, plural, one {# more asset not listed} other {# more assets not listed}}", { count: data.more_assets })}
              />
            ) : undefined
          }
          emptyLabel={t_i18n("No asset")}
          rowTestId="ctem-case-affected-asset-row"
        />
      </CardContent>
    </Card>
  );
}
