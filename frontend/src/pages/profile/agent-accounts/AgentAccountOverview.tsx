/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The Overview tab of an agent account: what the account did over the
 * last 24 hours, 7 days or 30 days - how much it heard, answered, left alone
 * and why, where and for whom, how fast, and how its answers were rated.
 * Every figure comes from `GET /agent-accounts/{id}/activity`, computed by the
 * server; a conversation, a person or a rule opens the Decisions tab on it.
 */
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Clock,
  EyeOff,
  Flag,
  Forward,
  Hash,
  Hourglass,
  Inbox,
  MessageCircleOff,
  MessagesSquare,
  Send,
  ThumbsDown,
  ThumbsUp,
  User,
  Users,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { StatCard } from "@/components/ui/stat-card";
import { Switch } from "@/components/ui/switch";
import { useAgentAccountActivity, useAgentAccountProvider } from "@/hooks/api/useAgentAccountApi";
import { useFormatter } from "@/i18n";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { cn, formatCompactNumber, formatDurationShort, formatShortDate, formatTime } from "@/lib/utils";
import type {
  AgentAccount,
  AgentAccountActivity,
  AgentAccountActivityGroup,
  AgentAccountActivityPeriod,
  AgentAccountConversationType,
  AgentAccountProviderKind,
} from "@/types";
import {
  ACTIVITY_GROUPS,
  ACTIVITY_GROUP_LABELS,
  ACTIVITY_PERIOD_LABELS,
  activeGroups,
  answerRate,
  leftReasons,
  timelineRows,
  wording,
} from "./agent-accounts";
import { Spinner } from "@filigran/design-system";

/** A colour per group: what the account said in warm and bright tones, what it left in the muted one. */
const GROUP_COLORS: Record<AgentAccountActivityGroup, string> = {
  answered: EVALUATION_CHART_COLORS[3],
  needs_owner: EVALUATION_CHART_COLORS[7],
  awaiting_approval: EVALUATION_CHART_COLORS[1],
  shadow: EVALUATION_CHART_COLORS[2],
  silent: EVALUATION_CHART_COLORS[5],
  waiting: EVALUATION_CHART_COLORS[9],
  dropped: EVALUATION_CHART_COLORS[6],
  already_handled: EVALUATION_CHART_COLORS[8],
  failed: EVALUATION_CHART_COLORS[4],
  left: "var(--text-default-secondary)",
};

const TYPE_COLORS: Record<AgentAccountConversationType, string> = {
  direct: EVALUATION_CHART_COLORS[0],
  group: EVALUATION_CHART_COLORS[2],
  channel: EVALUATION_CHART_COLORS[1],
};

/** The stat cards' copy, which `StatCard` translates. @i18n-keys */
const STAT_COPY = {
  heard: "Messages heard",
  answered: "Answered",
  awaiting: "Waiting for approval",
  awaitingHint: "Drafts waiting for you",
  handed: "Handed to you",
  handedHint: "The agent left them to you",
  silent: "Stayed silent",
  silentHint: "The agent chose not to answer",
  left: "Left alone",
  leftHint: "Not for the agent",
  handledElsewhere: "Handled elsewhere",
  handledElsewhereHint: "Another pipeline of the agent took them",
  failed: "Failed",
  failedHint: "Nothing was posted",
  answerTime: "Median time to answer",
  answerTimeHint: "From the message to the reply posted on its own",
  approvalTime: "Median approval delay",
  approvalTimeHint: "From the draft to your approval",
  positive: "Positive ratings",
  negative: "Negative ratings",
  ratingsHint: "Reactions included",
} as const;

const AXIS_TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };

export function AgentAccountOverview({ account }: { account: AgentAccount }) {
  const { t_i18n } = useFormatter();
  const [period, setPeriod] = useState<AgentAccountActivityPeriod>("7d");
  const { data, isLoading } = useAgentAccountActivity(account.id, period);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="content-base text-default-secondary">
          {t_i18n("What the account did with every message it heard. Times are in your time zone.")}
        </p>
        <SegmentedFilter<AgentAccountActivityPeriod>
          options={(Object.keys(ACTIVITY_PERIOD_LABELS) as AgentAccountActivityPeriod[]).map((key) => ({
            key,
            label: ACTIVITY_PERIOD_LABELS[key],
          }))}
          value={period}
          onChange={setPeriod}
          ariaLabel={t_i18n("Period")}
        />
      </div>
      {isLoading || !data ? (
        <div className="flex justify-center py-16"><Spinner size="lg" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} /></div>
      ) : (
        <ActivityBody account={account} activity={data} />
      )}
    </div>
  );
}

function ActivityBody({ account, activity }: { account: AgentAccount; activity: AgentAccountActivity }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const info = useAgentAccountProvider(account.provider);
  const kind = info?.kind ?? "chat";
  const decisionsPath = `/profile/agent-accounts/${account.id}/decisions`;
  const decisionsHref = (params: Record<string, string>) =>
    `${decisionsPath}?${new URLSearchParams(params).toString()}`;
  const openDecisions = (params: Record<string, string>) => navigate(decisionsHref(params));
  const rate = answerRate(activity);
  const groups = activity.groups;
  const triages = !!info?.capabilities.triage || groups.needs_owner > 0;
  // Only an account another pipeline of its agent competes with ever has one.
  const handledElsewhere = groups.already_handled > 0;
  const cards = 6 + (triages ? 1 : 0) + (handledElsewhere ? 1 : 0);
  // At most 7 cards share one xl row: at 8 columns a card is 139 px wide at 1440 and the
  // two-line fr and es labels clip, so 8 cards sit in two rows of 4.
  const xlCols = cards >= 8 ? "xl:grid-cols-4" : cards === 7 ? "xl:grid-cols-7" : "xl:grid-cols-6";

  return (
    <>
      <div
        className={cn(
          "grid grid-cols-2 gap-3 md:grid-cols-3",
          xlCols,
        )}
      >
        <StatCard
          icon={Inbox}
          label={STAT_COPY.heard}
          value={activity.heard}
          subtitle={<>{t_i18n("{count} for the agent", { count: activity.for_agent })}</>}
          onClick={() => openDecisions({})}
        />
        <StatCard
          icon={Send}
          label={STAT_COPY.answered}
          value={groups.answered}
          subtitle={
            <>
              {rate === null
                ? t_i18n("Nothing was for the agent")
                : t_i18n("{rate}% of the messages for the agent", { rate })}
            </>
          }
          iconClassName="text-feedback-success-primary light:text-feedback-success-primary"
          onClick={() => openDecisions({ group: "answered" })}
        />
        {triages && (
          <StatCard
            icon={Flag}
            label={STAT_COPY.handed}
            value={groups.needs_owner}
            subtitle={STAT_COPY.handedHint}
            iconClassName="text-feedback-warning-primary light:text-feedback-warning-primary"
            onClick={() => openDecisions({ group: "needs_owner" })}
          />
        )}
        <StatCard
          icon={Hourglass}
          label={STAT_COPY.awaiting}
          value={groups.awaiting_approval}
          subtitle={STAT_COPY.awaitingHint}
          iconClassName="text-feedback-warning-primary light:text-feedback-warning-primary"
          onClick={() => openDecisions({ group: "awaiting_approval" })}
        />
        <StatCard
          icon={MessageCircleOff}
          label={STAT_COPY.silent}
          value={groups.silent}
          subtitle={STAT_COPY.silentHint}
          onClick={() => openDecisions({ group: "silent" })}
        />
        <StatCard
          icon={EyeOff}
          label={STAT_COPY.left}
          value={groups.left}
          subtitle={STAT_COPY.leftHint}
          iconClassName="text-default-secondary"
          onClick={() => openDecisions({ group: "left" })}
        />
        {handledElsewhere && (
          <StatCard
            icon={Forward}
            label={STAT_COPY.handledElsewhere}
            value={groups.already_handled}
            subtitle={STAT_COPY.handledElsewhereHint}
            iconClassName="text-default-secondary"
            onClick={() => openDecisions({ group: "already_handled" })}
          />
        )}
        <StatCard
          icon={AlertTriangle}
          label={STAT_COPY.failed}
          value={groups.failed}
          subtitle={STAT_COPY.failedHint}
          iconClassName={groups.failed ? "text-feedback-error-primary" : undefined}
          onClick={() => openDecisions({ group: "failed" })}
        />
      </div>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <StatCard
          icon={Clock}
          label={STAT_COPY.answerTime}
          value={activity.median_answer_seconds === null ? "-" : formatDurationShort(activity.median_answer_seconds)}
          subtitle={STAT_COPY.answerTimeHint}
        />
        <StatCard
          icon={Hourglass}
          label={STAT_COPY.approvalTime}
          value={activity.median_approval_seconds === null ? "-" : formatDurationShort(activity.median_approval_seconds)}
          subtitle={STAT_COPY.approvalTimeHint}
        />
        <StatCard
          icon={ThumbsUp}
          label={STAT_COPY.positive}
          value={activity.ratings.positive}
          subtitle={STAT_COPY.ratingsHint}
          iconClassName="text-feedback-success-primary light:text-feedback-success-primary"
        />
        <StatCard
          icon={ThumbsDown}
          label={STAT_COPY.negative}
          value={activity.ratings.negative}
          subtitle={STAT_COPY.ratingsHint}
          iconClassName={activity.ratings.negative ? "text-feedback-error-primary" : undefined}
        />
      </div>

      {activity.heard === 0 ? (
        <Card>
          <CardContent className="py-10 text-center content-base text-default-secondary">
            {t_i18n("The account heard no message over this period.")}
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-3">
            <TimelineCard activity={activity} />
            <WhereCard activity={activity} kind={kind} />
          </div>
          {/* One card per row below 1280: at 1024 two cards leave the names 74 px and the counts scroll sideways. */}
          <div className="grid gap-4 xl:grid-cols-2">
            <LeftReasonsCard activity={activity} kind={kind} onOpen={() => openDecisions({ group: "left" })} />
            <RulesCard activity={activity} />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <TopTable
              title={t_i18n("Conversations")}
              description={t_i18n("Where the account heard the most, and what it did there.")}
              icon={MessagesSquare}
              urlKey="aoc_"
              exportName="agent-account-conversations"
              rows={activity.conversations.map((row) => {
                const label = conversationLabel(row, t_i18n, kind);
                return {
                  key: `${row.conversation_type}:${row.conversation_id}`,
                  label,
                  icon: row.conversation_type === "channel" ? Hash : row.conversation_type === "group" ? Users : User,
                  heard: row.heard,
                  forAgent: row.for_agent,
                  answered: row.answered,
                  href: decisionsHref({ conversation: row.conversation_id, label }),
                };
              })}
            />
            <TopTable
              title={t_i18n("People")}
              description={t_i18n("Who the account heard the most, and what it did for them.")}
              icon={Users}
              urlKey="aop_"
              exportName="agent-account-people"
              rows={activity.senders.map((row) => ({
                key: row.sender,
                label: row.sender_name || row.sender,
                icon: User,
                heard: row.heard,
                forAgent: row.for_agent,
                answered: row.answered,
                href: decisionsHref({ sender: row.sender, label: row.sender_name ?? row.sender }),
              }))}
            />
          </div>
        </>
      )}
    </>
  );
}

/** A conversation's name (an email's, its subject); a chat without one is named after who the account talks with there. */
function conversationLabel(
  row: AgentAccountActivity["conversations"][number],
  t_i18n: ReturnType<typeof useFormatter>["t_i18n"],
  kind: AgentAccountProviderKind,
): string {
  if (row.conversation_name) return row.conversation_name;
  if (row.with_name && kind === "mailbox") return t_i18n("Email with {name}", { name: row.with_name });
  if (row.with_name && row.conversation_type === "direct") return t_i18n("Direct message with {name}", { name: row.with_name });
  if (row.with_name && row.conversation_type === "group") return t_i18n("Group chat with {name} and others", { name: row.with_name });
  const kinds = wording(kind).kinds;
  return t_i18n(kinds[row.conversation_type] ?? kinds.direct);
}

function bucketLabel(at: string, bucket: AgentAccountActivity["bucket"]): string {
  return bucket === "hour" ? formatTime(at) : formatShortDate(at);
}

/** The tooltip lists the groups as the bars stack them, the top one first. */
const stackOrder = (item: { dataKey?: unknown }) => -ACTIVITY_GROUPS.indexOf(item.dataKey as AgentAccountActivityGroup);

function TimelineCard({ activity }: { activity: AgentAccountActivity }) {
  const { t_i18n } = useFormatter();
  const [showLeft, setShowLeft] = useState(true);
  const rows = timelineRows(activity);
  const shown = activeGroups(activity).filter((group) => showLeft || group !== "left");
  return (
    <Card className="lg:col-span-2">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3 space-y-0 pb-2">
        <div className="space-y-1.5">
          <CardTitle className="text-base">{t_i18n("Activity over time")}</CardTitle>
          <CardDescription>{t_i18n("Messages heard, by what the account did with them.")}</CardDescription>
        </div>
        {activity.groups.left > 0 && (
          <label className="flex items-center gap-2 content-compact text-default-secondary">
            <Switch checked={showLeft} onCheckedChange={setShowLeft} />
            {t_i18n("Show the messages left alone")}
          </label>
        )}
      </CardHeader>
      <CardContent>
        <div className="h-[280px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} vertical={false} />
              <XAxis
                dataKey="at"
                tickFormatter={(value) => bucketLabel(String(value), activity.bucket)}
                tick={AXIS_TICK}
                stroke="var(--border-elevation-subtle)"
                minTickGap={16}
              />
              <YAxis allowDecimals={false} tickFormatter={(value) => formatCompactNumber(Number(value))} tick={AXIS_TICK} stroke="var(--border-elevation-subtle)" />
              <Tooltip
                labelFormatter={(label) => bucketLabel(String(label), activity.bucket)}
                formatter={(value, key) => [formatCompactNumber(Number(value)), t_i18n(ACTIVITY_GROUP_LABELS[key as AgentAccountActivityGroup] ?? String(key))]}
                itemSorter={stackOrder}
                {...CHART_TOOLTIP_PROPS}
                cursor={{ fill: "color-mix(in srgb, var(--text-default-primary) calc(0.05 * 100%), transparent)" }}
              />
              <Legend
                formatter={(key) => (
                  <span className="text-default-secondary">
                    {t_i18n(ACTIVITY_GROUP_LABELS[key as AgentAccountActivityGroup] ?? String(key))}
                  </span>
                )}
                itemSorter={null}
                wrapperStyle={{ fontSize: 11 }}
              />
              {shown.map((group) => (
                <Bar key={group} dataKey={group} stackId="activity" fill={GROUP_COLORS[group]} maxBarSize={36} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

function WhereCard({ activity, kind }: { activity: AgentAccountActivity; kind: AgentAccountProviderKind }) {
  const { t_i18n } = useFormatter();
  const types = wording(kind).types;
  const slices = (Object.entries(activity.by_conversation_type) as [AgentAccountConversationType, Record<string, number>][])
    .map(([type, counts]) => ({
      type,
      name: t_i18n(types[type] ?? types.direct),
      value: Object.values(counts).reduce((sum, count) => sum + count, 0),
      answered: counts.answered ?? 0,
    }))
    .filter((slice) => slice.value > 0);
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{t_i18n("Kinds of conversation")}</CardTitle>
        <CardDescription>{t_i18n("Messages heard by kind of conversation.")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="h-[180px]">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={slices} dataKey="value" nameKey="name" innerRadius={48} outerRadius={78} paddingAngle={2} stroke="none">
                {slices.map((slice) => <Cell key={slice.type} fill={TYPE_COLORS[slice.type]} />)}
              </Pie>
              <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(value) => formatCompactNumber(Number(value))} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <ul className="space-y-1.5 content-base">
          {slices.map((slice) => (
            <li key={slice.type} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <svg className="h-2.5 w-2.5" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="5" fill={TYPE_COLORS[slice.type]} /></svg>
                {slice.name}
              </span>
              <span className="text-default-secondary">
                {t_i18n("{heard} heard - {answered} answered", { heard: formatCompactNumber(slice.value), answered: formatCompactNumber(slice.answered) })}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function LeftReasonsCard({ activity, kind, onOpen }: {
  activity: AgentAccountActivity;
  kind: AgentAccountProviderKind;
  onOpen: () => void;
}) {
  const { t_i18n } = useFormatter();
  const reasons = leftReasons(activity, kind).map((reason) => ({ ...reason, name: t_i18n(reason.label) }));
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{t_i18n("Why messages were left alone")}</CardTitle>
        <CardDescription>{t_i18n("What kept the agent from answering: nobody mentioned the account, an app wrote, a rule ignores it...")}</CardDescription>
      </CardHeader>
      <CardContent>
        {reasons.length === 0 ? (
          <p className="py-8 text-center content-base text-default-secondary">{t_i18n("Every message heard was for the agent.")}</p>
        ) : (
          <div className="h-[240px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={reasons} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }} onClick={onOpen}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} horizontal={false} />
                <XAxis type="number" allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-elevation-subtle)" />
                <YAxis type="category" dataKey="name" width={180} tick={AXIS_TICK} stroke="var(--border-elevation-subtle)" />
                <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(value) => formatCompactNumber(Number(value))} cursor={{ fill: "color-mix(in srgb, var(--text-default-primary) calc(0.05 * 100%), transparent)" }} />
                <Bar dataKey="count" fill={GROUP_COLORS.left} radius={[0, 4, 4, 0]} maxBarSize={22} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

type RuleRow = AgentAccountActivity["rules"][number];

/** Which rule decided (one row per rule that matched over the period): client-side search and sort. */
function useRuleColumns() {
  return useMemo<DataTableColumn<RuleRow>[]>(
    () => [
      {
        key: "rule",
        header: "Rule",
        className: "w-full max-w-0",
        cell: (rule) => <span className="block truncate" title={rule.rule_name || rule.rule_id}>{rule.rule_name || rule.rule_id}</span>,
        sortValue: (rule) => rule.rule_name || rule.rule_id,
        csv: (rule) => rule.rule_name || rule.rule_id,
      },
      {
        key: "matched",
        header: "Matched",
        hideable: true,
        cell: (rule) => formatCompactNumber(rule.matched),
        sortValue: (rule) => rule.matched,
        csv: (rule) => rule.matched,
        align: "right",
        className: "tabular-nums",
      },
      {
        key: "answered",
        header: "Answered",
        hideable: true,
        cell: (rule) => formatCompactNumber(rule.answered),
        sortValue: (rule) => rule.answered,
        csv: (rule) => rule.answered,
        align: "right",
        className: "tabular-nums",
      },
      {
        key: "silent",
        header: "Silent",
        hideable: true,
        cell: (rule) => <span className="text-default-secondary">{formatCompactNumber(rule.silent)}</span>,
        sortValue: (rule) => rule.silent,
        csv: (rule) => rule.silent,
        align: "right",
        className: "tabular-nums",
      },
    ],
    [],
  );
}

function RulesCard({ activity }: { activity: AgentAccountActivity }) {
  const { t_i18n } = useFormatter();
  const columns = useRuleColumns();
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{t_i18n("Rules")}</CardTitle>
        <CardDescription>{t_i18n("Which rule decided, and what the agent then did.")}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          rows={activity.rules}
          columns={columns}
          rowKey={(rule) => rule.rule_id}
          defaultSort={{ key: "matched", direction: "desc" }}
          searchFields={(rule) => [rule.rule_name, rule.rule_id]}
          searchPlaceholder={t_i18n("Search...")}
          noFilters="One row per rule that matched over the period, a name and three counts: the search is the only narrowing it needs."
          exportName="agent-account-rules"
          storageKey="agent-account-rules"
          urlKey="aor_"
          framed={false}
          emptyLabel={t_i18n("No rule matched a message over this period.")}
        />
      </CardContent>
    </Card>
  );
}

interface TopRow {
  key: string;
  label: string;
  icon: React.ElementType;
  heard: number;
  forAgent: number;
  answered: number;
  /** The Decisions tab filtered on this row: the name cell is a real link (Tab, Enter, middle-click). */
  href: string;
}

/** The conversations or people the account heard the most: client-side search and sort, a row opens the Decisions tab on it. */
function useTopColumns() {
  return useMemo<DataTableColumn<TopRow>[]>(
    () => [
      {
        key: "name",
        header: "Name",
        className: "w-full max-w-0",
        cell: (row) => {
          const RowIcon = row.icon;
          return (
            <span className="flex min-w-0 items-center gap-2">
              <RowIcon className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
              <span className="truncate" title={row.label}>{row.label}</span>
            </span>
          );
        },
        sortValue: (row) => row.label,
        csv: (row) => row.label,
      },
      {
        key: "heard",
        header: "Heard",
        hideable: true,
        cell: (row) => formatCompactNumber(row.heard),
        sortValue: (row) => row.heard,
        csv: (row) => row.heard,
        align: "right",
        className: "whitespace-nowrap tabular-nums",
      },
      {
        key: "for_agent",
        header: "For the agent",
        hideable: true,
        cell: (row) => formatCompactNumber(row.forAgent),
        sortValue: (row) => row.forAgent,
        csv: (row) => row.forAgent,
        align: "right",
        className: "whitespace-nowrap tabular-nums",
        headerClassName: "whitespace-nowrap",
      },
      {
        key: "answered",
        header: "Answered",
        hideable: true,
        cell: (row) => formatCompactNumber(row.answered),
        sortValue: (row) => row.answered,
        csv: (row) => row.answered,
        align: "right",
        className: "whitespace-nowrap tabular-nums",
      },
    ],
    [],
  );
}

function TopTable({ title, description, icon: Icon, rows, urlKey, exportName }: {
  title: string;
  description: string;
  icon: React.ElementType;
  rows: TopRow[];
  urlKey: string;
  exportName: string;
}) {
  const { t_i18n } = useFormatter();
  const columns = useTopColumns();
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon className="h-4 w-4 text-accent" />
          {title}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(row) => row.key}
          defaultSort={{ key: "heard", direction: "desc" }}
          searchFields={(row) => [row.label]}
          searchPlaceholder={t_i18n("Search...")}
          noFilters="The top conversations or people of the period, a name and three counts: the search is the only narrowing it needs."
          exportName={exportName}
          urlKey={urlKey}
          storageKey={exportName}
          framed={false}
          rowHref={(row) => row.href}
          emptyLabel={t_i18n("The account heard no message over this period.")}
        />
      </CardContent>
    </Card>
  );
}
