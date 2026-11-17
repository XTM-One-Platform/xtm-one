import { lazy, Suspense, useMemo } from "react";
import { Link } from "react-router-dom";
import { Activity, CalendarDays, Coins, CreditCard, KeyRound, Wallet } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { StatCard } from "@/components/ui/stat-card";
import { Tip } from "@/components/ui/tooltip";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useOpenRouterActivity, useOpenRouterOverview } from "@/hooks/api/useOpenRouterApi";
import { CompactFigure } from "@/components/ui/compact-figure";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { exactCurrency } from "@/lib/number-format";
import { formatDate } from "@/lib/utils";
import type { Integration, OpenRouterConsoleError, OpenRouterKeyInfo } from "@/types";
import { LIMIT_RESET_LABELS, exactUsd, formatUsd, limitShare, rollUpActivity } from "./openrouter-console";

type ModelRow = ReturnType<typeof rollUpActivity>["byModel"][number];

type NumberFormat = ReturnType<typeof useNumberFormat>;

/**
 * Width floor of the Model column. The table lays out fixed so the model name keeps
 * this width and the figure columns share the rest, instead of `auto` shrinking the
 * names to their minimum (UX r1 B1 on #10550; the DevelopmentTab precedent).
 */
export const MODEL_COLUMN_WIDTH = 240;

function Figure({ text, exact, strong = false }: { text: string; exact: string; strong?: boolean }) {
  return (
    <CompactFigure exact={exact} className={strong ? "font-medium" : undefined}>
      {text}
    </CompactFigure>
  );
}

/** The activity columns; figures compact in the reader's locale, exact on hover (UX r1 B2 on #10550). */
function modelColumns(nf: NumberFormat): DataTableColumn<ModelRow>[] {
  const outputTokens = (m: ModelRow) => m.completion_tokens + m.reasoning_tokens;
  return [
    {
      key: "model",
      header: "Model",
      width: MODEL_COLUMN_WIDTH,
      cell: (m) => (
        <Tip content={m.model}>
          <span className="block truncate font-mono-plex">{m.model}</span>
        </Tip>
      ),
      sortValue: (m) => m.model.toLowerCase(),
      firstSort: "asc",
      csv: (m) => m.model,
    },
    {
      key: "providers",
      header: "Providers",
      className: "max-w-0",
      hideBelow: "4xl",
      hideable: true,
      cell: (m) => {
        const text = m.providers.join(", ") || "—";
        return (
          <Tip content={text}>
            <span className="block truncate text-default-secondary">{text}</span>
          </Tip>
        );
      },
      sortValue: (m) => m.providers.join(", ").toLowerCase(),
      csv: (m) => m.providers.join(", "),
    },
    {
      key: "requests",
      header: "Requests",
      align: "right",
      hideable: true,
      cell: (m) => <Figure text={nf.count(m.requests)} exact={nf.exactCount(m.requests)} />,
      sortValue: (m) => m.requests,
      csv: (m) => m.requests,
    },
    {
      key: "input",
      header: "Input tokens",
      align: "right",
      hideable: true,
      cell: (m) => <Figure text={nf.count(m.prompt_tokens)} exact={nf.exactCount(m.prompt_tokens)} />,
      sortValue: (m) => m.prompt_tokens,
      csv: (m) => m.prompt_tokens,
    },
    {
      key: "output",
      header: "Output tokens",
      align: "right",
      hideable: true,
      cell: (m) => <Figure text={nf.count(outputTokens(m))} exact={nf.exactCount(outputTokens(m))} />,
      sortValue: outputTokens,
      csv: outputTokens,
    },
    {
      key: "usage",
      header: "Spent",
      align: "right",
      cell: (m) => <Figure text={nf.money(m.usage)} exact={nf.exactMoney(m.usage)} strong />,
      sortValue: (m) => m.usage,
      csv: (m) => m.usage,
    },
  ];
}

const MODEL_FILTERS: DataTableFilter<ModelRow>[] = [
  { key: "provider", label: "Providers", values: (m) => m.providers },
];

const CostOverTimeChart = lazy(() => import("@/components/ui/charts/cost-over-time-chart"));

/** A part of the page OpenRouter refused: its fixed sentence, in place of the figures. */
export function ConsoleErrorNote({ error }: { error: OpenRouterConsoleError | { message: string } }) {
  return (
    <p className="rounded-sm bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary">{error.message}</p>
  );
}

/** Where the management key is entered, for a part of the console that needs one. */
export function ManagementKeyNeeded({ integration, what }: { integration: Integration; what: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-4 content-base text-default-secondary space-y-3">
      <p>{t_i18n(what)}</p>
      <p className="content-compact">
        {t_i18n("OpenRouter keeps this behind a management key, which cannot run models. Create one in OpenRouter > Settings > Management keys and paste it in the Management key field of this integration.")}
      </p>
      <Button asChild variant="secondary" size="sm">
        <Link to={`/integrations/${integration.id}/config`}>{t_i18n("Open Configuration")}</Link>
      </Button>
    </div>
  );
}

function KeyCard({ info }: { info: OpenRouterKeyInfo }) {
  const { t_i18n, intl } = useFormatter();
  const usd = (value: number | null | undefined) => formatUsd(value, intl.locale);
  const share = limitShare(info.limit, info.limit_remaining);
  const free = info.free_model_daily_requests;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <KeyRound className="h-4 w-4 text-accent" />
          {t_i18n("API key")}
        </CardTitle>
        <CardDescription className="font-mono-plex content-compact">{info.label ?? "—"}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 content-base">
        <div className="flex flex-wrap gap-2">
          {info.is_free_tier && <Badge variant="warning">{t_i18n("Free tier")}</Badge>}
          {info.limit_reset && (
            <Badge variant="outline">{t_i18n(LIMIT_RESET_LABELS[info.limit_reset])}</Badge>
          )}
          {info.expires_at && (
            <Badge variant="outline">{t_i18n("Expires {date}", { date: formatDate(info.expires_at) })}</Badge>
          )}
          {info.allowed_data_regions.length > 0 && (
            <Badge variant="outline">{t_i18n("Regions: {regions}", { regions: info.allowed_data_regions.join(", ") })}</Badge>
          )}
        </div>
        {info.limit === null ? (
          <p className="content-compact text-default-secondary">{t_i18n("No spending limit on this key.")}</p>
        ) : (
          <div className="space-y-1.5">
            <div className="flex justify-between content-compact">
              <span className="text-default-secondary">{t_i18n("Spending limit")}</span>
              <span
                title={
                  exactUsd(info.limit_remaining, intl.locale) || exactUsd(info.limit, intl.locale)
                    ? t_i18n("{remaining} left of {limit}", {
                        remaining: info.limit_remaining === null ? "—" : exactCurrency(intl.locale, "USD", info.limit_remaining),
                        limit: exactCurrency(intl.locale, "USD", info.limit),
                      })
                    : undefined
                }
              >
                {t_i18n("{remaining} left of {limit}", {
                  remaining: usd(info.limit_remaining),
                  limit: usd(info.limit),
                })}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-elevation-hover">
              <div
                className={share !== null && share >= 90 ? "h-full bg-feedback-error-primary" : share !== null && share >= 70 ? "h-full bg-feedback-alert-primary" : "h-full bg-accent"}
                style={{ width: `${share ?? 0}%` }}
              />
            </div>
          </div>
        )}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 content-compact">
          <dt className="text-default-secondary">{t_i18n("Spent since creation")}</dt>
          <dd className="text-right" title={exactUsd(info.usage, intl.locale)}>{usd(info.usage)}</dd>
          <dt className="text-default-secondary">{t_i18n("Bring-your-own-key spend this month")}</dt>
          <dd className="text-right" title={exactUsd(info.byok_usage_monthly, intl.locale)}>{usd(info.byok_usage_monthly)}</dd>
          {free && free.limit !== null && (
            <>
              <dt className="text-default-secondary">{t_i18n("Free model requests today")}</dt>
              <dd className="text-right">{t_i18n("{used} of {limit}", { used: free.used ?? 0, limit: free.limit })}</dd>
            </>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}

function ActivityCard({ integration, hasManagementKey }: { integration: Integration; hasManagementKey: boolean }) {
  const { t_i18n } = useFormatter();
  const activity = useOpenRouterActivity(integration.id, hasManagementKey);
  const rollup = useMemo(() => rollUpActivity(activity.data?.rows ?? []), [activity.data]);
  const nf = useNumberFormat();
  const columns = useMemo(() => modelColumns(nf), [nf]);
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Activity className="h-4 w-4 text-accent" />
          {t_i18n("Activity, last 30 days")}
        </CardTitle>
        <CardDescription>
          {t_i18n("Spend per day and model across every key of the account, as OpenRouter reports it for the last 30 completed days (UTC).")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {!hasManagementKey ? (
          <ManagementKeyNeeded integration={integration} what="The activity per model needs a management key." />
        ) : activity.error ? (
          <ConsoleErrorNote error={activity.error as Error} />
        ) : (
          <>
            <div className="grid grid-cols-3 gap-3 text-center">
              <div>
                <p className="text-lg font-semibold">
                  <CompactFigure exact={nf.exactMoney(rollup.totals.usage)}>{nf.money(rollup.totals.usage)}</CompactFigure>
                </p>
                <p className="content-caption uppercase tracking-wider text-default-secondary">{t_i18n("Spent")}</p>
              </div>
              <div>
                <p className="text-lg font-semibold">
                  <CompactFigure exact={nf.exactCount(rollup.totals.requests)}>{nf.count(rollup.totals.requests)}</CompactFigure>
                </p>
                <p className="content-caption uppercase tracking-wider text-default-secondary">{t_i18n("Requests")}</p>
              </div>
              <div>
                <p className="text-lg font-semibold">
                  <CompactFigure exact={nf.exactCount(rollup.totals.tokens)}>{nf.count(rollup.totals.tokens)}</CompactFigure>
                </p>
                <p className="content-caption uppercase tracking-wider text-default-secondary">{t_i18n("Tokens")}</p>
              </div>
            </div>
            <Suspense fallback={<div className="h-[280px]" />}>
              <CostOverTimeChart isLoading={activity.isLoading} series={rollup.series} />
            </Suspense>
            {rollup.byModel.length > 0 && (
              <DataTable
                rows={rollup.byModel}
                columns={columns}
                layout="fixed"
                rowKey={(m) => m.model}
                defaultSort={{ key: "usage", direction: "desc" }}
                searchFields={(m) => [m.model, ...m.providers]}
                searchPlaceholder={t_i18n("Search models...")}
                filters={MODEL_FILTERS}
                exportName="openrouter-activity-by-model"
                urlKey="orm_"
                storageKey="openrouter-activity-by-model"
                emptyLabel={t_i18n("No activity yet")}
                rowTestId="openrouter-model-row"
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// OpenRouter usage - sub-page under /integrations/:id/usage: what the key has
// spent, what the account has left, and the activity per model.
// ---------------------------------------------------------------------------
export function OpenRouterUsageTab({ integration }: { integration: Integration }) {
  const { t_i18n, intl } = useFormatter();
  const usd = (value: number | null | undefined) => formatUsd(value, intl.locale);
  // The exact amount on hover once compacted; StatCard keeps its own title otherwise.
  const exact = (value: number | null | undefined) => exactUsd(value, intl.locale);
  const aiOff = useAiActionDisabled();
  const overview = useOpenRouterOverview(integration.id, !aiOff);

  if (aiOff) return <AiDisabledNotice />;
  if (overview.isLoading) return <PageSkeleton cards={3} />;
  if (overview.error || !overview.data) {
    return <ConsoleErrorNote error={(overview.error as Error) ?? { message: t_i18n("The OpenRouter account could not be read.") }} />;
  }
  const { key, key_error, credits, credits_error, has_management_key, has_api_key } = overview.data;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard icon={Coins} label="Spent today" value={usd(key?.usage_daily)} exact={exact(key?.usage_daily)} subtitle="This key, UTC day" />
        <StatCard icon={CalendarDays} label="This week" value={usd(key?.usage_weekly)} exact={exact(key?.usage_weekly)} subtitle="This key, since Monday" />
        <StatCard icon={Wallet} label="This month" value={usd(key?.usage_monthly)} exact={exact(key?.usage_monthly)} subtitle="This key, this UTC month" />
        <StatCard
          icon={CreditCard}
          label="Credits left"
          value={credits ? usd(credits.remaining) : "—"}
          exact={credits ? exact(credits.remaining) : undefined}
          subtitle={
            credits
              ? t_i18n("{used} used of {total}", { used: usd(credits.total_usage), total: usd(credits.total_credits) })
              : has_management_key
                ? "Could not be read"
                : "Needs a management key"
          }
        />
      </div>

      {!has_api_key ? (
        <ConsoleErrorNote error={{ message: t_i18n("Add the integration's OpenRouter API key in the Configuration tab first.") }} />
      ) : key_error ? (
        <ConsoleErrorNote error={key_error} />
      ) : key ? (
        <KeyCard info={key} />
      ) : null}
      {credits_error && <ConsoleErrorNote error={credits_error} />}

      <ActivityCard integration={integration} hasManagementKey={has_management_key} />
    </div>
  );
}
