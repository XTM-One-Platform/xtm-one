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
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { INVENTORY_LABEL, INVENTORY_PATH, assetCategoryLabel, assetFieldLabel, sourceLabel } from "./ctem/inventory";

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

/** The cases still to work: a resolved one is history, read from the Cases page. */
const OPEN_STATES = new Set(["needs_attention", "in_progress", "mitigated_watching", "accepted_risk"]);

export default function CtemAssetDetail() {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [softwarePage, setSoftwarePage] = useState(0);
  const { data, isLoading, isError, refetch } = useCanonicalAsset(id, {
    offset: softwarePage * SOFTWARE_PAGE,
    limit: SOFTWARE_PAGE,
  });
  const cases = useCtemCasesForAsset(data?.name);
  const { data: context } = useOrganizationContext();
  const importSbom = useImportAssetSbom(id);
  const decide = useRecordAssetDecision();
  const fileInput = useRef<HTMLInputElement>(null);
  const [splitPicked, setSplitPicked] = useState<string[]>([]);
  const [confirmSplit, setConfirmSplit] = useState(false);

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
      <CtemPageHeader
        back={{ to: INVENTORY_PATH, label: t_i18n(INVENTORY_LABEL) }}
        title={data?.name ?? ""}
        hint={data?.explanation || undefined}
        hintLabel={t_i18n("How this asset was matched")}
        stats={HEADER_FIELDS.map((field) => ({
          id: field,
          label: t_i18n(HEADER_LABELS[field]),
          value:
            (field === "category" && data
              ? assetCategoryLabel(t_i18n, data)
              : assetFieldLabel(t_i18n, field, String(data?.fields?.[field]?.value ?? ""))) || "-",
          title: origin(field),
        }))}
      />

      <section aria-labelledby="asset-cases" className="space-y-2">
        <h2 id="asset-cases" className="text-base font-semibold">{t_i18n("Open cases")}</h2>
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
      </section>

      <section aria-labelledby="asset-jewels" className="space-y-2">
        <h2 id="asset-jewels" className="text-base font-semibold">{t_i18n("Crown jewels it supports")}</h2>
        {jewels.length ? (
          <ul className="flex flex-wrap gap-2" data-testid="ctem-asset-jewels">
            {jewels.map((j) => (
              <li key={j.name}>
                <Link to="/settings/organization?card=crown_jewels" className="text-sm underline-offset-2 hover:underline">
                  {j.name}
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-default-secondary">{t_i18n("No crown jewel names this asset.")}</p>
        )}
      </section>

      <section aria-labelledby="asset-software" className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 id="asset-software" className="text-base font-semibold">{t_i18n("Software")}</h2>
          {isAdmin && (
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
          )}
        </div>
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
          selection={
            isAdmin && (data?.records.length ?? 0) > 1
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
      </section>
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
