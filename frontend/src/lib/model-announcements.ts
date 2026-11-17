// SPDX-License-Identifier: Apache-2.0

import type { CatalogModelRef, ModelAnnouncementStatus, ModelAnnouncementSummary, ModelCatalogChange, RetiredModelChoice } from "@/types";

/** Whether Settings may offer to email the users: SMTP configured and the emails not turned off platform-wide. */
export function canEmailUsers(status: Pick<ModelAnnouncementStatus, "email_configured" | "emails_disabled"> | null | undefined): boolean {
  return !!status?.email_configured && !status.emails_disabled;
}

/** The users an announcement reached so far, and all it goes to. */
export function deliveryCounts(latest: ModelAnnouncementSummary): { done: number; total: number } {
  const done = latest.delivered + latest.refused + latest.unknown;
  return { done, total: done + latest.remaining };
}

/** How long the users still to email take at the scheduler's pace, one batch a minute. */
export function minutesLeft(remaining: number, emailsPerMinute: number): number {
  return Math.max(1, Math.ceil(remaining / Math.max(1, emailsPerMinute)));
}

function ahead(time: string | null, now: number): string | null {
  return time && new Date(time).getTime() > now ? time : null;
}

/** An env change still waiting for the deployed models to stop changing, or `null` once it may go out. */
export function heldUntil(latest: ModelAnnouncementSummary, now: number = Date.now()): string | null {
  return ahead(latest.held_until, now);
}

/** When the announcement it is queued behind runs next, or `null` when that one is not waiting. */
export function blockedUntil(latest: ModelAnnouncementSummary, now: number = Date.now()): string | null {
  return ahead(latest.blocked_until, now);
}

/** When something an announcement waits for runs: at a time still ahead, or now. */
type NextRun = { when: "at"; time: string } | { when: "now" };

function nextRun(time: string, now: number): NextRun {
  return ahead(time, now) ? { when: "at", time } : { when: "now" };
}

/** The addresses the email server refused for now, and when they are tried again. */
export interface DeferredRetry {
  count: number;
  retry: NextRun | { when: "after_others" };
}

/** The addresses the email server refused for now and their retry, or `null` when there are none. */
export function deferredRetry(latest: ModelAnnouncementSummary, now: number = Date.now()): DeferredRetry | null {
  if (latest.deferred === 0) return null;
  const count = latest.deferred;
  if (latest.deferred_retrying) return { count, retry: { when: "now" } };
  if (!latest.deferred_retry_at) return { count, retry: { when: "after_others" } };
  return { count, retry: nextRun(latest.deferred_retry_at, now) };
}

/** An announcement being emailed: what holds it back, or how far it got and what is left. */
type InFlightStatus =
  | { kind: "queued"; count: number; blockedUntil: string | null }
  | { kind: "held"; until: string }
  | {
      kind: "emailing";
      done: number;
      total: number;
      /** Paused after an email server error: the next attempt. */
      paused: NextRun | null;
      /** Minutes the users still to email take, when worth saying. */
      minutesLeft: number | null;
      deferred: DeferredRetry | null;
    };

export function inFlightStatus(
  latest: ModelAnnouncementSummary,
  emailsPerMinute: number,
  now: number = Date.now(),
): InFlightStatus {
  // Its own hold is no time to show: nothing goes out before the oldest one is through.
  if (latest.queued_behind > 0) {
    return { kind: "queued", count: latest.queued_behind, blockedUntil: blockedUntil(latest, now) };
  }
  const held = heldUntil(latest, now);
  if (held) return { kind: "held", until: held };
  const { done, total } = deliveryCounts(latest);
  const paused = latest.retry_at ? nextRun(latest.retry_at, now) : null;
  // No estimate while paused, nor while only the retries are left, waiting for their time.
  const minutes = paused || latest.remaining === 0 || latest.deferred_retry_at
    ? null
    : minutesLeft(latest.remaining, emailsPerMinute);
  return { kind: "emailing", done, total, paused, minutesLeft: minutes, deferred: deferredRetry(latest, now) };
}

// The bounds the backend holds a chosen model to (`resolve_model_selection`).
export const MAX_ANNOUNCED_MODELS = 100;
export const MODEL_ID_MAX_CHARS = 200;
// What a model id looks like: `MODEL_ID_PATTERN` in the backend (#5045), so a
// retired model typed by hand never carries a sentence into the email.
const MODEL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;

/** What a model changes email announces, as the admin edits it. */
export interface ModelSelection {
  added: CatalogModelRef[];
  removed: CatalogModelRef[];
}

/** The saved changes as the email would announce them untouched; nothing for an announcement of the admin's own. */
export function selectionOf(change: ModelCatalogChange | null): ModelSelection {
  return { added: [...(change?.added ?? [])], removed: [...(change?.removed ?? [])] };
}

/** Model ids in reading order: `gpt-5.10` after `gpt-5.9`. */
export function compareModelIds(a: string, b: string): number {
  return a.localeCompare(b, "en", { numeric: true, sensitivity: "base" }) || (a < b ? -1 : a > b ? 1 : 0);
}

/** Providers by key, the unrecorded one (`""`) last. */
function compareProviders(a: string, b: string): number {
  return a === b ? 0 : a === "" ? 1 : b === "" ? -1 : a < b ? -1 : 1;
}

function compareModels(a: CatalogModelRef, b: CatalogModelRef): number {
  return compareProviders(a.provider, b.provider) || compareModelIds(a.id, b.id);
}

/** Models grouped under their provider: providers by key, the unrecorded one (`""`) last, models in reading order. */
export function groupByProvider<T extends CatalogModelRef>(models: readonly T[]): { provider: string; models: T[] }[] {
  const groups = new Map<string, T[]>();
  for (const model of models) {
    const group = groups.get(model.provider);
    if (group) group.push(model);
    else groups.set(model.provider, [model]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => compareProviders(a, b))
    .map(([provider, list]) => ({ provider, models: [...list].sort((a, b) => compareModelIds(a.id, b.id)) }));
}

export function withModel(models: readonly CatalogModelRef[], model: CatalogModelRef): CatalogModelRef[] {
  return models.some((m) => m.id === model.id) ? [...models] : [...models, model];
}

export function withoutModel(models: readonly CatalogModelRef[], id: string): CatalogModelRef[] {
  return models.filter((m) => m.id !== id);
}

/** Whether two selections announce the same models under the same providers (order aside). */
export function sameSelection(a: ModelSelection, b: ModelSelection): boolean {
  const keys = (models: CatalogModelRef[]) => models.map((m) => `${m.id}\n${m.provider}`).sort().join("\n\n");
  return keys(a.added) === keys(b.added) && keys(a.removed) === keys(b.removed);
}

export function isEmptySelection(selection: ModelSelection): boolean {
  return selection.added.length === 0 && selection.removed.length === 0;
}

export function exceedsLimit(selection: ModelSelection): boolean {
  return selection.added.length > MAX_ANNOUNCED_MODELS || selection.removed.length > MAX_ANNOUNCED_MODELS;
}

/**
 * The providers an email can name: `ANNOUNCEABLE_PROVIDERS` in the backend
 * (pinned by `test_model_catalog_announcements.py`), every chat provider and
 * never the embedding server.
 */
export const ANNOUNCEABLE_PROVIDERS: readonly string[] = [
  "anthropic",
  "openai",
  "gemini",
  "openrouter",
  "ollama",
  "mistral",
  "xai",
  "azure_openai",
  "bedrock",
  "litellm",
  "custom_openai",
  "filigran_gateway",
  "lmstudio",
];

/**
 * The request body: new models by id (the backend names their provider),
 * retired ones with the provider the admin saw when an email can name it (a
 * model recorded under another one goes out under none).
 */
export function selectionBody(selection: ModelSelection): { added_models: string[]; removed_models: RetiredModelChoice[] } {
  return {
    added_models: selection.added.map((m) => m.id),
    removed_models: selection.removed.map((m) => ({
      id: m.id,
      provider: ANNOUNCEABLE_PROVIDERS.includes(m.provider) ? m.provider : "",
    })),
  };
}

// ---------------------------------------------------------------------------
// The model directory the editor picks from
// ---------------------------------------------------------------------------

/**
 * Where the editor found a model: `offered` the users can pick it now,
 * `formerly` they could once, `listed` a configured provider serves it but it
 * is not offered, `typed` the admin entered it by hand.
 */
export type ModelOrigin = "offered" | "formerly" | "listed" | "typed";

export interface DirectoryModel extends CatalogModelRef {
  origin: ModelOrigin;
}

/**
 * Every model the admin can announce, once per id: what the users can pick,
 * what they could once, what the providers list, then the chosen models none
 * of those holds. An id found twice keeps its first origin in that order.
 */
export function buildDirectory(sources: {
  offered: readonly CatalogModelRef[];
  formerly: readonly CatalogModelRef[];
  listed: readonly CatalogModelRef[];
  chosen: readonly CatalogModelRef[];
}): DirectoryModel[] {
  const byId = new Map<string, DirectoryModel>();
  const take = (models: readonly CatalogModelRef[], origin: ModelOrigin) => {
    for (const { id, provider } of models) {
      if (!byId.has(id)) byId.set(id, { id, provider, origin });
    }
  };
  take(sources.offered, "offered");
  take(sources.formerly, "formerly");
  take(sources.listed, "listed");
  take(sources.chosen, "typed");
  return [...byId.values()].sort(compareModels);
}

export type AnnouncedKind = "new" | "retired";

export function kindOf(selection: ModelSelection, id: string): AnnouncedKind | null {
  if (selection.added.some((m) => m.id === id)) return "new";
  if (selection.removed.some((m) => m.id === id)) return "retired";
  return null;
}

/** The selection with `model` announced as `kind` only, or not at all for `null`: a model is never both new and retired. */
export function withKind(selection: ModelSelection, model: CatalogModelRef, kind: AnnouncedKind | null): ModelSelection {
  const ref = { id: model.id, provider: model.provider };
  const added = withoutModel(selection.added, model.id);
  const removed = withoutModel(selection.removed, model.id);
  if (kind === "new") return { added: [...added, ref], removed };
  if (kind === "retired") return { added, removed: [...removed, ref] };
  return { added, removed };
}

/** What choosing a model without saying how means: new when the users can pick it, retired otherwise. */
export function naturalKind(model: DirectoryModel): AnnouncedKind {
  return model.origin === "offered" ? "new" : "retired";
}

/** Which models the directory shows: all, the ones users can pick, the ones they cannot, the ones in the email. */
export type DirectoryView = "all" | "offered" | "unoffered" | "chosen";

export const DIRECTORY_VIEWS: readonly DirectoryView[] = ["all", "offered", "unoffered", "chosen"];

function inView(model: DirectoryModel, view: DirectoryView, selection: ModelSelection): boolean {
  switch (view) {
    case "offered":
      return model.origin === "offered";
    case "unoffered":
      return model.origin !== "offered";
    case "chosen":
      return kindOf(selection, model.id) !== null;
    default:
      return true;
  }
}

/** Whether every word of `query` is in the model id, its provider key or its provider's name (case-insensitive). */
function matchesQuery(model: CatalogModelRef, query: string, providerLabel: (provider: string) => string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = `${model.id}\n${model.provider}\n${providerLabel(model.provider)}`.toLowerCase();
  return words.every((word) => haystack.includes(word));
}

export interface DirectoryFilter {
  query: string;
  view: DirectoryView;
  /** A provider key, `""` the models with none recorded, `null` every provider. */
  provider: string | null;
  selection: ModelSelection;
  providerLabel: (provider: string) => string;
}

function matchesSearch(model: DirectoryModel, filter: DirectoryFilter): boolean {
  return (filter.provider === null || model.provider === filter.provider) && matchesQuery(model, filter.query, filter.providerLabel);
}

/** The directory as the admin narrowed it, in directory order. */
export function filterDirectory(models: readonly DirectoryModel[], filter: DirectoryFilter): DirectoryModel[] {
  return models.filter((m) => inView(m, filter.view, filter.selection) && matchesSearch(m, filter));
}

/** How many models each view would show under the current search and provider. */
export function viewCounts(models: readonly DirectoryModel[], filter: Omit<DirectoryFilter, "view">): Record<DirectoryView, number> {
  const counts: Record<DirectoryView, number> = { all: 0, offered: 0, unoffered: 0, chosen: 0 };
  for (const model of models) {
    if (!matchesSearch(model, { ...filter, view: "all" })) continue;
    for (const view of DIRECTORY_VIEWS) {
      if (inView(model, view, filter.selection)) counts[view] += 1;
    }
  }
  return counts;
}

// ---------------------------------------------------------------------------
// Models typed or pasted by hand
// ---------------------------------------------------------------------------

const LIST_SEPARATORS = /[\r\n,;\t]+/;

/** Whether the search text is a list of ids (pasted from a changelog, a spreadsheet...) rather than a search. */
export function isModelList(text: string): boolean {
  return parseModelList(text).length > 1;
}

/**
 * The directory row the keyboard is on, `-1` for the typed or pasted models
 * above the list (nothing when there are none). `moved` is where the admin
 * took it with the arrows, `null` until they do: it then starts on a pasted
 * list, and for a typed id on the first row matching it - `gpt-4` also finds
 * `gpt-4o`, and Enter must not retire the prefix - unless no row does.
 */
export function keyboardRow(moved: number | null, rows: number, panel: "typed" | "list" | null): number {
  if (rows === 0) return -1;
  if (moved === null) return panel === "list" ? -1 : 0;
  if (moved < 0) return panel ? -1 : 0;
  return Math.min(moved, rows - 1);
}

/** The ids of a list separated by new lines, commas, semicolons or tabs: trimmed, once each, in order. */
export function parseModelList(text: string): string[] {
  const ids: string[] = [];
  for (const part of text.split(LIST_SEPARATORS)) {
    const id = part.trim();
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

// The provider an id's form points to, after `_MODEL_PROVIDER_PREFIXES` in `backend/app/llm/router.py`.
const PROVIDER_PATTERNS: readonly [RegExp, string][] = [
  [/^(anthropic|meta|mistral|amazon|cohere|ai21)\./, "bedrock"],
  [/\//, "openrouter"],
  [/^claude-/, "anthropic"],
  [/^(gpt-|chatgpt-|o\d(-|$))/, "openai"],
  [/^(gemini-|gemma-)/, "gemini"],
  [/^(mistral-|open-mistral-|open-mixtral-|magistral-|codestral-|ministral-|devstral-|pixtral-)/, "mistral"],
  [/^grok-/, "xai"],
];

/** The provider a model id typed by hand most likely belongs to, `""` when its form says nothing. */
export function inferProvider(id: string): string {
  const lower = id.trim().toLowerCase();
  return PROVIDER_PATTERNS.find(([pattern]) => pattern.test(lower))?.[1] ?? "";
}

/** An id as the directory knows it, or typed by hand under the provider its form points to. */
export function resolveModel(id: string, directory: readonly DirectoryModel[]): DirectoryModel {
  const trimmed = id.trim();
  return directory.find((m) => m.id === trimmed) ?? { id: trimmed, provider: inferProvider(trimmed), origin: "typed" };
}

type TypedModelProblem = "too_long" | "malformed";

/** Why an id typed by hand cannot be announced, or `null` (a blank id is simply not added). */
export function typedModelProblem(raw: string): TypedModelProblem | null {
  const id = raw.trim();
  if (!id) return null;
  if ([...id].length > MODEL_ID_MAX_CHARS) return "too_long";
  if (!MODEL_ID_PATTERN.test(id)) return "malformed";
  return null;
}

/** The chosen models a kind can no longer carry: new ones the users cannot pick (blocking), retired ones they still can (a warning). */
export function selectionWarnings(
  selection: ModelSelection,
  offered: readonly CatalogModelRef[],
): { noLongerOffered: CatalogModelRef[]; stillOffered: CatalogModelRef[] } {
  const offeredIds = new Set(offered.map((m) => m.id));
  return {
    noLongerOffered: selection.added.filter((m) => !offeredIds.has(m.id)),
    stillOffered: selection.removed.filter((m) => offeredIds.has(m.id)),
  };
}
