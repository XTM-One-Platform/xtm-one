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
import { DataTable, type DataTableColumn, type DataTableFilter, type DataTableState } from "@/components/ui/data-table";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import { type CtemCaseSummary, useCtemCasesForPrimaryAsset } from "@/hooks/api/useCtemCasesApi";
import { usePrimaryAsset, type PrimaryAssetSupporter } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";
import { AssetSection, Fact } from "./ctem/AssetOverview";
import { CasePriorityChip, CaseStateChip } from "./ctem/cases-shared";
import { INVENTORY_PATH, criticalityShortLabel, sourceLabel } from "./ctem/inventory";
import { SourceLogo } from "./ctem/inventory-icons";
import { linkRoleLabel } from "./ctem/loop/decision-primary-assets";

/** The supporting asset table opens at 25 rows, the platform's page size; the picker changes it. */
const SUPPORTERS_PAGE_SIZE = 25;
const BUSINESS_TAB = `${INVENTORY_PATH}/business`;

/** The cases still to work: a resolved one is history, read from the Cases page. */
const OPEN_STATES = new Set(["new", "analyzing", "waiting_decision", "remediating", "validating", "mitigated_watching", "accepted_risk"]);

/**
 * The four security needs a supporting asset inherits from the primary asset (OpenCRQ `SECURITY_NEED_CRITERIA`,
 * `SECURITY_NEEDS` of backend/app/services/organization_context_opencrq_links.py). @i18n-keys
 */
const NEED_LABELS = {
  availability: "Availability",
  integrity: "Integrity",
  confidentiality: "Confidentiality",
  traceability: "Traceability",
} as const;

/** How much of a need the link passes on (`_INHERITANCE_ORDER`: none, lower, full). @i18n-keys */
const INHERITANCE_LABELS = {
  none: "Not inherited",
  lower: "Inherited lower",
  full: "Inherited in full",
} as const;

/** How a supporting asset reaches the primary asset, the values of the Reaches it filter. @i18n-keys */
const THROUGH_LABELS = {
  asset: "Linked directly",
  asset_group: "Through an asset group",
} as const;

/** The table's server sorts (`SUPPORTERS_SORTS` of backend/app/services/ctem_primary_assets.py). */
const SERVER_SORTS = new Set(["name", "criticality"]);

const pick = (table: Readonly<Record<string, string>>, value: string): string | undefined => table[value.trim().toLowerCase()];

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const GemGlyph = ({ className }: { className?: string }) => <Icon name="gem" size={16} className={className} />;

export default function CtemPrimaryAssetDetail() {
  const { t_i18n, intl } = useFormatter();
  // The primary asset scale (low to critical) of the Business tab; any other value reads on the asset scale.
  const primaryCriticality = (value: string | null | undefined) => {
    const label = pick(CRITICALITY_LABELS, value ?? "");
    return label ? t_i18n(label) : criticalityShortLabel(t_i18n, value || "unknown");
  };
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  // Page, page size, search, filters and sort all reach the API: the supporting assets are read on the server.
  const [page, setPage] = useState({ index: 0, size: SUPPORTERS_PAGE_SIZE, search: "", through: "", source: "", sort: "name", direction: "asc" });
  const read = usePrimaryAsset(name, {
    q: page.search,
    offset: page.index * page.size,
    limit: page.size,
    through: page.through,
    source: page.source,
    sort: page.sort,
    direction: page.direction,
  });
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
        <Fact key="criticality" icon="gem" label={t_i18n("Criticality")} values={[primaryCriticality(entry.criticality)]} />,
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
  const linkSource = (source: string) => (source === "rule" ? t_i18n("Rule") : sourceLabel(t_i18n, source));
  const rolesText = (s: PrimaryAssetSupporter) => [...new Set(s.roles.filter(Boolean).map((role) => linkRoleLabel(role, t_i18n)))].join(", ");
  const needsText = (s: PrimaryAssetSupporter) =>
    Object.entries(s.inherited_needs)
      .map(([need, level]) => {
        const needLabel = pick(NEED_LABELS, need);
        const levelLabel = pick(INHERITANCE_LABELS, level);
        return needLabel && levelLabel ? t_i18n("{need}: {level}", { need: t_i18n(needLabel), level: t_i18n(levelLabel) }) : null;
      })
      .filter((text): text is string => Boolean(text))
      .join(", ");
  // A blank cell reads "None", muted, as the other CTEM tables write an absent value.
  const orNone = (text: string, max: string) =>
    text ? <TruncatedText text={text} className={max} /> : <span className="text-default-secondary">{t_i18n("None")}</span>;

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
      cell: (s) => <TruncatedText text={reachLabel(s)} className="max-w-[32rem]" />,
      csv: (s) => (s.through_kind === "asset_group" ? `asset_group:${s.through}` : "asset"),
    },
    {
      key: "roles",
      header: "Roles",
      cell: (s) => orNone(rolesText(s), "max-w-[12rem]"),
      csv: (s) => s.roles.join("; "),
      hideable: true,
    },
    {
      key: "needs",
      header: "Inherited security needs",
      cell: (s) => orNone(needsText(s), "max-w-[14rem]"),
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
      // Sorted on the server, on the criticality scale (very high first when descending).
      sortValue: (s) => s.criticality,
      firstSort: "desc",
      hideable: true,
    },
    {
      key: "source",
      header: "Link source",
      cell: (s) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <SourceLogo source={s.source} />
          <TruncatedText text={linkSource(s.source)} className="max-w-[8rem]" />
        </span>
      ),
      csv: (s) => s.source,
      hideable: true,
    },
  ];

  // Server filters: the values come from the whole primary asset's counts, so a pill never offers an empty choice.
  const filters: DataTableFilter<PrimaryAssetSupporter>[] = [
    {
      key: "through",
      label: "Reaches it",
      options: Object.keys(counts?.through ?? {}).map((value) => ({ value, label: t_i18n(pick(THROUGH_LABELS, value) ?? THROUGH_LABELS.asset) })),
    },
    {
      key: "source",
      label: "Link source",
      options: Object.keys(counts?.source ?? {}).map((value) => ({ value, label: linkSource(value) })),
    },
  ];
  const onTableState = (state: DataTableState) => {
    const sort = state.sort && SERVER_SORTS.has(state.sort.key) ? state.sort : null;
    setPage({
      index: state.page,
      size: state.pageSize,
      search: state.search,
      through: (state.filters.through ?? []).join(","),
      source: (state.filters.source ?? []).join(","),
      sort: sort?.key ?? "name",
      direction: sort?.direction ?? "asc",
    });
  };

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
          onStateChange={onTableState}
          filters={filters}
          defaultPageSize={SUPPORTERS_PAGE_SIZE}
          searchPlaceholder={t_i18n("Search assets or asset groups by name")}
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
