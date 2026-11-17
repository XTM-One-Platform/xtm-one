/**
 * The pure parts of the OpenRouter console tabs - formatting, the activity
 * rollups and the catalog filter - kept free of React so `node --test` runs
 * them (`openrouter-console.test.ts`).
 */
import type { OpenRouterActivityRow, OpenRouterLimitReset, OpenRouterModel } from "@/types";
import { exactCount, exactCurrency, formatCount, formatCurrency, isCompacted } from "../../../lib/number-format.ts";

/** When a key's limit starts over, as a label; every value is a catalog key
 *  `validate-i18n` checks. @i18n-keys */
export const LIMIT_RESET_LABELS = {
  daily: "Resets every day",
  weekly: "Resets every Monday",
  monthly: "Resets every month",
} as const satisfies Record<OpenRouterLimitReset, string>;

/**
 * USD on the platform's compact rule (`lib/number-format`, #10476): the
 * precision a small spend needs ($0.0042), cents under $100, whole dollars to
 * $999, then compact ($12.3K); `exactUsd` is its hover.
 */
export function formatUsd(value: number | null | undefined, locale = "en"): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  // Zero reads in cents in the user's locale ("$0.00", fr "0,00 $US"), never a literal.
  if (value === 0) return exactCurrency(locale, "USD", 0);
  return formatCurrency(locale, "USD", value);
}

/** The exact amount behind a compacted `formatUsd` figure, for its `title`; undefined when the drawn text is already exact. */
export function exactUsd(value: number | null | undefined, locale = "en"): string | undefined {
  if (value === null || value === undefined || !Number.isFinite(value)) return undefined;
  const shown = Math.abs(value) >= 100 ? Math.round(Math.abs(value)) : Math.abs(value);
  return isCompacted(shown) ? exactCurrency(locale, "USD", value) : undefined;
}

/** A request count, compact from 1,000 ("12.3K"). */
export function formatRequests(value: number, locale = "en"): string {
  return formatCount(locale, value);
}

/** The exact request count behind a compacted `formatRequests` figure; undefined when already exact. */
export function exactRequests(value: number, locale = "en"): string | undefined {
  return isCompacted(value) ? exactCount(locale, value) : undefined;
}

/** A price per million tokens; `null` is a price OpenRouter does not state. */
export function formatPerMillion(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

/**
 * A token count on the platform's compact rule (`lib/number-format`, #10476):
 * 128K, 1M, 41.6M (at most 3 significant digits, in the reader's locale); none
 * or zero reads "—". `exactTokens` is its hover.
 */
export function formatTokens(value: number | null | undefined, locale = "en"): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return "—";
  return formatCount(locale, value);
}

/** The exact token count behind a compacted `formatTokens` figure; undefined when already exact. */
export function exactTokens(value: number | null | undefined, locale = "en"): string | undefined {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return undefined;
  return isCompacted(value) ? exactCount(locale, value) : undefined;
}

/** Spent / limit as a 0-100 share, or `null` without a limit. */
export function limitShare(limit: number | null, remaining: number | null): number | null {
  if (limit === null || limit <= 0 || remaining === null) return null;
  return Math.min(100, Math.max(0, ((limit - remaining) / limit) * 100));
}

interface ModelActivity {
  model: string;
  usage: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  providers: string[];
}

interface ActivityRollup {
  /** One point per day and model, for the stacked spend chart. */
  series: { date: string; model: string; cost: number }[];
  /** Per model, most spent first. */
  byModel: ModelActivity[];
  totals: { usage: number; requests: number; tokens: number; days: number };
}

/** The activity rows (day x model x provider) rolled up for the Usage tab. */
export function rollUpActivity(rows: readonly OpenRouterActivityRow[]): ActivityRollup {
  const perDay = new Map<string, number>();
  const perModel = new Map<string, ModelActivity & { providerSet: Set<string> }>();
  const days = new Set<string>();
  let usage = 0;
  let requests = 0;
  let tokens = 0;
  for (const row of rows) {
    days.add(row.date);
    usage += row.usage;
    requests += row.requests;
    tokens += row.prompt_tokens + row.completion_tokens + row.reasoning_tokens;
    const dayKey = `${row.date}\u0000${row.model}`;
    perDay.set(dayKey, (perDay.get(dayKey) ?? 0) + row.usage);
    const model = perModel.get(row.model) ?? {
      model: row.model,
      usage: 0,
      requests: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      reasoning_tokens: 0,
      providers: [],
      providerSet: new Set<string>(),
    };
    model.usage += row.usage;
    model.requests += row.requests;
    model.prompt_tokens += row.prompt_tokens;
    model.completion_tokens += row.completion_tokens;
    model.reasoning_tokens += row.reasoning_tokens;
    if (row.provider_name) model.providerSet.add(row.provider_name);
    perModel.set(row.model, model);
  }
  const series = [...perDay.entries()]
    .map(([dayKey, cost]) => {
      const [date, model] = dayKey.split("\u0000");
      return { date, model, cost };
    })
    .sort((a, b) => a.date.localeCompare(b.date) || a.model.localeCompare(b.model));
  const byModel = [...perModel.values()]
    .map(({ providerSet, ...model }) => ({ ...model, providers: [...providerSet].sort() }))
    .sort((a, b) => b.usage - a.usage || b.requests - a.requests || a.model.localeCompare(b.model));
  return { series, byModel, totals: { usage, requests, tokens, days: days.size } };
}

/** The authors present in the catalog, for the provider filter. */
export function catalogAuthors(models: readonly OpenRouterModel[]): string[] {
  return [...new Set(models.map((model) => model.author))].sort();
}

/** Whether two model selections differ (order does not count). */
export function selectionChanged(saved: readonly string[], current: ReadonlySet<string>): boolean {
  if (saved.length !== current.size) return true;
  return saved.some((model) => !current.has(model));
}

/**
 * The selection in the order it is saved: the models already offered keep
 * their place (the first one seeds the platform default on a first save), new
 * ones follow in catalog order.
 */
export function orderedSelection(
  saved: readonly string[],
  current: ReadonlySet<string>,
  catalogOrder: readonly string[],
): string[] {
  const kept = saved.filter((model) => current.has(model));
  const keptSet = new Set(kept);
  const added = catalogOrder.filter((model) => current.has(model) && !keptSet.has(model));
  const unknown = [...current].filter((model) => !keptSet.has(model) && !catalogOrder.includes(model)).sort();
  return [...kept, ...added, ...unknown];
}

/** `YYYY-MM-DD` (a date input) as the end of that day, UTC, ISO 8601 - or `null`. */
export function expiryFromDateInput(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  return `${value}T23:59:59Z`;
}
