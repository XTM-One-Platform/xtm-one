// SPDX-License-Identifier: Apache-2.0

/**
 * One asset group of CTEM > Inventory `/ctem/inventory/asset-groups/:id` (#1556): what the group is (its fields,
 * its source and when its source last answered its members), then its member assets as the asset table, read from
 * the server one page at a time (`GET /assets?group=<id>`), each row opening its asset. The members its source named
 * that the inventory does not hold yet are counted, never dropped silently.
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAssetGroup, useCanonicalAssets, type CanonicalAssetSummary } from "@/hooks/api/useOrganizationAssetsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { AssetSection, Fact } from "./ctem/AssetOverview";
import { InventoryEntryDialog, type EntryDialogMode } from "./ctem/InventoryEntryDialog";
import { INVENTORY_PATH, assetCategory, assetCategoryLabel, assetFieldLabel, criticalityShortLabel, sourceLabel } from "./ctem/inventory";
import { CategoryMark, SourceLogo } from "./ctem/inventory-icons";

/** The member table opens at 25 rows, the platform's page size; the picker changes it. */
const MEMBERS_PAGE_SIZE = 25;
const GROUPS_TAB = `${INVENTORY_PATH}/asset-groups`;

/** The exposure of a group, as the Asset groups tab words it. @i18n-keys */
const EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const GroupGlyph = ({ className }: { className?: string }) => <Icon name="custom/asset-groups" size={16} className={className} />;

export default function CtemAssetGroupDetail() {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const group = useAssetGroup(id);
  // Page, page size and search all reach the API: the members are paged and searched on the server.
  const [members, setMembers] = useState({ index: 0, size: MEMBERS_PAGE_SIZE, search: "" });
  const page = useCanonicalAssets({
    q: members.search,
    offset: members.index * members.size,
    limit: members.size,
    filters: { group: id ? [id] : [] },
  });
  const [dialog, setDialog] = useState<EntryDialogMode | null>(null);
  const data = group.data;

  if (group.isError && !data) {
    return (
      <NotFoundState
        icon={GroupGlyph}
        title="Asset group not found"
        description="This asset group does not exist any more: its source may have removed it, or it was deleted."
        actionLabel="Back to Inventory"
        onAction={() => navigate(GROUPS_TAB)}
        backTo={GROUPS_TAB}
        backLabel="Back to Inventory"
      />
    );
  }

  const unresolved = data?.unresolved_members ?? 0;
  const facts = data
    ? [
        <Fact key="source" mark={<SourceLogo source={data.source || "manual"} />} label={t_i18n("Source")} values={[sourceLabel(t_i18n, data.source || "manual")]} />,
        ...(data.members_synced_at
          ? [
              <Fact
                key="synced"
                icon="clock"
                label={t_i18n("Members last synced")}
                values={[intl.formatDate(data.members_synced_at, { dateStyle: "medium", timeStyle: "short" })]}
              />,
            ]
          : []),
        ...(data.environment ? [<Fact key="environment" icon="layers" label={t_i18n("Environment")} values={[assetFieldLabel(t_i18n, "environment", data.environment)]} />] : []),
        <Fact key="exposure" icon="earth" label={t_i18n("Exposure")} values={[t_i18n((EXPOSURE_LABELS as Record<string, string>)[data.internet_facing] ?? data.internet_facing)]} />,
        ...(data.owner ? [<Fact key="owner" icon="user" label={t_i18n("Owner")} values={[data.owner]} />] : []),
        ...(data.tech_stack.length ? [<Fact key="tech" icon="layers" label={t_i18n("Software")} values={[data.tech_stack.join(", ")]} />] : []),
      ]
    : [];

  const columns: DataTableColumn<CanonicalAssetSummary>[] = [
    {
      key: "name",
      header: "Asset",
      cell: (a) => <TruncatedText text={a.name} className="max-w-[16rem] font-medium" />,
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
      cell: (a) => <span className="whitespace-nowrap">{criticalityShortLabel(t_i18n, a.criticality)}</span>,
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
      key: "sources",
      header: "Sources",
      cell: (a) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {a.sources.map((source) => (
            <SourceLogo key={source} source={source} />
          ))}
          <TruncatedText text={a.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")} className="max-w-[10rem]" />
        </span>
      ),
      csv: (a) => a.sources.join(" "),
      hideable: true,
    },
  ];

  return (
    <div className="space-y-4">
      <EntityPageHeader
        backTo={GROUPS_TAB}
        backLabel="Back to Inventory"
        icon={<Icon name="custom/asset-groups" size={20} aria-hidden />}
        title={data?.name ?? ""}
        hint={data?.description || undefined}
        hintLabel={t_i18n("About this asset group")}
        actions={
          isAdmin && data ? (
            <Button variant="secondary" size="sm" onClick={() => setDialog({ kind: "update", id: data.id })} data-testid="ctem-asset-group-update">
              {t_i18n("Update")}
            </Button>
          ) : undefined
        }
      />
      <InventoryEntryDialog section="asset_groups" mode={dialog} onOpenChange={(o) => !o && setDialog(null)} />

      {data && (
        <Card className="p-4" data-testid="ctem-asset-group-overview">
          <div className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2 xl:grid-cols-4">{facts}</div>
        </Card>
      )}

      <AssetSection
        id="asset-group-members"
        title={t_i18n("Assets")}
        count={data?.member_count}
        actions={
          unresolved > 0 ? (
            <span className="content-caption text-default-secondary" data-testid="ctem-asset-group-unresolved">
              {t_i18n(
                "{count, plural, one {# more member its source names is not in the inventory yet} other {# more members its source names are not in the inventory yet}}",
                { count: unresolved },
              )}
            </span>
          ) : undefined
        }
        empty={!group.isLoading && !page.isLoading && (data?.member_count ?? 0) === 0}
        emptyLabel={t_i18n("No asset of the inventory is in this group yet.")}
      >
        <DataTable<CanonicalAssetSummary>
          rows={page.data?.items ?? []}
          columns={columns}
          rowKey={(a) => a.id}
          rowHref={(a) => `${INVENTORY_PATH}/assets/${a.id}`}
          server={{ total: page.data?.total ?? 0 }}
          onStateChange={(state) => setMembers({ index: state.page, size: state.pageSize, search: state.search })}
          defaultPageSize={MEMBERS_PAGE_SIZE}
          searchPlaceholder={t_i18n("Search assets by name")}
          noFilters="One group's assets, searched and paged on the server; the Assets list filters by group."
          exportName="ctem-asset-group-assets"
          isLoading={page.isLoading && !page.data}
          isError={page.isError}
          onRetry={() => void page.refetch()}
          emptyLabel={t_i18n("No asset of the inventory is in this group yet.")}
          noResultsLabel={t_i18n("No asset matches this search")}
          rowTestId="ctem-asset-group-member-row"
        />
      </AssetSection>
    </div>
  );
}
