// SPDX-License-Identifier: Apache-2.0

/**
 * One software of CTEM > Inventory `/ctem/inventory/software/:id` (#1495 slice 2): what the organization knows of
 * one vendor and product across every asset and SBOM. Its identity and CPE (how sure the CPE is, the hand override),
 * its versions with the assets running each (`unknown` said as such), the assets it is installed on, what it depends
 * on and what uses it, and its sources. The asset page's Software section links each row here.
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Card } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  type SoftwareEdge,
  type SoftwareInstallation,
  type SoftwareVersion,
  useSoftware,
} from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { AssetSection, Fact } from "./ctem/AssetOverview";
import { INVENTORY_PATH, assetFieldLabel, softwarePath, sourceLabel } from "./ctem/inventory";
import { SourceLogo } from "./ctem/inventory-icons";

/** The Installations table opens at 25 rows, the platform's page size; the picker changes it. */
const INSTALLATIONS_PAGE_SIZE = 25;

/** The Software tab, where the back link returns. */
const SOFTWARE_TAB_PATH = `${INVENTORY_PATH}/software`;

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const SoftwareGlyph = ({ className }: { className?: string }) => <Icon name="box" size={16} className={className} />;

/** A version as people read it: `unknown` in the reader's language, never an empty cell. */
function VersionText({ version }: { version: string }) {
  const { t_i18n } = useFormatter();
  return version === "unknown" ? <span className="text-default-secondary">{t_i18n("Unknown")}</span> : <>{version}</>;
}

/** The logos and names of a row's sources, as the inventory tables draw them. */
function SourcesCell({ sources }: { sources: readonly string[] }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {sources.map((source) => (
        <SourceLogo key={source} source={source} />
      ))}
      <TruncatedText text={sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[12rem]" />
    </span>
  );
}

export default function CtemSoftwareDetail() {
  const { t_i18n, intl } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  // Page and page size both reach the API: a size picked in the table is the request's limit.
  const [page, setPage] = useState({ index: 0, size: INSTALLATIONS_PAGE_SIZE });
  const { data, isLoading, isError, refetch } = useSoftware(id, { offset: page.index * page.size, limit: page.size });

  if (isError && !data) {
    return (
      <NotFoundState
        icon={SoftwareGlyph}
        title="Software not found"
        description="This software does not exist any more: no source reports it since the last sync, or it was deleted."
        actionLabel="Back to Software"
        onAction={() => navigate(SOFTWARE_TAB_PATH)}
        backTo={SOFTWARE_TAB_PATH}
        backLabel="Back to Software"
      />
    );
  }

  const date = (value: string | null) => (value ? intl.formatDate(value, { dateStyle: "medium" }) : "");
  const versions = data?.versions ?? [];
  const installations = data?.installations;
  const dependsOn = data?.depends_on ?? [];
  const usedBy = data?.used_by ?? [];
  // A version id the dependency edges name, read as its version (an edge from one of several versions says which).
  const versionOf = new Map(versions.map((v) => [v.id, v.version]));

  const versionColumns: DataTableColumn<SoftwareVersion>[] = [
    {
      key: "version",
      header: "Version",
      cell: (v) => <VersionText version={v.version} />,
      csv: (v) => v.version,
      sortValue: (v) => v.version.toLowerCase(),
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      cell: (v) => formatCount(intl.locale, v.asset_count),
      csv: (v) => v.asset_count,
      sortValue: (v) => v.asset_count,
    },
    {
      key: "cpe",
      header: "CPE",
      cell: (v) => <TruncatedText text={v.cpe} className="max-w-[18rem] font-mono" />,
      csv: (v) => v.cpe,
      hideable: true,
    },
    {
      key: "purl",
      header: "Package URL",
      cell: (v) => <TruncatedText text={v.purl} className="max-w-[16rem] font-mono" />,
      csv: (v) => v.purl,
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "last_seen",
      header: "Last seen",
      cell: (v) => date(v.last_seen),
      csv: (v) => v.last_seen,
      hideable: true,
      hideBelow: "4xl",
    },
  ];

  const installationColumns: DataTableColumn<SoftwareInstallation>[] = [
    {
      key: "asset",
      header: "Asset",
      cell: (i) => <TruncatedText text={i.asset_name} className="max-w-[18rem] font-medium" />,
      csv: (i) => i.asset_name,
      // The store pages the installations by asset name: the page reads in that order.
      sortValue: (i) => i.asset_name.toLowerCase(),
    },
    {
      key: "version",
      header: "Version",
      cell: (i) => <VersionText version={i.version} />,
      csv: (i) => i.version,
    },
    {
      key: "bundled",
      header: "Bundled in",
      cell: (i) => <TruncatedText text={i.bundled_in ?? ""} className="max-w-[14rem]" />,
      csv: (i) => i.bundled_in,
      hideable: true,
    },
    {
      key: "sources",
      header: "Sources",
      cell: (i) => <SourcesCell sources={i.sources} />,
      csv: (i) => i.sources.join(" "),
      hideable: true,
    },
    {
      key: "last_seen",
      header: "Last seen",
      cell: (i) => date(i.last_seen),
      csv: (i) => i.last_seen,
      hideable: true,
      hideBelow: "4xl",
    },
  ];

  /** The columns of one dependency direction: the other software and its version, and which version of this one. */
  const edgeColumns = (own: "from_version_id" | "version_id"): DataTableColumn<SoftwareEdge>[] => [
    {
      key: "software",
      header: "Software",
      cell: (e) => <TruncatedText text={e.software_name} className="max-w-[18rem] font-medium" />,
      csv: (e) => e.software_name,
      sortValue: (e) => e.software_name.toLowerCase(),
    },
    {
      key: "version",
      header: "Version",
      cell: (e) => <VersionText version={e.version} />,
      csv: (e) => e.version,
    },
    {
      key: "own",
      header: "Of this software",
      hint: t_i18n("The version of this software the dependency was reported for."),
      cell: (e) => <VersionText version={versionOf.get(e[own]) ?? "unknown"} />,
      csv: (e) => versionOf.get(e[own]) ?? "unknown",
      hideable: true,
    },
    {
      key: "scope",
      header: "Scope",
      cell: (e) => e.scope,
      csv: (e) => e.scope,
      hideable: true,
      hideBelow: "4xl",
    },
  ];

  const facts = data
    ? [
        <Fact key="type" icon="box" label={t_i18n("Type")} values={[assetFieldLabel(t_i18n, "software_type", data.type)]} />,
        ...(data.vendor ? [<Fact key="vendor" icon="building-2" label={t_i18n("Vendor")} values={[data.vendor]} />] : []),
        <Fact
          key="cpe"
          icon="key-round"
          label={t_i18n("CPE")}
          values={[
            data.cpe ? (
              <span key="cpe" className="font-mono" title={data.cpe}>
                {data.cpe}
              </span>
            ) : (
              assetFieldLabel(t_i18n, "cpe_state", data.cpe_state)
            ),
          ]}
          hint={data.cpe ? assetFieldLabel(t_i18n, "cpe_state", data.cpe_state) : undefined}
        />,
        ...(data.purl
          ? [
              <Fact
                key="purl"
                icon="link-2"
                label={t_i18n("Package URL")}
                values={[
                  <span key="purl" className="font-mono" title={data.purl}>
                    {data.purl}
                  </span>,
                ]}
              />,
            ]
          : []),
        <Fact key="origin" icon="database" label={t_i18n("Origin")} values={[assetFieldLabel(t_i18n, "origin", data.origin)]} />,
        <Fact
          key="sources"
          icon="layers"
          label={t_i18n("Sources")}
          values={data.sources.length ? [<SourcesCell key="sources" sources={data.sources} />] : [t_i18n("Entered by hand")]}
        />,
        ...(data.last_seen ? [<Fact key="last_seen" icon="clock" label={t_i18n("Last seen")} values={[date(data.last_seen)]} />] : []),
      ]
    : [];

  return (
    <div className="space-y-4">
      <EntityPageHeader
        backTo={SOFTWARE_TAB_PATH}
        backLabel="Back to Software"
        icon={data ? <Icon name="box" size={20} className="text-default-secondary" aria-hidden /> : undefined}
        title={data?.name ?? ""}
        description={data?.description || undefined}
        badges={
          data ? (
            <span className="content-caption text-default-secondary" data-testid="ctem-software-header-counts">
              {t_i18n("{count, plural, one {# asset} other {# assets}}", { count: data.asset_count })}
              {" · "}
              {t_i18n("{count, plural, one {# version} other {# versions}}", { count: data.version_count })}
            </span>
          ) : undefined
        }
      />

      {data && (
        <Card className="p-4" data-testid="ctem-software-overview">
          <div className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2 xl:grid-cols-4">{facts}</div>
        </Card>
      )}

      <AssetSection
        id="software-versions"
        title={t_i18n("Versions")}
        count={versions.length}
        empty={!isLoading && versions.length === 0}
        emptyLabel={t_i18n("No version known for this software yet.")}
      >
        <DataTable<SoftwareVersion>
          rows={versions}
          columns={versionColumns}
          rowKey={(v) => v.id}
          defaultSort={{ key: "version", direction: "asc" }}
          defaultPageSize={25}
          noFilters="The versions of one software, a short list."
          exportName="ctem-software-versions"
          isLoading={isLoading && !data}
          emptyLabel={t_i18n("No version known for this software yet.")}
          rowTestId="ctem-software-version-row"
        />
      </AssetSection>

      <AssetSection
        id="software-installations"
        title={t_i18n("Installed on")}
        count={installations?.total}
        empty={!isLoading && (installations?.total ?? 0) === 0}
        emptyLabel={t_i18n("No asset runs this software.")}
      >
        <DataTable<SoftwareInstallation>
          rows={installations?.items ?? []}
          columns={installationColumns}
          rowKey={(i) => `${i.asset_id}|${i.version_id ?? i.version}|${i.bundled_in ?? ""}`}
          rowHref={(i) => `${INVENTORY_PATH}/assets/${i.asset_id}`}
          server={{ total: installations?.total ?? 0 }}
          onStateChange={(state) => setPage({ index: state.page, size: state.pageSize })}
          defaultPageSize={INSTALLATIONS_PAGE_SIZE}
          noFilters="One software's installations, paged on the server."
          exportName="ctem-software-installations"
          isLoading={isLoading && !data}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No asset runs this software.")}
          rowTestId="ctem-software-installation-row"
        />
      </AssetSection>

      <AssetSection
        id="software-depends-on"
        title={t_i18n("Depends on")}
        count={dependsOn.length}
        empty={!isLoading && dependsOn.length === 0}
        emptyLabel={t_i18n("No dependency reported. An SBOM with a dependencies block fills it.")}
      >
        <DataTable<SoftwareEdge>
          rows={dependsOn}
          columns={edgeColumns("from_version_id")}
          rowKey={(e) => `${e.from_version_id}|${e.version_id}|${e.kind}`}
          rowHref={(e) => softwarePath(e.software_id)}
          defaultSort={{ key: "software", direction: "asc" }}
          defaultPageSize={25}
          noFilters="The dependencies one SBOM reports, a short list."
          exportName="ctem-software-depends-on"
          isLoading={isLoading && !data}
          emptyLabel={t_i18n("No dependency reported. An SBOM with a dependencies block fills it.")}
          rowTestId="ctem-software-depends-row"
        />
      </AssetSection>

      <AssetSection
        id="software-used-by"
        title={t_i18n("Used by")}
        count={usedBy.length}
        empty={!isLoading && usedBy.length === 0}
        emptyLabel={t_i18n("No other software is reported to depend on it.")}
      >
        <DataTable<SoftwareEdge>
          rows={usedBy}
          columns={edgeColumns("version_id")}
          rowKey={(e) => `${e.from_version_id}|${e.version_id}|${e.kind}`}
          rowHref={(e) => softwarePath(e.software_id)}
          defaultSort={{ key: "software", direction: "asc" }}
          defaultPageSize={25}
          noFilters="The software one SBOM reports depending on it, a short list."
          exportName="ctem-software-used-by"
          isLoading={isLoading && !data}
          emptyLabel={t_i18n("No other software is reported to depend on it.")}
          rowTestId="ctem-software-used-by-row"
        />
      </AssetSection>

    </div>
  );
}
