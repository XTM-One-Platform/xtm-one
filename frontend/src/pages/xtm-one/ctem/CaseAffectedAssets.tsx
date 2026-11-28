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
 * B2 on #1324). Sources stay: as 16 px marks they take about 60 px (UX r2 B1 on #1324). The
 * text cells are cut on one line with a tooltip.
 */
const SECONDARY_HIDE_BELOW = "4xl" as const;

const STATUS_RANK: Record<NonNullable<CtemCaseAffectedAsset["status"]>, number> = { below_fixed: 0, not_comparable: 1, fixed: 2 };

/** The version cell of an asset: its versions, "Unknown" for an unknown one, "Not in the inventory" when no inventory asset runs the product. */
function assetVersionText(asset: CtemCaseAffectedAsset, t: (s: string) => string): string {
  if (!asset.in_inventory) return t("Not in the inventory");
  return (asset.versions ?? []).map((v) => (v === "unknown" ? t("Unknown") : v)).join(", ");
}

export function CaseAffectedAssets({ data, className }: { data: CtemCaseAffectedAssets | undefined; className?: string }) {
  const { t_i18n, intl } = useFormatter();
  if (!data || data.asset_count === 0) return null;
  const fromInventory = data.version_source === "inventory";
  const withStatus = fromInventory && data.assets.some((a) => a.status);

  const columns: DataTableColumn<CtemCaseAffectedAsset>[] = [
    {
      key: "name",
      header: "Asset",
      // A free-text cell of an auto-layout table carries a width cap (13.ak).
      cell: (a) => <TruncatedText text={a.name} className="max-w-[14rem]" />,
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
              <span className={a.in_inventory ? undefined : "text-default-secondary"}>
                <TruncatedText text={assetVersionText(a, t_i18n)} className="max-w-[8rem]" />
              </span>
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
            // Half width beside the issues (UX round 1 B2 on #2738): under Columns below a wide card.
            hideBelow: SECONDARY_HIDE_BELOW,
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
    <Card className={className} data-testid="ctem-case-affected-assets">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.affected_assets}>{t_i18n("Affected assets")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {!fromInventory && (
          <p className="text-default-secondary" data-testid="ctem-case-affected-assets-no-version">
            {t_i18n("Version source: none yet")}
          </p>
        )}
        <DataTable
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
