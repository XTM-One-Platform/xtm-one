// SPDX-License-Identifier: Apache-2.0

/**
 * "Running versions" on an upgrade case page (#1252, dev-docs/ctem-cases.md
 * section 4.2ter): the versions of the case's product the organization's assets
 * run, read from the asset inventory (Intune, Defender, an SBOM, OpenAEV),
 * never typed in the software inventory. Each version shows how many assets run it,
 * where it stands against the fixed version when one is known, and the sources
 * that report it as their logos (an SBOM, a CSV import or a hand entry has no
 * brand: it gets a glyph and its name). With no fixed version the card says so
 * and names what was checked; with no asset running the product it says that
 * too, never a blank card. Per platform (#1572): each row is one version on one
 * platform (the asset's os_family), with the fixed version of that platform when
 * the remediation names one; a row whose platform has no known fix says so, and
 * an asset running several versions is said to be counted in each of its rows.
 */
import { Icon } from "@filigran/design-system";
import type { ComponentProps } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CASE_SECTION_ICON, CaseCardTitleText } from "./case-icons";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { InfoTip } from "@/components/ui/info-tip";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import type { CtemCaseRunningVersion, CtemCaseRunningVersions } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES, PROVIDER_LOGOS } from "@/lib/constants";
import { pivotModuleOf } from "@/lib/module-pivot";
import { formatCount } from "@/lib/number-format";

const UNKNOWN = "unknown";

/** A source with no brand mark: its glyph and its English name (translated where drawn). */
type IconName = ComponentProps<typeof Icon>["name"];

const SOURCE_GLYPHS: Record<string, { icon: IconName; label: string }> = {
  sbom: { icon: "file-text", label: "SBOM" },
  csv: { icon: "file-text", label: "CSV import" },
  manual: { icon: "pencil", label: "Manual" },
  api: { icon: "code-xml", label: "API" },
};

const STATUS_LABEL: Record<NonNullable<CtemCaseRunningVersion["status"]>, string> = {
  below_fixed: "Below the fixed version",
  fixed: "On the fixed version or later",
  not_comparable: "Cannot be compared",
};

const STATUS_RANK: Record<NonNullable<CtemCaseRunningVersion["status"]>, number> = { below_fixed: 0, not_comparable: 1, fixed: 2 };

/** The backend's status_reason when no fixed version applies to the row's platform (ctem_case_versions.FIX_NOT_KNOWN_FOR_PLATFORM). */
const FIX_NOT_KNOWN_FOR_PLATFORM = "fix not known for this platform";

/** The name a source is read under: a CTEM product, an integration, a glyph source, an agent, else its key. */
export function versionSourceName(raw: string): string {
  if (raw.startsWith("agent:")) return "Agent";
  // A module under a pre-rename key (openbas, opengrc) reads under its current name (#1357).
  const source = pivotModuleOf(raw) ?? raw;
  return CTEM_PRODUCT_INFO[source]?.name ?? INTEGRATION_PROVIDER_NAMES[source] ?? SOURCE_GLYPHS[source]?.label ?? source;
}

/** One source as its logo or glyph, its name in the product tooltip and for screen readers; shared by Running versions and Affected assets (UX r2 B1 on #1324). */
export function SourceMark({ source: raw }: { source: string }) {
  const { t_i18n } = useFormatter();
  // The module's canonical emblem, the one SourceBadge draws (#1357), whatever key the inventory used.
  const source = pivotModuleOf(raw) ?? raw;
  const name = t_i18n(versionSourceName(source));
  const glyph: { icon: IconName } | undefined = source.startsWith("agent:") ? { icon: "bot" } : SOURCE_GLYPHS[source];
  // Inside a routed row the row link is the single tab stop (#12070); elsewhere the mark keeps its own (UX r1 B1 on #1361).
  const tabIndex = useTriggerTabIndex();
  // The product tooltip, shown on hover and on keyboard focus, never the native title (UX r1 B3 on #1277).
  return (
    <Tip content={name}>
      <span
        className={`inline-flex shrink-0 items-center ${TIP_WRAP_FOCUS_RING}`}
        tabIndex={tabIndex}
        data-testid="ctem-case-version-source"
      >
        {PROVIDER_LOGOS[source] ? (
          <ProviderLogo provider={source} decorative className="h-4 w-4" />
        ) : glyph ? (
          <Icon name={glyph.icon} size={16} className="text-default-secondary" aria-hidden />
        ) : (
          <ProviderLogo provider={source} decorative className="h-4 w-4" />
        )}
        <span className="sr-only">{name}</span>
      </span>
    </Tip>
  );
}

export function CaseRunningVersions({
  productLabel,
  fixedVersion,
  versions,
}: {
  productLabel: string | null;
  fixedVersion: string | null;
  versions: CtemCaseRunningVersions | null | undefined;
}) {
  const { t_i18n, intl } = useFormatter();
  const product = productLabel || t_i18n("this product");
  const fixed = versions?.fixed_version ?? fixedVersion;
  const rows = versions?.versions ?? [];
  const byPlatform = Object.entries(versions?.fixed_versions ?? {});
  // A per-platform map alone (no generic version) is a known fix too (#1572).
  const hasFix = Boolean(fixed) || byPlatform.length > 0;
  const withPlatform = rows.some((r) => r.platform);

  const columns: DataTableColumn<CtemCaseRunningVersion>[] = [
    {
      key: "version",
      header: "Version",
      cell: (r) => (r.version === UNKNOWN ? t_i18n("Unknown") : r.version),
      sortValue: (r) => r.version,
      csv: (r) => r.version,
    },
    ...(withPlatform
      ? [
          {
            key: "platform",
            header: "Platform",
            cell: (r: CtemCaseRunningVersion) => r.platform ?? t_i18n("Not known"),
            sortValue: (r: CtemCaseRunningVersion) => r.platform ?? "",
            csv: (r: CtemCaseRunningVersion) => r.platform ?? "",
          } satisfies DataTableColumn<CtemCaseRunningVersion>,
        ]
      : []),
    ...(byPlatform.length > 0
      ? [
          {
            key: "fixed",
            header: "Fixed version",
            cell: (r: CtemCaseRunningVersion) => r.fixed_version ?? t_i18n("Not known"),
            sortValue: (r: CtemCaseRunningVersion) => r.fixed_version ?? "",
            csv: (r: CtemCaseRunningVersion) => r.fixed_version ?? "",
            hideable: true,
          } satisfies DataTableColumn<CtemCaseRunningVersion>,
        ]
      : []),
    {
      key: "assets",
      // Its own key, translated with the word of the card's sentences (actifs, activos, 資産), never the shared "Assets" (UX r1 B2 on #1277).
      header: "Asset count",
      align: "right",
      cell: (r) => formatCount(intl.locale, r.asset_count),
      sortValue: (r) => r.asset_count,
      csv: (r) => r.asset_count,
      firstSort: "desc",
    },
    ...(hasFix
      ? [
          {
            key: "status",
            header: "Status",
            // A platform with no known fix reads so, never "Cannot be compared" alone (#1572).
            cell: (r: CtemCaseRunningVersion) =>
              !r.status
                ? null
                : r.status_reason === FIX_NOT_KNOWN_FOR_PLATFORM
                  ? t_i18n("Fix not known for this platform")
                  : t_i18n(STATUS_LABEL[r.status]),
            sortValue: (r: CtemCaseRunningVersion) => (r.status ? STATUS_RANK[r.status] : 9),
            csv: (r: CtemCaseRunningVersion) => r.status ?? "",
            hideable: true,
          } satisfies DataTableColumn<CtemCaseRunningVersion>,
        ]
      : []),
    {
      key: "sources",
      header: "Sources",
      cell: (r) => (
        <span className="flex flex-wrap items-center gap-1.5">
          {r.sources.map((s) => (
            <SourceMark key={s} source={s} />
          ))}
        </span>
      ),
      csv: (r) => r.sources.map(versionSourceName).join("; "),
      hideable: true,
      // The last column takes the free width: the others hug their content (#2750).
      headerClassName: "w-full",
    },
  ];

  const allSources = [...new Set(rows.flatMap((r) => r.sources))];
  const filters: DataTableFilter<CtemCaseRunningVersion>[] = [
    {
      key: "source",
      label: "Source",
      values: (r) => r.sources,
      options: allSources.map((s) => ({ value: s, label: t_i18n(versionSourceName(s)) })),
    },
    ...(hasFix
      ? [
          {
            key: "status",
            label: "Status",
            values: (r: CtemCaseRunningVersion) => [r.status],
            options: (Object.keys(STATUS_LABEL) as (keyof typeof STATUS_LABEL)[]).map((s) => ({ value: s, label: t_i18n(STATUS_LABEL[s]) })),
          } satisfies DataTableFilter<CtemCaseRunningVersion>,
        ]
      : []),
  ];

  // The API lists the first versions only: the toolbar total reads "shown of all" and the rest is
  // said in the toolbar's tip, nothing is drawn under the table (UX r1 B1 on #1277).
  const moreVersions = versions?.more_versions ?? 0;
  const multiVersion = versions?.assets_multi_version ?? 0;
  const fixNotKnown = versions?.assets_fix_not_known_for_platform ?? 0;

  return (
    // A flex column, so next to Affected assets both cards share the row's height and the pager sits at the bottom (#2750).
    <Card data-testid="ctem-case-running-versions" className="flex flex-col">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.running_versions}>{t_i18n("Running versions")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3 content-base">
        {versions ? (
          <p data-testid="ctem-case-running-versions-summary">
            {t_i18n("The asset inventory reports {product} on {assets, plural, one {# asset} other {# assets}} in {versions, plural, one {# version} other {# versions}}.", {
              product,
              assets: versions.asset_count,
              versions: versions.version_count,
            })}
          </p>
        ) : (
          <p className="text-default-secondary" data-testid="ctem-case-running-versions-none">
            {t_i18n("No asset in the inventory reports {product}: the running versions come from the assets (Intune, Defender, an SBOM, OpenAEV).", { product })}
          </p>
        )}

        {hasFix && versions ? (
          <ul className="space-y-0.5" data-testid="ctem-case-running-versions-against-fixed">
            {byPlatform.length > 0 ? (
              <>
                <li className="font-medium">
                  {t_i18n("{count, plural, =0 {No asset is below the fixed version of its platform} one {# asset still below the fixed version of its platform} other {# assets still below the fixed version of their platform}}", {
                    count: versions.assets_below_fixed ?? 0,
                  })}
                </li>
                <li className="text-default-secondary">
                  {t_i18n("{count, plural, =0 {No asset runs the fixed version of its platform yet} one {# asset runs the fixed version of its platform or later} other {# assets run the fixed version of their platform or later}}", {
                    count: versions.assets_on_fixed ?? 0,
                  })}
                </li>
                <li className="text-default-secondary" data-testid="ctem-case-fixed-versions-by-platform">
                  {t_i18n("Fixed version per platform: {list}", {
                    list: intl.formatList(
                      byPlatform.map(([platform, version]) => t_i18n("{label}: {value}", { label: platform, value: version })),
                      { type: "conjunction" },
                    ),
                  })}
                </li>
              </>
            ) : (
              <>
                <li className="font-medium">
                  {t_i18n("{count, plural, =0 {No asset is below {version}} one {# asset still below {version}} other {# assets still below {version}}}", {
                    count: versions.assets_below_fixed ?? 0,
                    version: fixed ?? "",
                  })}
                </li>
                <li className="text-default-secondary">
                  {t_i18n("{count, plural, =0 {No asset runs {version} or later yet} one {# asset runs {version} or later} other {# assets run {version} or later}}", {
                    count: versions.assets_on_fixed ?? 0,
                    version: fixed ?? "",
                  })}
                </li>
              </>
            )}
            {versions.platform_unverified && fixed ? (
              <li className="text-default-secondary" data-testid="ctem-case-fix-platform-unverified">
                {t_i18n("{version} names no platform while the assets run several: an asset not below it is not read as fixed.", { version: fixed })}
              </li>
            ) : null}
            {fixNotKnown > 0 && (
              <li className="text-default-secondary" data-testid="ctem-case-fix-not-known-for-platform">
                {t_i18n("{count, plural, one {# asset is on a platform with no known fix} other {# assets are on a platform with no known fix}}", {
                  count: fixNotKnown,
                })}
              </li>
            )}
            {(versions.assets_not_comparable ?? 0) > 0 && (
              <li className="text-default-secondary">
                {t_i18n("{count, plural, one {# asset runs a version that cannot be compared} other {# assets run a version that cannot be compared}}", {
                  count: versions.assets_not_comparable ?? 0,
                })}
              </li>
            )}
          </ul>
        ) : !hasFix ? (
          <p className="text-default-secondary" data-testid="ctem-case-fixed-version-unknown">
            {t_i18n("Fixed version unknown: OpenCTI has no Software that remediates these issues and no course of action names a version.")}
          </p>
        ) : null}

        {multiVersion > 0 && (
          <p className="text-default-secondary" data-testid="ctem-case-running-versions-multi">
            {t_i18n("{count, plural, one {# asset runs several versions and is counted in each of its rows} other {# assets run several versions and are counted in each of their rows}}", {
              count: multiVersion,
            })}
          </p>
        )}

        {rows.length > 0 && (
          <DataTable
            fill
            framed={false}
            rows={rows}
            columns={columns}
            rowKey={(r) => `${r.version}|${r.platform ?? ""}`}
            searchFields={(r) => [r.version, r.platform ?? "", ...r.sources.map((s) => t_i18n(versionSourceName(s)))]}
            filters={filters}
            defaultSort={{ key: "assets", direction: "desc" }}
            defaultPageSize={10}
            exportName="ctem-case-running-versions"
            urlKey="v_"
            totalAll={versions?.version_count}
            toolbar={
              moreVersions > 0 ? (
                <InfoTip
                  label={t_i18n("Versions not listed")}
                  content={t_i18n("{count, plural, one {# more version not listed} other {# more versions not listed}}", { count: moreVersions })}
                />
              ) : undefined
            }
            emptyLabel={t_i18n("No version")}
            rowTestId="ctem-case-running-version-row"
          />
        )}
      </CardContent>
    </Card>
  );
}
