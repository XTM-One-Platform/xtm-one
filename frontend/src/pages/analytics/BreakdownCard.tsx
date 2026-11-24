// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { Info, Layers } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ChartCard } from "@/components/ui/charts/chart-card";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { CostShare, CostValue, HintText } from "@/components/ui/charts/breakdown-shared";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TagList } from "@/components/ui/tag-list";
import { useFormatter } from "@/i18n";
import { cn, formatCompactNumber, formatModelLabel } from "@/lib/utils";
import { BREAKDOWN_LABELS, type Breakdown } from "@/pages/analytics/constants";
import { formatLatency, formatScore } from "@/pages/analytics/format";
import { TopAgentStatusBadge, topAgentHref, topAgentLabel } from "@/pages/analytics/top-agents";
import type {
  TraceAnalyticsCostByAgentItem,
  TraceAnalyticsCostByGroupItem,
  TraceAnalyticsCostByGroupResponse,
  TraceAnalyticsCostByModelItem,
  TraceAnalyticsCostByUserItem,
  TraceAnalyticsCostByUserResponse,
  TraceAnalyticsLatencyByModelItem,
  TraceAnalyticsUsageTotals,
} from "@/types";

// Width cap of the By agent name cells (Agent, Owner): 9rem at every width.
// A 10rem step in the 1440 card (1112 px) overflowed it by 9 px in es and 18 px
// in ja with long names, measured by the UX review.
const NAME_CELL_CAP = "max-w-36";
// Width cap of the By user Groups cell: one line of badges, the rest folded into
// "+N" (UX round 1 on #10980, B1: two badges stacked and one name wrapped to
// three lines, 92 px rows at 1080).
const GROUPS_CELL_CAP = "max-w-40";
// URL prefix of each breakdown table: one per table, since they share the
// /analytics page with the Top errors table (`te_`) and the page's own keys
// (period, date_from, date_to, breakdown, group_ids, agent_ids, models, sources).
const BREAKDOWN_URL_KEYS: Record<Breakdown, string> = { agents: "ba_", users: "bu_", groups: "bg_", models: "bm_" };

const BREAKDOWN_OPTIONS = (Object.keys(BREAKDOWN_LABELS) as Breakdown[]).map((key) => ({ key, label: BREAKDOWN_LABELS[key] }));

/** Column headers, translated by the table and the CSV it exports. @i18n-keys */
const HEADERS = {
  agent: "Agent",
  agentId: "Agent ID",
  owner: "Owner",
  ownerEmail: "Owner email",
  traces: "Traces",
  totalCost: "Total cost",
  totalCostCsv: "Total cost (USD)",
  share: "Share of cost",
  shareCsv: "Share of cost (%)",
  perTrace: "Avg cost / trace",
  perTraceCsv: "Avg cost per trace (USD)",
  latency: "Avg latency",
  latencyCsv: "Avg latency (ms)",
  feedback: "Avg feedback",
  feedbackCsv: "Avg feedback (0-1)",
  user: "User",
  email: "Email",
  groups: "Groups",
  agents: "Agents",
  tokens: "Tokens",
  group: "User group",
  active: "Active users",
  members: "Members",
  perUser: "Avg cost / active user",
  perUserCsv: "Avg cost per active user (USD)",
  model: "Model",
  inputTokens: "Input Tokens",
  outputTokens: "Output Tokens",
  inputCost: "Input Cost",
  inputCostCsv: "Input cost (USD)",
  outputCost: "Output Cost",
  outputCostCsv: "Output cost (USD)",
  p50: "P50",
  p50Csv: "P50 latency (ms)",
  p95: "P95",
  p95Csv: "P95 latency (ms)",
} as const;

const share = (cost: number, total: number) => (total > 0 ? Number(((cost / total) * 100).toFixed(2)) : 0);

/** A cost where the platform shows costs, nothing where it masks them: sorted and exported alike. */
function maskedValue(value: number | null | undefined, available: boolean): number | null {
  return available ? value ?? null : null;
}

interface BreakdownCardProps {
  breakdown: Breakdown;
  onBreakdownChange: (next: Breakdown) => void;
  totalCost: number;
  costDataAvailable: boolean;
  /** Admins open users and groups; a manager reads them. An agent opens where the server allows it. */
  linkEntities: boolean;
  /** `onRetry` reloads the failed read: the table shows its own error line with Retry. */
  agents: { items: TraceAnalyticsCostByAgentItem[]; isLoading: boolean; isError: boolean; onRetry?: () => void };
  users: { data?: TraceAnalyticsCostByUserResponse; isLoading: boolean; isError: boolean; onRetry?: () => void };
  groups: { data?: TraceAnalyticsCostByGroupResponse; isLoading: boolean; isError: boolean; onRetry?: () => void };
  models: {
    items: TraceAnalyticsCostByModelItem[];
    latency: TraceAnalyticsLatencyByModelItem[];
    isLoading: boolean;
    isError: boolean;
    onRetry?: () => void;
  };
}

/**
 * The window's usage, one row per agent, person, group or model: every
 * column sortable, searchable, paged above and below, exported to CSV as shown.
 */
export function BreakdownCard({
  breakdown,
  onBreakdownChange,
  totalCost,
  costDataAvailable,
  linkEntities,
  agents,
  users,
  groups,
  models,
}: BreakdownCardProps) {
  const { t_i18n } = useFormatter();

  const agentColumns = useMemo<DataTableColumn<TraceAnalyticsCostByAgentItem>[]>(
    () => [
      {
        key: "agent",
        header: HEADERS.agent,
        cell: (item) => {
          // Same row as Top agents: the server says who may open it.
          // The cell is capped so a long name (up to 200 characters) truncates
          // instead of widening the auto-layout table; the title keeps it whole.
          // NAME_CELL_CAP, shared with Owner, keeps the 1024 card (five columns)
          // and the 1440 card (eight columns) free of sideways scroll in es and ja.
          // Plain text: the row opens the agent (rowHref, #12070).
          const name = topAgentLabel(item, t_i18n);
          return (
            <div className={cn("flex min-w-0 items-center gap-2", NAME_CELL_CAP)}>
              <span title={name} className="min-w-0 truncate font-medium">
                {name}
              </span>
              <TopAgentStatusBadge item={item} />
            </div>
          );
        },
        sortValue: (item) => topAgentLabel(item, t_i18n),
        // The CSV says what the screen says: a deleted agent with no last
        // known name is "Deleted agent", never an empty cell; agent_id tells two apart.
        csv: (item) => topAgentLabel(item, t_i18n),
      },
      { key: "agent_id", header: HEADERS.agentId, cell: () => null, csv: (item) => item.agent_id, exportOnly: true },
      {
        key: "owner",
        hideable: true,
        header: HEADERS.owner,
        cell: (item) => {
          // The short label keeps the badge on one line in the narrow Owner column.
          if (item.is_company_managed) return <CompanyManagedBadge label="Company" />;
          if (!item.owner_name && !item.owner_email) return <span className="text-default-secondary">-</span>;
          // Capped like the Agent cell: a long email truncates instead of widening
          // the table; the title keeps the name and email whole.
          const fullOwner = [item.owner_name, item.owner_email].filter(Boolean).join("\n");
          const name = (
            <>
              <span className="block truncate">{item.owner_name || item.owner_email}</span>
              {item.owner_name && item.owner_email && <span className="block truncate text-default-secondary">{item.owner_email}</span>}
            </>
          );
          return (
            <div title={fullOwner} className={cn("min-w-0", NAME_CELL_CAP)}>
              {name}
            </div>
          );
        },
        sortValue: (item) => (item.is_company_managed ? t_i18n("Company-managed") : item.owner_name || item.owner_email),
        csv: (item) => (item.is_company_managed ? t_i18n("Company-managed") : item.owner_name ?? ""),
      },
      { key: "owner_email", header: HEADERS.ownerEmail, cell: () => null, csv: (item) => item.owner_email ?? "", exportOnly: true },
      {
        key: "traces",
        hideable: true,
        header: HEADERS.traces,
        cell: (item) => formatCompactNumber(item.trace_count),
        sortValue: (item) => item.trace_count,
        csv: (item) => item.trace_count,
        align: "right",
      },
      {
        key: "cost",
        header: HEADERS.totalCost,
        csvHeader: HEADERS.totalCostCsv,
        cell: (item) => <CostValue value={item.total_cost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => maskedValue(item.total_cost, costDataAvailable),
        align: "right",
        className: "font-medium",
      },
      {
        key: "share",
        hideable: true,
        header: HEADERS.share,
        csvHeader: HEADERS.shareCsv,
        cell: (item) => <CostShare cost={item.total_cost} totalCost={totalCost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => (costDataAvailable ? share(item.total_cost, totalCost) : null),
        align: "right",
      },
      {
        key: "per_trace",
        hideable: true,
        header: HEADERS.perTrace,
        csvHeader: HEADERS.perTraceCsv,
        cell: (item) => <CostValue value={item.avg_cost_per_trace} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.avg_cost_per_trace, costDataAvailable),
        csv: (item) => maskedValue(item.avg_cost_per_trace, costDataAvailable),
        align: "right",
        // The three secondary figures step aside in the 1024 card:
        // eight columns scrolled sideways there. The CSV keeps them all.
        hideBelow: "3xl",
      },
      {
        key: "latency",
        hideable: true,
        header: HEADERS.latency,
        csvHeader: HEADERS.latencyCsv,
        cell: (item) => formatLatency(item.avg_duration_ms),
        sortValue: (item) => item.avg_duration_ms,
        csv: (item) => Math.round(item.avg_duration_ms),
        align: "right",
        hideBelow: "4xl",
      },
      {
        key: "feedback",
        hideable: true,
        header: HEADERS.feedback,
        csvHeader: HEADERS.feedbackCsv,
        cell: (item) => formatScore(item.avg_feedback_score),
        sortValue: (item) => item.avg_feedback_score,
        csv: (item) => item.avg_feedback_score,
        align: "right",
        hideBelow: "4xl",
      },
    ],
    [costDataAvailable, totalCost, t_i18n],
  );

  const userColumns = useMemo<DataTableColumn<TraceAnalyticsCostByUserItem>[]>(
    () => [
      {
        key: "user",
        header: HEADERS.user,
        cell: (item) => {
          if (item.is_active === null) return <span className="italic text-default-secondary">{t_i18n("Deleted user")}</span>;
          const name = (
            <>
              <span className="block truncate font-medium">{item.user_name || item.user_email}</span>
              {item.user_name && item.user_email && <span className="block truncate text-default-secondary">{item.user_email}</span>}
            </>
          );
          return (
            <div className="flex min-w-0 items-center gap-2">
              {/* Plain text: the row opens the user for an admin (rowHref, #12070). */}
              <div className="min-w-0">{name}</div>
              {item.is_active === false && (
                <Badge variant="outline" className="shrink-0 content-caption text-default-secondary">
                  {t_i18n("Inactive")}
                </Badge>
              )}
            </div>
          );
        },
        sortValue: (item) => item.user_name || item.user_email,
        csv: (item) => item.user_name ?? "",
      },
      { key: "email", header: HEADERS.email, cell: () => null, csv: (item) => item.user_email ?? "", exportOnly: true },
      {
        key: "groups",
        hideable: true,
        header: HEADERS.groups,
        cell: (item) =>
          item.group_names.length === 0 ? (
            <span className="text-default-secondary">-</span>
          ) : (
            // One line that never wraps: the badges keep a readable width and the
            // last ones fold into the shared "+N" marker, its tooltip naming them.
            <TagList
              tags={item.group_names}
              max={item.group_names.length}
              badgeMin="readable"
              className={cn(GROUPS_CELL_CAP, "whitespace-nowrap")}
            />
          ),
        sortValue: (item) => item.group_names.join(", ") || null,
        csv: (item) => item.group_names.join("; "),
      },
      {
        key: "agents",
        hideable: true,
        header: HEADERS.agents,
        cell: (item) => item.agent_count,
        sortValue: (item) => item.agent_count,
        csv: (item) => item.agent_count,
        align: "right",
        // The 800 px card at 1080 drew 892 px and scrolled sideways (UX round 1 on #10980, B1).
        hideBelow: "4xl",
      },
      {
        key: "traces",
        hideable: true,
        header: HEADERS.traces,
        cell: (item) => formatCompactNumber(item.trace_count),
        sortValue: (item) => item.trace_count,
        csv: (item) => item.trace_count,
        align: "right",
      },
      {
        key: "tokens",
        hideable: true,
        header: HEADERS.tokens,
        cell: (item) => formatCompactNumber(item.total_tokens),
        sortValue: (item) => item.total_tokens,
        csv: (item) => item.total_tokens,
        align: "right",
      },
      {
        key: "per_trace",
        hideable: true,
        header: HEADERS.perTrace,
        csvHeader: HEADERS.perTraceCsv,
        cell: (item) => <CostValue value={item.avg_cost_per_trace} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.avg_cost_per_trace, costDataAvailable),
        csv: (item) => maskedValue(item.avg_cost_per_trace, costDataAvailable),
        align: "right",
        hideBelow: "4xl",
      },
      {
        key: "cost",
        header: HEADERS.totalCost,
        csvHeader: HEADERS.totalCostCsv,
        cell: (item) => <CostValue value={item.total_cost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => maskedValue(item.total_cost, costDataAvailable),
        align: "right",
        className: "font-medium",
      },
      {
        key: "share",
        hideable: true,
        header: HEADERS.share,
        csvHeader: HEADERS.shareCsv,
        cell: (item) => <CostShare cost={item.total_cost} totalCost={totalCost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => (costDataAvailable ? share(item.total_cost, totalCost) : null),
        align: "right",
      },
    ],
    [costDataAvailable, totalCost, t_i18n],
  );

  const groupColumns = useMemo<DataTableColumn<TraceAnalyticsCostByGroupItem>[]>(
    () => [
      {
        key: "group",
        header: HEADERS.group,
        // Plain text: the row opens the group for an admin (rowHref, #12070).
        cell: (item) => <span className="font-medium">{item.group_name}</span>,
        sortValue: (item) => item.group_name,
        csv: (item) => item.group_name,
      },
      {
        key: "active",
        hideable: true,
        header: HEADERS.active,
        cell: (item) => (
          <>
            {item.active_user_count}
            <span className="text-default-secondary"> / {item.member_count}</span>
          </>
        ),
        sortValue: (item) => item.active_user_count,
        csv: (item) => item.active_user_count,
        align: "right",
      },
      {
        key: "members",
        header: HEADERS.members,
        cell: () => null,
        csv: (item) => item.member_count,
        exportOnly: true,
      },
      {
        key: "traces",
        hideable: true,
        header: HEADERS.traces,
        cell: (item) => formatCompactNumber(item.trace_count),
        sortValue: (item) => item.trace_count,
        csv: (item) => item.trace_count,
        align: "right",
      },
      {
        key: "tokens",
        hideable: true,
        header: HEADERS.tokens,
        cell: (item) => formatCompactNumber(item.total_tokens),
        sortValue: (item) => item.total_tokens,
        csv: (item) => item.total_tokens,
        align: "right",
      },
      {
        key: "per_user",
        hideable: true,
        header: HEADERS.perUser,
        csvHeader: HEADERS.perUserCsv,
        cell: (item) => <CostValue value={item.avg_cost_per_active_user} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.avg_cost_per_active_user, costDataAvailable),
        csv: (item) => maskedValue(item.avg_cost_per_active_user, costDataAvailable),
        align: "right",
      },
      {
        key: "cost",
        header: HEADERS.totalCost,
        csvHeader: HEADERS.totalCostCsv,
        cell: (item) => <CostValue value={item.total_cost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => maskedValue(item.total_cost, costDataAvailable),
        align: "right",
        className: "font-medium",
      },
      {
        key: "share",
        hideable: true,
        header: HEADERS.share,
        csvHeader: HEADERS.shareCsv,
        cell: (item) => <CostShare cost={item.total_cost} totalCost={totalCost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => (costDataAvailable ? share(item.total_cost, totalCost) : null),
        align: "right",
      },
    ],
    [costDataAvailable, totalCost],
  );

  type ModelRow = TraceAnalyticsCostByModelItem & { p50: number | null; p95: number | null };
  const modelRows = useMemo<ModelRow[]>(() => {
    const latency = new Map(models.latency.map((item) => [item.model, item]));
    return models.items.map((item) => ({
      ...item,
      p50: latency.get(item.model)?.p50 ?? null,
      p95: latency.get(item.model)?.p95 ?? null,
    }));
  }, [models.items, models.latency]);

  const modelColumns = useMemo<DataTableColumn<ModelRow>[]>(
    () => [
      {
        key: "model",
        header: HEADERS.model,
        cell: (item) => <span className="font-medium">{formatModelLabel(item.model, t_i18n)}</span>,
        sortValue: (item) => formatModelLabel(item.model, t_i18n),
        csv: (item) => formatModelLabel(item.model, t_i18n),
      },
      {
        key: "traces",
        hideable: true,
        header: HEADERS.traces,
        cell: (item) => formatCompactNumber(item.trace_count),
        sortValue: (item) => item.trace_count,
        csv: (item) => item.trace_count,
        align: "right",
      },
      {
        key: "input_tokens",
        hideable: true,
        header: HEADERS.inputTokens,
        cell: (item) => formatCompactNumber(item.input_tokens),
        sortValue: (item) => item.input_tokens,
        csv: (item) => item.input_tokens,
        align: "right",
      },
      {
        key: "output_tokens",
        hideable: true,
        header: HEADERS.outputTokens,
        cell: (item) => formatCompactNumber(item.output_tokens),
        sortValue: (item) => item.output_tokens,
        csv: (item) => item.output_tokens,
        align: "right",
      },
      {
        key: "input_cost",
        hideable: true,
        header: HEADERS.inputCost,
        csvHeader: HEADERS.inputCostCsv,
        cell: (item) => <CostValue value={item.input_cost ?? 0} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.input_cost, costDataAvailable),
        csv: (item) => maskedValue(item.input_cost, costDataAvailable),
        align: "right",
        // By model drew 989 px in the 800 px card at 1080 and scrolled sideways (UX round 2 on #10980).
        hideBelow: "4xl",
      },
      {
        key: "output_cost",
        hideable: true,
        header: HEADERS.outputCost,
        csvHeader: HEADERS.outputCostCsv,
        cell: (item) => <CostValue value={item.output_cost ?? 0} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.output_cost, costDataAvailable),
        csv: (item) => maskedValue(item.output_cost, costDataAvailable),
        align: "right",
        hideBelow: "4xl",
      },
      {
        key: "cost",
        header: HEADERS.totalCost,
        csvHeader: HEADERS.totalCostCsv,
        cell: (item) => <CostValue value={item.total_cost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => maskedValue(item.total_cost, costDataAvailable),
        align: "right",
        className: "font-medium",
      },
      {
        key: "share",
        hideable: true,
        header: HEADERS.share,
        csvHeader: HEADERS.shareCsv,
        cell: (item) => <CostShare cost={item.total_cost} totalCost={totalCost} available={costDataAvailable} />,
        sortValue: (item) => maskedValue(item.total_cost, costDataAvailable),
        csv: (item) => (costDataAvailable ? share(item.total_cost, totalCost) : null),
        align: "right",
      },
      {
        key: "p50",
        hideable: true,
        header: HEADERS.p50,
        csvHeader: HEADERS.p50Csv,
        cell: (item) => formatLatency(item.p50),
        sortValue: (item) => item.p50,
        csv: (item) => (item.p50 === null ? null : Math.round(item.p50)),
        align: "right",
        hideBelow: "4xl",
      },
      {
        key: "p95",
        hideable: true,
        header: HEADERS.p95,
        csvHeader: HEADERS.p95Csv,
        cell: (item) => formatLatency(item.p95),
        sortValue: (item) => item.p95,
        csv: (item) => (item.p95 === null ? null : Math.round(item.p95)),
        align: "right",
      },
    ],
    [costDataAvailable, totalCost, t_i18n],
  );

  const remainderLabel = (label: string, hint: string, colSpan = 1) => (
    <td className="px-3 py-2" colSpan={colSpan}>
      <HintText hint={hint} className="italic">
        {label}
      </HintText>
    </td>
  );
  const costCells = (usage: TraceAnalyticsUsageTotals) => (
    <>
      <td className="px-3 py-2 text-right tabular-nums">
        <CostValue value={usage.total_cost} available={costDataAvailable} />
      </td>
      <td className="px-3 py-2">
        <CostShare cost={usage.total_cost} totalCost={totalCost} available={costDataAvailable} />
      </td>
    </>
  );
  // The users table's remainders: user, groups and agents under the label.
  const userRemainder = (key: string, label: string, hint: string, usage: TraceAnalyticsUsageTotals) => (
    <tr key={key} className="border-t border-elevation-subtle-soft text-default-secondary">
      {remainderLabel(label, hint, 3)}
      <td className="px-3 py-2 text-right tabular-nums">{formatCompactNumber(usage.trace_count)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{formatCompactNumber(usage.total_tokens)}</td>
      <td className="px-3 py-2 text-right tabular-nums">
        <CostValue value={usage.avg_cost_per_trace} available={costDataAvailable} />
      </td>
      {costCells(usage)}
    </tr>
  );
  // The groups table's remainders: a per-user figure only where users are counted.
  const groupRemainder = (
    key: string,
    label: string,
    hint: string,
    usage: TraceAnalyticsUsageTotals,
    perUser: { active: number; avg: number } | null,
  ) => (
    <tr key={key} className="border-t border-elevation-subtle-soft text-default-secondary">
      {remainderLabel(label, hint)}
      <td className="px-3 py-2 text-right tabular-nums">{perUser ? perUser.active : "-"}</td>
      <td className="px-3 py-2 text-right tabular-nums">{formatCompactNumber(usage.trace_count)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{formatCompactNumber(usage.total_tokens)}</td>
      <td className="px-3 py-2 text-right tabular-nums">
        {perUser ? <CostValue value={perUser.avg} available={costDataAvailable} /> : "-"}
      </td>
      {costCells(usage)}
    </tr>
  );

  // Agents narrow by their state (a deleted or disabled agent still costs), users by their groups.
  const agentFilters: DataTableFilter<TraceAnalyticsCostByAgentItem>[] = [
    {
      key: "status",
      label: "Status",
      values: (item) => [item.agent_status],
      options: [
        { value: "active", label: t_i18n("Active") },
        { value: "disabled", label: t_i18n("Disabled") },
        { value: "deleted", label: t_i18n("Deleted") },
      ],
    },
  ];
  const userFilters: DataTableFilter<TraceAnalyticsCostByUserItem>[] = [
    { key: "group", label: "Groups", values: (item) => item.group_names },
  ];

  let table;
  if (breakdown === "agents") {
    table = (
      <DataTable
        rows={agents.items}
        columns={agentColumns}
        rowKey={(item) => item.agent_id}
        rowHref={(item) => topAgentHref(item) ?? ""}
        defaultSort={{ key: "cost", direction: "desc" }}
        searchFields={(item) => [item.agent_name, item.agent_id, item.owner_name, item.owner_email]}
        searchPlaceholder={t_i18n("Search agents or owners...")}
        filters={agentFilters}
        urlKey={BREAKDOWN_URL_KEYS.agents}
        storageKey="analytics-breakdown-agents"
        rowLines={2}
        exportName="llm-usage-by-agent"
        isLoading={agents.isLoading}
        isError={agents.isError}
        onRetry={agents.onRetry}
        errorLabel={t_i18n("Could not load this breakdown.")}
        emptyLabel={t_i18n("No agent activity in this window")}
      />
    );
  } else if (breakdown === "users") {
    const data = users.data;
    table = (
      <div className="space-y-2">
        <DataTable
          rows={data?.items ?? []}
          columns={userColumns}
          rowKey={(item) => item.user_id}
          rowHref={(item) => (linkEntities && item.is_active !== null ? `/users-and-groups/users/${item.user_id}` : "")}
          defaultSort={{ key: "cost", direction: "desc" }}
          searchFields={(item) => [item.user_name, item.user_email, ...item.group_names]}
          searchPlaceholder={t_i18n("Search by name, email or group...")}
          filters={userFilters}
          urlKey={BREAKDOWN_URL_KEYS.users}
          storageKey="analytics-breakdown-users"
          rowLines={2}
          exportName="llm-usage-by-user"
          isLoading={users.isLoading}
          isError={users.isError}
          onRetry={users.onRetry}
          errorLabel={t_i18n("Could not load this breakdown.")}
          emptyLabel={t_i18n("No user activity in this window")}
          trailingRows={
            <>
              {data?.other_users &&
                userRemainder(
                  "other-users",
                  t_i18n("{count, plural, one {# other user} other {# other users}}", { count: data.other_users.user_count }),
                  t_i18n("The users who ran traces in this window past the ones listed, counted together."),
                  data.other_users,
                )}
              {data?.unattributed &&
                userRemainder(
                  "unattributed",
                  t_i18n("No user account"),
                  t_i18n("Runs no account is behind: anonymous channel senders, public widget visitors, the global MCP key and assignment runs without a user."),
                  data.unattributed,
                )}
            </>
          }
        />
        {data && data.total_users > data.items.length && (
          <p className="content-caption text-default-secondary">
            {t_i18n("Showing the {shown} users with the highest cost out of {total}; the others are counted together on the last page.", {
              shown: data.items.length,
              total: data.total_users,
            })}
          </p>
        )}
      </div>
    );
  } else if (breakdown === "groups") {
    const data = groups.data;
    table = (
      <div className="space-y-2">
        {data?.overlapping_membership && (
          <p className="flex items-start gap-2 content-caption text-default-secondary">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {t_i18n("Some users belong to several groups and count in each of them, so the groups add up to more than the total.")}
          </p>
        )}
        <DataTable
          rows={data?.items ?? []}
          columns={groupColumns}
          rowKey={(item) => item.group_id}
          rowHref={(item) => (linkEntities ? `/users-and-groups/groups/${item.group_id}` : "")}
          defaultSort={{ key: "cost", direction: "desc" }}
          searchFields={(item) => [item.group_name]}
          searchPlaceholder={t_i18n("Search groups...")}
          noFilters="One row per user group: a group has no other dimension than its name."
          urlKey={BREAKDOWN_URL_KEYS.groups}
          storageKey="analytics-breakdown-groups"
          exportName="llm-usage-by-group"
          isLoading={groups.isLoading}
          isError={groups.isError}
          onRetry={groups.onRetry}
          errorLabel={t_i18n("Could not load this breakdown.")}
          emptyLabel={t_i18n("No group activity in this window")}
          trailingRows={
            <>
              {data?.ungrouped &&
                groupRemainder(
                  "ungrouped",
                  t_i18n("Users in no group"),
                  t_i18n("Users who ran traces in this window and belong to no group today."),
                  data.ungrouped,
                  { active: data.ungrouped.active_user_count, avg: data.ungrouped.avg_cost_per_active_user },
                )}
              {data?.unattributed &&
                groupRemainder(
                  "unattributed",
                  t_i18n("No user account"),
                  t_i18n("Runs no account is behind: anonymous channel senders, public widget visitors, the global MCP key and assignment runs without a user."),
                  data.unattributed,
                  null,
                )}
            </>
          }
        />
        <p className="content-caption text-default-secondary">
          {t_i18n("Groups are read as they are today: a trace counts for the groups its user belongs to now.")}
        </p>
      </div>
    );
  } else {
    table = (
      <DataTable
        rows={modelRows}
        columns={modelColumns}
        rowKey={(item) => item.model || "__platform_default__"}
        defaultSort={{ key: "cost", direction: "desc" }}
        searchFields={(item) => [formatModelLabel(item.model, t_i18n)]}
        searchPlaceholder={t_i18n("Search models...")}
        noFilters="One row per model of the window: the search is the only narrowing it needs."
        urlKey={BREAKDOWN_URL_KEYS.models}
        storageKey="analytics-breakdown-models"
        exportName="llm-usage-by-model"
        isLoading={models.isLoading}
        isError={models.isError}
        onRetry={models.onRetry}
        errorLabel={t_i18n("Could not load this breakdown.")}
        emptyLabel={t_i18n("No cost-by-model data for this filter set.")}
      />
    );
  }

  return (
    <ChartCard
      title={t_i18n("Cost breakdown")}
      icon={Layers}
      className="xl:col-span-2"
      actions={
        <SegmentedFilter options={BREAKDOWN_OPTIONS} value={breakdown} onChange={onBreakdownChange} ariaLabel={t_i18n("Cost breakdown")} />
      }
    >
      {table}
    </ChartCard>
  );
}
