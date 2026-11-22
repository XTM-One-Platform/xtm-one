// SPDX-License-Identifier: Apache-2.0

/**
 * The Asset groups and AI systems tabs of CTEM > Inventory (#1178, Sam 22 Nov 2026: four tabs Assets, Asset groups,
 * Tech stack, AI systems). Both lists are sections of the organization profile, read from the same API the agents
 * read; nothing is stored twice. The lists are short (the profile caps them), so search, filters and export run in
 * the page.
 */
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import type { AssetGroupRow } from "@/lib/organization-profile";
import { assetLabel, sourceLabel } from "./inventory";

/** The exposure of an asset group, as the Settings table words it. @i18n-keys */
const GROUP_EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;
/** @i18n-keys */
const AI_KIND_LABELS = { assistant: "Assistant", agent: "Agent", model_api: "Model API", embedded: "Embedded" } as const;
/** @i18n-keys */
const AI_EXPOSURE_LABELS = { internal: "Internal", partner: "Partner", public: "Public" } as const;

type AiSystemRow = { name: string; kind: string; owner: string; exposure: string; openaev_asset_id: string; source?: string };

const label = (labels: Record<string, string>, value: string) => labels[value] ?? assetLabel(value);

export function AssetGroupsTab() {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError, refetch } = useOrganizationContext();
  const groups = ((data?.profile as { asset_groups?: AssetGroupRow[] } | undefined)?.asset_groups ?? []).filter((g) => g.name.trim());

  const columns: DataTableColumn<AssetGroupRow>[] = [
    {
      key: "name",
      header: "Asset group",
      cell: (g) => <TruncatedText text={g.name} className="max-w-[18rem] font-medium" />,
      csv: (g) => g.name,
      sortValue: (g) => g.name.toLowerCase(),
    },
    {
      key: "environment",
      header: "Environment",
      cell: (g) => assetLabel(g.environment),
      csv: (g) => g.environment,
      sortValue: (g) => g.environment,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Exposure",
      cell: (g) => t_i18n(label(GROUP_EXPOSURE_LABELS, g.internet_facing)),
      csv: (g) => g.internet_facing,
      sortValue: (g) => Object.keys(GROUP_EXPOSURE_LABELS).indexOf(g.internet_facing),
      hideable: true,
    },
    {
      key: "owner",
      header: "Owner",
      cell: (g) => <TruncatedText text={g.owner} className="max-w-[12rem]" />,
      csv: (g) => g.owner,
      sortValue: (g) => g.owner.toLowerCase(),
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "tech",
      header: "Tech stack",
      align: "right",
      cell: (g) => formatCount(intl.locale, g.tech_stack.length),
      csv: (g) => g.tech_stack.join(" "),
      sortValue: (g) => g.tech_stack.length,
      hideable: true,
    },
    {
      key: "source",
      header: "Source",
      cell: (g) => sourceLabel(g.source || "manual"),
      csv: (g) => g.source || "manual",
      hideable: true,
      hideBelow: "6xl",
    },
  ];

  return (
    <DataTable<AssetGroupRow>
      rows={groups}
      columns={columns}
      rowKey={(g) => g.name}
      searchFields={(g) => [g.name, g.description, g.owner, ...g.tags]}
      searchPlaceholder={t_i18n("Search...")}
      filters={[
        { key: "exposure", label: "Exposure", values: (g: AssetGroupRow) => [t_i18n(label(GROUP_EXPOSURE_LABELS, g.internet_facing))] },
        { key: "environment", label: "Environment", values: (g: AssetGroupRow) => (g.environment ? [assetLabel(g.environment)] : []) },
      ]}
      urlKey="g_"
      storageKey="ctem-inventory-asset-groups"
      exportName="ctem-inventory-asset-groups"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No asset group yet. Add a source to the inventory to fill it.")}
      rowTestId="ctem-inventory-group-row"
    />
  );
}

export function AiSystemsTab() {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError, refetch } = useOrganizationContext();
  const systems = ((data?.profile.ai_systems ?? []) as AiSystemRow[]).filter((s) => s.name.trim());

  const columns: DataTableColumn<AiSystemRow>[] = [
    {
      key: "name",
      header: "AI system",
      cell: (s) => <TruncatedText text={s.name} className="max-w-[18rem] font-medium" />,
      csv: (s) => s.name,
      sortValue: (s) => s.name.toLowerCase(),
    },
    {
      key: "kind",
      header: "Kind",
      cell: (s) => t_i18n(label(AI_KIND_LABELS, s.kind)),
      csv: (s) => s.kind,
      sortValue: (s) => s.kind,
      hideable: true,
    },
    {
      key: "exposure",
      header: "Exposure",
      cell: (s) => t_i18n(label(AI_EXPOSURE_LABELS, s.exposure)),
      csv: (s) => s.exposure,
      sortValue: (s) => Object.keys(AI_EXPOSURE_LABELS).indexOf(s.exposure),
      hideable: true,
    },
    {
      key: "owner",
      header: "Owner",
      cell: (s) => <TruncatedText text={s.owner} className="max-w-[12rem]" />,
      csv: (s) => s.owner,
      sortValue: (s) => s.owner.toLowerCase(),
      hideable: true,
    },
    {
      key: "source",
      header: "Source",
      cell: (s) => sourceLabel(s.source || "manual"),
      csv: (s) => s.source || "manual",
      hideable: true,
      hideBelow: "6xl",
    },
  ];

  return (
    <DataTable<AiSystemRow>
      rows={systems}
      columns={columns}
      rowKey={(s) => s.name}
      searchFields={(s) => [s.name, s.owner]}
      searchPlaceholder={t_i18n("Search...")}
      filters={[
        { key: "kind", label: "Kind", values: (s: AiSystemRow) => [t_i18n(label(AI_KIND_LABELS, s.kind))] },
        { key: "exposure", label: "Exposure", values: (s: AiSystemRow) => [t_i18n(label(AI_EXPOSURE_LABELS, s.exposure))] },
      ]}
      urlKey="ai_"
      storageKey="ctem-inventory-ai-systems"
      exportName="ctem-inventory-ai-systems"
      isLoading={isLoading && !data}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No AI system yet: the AI security fleet has nothing to test.")}
      rowTestId="ctem-inventory-ai-row"
    />
  );
}
