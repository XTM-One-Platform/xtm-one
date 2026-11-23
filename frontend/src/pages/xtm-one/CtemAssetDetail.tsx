// SPDX-License-Identifier: Apache-2.0

/**
 * One asset of CTEM > Inventory `/ctem/inventory/assets/:id` (#1178, dev-docs/asset-model.md section 9): the
 * recap of everything known on one real thing. Its fields with the source that set each one, the source records
 * merged into it and why, and its software (the version `unknown` when no source knows it, the bundling
 * component when an SBOM gave it), the open cases on it and the crown jewels it supports. Administrators import an
 * SBOM (CycloneDX or SPDX JSON) and keep two of its records apart (split, audited).
 */
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { NotFoundState } from "@/components/ui/not-found-state";
import { useToast } from "@/components/ui/toast";
import { TruncatedText } from "@/components/ui/truncated-text";
import { type CtemCaseSummary, useCtemCasesForAsset } from "@/hooks/api/useCtemCasesApi";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import {
  useCanonicalAsset,
  useImportAssetSbom,
  useRecordAssetDecision,
  type AssetSoftware,
  type AssetSourceRecord,
} from "@/hooks/api/useOrganizationAssetsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { CasePriorityChip, CaseStateChip } from "./ctem/cases-shared";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { AssetOverviewCard, AssetSection } from "./ctem/AssetOverview";
import { assetRowIndexes } from "./ctem/inventory-entries";
import { InventoryEntryDialog, type EntryDialogMode } from "./ctem/InventoryEntryDialog";
import type { AssetRow } from "@/lib/organization-profile";
import { INVENTORY_PATH, isCodeRepository, softwarePath, sourceLabel } from "./ctem/inventory";
import { CategoryMark, SourceLogo } from "./ctem/inventory-icons";

/** The Software (or Dependencies) table opens at 25 rows, the platform's page size; the picker changes it. */
const SOFTWARE_PAGE_SIZE = 25;

/* The library glyph handed to NotFoundState (no direct lucide-react import, src/no-direct-lucide-import.test.ts). */
const AssetGlyph = ({ className }: { className?: string }) => <Icon name="server" size={16} className={className} />;

/** The cases still to work: a resolved one is history, read from the Cases page. */
const OPEN_STATES = new Set(["needs_attention", "in_progress", "mitigated_watching", "accepted_risk"]);

export default function CtemAssetDetail() {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  // Page and page size both reach the API: a size picked in the table is the request's limit.
  const [softwarePage, setSoftwarePage] = useState({ index: 0, size: SOFTWARE_PAGE_SIZE });
  const { data, isLoading, isError, refetch } = useCanonicalAsset(id, {
    offset: softwarePage.index * softwarePage.size,
    limit: softwarePage.size,
  });
  const cases = useCtemCasesForAsset(data?.name);
  const { data: context } = useOrganizationContext();
  const importSbom = useImportAssetSbom(id);
  const decide = useRecordAssetDecision();
  const fileInput = useRef<HTMLInputElement>(null);
  const [splitPicked, setSplitPicked] = useState<string[]>([]);
  const [confirmSplit, setConfirmSplit] = useState(false);
  // Update from the asset page (#1367, point 2): the profile rows behind its records, synced fields locked.
  const [entryDialog, setEntryDialog] = useState<EntryDialogMode | null>(null);
  const profileAssets = ((context?.profile as { assets_exposure?: AssetRow[] } | undefined)?.assets_exposure ?? []) as AssetRow[];
  const rowIndexes = data ? assetRowIndexes(profileAssets, data.records) : [];

  const openCases = (cases.data?.cases ?? []).filter((c) => OPEN_STATES.has(c.state));
  const nameKey = (data?.name ?? "").trim().toLowerCase();
  const jewels = nameKey
    ? (context?.profile.crown_jewels ?? []).filter((j) => (j.asset_names ?? []).some((n) => n.trim().toLowerCase() === nameKey))
    : [];

  const onSbom = (file: File | undefined) => {
    if (!file) return;
    importSbom.mutate(file, {
      onSuccess: (result) =>
        toast({
          title: t_i18n("{count, plural, one {# component imported} other {# components imported}}", { count: result.components }),
          variant: "success",
        }),
      onError: (error) => toast({ title: t_i18n("The SBOM was not imported"), description: error.message, variant: "error" }),
    });
  };
  const splitRefs = (data?.records ?? [])
    .filter((r) => splitPicked.includes(`${r.source}|${r.external_id}`))
    .map((r) => ({ source: r.source, external_id: r.external_id }));
  const onSplit = () => {
    decide.mutate(
      { kind: "split", records: splitRefs },
      {
        onSuccess: () => {
          setSplitPicked([]);
          toast({ title: t_i18n("The two records are kept apart"), variant: "success" });
          navigate(INVENTORY_PATH);
        },
        onError: (error) => toast({ title: t_i18n("The records were not split"), description: error.message, variant: "error" }),
      },
    );
  };

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

  const date = (value: string | null) => (value ? intl.formatDate(value, { dateStyle: "medium" }) : "");
  // A code repository's software is its dependencies (GitHub or GitLab SBOM, #1331): said as such, with the
  // package URL the vulnerability matching keys on.
  const isRepository = isCodeRepository(data);

  const softwareColumns: DataTableColumn<AssetSoftware>[] = [
    {
      key: "product",
      header: isRepository ? "Package" : "Software",
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
    ...(isRepository
      ? [
          {
            key: "purl",
            header: "Package URL",
            cell: (s: AssetSoftware) => <TruncatedText text={s.purl ?? ""} className="max-w-[16rem] font-mono 4xl:max-w-[22rem]" />,
            csv: (s: AssetSoftware) => s.purl,
            hideable: true,
          },
        ]
      : []),
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
      cell: (s) => (
        <span className="inline-flex items-center gap-1.5" title={s.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")}>
          {s.sources.map((source) => (
            <SourceLogo key={source} source={source} />
          ))}
        </span>
      ),
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
    {
      key: "source",
      header: "Source",
      cell: (r) => (
        <span className="inline-flex items-center gap-1.5">
          <SourceLogo source={r.source} />
          {sourceLabel(t_i18n, r.source)}
        </span>
      ),
      csv: (r) => r.source, sortValue: (r) => sourceLabel(t_i18n, r.source).toLowerCase(),
    },
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

  const records = data?.records ?? [];
  const software = data?.software;
  const softwareEmpty = !isLoading && (software?.total ?? 0) === 0;
  const sbomButton = isAdmin && (
    <>
      <input
        ref={fileInput}
        type="file"
        accept=".json,application/json"
        className="hidden"
        aria-label={t_i18n("SBOM file")}
        onChange={(event) => {
          onSbom(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
      <Button size="sm" variant="secondary" className="gap-1.5" disabled={!data || importSbom.isPending} onClick={() => fileInput.current?.click()}>
        <Icon name="upload" size={16} aria-hidden />
        {importSbom.isPending ? t_i18n("Importing...") : t_i18n("Import SBOM")}
      </Button>
    </>
  );

  return (
    <div className="space-y-4">
      {/* The app's standard entity header (#1367, Sam 23 Nov 2026): the back link, the category mark as the tile,
          the name with how it was matched behind the "i", criticality and the sources on the meta row. */}
      <EntityPageHeader
        backTo={INVENTORY_PATH}
        backLabel="Back to Inventory"
        icon={data ? <CategoryMark category={data.category} subcategory={data.subcategory} className="h-5 w-5" /> : undefined}
        title={data?.name ?? ""}
        hint={data?.explanation || undefined}
        hintLabel={t_i18n("How this asset was matched")}
        badges={
          data ? (
            <span className="inline-flex items-center gap-1.5" data-testid="ctem-asset-header-sources">
              {data.sources.map((source) => (
                <SourceLogo key={source} source={source} />
              ))}
              <span className="content-caption text-default-secondary">{data.sources.map((source) => sourceLabel(t_i18n, source)).join(", ")}</span>
            </span>
          ) : undefined
        }
        actions={
          isAdmin && rowIndexes.length > 0 ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setEntryDialog(rowIndexes.length === 1 ? { kind: "update", index: rowIndexes[0] } : { kind: "bulk", indexes: rowIndexes })}
              data-testid="ctem-asset-update"
            >
              {t_i18n("Update")}
            </Button>
          ) : undefined
        }
      />
      <InventoryEntryDialog section="assets_exposure" mode={entryDialog} onOpenChange={(o) => !o && setEntryDialog(null)} />

      {data && <AssetOverviewCard asset={data} />}

      <AssetSection
        id="asset-cases"
        title={t_i18n("Open cases")}
        count={openCases.length}
        empty={!cases.isLoading && openCases.length === 0 && !cases.isError}
        emptyLabel={t_i18n("No open case on this asset.")}
      >
        <DataTable<CtemCaseSummary>
          rows={openCases}
          columns={caseColumns}
          rowKey={(c) => c.id}
          rowHref={(c) => `/ctem/cases/${c.id}`}
          defaultPageSize={25}
          noFilters="The few cases on one asset."
          exportName="ctem-asset-cases"
          isLoading={cases.isLoading && !cases.data}
          isError={cases.isError}
          onRetry={() => void cases.refetch()}
          emptyLabel={t_i18n("No open case on this asset.")}
          rowTestId="ctem-asset-case-row"
        />
      </AssetSection>

      <AssetSection
        id="asset-jewels"
        title={t_i18n("Crown jewels it supports")}
        count={jewels.length}
        empty={jewels.length === 0}
        emptyLabel={t_i18n("No crown jewel names this asset.")}
      >
        <ul className="flex flex-wrap gap-2" data-testid="ctem-asset-jewels">
          {jewels.map((j) => (
            <li key={j.name}>
              <Link to="/settings/organization?card=crown_jewels" className="inline-flex items-center gap-1.5 text-sm underline-offset-2 hover:underline">
                <Icon name="gem" size={16} className="text-default-secondary" aria-hidden />
                {j.name}
              </Link>
            </li>
          ))}
        </ul>
      </AssetSection>

      <AssetSection
        id="asset-software"
        title={isRepository ? t_i18n("Dependencies") : t_i18n("Software")}
        count={software?.total}
        actions={sbomButton}
        empty={softwareEmpty}
        emptyLabel={isRepository ? t_i18n("No dependency known in this repository yet.") : t_i18n("No software known on this asset yet.")}
      >
        <DataTable<AssetSoftware>
          rows={software?.items ?? []}
          columns={softwareColumns}
          rowKey={(s) => `${s.vendor}|${s.product}|${s.version}`}
          // Each row opens its software's page (#1495 slice 2); a row no rebuild has linked yet opens nothing.
          rowHref={(s) => (s.software_id ? softwarePath(s.software_id) : undefined)}
          server={{ total: software?.total ?? 0 }}
          onStateChange={(state) => setSoftwarePage({ index: state.page, size: state.pageSize })}
          defaultPageSize={SOFTWARE_PAGE_SIZE}
          noFilters="One asset's software, paged on the server."
          exportName="ctem-asset-software"
          isLoading={isLoading && !data}
          onRetry={() => void refetch()}
          emptyLabel={isRepository ? t_i18n("No dependency known in this repository yet.") : t_i18n("No software known on this asset yet.")}
          rowTestId="ctem-asset-software-row"
        />
      </AssetSection>

      <AssetSection
        id="asset-sources"
        title={t_i18n("Sources")}
        count={records.length}
        empty={!isLoading && records.length === 0}
        emptyLabel={t_i18n("No source record.")}
      >
        <DataTable<AssetSourceRecord>
          rows={records}
          columns={recordColumns}
          rowKey={(r) => `${r.source}|${r.external_id}`}
          defaultSort={{ key: "source", direction: "asc" }}
          defaultPageSize={25}
          noFilters="The few records merged into one asset."
          exportName="ctem-asset-sources"
          isLoading={isLoading && !data}
          emptyLabel={t_i18n("No source record.")}
          rowTestId="ctem-asset-record-row"
          selection={
            isAdmin && records.length > 1
              ? {
                  selected: splitPicked,
                  onChange: setSplitPicked,
                  actions: (
                    <Button size="sm" variant="secondary" disabled={splitRefs.length !== 2 || decide.isPending} onClick={() => setConfirmSplit(true)}>
                      {t_i18n("Keep apart")}
                    </Button>
                  ),
                }
              : undefined
          }
        />
      </AssetSection>
      <ConfirmDialog
        open={confirmSplit}
        onOpenChange={setConfirmSplit}
        title={t_i18n("Keep these two records apart?")}
        description={t_i18n("They become two assets, whatever their identifiers say. The split is audited and lasts across syncs.")}
        confirmLabel={t_i18n("Keep apart")}
        onConfirm={onSplit}
      />
    </div>
  );
}
