// SPDX-License-Identifier: Apache-2.0

/**
 * The primary assets a signal reached, one card each (loop trace v2, #1979): the
 * name, the criticality the gate used, the roles, the real count of inventory
 * assets reaching it (direct and through an asset group) and the first chains the
 * decision record kept. "Show all supporting assets" opens a searched, paged table
 * read live from GET /ctem-signals/{id}/primary-assets/{key}/supporting-assets, so
 * 10,000 workstations read 10,000 with every one of them listed, never five
 * lines. A record older than #1979 has no total: the card shows its chains and
 * the table still lists the live answer.
 */
import { useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { CompactFigure } from "@/components/ui/compact-figure";
import { exactCount, formatCount } from "@/lib/number-format";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { useCtemSupportingAssets, type CtemDecisionRecord, type CtemSupportingAsset } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { levelLabel } from "./DecisionReason";
import { CHAINS_PER_PRIMARY_ASSET, chainText, linkRoleLabel, primaryAssetsReached, type PrimaryAssetReached } from "./decision-primary-assets";

const SUPPORTING_PAGE_SIZE = 10;

function SupportingAssetsTable({ signalId, primary }: { signalId: string; primary: PrimaryAssetReached }) {
  const { t_i18n } = useFormatter();
  const [page, setPage] = useState({ index: 0, size: SUPPORTING_PAGE_SIZE, search: "" });
  const query = useCtemSupportingAssets(signalId, primary.key, { search: page.search, offset: page.index * page.size, limit: page.size });
  const columns: DataTableColumn<CtemSupportingAsset>[] = [
    { key: "asset", header: t_i18n("Asset"), cell: (row) => row.asset, sortValue: (row) => row.asset },
    {
      key: "through",
      header: t_i18n("Through"),
      cell: (row) => (row.through_kind === "asset_group" ? t_i18n("group {group}", { group: row.through }) : t_i18n("Direct link")),
    },
    { key: "software", header: t_i18n("Software"), cell: (row) => row.software || "--", hideable: true },
    { key: "roles", header: t_i18n("Roles"), hideable: true, cell: (row) => row.roles.map((role) => linkRoleLabel(role, t_i18n)).join(", ") || "--" },
  ];
  return (
    <div data-testid="ctem-primary-asset-supporting">
      <DataTable
        rows={query.data?.assets ?? []}
        columns={columns}
        rowKey={(row) => `${row.asset}|${row.through}`}
        server={{ total: query.data?.total ?? 0 }}
        onStateChange={(state) => setPage({ index: state.page, size: state.pageSize, search: state.search })}
        defaultPageSize={SUPPORTING_PAGE_SIZE}
        searchPlaceholder={t_i18n("Search supporting assets")}
        noFilters="One primary asset's supporting assets for one signal, searched and paged on the server."
        isLoading={query.isLoading}
        isError={query.isError}
        emptyLabel={t_i18n("No supporting asset")}
      />
    </div>
  );
}

function PrimaryAssetCard({ signalId, primary }: { signalId: string; primary: PrimaryAssetReached }) {
  const { t_i18n, intl } = useFormatter();
  const [open, setOpen] = useState(false);
  const roles = [...new Set(primary.chains.flatMap((hit) => hit.roles ?? []).filter(Boolean).map((role) => linkRoleLabel(role, t_i18n)))];
  const shown = primary.chains.slice(0, CHAINS_PER_PRIMARY_ASSET);
  const total = primary.assetsTotal;
  const moreChains = Math.max(primary.chains.length, primary.chainTotal ?? 0) - shown.length;
  return (
    <li className="flex min-w-0 flex-col gap-2 rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-3" data-testid="ctem-primary-asset-card" data-key={primary.key}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <Icon name="shield-check" size={16} className="shrink-0 text-accent" aria-hidden />
        <span className="min-w-0 flex-1 truncate content-compact font-medium">{primary.name}</span>
        {primary.criticality && (
          <span className="content-caption text-default-secondary">
            {t_i18n("Criticality")}: <span className="text-default-primary">{levelLabel(primary.criticality, t_i18n)}</span>
          </span>
        )}
      </div>
      <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-1 content-caption text-default-secondary">
        {total !== null && (
          <span data-testid="ctem-primary-asset-total">
            <CompactFigure className="title-xs text-default-primary" exact={exactCount(intl.locale, total)}>
              {formatCount(intl.locale, total)}
            </CompactFigure>{" "}
            {t_i18n("{count, plural, one {supporting asset} other {supporting assets}}", { count: total })}
          </span>
        )}
        {primary.direct !== null && <span>{t_i18n("{count, plural, one {# direct link} other {# direct links}}", { count: primary.direct })}</span>}
        {primary.viaGroup !== null && <span>{t_i18n("{count, plural, one {# through an asset group} other {# through asset groups}}", { count: primary.viaGroup })}</span>}
        {roles.length > 0 && <span>{t_i18n("Roles")}: {roles.join(", ")}</span>}
      </div>
      <ul className="flex flex-col content-caption break-words" data-testid="ctem-primary-asset-chains">
        {shown.map((hit, index) => (
          <li key={`${index}-${hit.asset}-${hit.through}`}>{chainText(primary.name, hit, t_i18n)}</li>
        ))}
        {moreChains > 0 && <li className="text-default-secondary">{t_i18n("+{count} more", { count: moreChains })}</li>}
      </ul>
      <Button variant="link" size="sm" className="h-auto self-start gap-1 p-0" onClick={() => setOpen((v) => !v)} aria-expanded={open} data-testid="ctem-primary-asset-toggle">
        <Icon name={open ? "chevron-up" : "chevron-down"} size={16} aria-hidden />
        {open ? t_i18n("Hide supporting assets") : t_i18n("Show all supporting assets")}
      </Button>
      {open && <SupportingAssetsTable signalId={signalId} primary={primary} />}
    </li>
  );
}

/** The cards, or the one line saying none was reached once the links were read; nothing when the record says nothing. */
export function PrimaryAssetCards({ signalId, decision }: { signalId: string; decision: CtemDecisionRecord | null | undefined }) {
  const { t_i18n } = useFormatter();
  const reached = primaryAssetsReached(decision);
  if (reached.length === 0) {
    if (decision?.reconcile?.inventory_links_read === true)
      return <p className="content-caption text-default-secondary" data-testid="ctem-primary-assets-none">{t_i18n("None: no inventory link reaches a primary asset")}</p>;
    return null;
  }
  return (
    <ul className="flex min-w-0 flex-col gap-2" aria-label={t_i18n("Primary assets reached")} data-testid="ctem-primary-asset-cards">
      {reached.map((primary) => (
        <PrimaryAssetCard key={primary.key} signalId={signalId} primary={primary} />
      ))}
    </ul>
  );
}
