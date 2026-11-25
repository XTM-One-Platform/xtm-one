// SPDX-License-Identifier: Apache-2.0

/**
 * One primary asset of CTEM > Inventory `/ctem/inventory/primary-assets/:name` (#1980 S2): what the business asset is
 * (criticality, owner, business service, source), then every inventory asset that supports it, linked directly or as
 * a member of a linked asset group, read from the server one page at a time and searched there (263 laptops on one
 * primary asset is the norm), with how it reaches it, its roles and the security needs it inherits; then the open
 * cases one of whose issues reaches it. Opened from the Business tab and from the case page, by name, never by id.
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Card } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import { type CtemCaseSummary, useCtemCasesForPrimaryAsset } from "@/hooks/api/useCtemCasesApi";
import { usePrimaryAsset, type PrimaryAssetSupporter } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { AssetSection, Fact } from "./ctem/AssetOverview";
import { CasePriorityChip, CaseStateChip } from "./ctem/cases-shared";
import { INVENTORY_PATH, criticalityShortLabel, sourceLabel } from "./ctem/inventory";
import { SourceLogo } from "./ctem/inventory-icons";

/** The supporting asset table opens at 25 rows, the platform's page size; the picker changes it. */
const SUPPORTERS_PAGE_SIZE = 25;
const BUSINESS_TAB = `${INVENTORY_PATH}/business`;

/** The cases still to work: a resolved one is history, read from the Cases page. */
const OPEN_STATES = new Set(["new", "analyzing", "waiting_decision", "remediating", "validating", "mitigated_watching", "accepted_risk"]);

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const GemGlyph = ({ className }: { className?: string }) => <Icon name="gem" size={16} className={className} />;

export default function CtemPrimaryAssetDetail() {
  const { t_i18n, intl } = useFormatter();
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  // Page, page size and search all reach the API: the supporting assets are paged and searched on the server.
  const [page, setPage] = useState({ index: 0, size: SUPPORTERS_PAGE_SIZE, search: "" });
  const read = usePrimaryAsset(name, { q: page.search, offset: page.index * page.size, limit: page.size });
  const cases = useCtemCasesForPrimaryAsset(read.data?.primary_asset.name);
  const entry = read.data?.primary_asset;
  const supporters = read.data?.supporters;

  if (read.isError && !read.data) {
    return (
      <NotFoundState
        icon={GemGlyph}
        title="Primary asset not found"
        description="No primary asset of the organization has this name: it may have been renamed or removed."
        actionLabel="Back to Inventory"
        onAction={() => navigate(BUSINESS_TAB)}
        backTo={BUSINESS_TAB}
        backLabel="Back to Inventory"
      />
    );
  }

  const counts = supporters?.counts;
  const facts = entry
    ? [
        <Fact key="criticality" icon="gem" label={t_i18n("Criticality")} values={[criticalityShortLabel(t_i18n, entry.criticality || "unknown")]} />,
        ...(entry.owner ? [<Fact key="owner" icon="user" label={t_i18n("Owner")} values={[entry.owner]} />] : []),
        ...(entry.business_service ? [<Fact key="service" icon="layers" label={t_i18n("Business service")} values={[entry.business_service]} />] : []),
        <Fact key="source" mark={<SourceLogo source={entry.source || "manual"} />} label={t_i18n("Source")} values={[sourceLabel(t_i18n, entry.source || "manual")]} />,
        ...(counts
          ? [
              <Fact
                key="reach"
                icon="server"
                label={t_i18n("Supporting assets")}
                values={[
                  t_i18n("{count, plural, =0 {None linked yet} one {# linked directly} other {# linked directly}}", { count: counts.through.asset ?? 0 }),
                  t_i18n("{count, plural, =0 {none through an asset group} one {# through an asset group} other {# through asset groups}}", {
                    count: counts.through.asset_group ?? 0,
                  }),
                ]}
              />,
            ]
          : []),
      ]
    : [];

  const reachLabel = (s: PrimaryAssetSupporter) =>
    s.through_kind === "asset_group" ? t_i18n("Through the asset group {group}", { group: s.through }) : t_i18n("Linked directly");

  const columns: DataTableColumn<PrimaryAssetSupporter>[] = [
    {
      key: "name",
      header: "Asset",
      cell: (s) => <TruncatedText text={s.name} className="max-w-[16rem] font-medium" />,
      csv: (s) => s.name,
      // The store answers by name: the one server sort.
      sortValue: (s) => s.name.toLowerCase(),
    },
    {
      key: "through",
      header: "Reaches it",
      cell: (s) => <TruncatedText text={reachLabel(s)} className="max-w-[18rem]" />,
      csv: (s) => (s.through_kind === "asset_group" ? `asset_group:${s.through}` : "asset"),
    },
    {
      key: "roles",
      header: "Roles",
      cell: (s) => <TruncatedText text={s.roles.join(", ")} className="max-w-[12rem]" />,
      csv: (s) => s.roles.join("; "),
      hideable: true,
    },
    {
      key: "needs",
      header: "Inherited security needs",
      cell: (s) => (
        <TruncatedText
          text={Object.entries(s.inherited_needs)
            .map(([need, level]) => `${need}: ${level}`)
            .join(", ")}
          className="max-w-[14rem]"
        />
      ),
      csv: (s) =>
        Object.entries(s.inherited_needs)
          .map(([need, level]) => `${need}=${level}`)
          .join("; "),
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "criticality",
      header: "Criticality",
      cell: (s) => <span className="whitespace-nowrap">{criticalityShortLabel(t_i18n, s.criticality)}</span>,
      csv: (s) => s.criticality,
      hideable: true,
    },
    {
      key: "source",
      header: "Link source",
      cell: (s) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <SourceLogo source={s.source} />
          <TruncatedText text={sourceLabel(t_i18n, s.source)} className="max-w-[8rem]" />
        </span>
      ),
      csv: (s) => s.source,
      hideable: true,
    },
  ];

  const openCases = (cases.data?.cases ?? []).filter((c) => OPEN_STATES.has(c.state));
  const date = (value: string | null) => (value ? intl.formatDate(value, { dateStyle: "medium" }) : "");
  const caseColumns: DataTableColumn<CtemCaseSummary>[] = [
    {
      key: "title",
      header: "Case",
      cell: (c) => <TruncatedText text={c.title} className="max-w-[24rem] font-medium" />,
      csv: (c) => c.title,
      sortValue: (c) => c.title.toLowerCase(),
    },
    { key: "state", header: "State", cell: (c) => <CaseStateChip state={c.state} />, csv: (c) => c.state, sortValue: (c) => c.state },
    { key: "priority", header: "Priority", cell: (c) => <CasePriorityChip priority={c.priority} />, csv: (c) => c.priority },
    { key: "due", header: "Due", cell: (c) => date(c.due_at), csv: (c) => c.due_at, sortValue: (c) => c.due_at, hideable: true },
  ];

  return (
    <div className="space-y-4">
      <EntityPageHeader
        backTo={BUSINESS_TAB}
        backLabel="Back to Inventory"
        icon={<Icon name="gem" size={20} aria-hidden />}
        title={entry?.name ?? ""}
        hint={entry?.description || undefined}
        hintLabel={t_i18n("About this primary asset")}
      />

      {entry && (
        <Card className="p-4" data-testid="ctem-primary-asset-overview">
          <div className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2 xl:grid-cols-4">{facts}</div>
        </Card>
      )}

      <AssetSection
        id="primary-asset-supporters"
        title={t_i18n("Supporting assets")}
        count={counts?.total}
        empty={!read.isLoading && (counts?.total ?? 0) === 0}
        emptyLabel={t_i18n("No asset of the inventory supports this primary asset yet.")}
      >
        <DataTable<PrimaryAssetSupporter>
          rows={supporters?.items ?? []}
          columns={columns}
          rowKey={(s) => `${s.record_id}|${s.through_kind}|${s.through}`}
          rowHref={(s) => (s.asset_id ? `${INVENTORY_PATH}/assets/${s.asset_id}` : undefined)}
          server={{ total: supporters?.total ?? 0 }}
          onStateChange={(state) => setPage({ index: state.page, size: state.pageSize, search: state.search })}
          defaultPageSize={SUPPORTERS_PAGE_SIZE}
          searchPlaceholder={t_i18n("Search assets or asset groups by name")}
          noFilters="One primary asset's supporting assets, searched and paged on the server."
          exportName="ctem-primary-asset-supporters"
          isLoading={read.isLoading && !read.data}
          isError={read.isError}
          onRetry={() => void read.refetch()}
          emptyLabel={t_i18n("No asset of the inventory supports this primary asset yet.")}
          noResultsLabel={t_i18n("No asset matches this search")}
          rowTestId="ctem-primary-asset-supporter-row"
        />
      </AssetSection>

      <AssetSection
        id="primary-asset-cases"
        title={t_i18n("Open cases")}
        count={openCases.length}
        empty={!cases.isLoading && openCases.length === 0 && !cases.isError}
        emptyLabel={t_i18n("No open case reaches this primary asset.")}
      >
        <DataTable<CtemCaseSummary>
          rows={openCases}
          columns={caseColumns}
          rowKey={(c) => c.id}
          rowHref={(c) => `/ctem/cases/${c.id}`}
          defaultPageSize={25}
          noFilters="The cases on one primary asset."
          exportName="ctem-primary-asset-cases"
          isLoading={cases.isLoading && !cases.data}
          isError={cases.isError}
          onRetry={() => void cases.refetch()}
          emptyLabel={t_i18n("No open case reaches this primary asset.")}
          rowTestId="ctem-primary-asset-case-row"
        />
      </AssetSection>
    </div>
  );
}
