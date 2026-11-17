/**
 * Unit tests for the model changes editor's selection rules -
 * `yarn test:unit`. The backend holds the same bounds in
 * `resolve_model_selection`; these keep the editor from offering what it refuses.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { ModelAnnouncementSummary, ModelCatalogChange } from "../types/index.ts";
import {
  ANNOUNCEABLE_PROVIDERS,
  blockedUntil,
  buildDirectory,
  canEmailUsers,
  compareModelIds,
  deferredRetry,
  exceedsLimit,
  filterDirectory,
  groupByProvider,
  heldUntil,
  inferProvider,
  inFlightStatus,
  isEmptySelection,
  isModelList,
  keyboardRow,
  kindOf,
  MAX_ANNOUNCED_MODELS,
  MODEL_ID_MAX_CHARS,
  naturalKind,
  parseModelList,
  resolveModel,
  sameSelection,
  selectionBody,
  selectionOf,
  selectionWarnings,
  typedModelProblem,
  viewCounts,
  withKind,
  withModel,
  withoutModel,
  type DirectoryFilter,
} from "./model-announcements.ts";

const offered = [
  { id: "gpt-5.5", provider: "openai" },
  { id: "claude-haiku-4-5", provider: "anthropic" },
  { id: "gpt-5.5-mini", provider: "openai" },
];

const change: ModelCatalogChange = {
  id: "draft",
  added_models: ["gpt-5.5"],
  removed_models: ["gpt-4o"],
  added: [{ id: "gpt-5.5", provider: "openai" }],
  removed: [{ id: "gpt-4o", provider: "openai" }],
  created_at: "2026-09-30T10:00:00+00:00",
  first_saved_at: "2026-09-30T08:00:00+00:00",
  saves: 3,
};

const labels: Record<string, string> = { openai: "OpenAI", anthropic: "Anthropic", gemini: "Google Gemini" };
const providerLabel = (provider: string) => labels[provider] ?? provider;

test("the editor starts from the saved changes, or from nothing", () => {
  assert.deepEqual(selectionOf(change), { added: change.added, removed: change.removed });
  assert.deepEqual(selectionOf(null), { added: [], removed: [] });
  assert.ok(isEmptySelection(selectionOf(null)));
  // Editing the selection never writes into the change it started from.
  const selection = selectionOf(change);
  selection.added.push({ id: "gpt-5.5-mini", provider: "openai" });
  assert.equal(change.added.length, 1);
});

test("model ids sort in reading order", () => {
  assert.deepEqual(["gpt-5.10", "gpt-5.9", "gpt-5"].sort(compareModelIds), ["gpt-5", "gpt-5.9", "gpt-5.10"]);
  // Ids differing in case only still get a stable order.
  assert.notEqual(compareModelIds("GPT-5", "gpt-5"), 0);
});

test("models are grouped by provider, the unrecorded provider last", () => {
  const groups = groupByProvider([
    { id: "legacy-model", provider: "" },
    { id: "gpt-5.5-mini", provider: "openai" },
    { id: "claude-haiku-4-5", provider: "anthropic" },
    { id: "gpt-5.5", provider: "openai" },
  ]);
  assert.deepEqual(
    groups.map((g) => [g.provider, g.models.map((m) => m.id)]),
    [
      ["anthropic", ["claude-haiku-4-5"]],
      ["openai", ["gpt-5.5", "gpt-5.5-mini"]],
      ["", ["legacy-model"]],
    ],
  );
  assert.deepEqual(groupByProvider([]), []);
});

test("adding a model twice keeps one, removing drops it", () => {
  const once = withModel([], offered[0]);
  assert.deepEqual(withModel(once, offered[0]), once);
  assert.deepEqual(withoutModel(once, "gpt-5.5"), []);
});

test("a selection is compared by the models it announces and their providers, not their order", () => {
  const a = { added: [offered[0], offered[1]], removed: [] };
  const b = { added: [offered[1], offered[0]], removed: [] };
  assert.ok(sameSelection(a, b));
  assert.ok(!sameSelection(a, { added: [offered[0]], removed: [] }));
  // The same id as new and as retired are different announcements.
  assert.ok(!sameSelection({ added: [offered[0]], removed: [] }, { added: [], removed: [offered[0]] }));
  // A retired model moved to another provider is an edit too: the email names it.
  assert.ok(
    !sameSelection(
      { added: [], removed: [{ id: "gpt-4o", provider: "openai" }] },
      { added: [], removed: [{ id: "gpt-4o", provider: "azure_openai" }] },
    ),
  );
});

test("the body names new models by id and retired ones with their provider", () => {
  assert.deepEqual(
    selectionBody({
      added: [offered[0]],
      removed: [
        { id: "gpt-4o", provider: "azure_openai" },
        { id: "legacy", provider: "" },
        // Recorded under a provider no email names: sent under none, never refused.
        { id: "bge-reranker", provider: "tei" },
      ],
    }),
    {
      added_models: ["gpt-5.5"],
      removed_models: [
        { id: "gpt-4o", provider: "azure_openai" },
        { id: "legacy", provider: "" },
        { id: "bge-reranker", provider: "" },
      ],
    },
  );
  assert.ok(!ANNOUNCEABLE_PROVIDERS.includes("tei"));
});

test("the keyboard starts on the first matching row, on a pasted list, or on a typed id nothing matches", () => {
  // `gpt-4` also finds `gpt-4o`: Enter acts on the row, it never retires the prefix.
  assert.equal(keyboardRow(null, 3, "typed"), 0);
  assert.equal(keyboardRow(null, 0, "typed"), -1);
  assert.equal(keyboardRow(null, 2, "list"), -1);
  assert.equal(keyboardRow(null, 3, null), 0);
  assert.equal(keyboardRow(null, 0, null), -1);
  // Up from the first row reaches the typed id or the pasted list, when there is one.
  assert.equal(keyboardRow(-1, 3, "typed"), -1);
  assert.equal(keyboardRow(-1, 3, "list"), -1);
  assert.equal(keyboardRow(-1, 3, null), 0);
  // A row the narrowed list no longer holds falls back to its last one.
  assert.equal(keyboardRow(5, 3, "typed"), 2);
  assert.equal(keyboardRow(1, 3, "list"), 1);
});

const directory = buildDirectory({
  offered,
  formerly: [
    { id: "gpt-5.3", provider: "openai" },
    // Offered again: stays offered.
    { id: "gpt-5.5", provider: "openai" },
  ],
  listed: [
    { id: "gemini-3-pro", provider: "gemini" },
    { id: "gpt-5.3", provider: "openai" },
  ],
  chosen: [
    { id: "legacy-model", provider: "" },
    { id: "gpt-5.5-mini", provider: "openai" },
  ],
});

test("the directory holds every source once, the first origin winning", () => {
  assert.deepEqual(
    directory.map((m) => [m.id, m.provider, m.origin]),
    [
      ["claude-haiku-4-5", "anthropic", "offered"],
      ["gemini-3-pro", "gemini", "listed"],
      ["gpt-5.3", "openai", "formerly"],
      ["gpt-5.5", "openai", "offered"],
      ["gpt-5.5-mini", "openai", "offered"],
      ["legacy-model", "", "typed"],
    ],
  );
});

test("a model is new or retired, never both", () => {
  const legacy = directory.find((m) => m.id === "legacy-model")!;
  let selection = withKind({ added: [], removed: [] }, offered[0], "new");
  assert.equal(kindOf(selection, "gpt-5.5"), "new");
  selection = withKind(selection, offered[0], "retired");
  assert.equal(kindOf(selection, "gpt-5.5"), "retired");
  assert.deepEqual(selection.added, []);
  selection = withKind(selection, legacy, "retired");
  // The directory's origin never reaches the selection.
  assert.deepEqual(selection.removed, [offered[0], { id: "legacy-model", provider: "" }]);
  selection = withKind(selection, offered[0], null);
  assert.equal(kindOf(selection, "gpt-5.5"), null);
  assert.equal(naturalKind(directory[0]), "new");
  assert.equal(naturalKind(legacy), "retired");
});

test("the directory narrows by view, provider and every word of the search", () => {
  const selection = { added: [offered[0]], removed: [{ id: "legacy-model", provider: "" }] };
  const filter: DirectoryFilter = { query: "", view: "all", provider: null, selection, providerLabel };
  const ids = (f: Partial<DirectoryFilter>) => filterDirectory(directory, { ...filter, ...f }).map((m) => m.id);
  assert.equal(ids({}).length, directory.length);
  assert.deepEqual(ids({ view: "offered" }), ["claude-haiku-4-5", "gpt-5.5", "gpt-5.5-mini"]);
  assert.deepEqual(ids({ view: "unoffered" }), ["gemini-3-pro", "gpt-5.3", "legacy-model"]);
  assert.deepEqual(ids({ view: "chosen" }), ["gpt-5.5", "legacy-model"]);
  assert.deepEqual(ids({ provider: "" }), ["legacy-model"]);
  assert.deepEqual(ids({ provider: "openai", query: "mini" }), ["gpt-5.5-mini"]);
  // A provider's display name matches, and every word must.
  assert.deepEqual(ids({ query: "  google " }), ["gemini-3-pro"]);
  assert.deepEqual(ids({ query: "openai 5.5 MINI" }), ["gpt-5.5-mini"]);
  assert.deepEqual(ids({ query: "openai gemini" }), []);
  assert.deepEqual(viewCounts(directory, { query: "gpt", provider: null, selection, providerLabel }), {
    all: 3,
    offered: 2,
    unoffered: 1,
    chosen: 1,
  });
});

test("a pasted list is split on new lines, commas, semicolons and tabs", () => {
  assert.deepEqual(parseModelList(" gpt-4o,\ngpt-4.1 ; o3\tgpt-4o\r\n \n"), ["gpt-4o", "gpt-4.1", "o3"]);
  assert.ok(isModelList("gpt-4o, gpt-4.1"));
  // One id, even with a space in it, is a search.
  assert.ok(!isModelList("my model"));
  assert.ok(!isModelList("gpt-4o,"));
});

test("an id typed by hand is filed under the provider its form points to", () => {
  assert.equal(inferProvider("claude-3-opus"), "anthropic");
  assert.equal(inferProvider(" GPT-4o "), "openai");
  assert.equal(inferProvider("o3"), "openai");
  assert.equal(inferProvider("o3-mini"), "openai");
  assert.equal(inferProvider("omni-model"), "");
  assert.equal(inferProvider("gemini-1.5-pro"), "gemini");
  assert.equal(inferProvider("codestral-2405"), "mistral");
  assert.equal(inferProvider("grok-3"), "xai");
  assert.equal(inferProvider("anthropic/claude-3.5-sonnet"), "openrouter");
  assert.equal(inferProvider("anthropic.claude-3-sonnet"), "bedrock");
  assert.equal(inferProvider("legacy-model"), "");
  // The directory's own record wins over the guess.
  assert.deepEqual(resolveModel(" gpt-5.3 ", directory), { id: "gpt-5.3", provider: "openai", origin: "formerly" });
  assert.deepEqual(resolveModel("gpt-4o", directory), { id: "gpt-4o", provider: "openai", origin: "typed" });
});

test("an id typed by hand is shaped like a model id within the backend's length (#5045)", () => {
  assert.equal(typedModelProblem("  "), null);
  assert.equal(typedModelProblem(" legacy-model "), null);
  for (const id of [
    "gpt-5.5",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "openai/gpt-4o:free",
    "llama3.1:8b-instruct-q4_K_M",
    "claude-3-5-sonnet@20240620",
    "models/gemini+exp",
  ]) {
    assert.equal(typedModelProblem(id), null, id);
  }
  assert.equal(typedModelProblem("x".repeat(MODEL_ID_MAX_CHARS)), null);
  assert.equal(typedModelProblem("x".repeat(MODEL_ID_MAX_CHARS + 1)), "too_long");
  // Counted in characters, as the backend does, not in UTF-16 units.
  assert.equal(typedModelProblem("\u{1F600}".repeat(MODEL_ID_MAX_CHARS + 1)), "too_long");
  // The email names a retired model as typed: never a sentence, markup or a line break.
  for (const id of [
    "my model",
    "Your account is suspended, visit evil.example",
    "gpt\u00a04o",
    "gpt\u200b4o",
    "gpt\t4o",
    "<b>gpt</b>",
    "[gpt](https://evil.example)",
    "-gpt-4o",
    ".gpt",
    "\u{1F600}",
    "gpt-4o!",
  ]) {
    assert.equal(typedModelProblem(id), "malformed", id);
  }
});

test("a new model the users cannot pick blocks, a retired one they still can is flagged", () => {
  const warnings = selectionWarnings(
    { added: [offered[0], { id: "gpt-9", provider: "openai" }], removed: [offered[1], { id: "gpt-4o", provider: "openai" }] },
    offered,
  );
  assert.deepEqual(warnings.noLongerOffered.map((m) => m.id), ["gpt-9"]);
  assert.deepEqual(warnings.stillOffered.map((m) => m.id), ["claude-haiku-4-5"]);
});

test("more models of one kind than the backend announces at once is flagged", () => {
  const many = Array.from({ length: MAX_ANNOUNCED_MODELS + 1 }, (_, i) => ({ id: `m${i}`, provider: "" }));
  assert.ok(exceedsLimit({ added: [], removed: many }));
  assert.ok(!exceedsLimit({ added: many.slice(1), removed: many.slice(1) }));
});

const IN_FLIGHT = {
  id: "a",
  source: "env",
  status: "sending",
  skip_reason: null,
  default_model: "claude-sonnet-5-5",
  previous_default_model: "claude-sonnet-5",
  added_models: [],
  removed_models: [],
  capabilities: [],
  created_at: "2026-09-30T09:00:00+00:00",
  sent_at: null,
  delivered: 87,
  refused: 0,
  unknown: 0,
  remaining: 9,
  queued_behind: 0,
  retry_at: null,
  failed_attempts: 0,
  last_error: null,
  held_until: null,
  blocked_until: null,
  deferred: 0,
  deferred_retry_at: null,
  deferred_retrying: false,
  last_refusal: null,
} satisfies ModelAnnouncementSummary;

const at = (iso: string) => new Date(iso).getTime();

test("addresses refused for now are retried once everyone else is emailed, at the time the backend set, or now", () => {
  const now = at("2026-09-30T09:10:00+00:00");
  assert.equal(deferredRetry(IN_FLIGHT, now), null);
  const refused = { ...IN_FLIGHT, deferred: 9 };
  // Others still to email: no time yet.
  assert.deepEqual(deferredRetry(refused, now), { count: 9, retry: { when: "after_others" } });
  // Everyone else emailed: the one retry, ahead, then due.
  const waiting = { ...refused, deferred_retry_at: "2026-09-30T09:20:00+00:00" };
  assert.deepEqual(deferredRetry(waiting, now), {
    count: 9,
    retry: { when: "at", time: "2026-09-30T09:20:00+00:00" },
  });
  assert.deepEqual(deferredRetry(waiting, at("2026-09-30T09:20:00+00:00")), { count: 9, retry: { when: "now" } });
  // A retry that takes several batches is under way, never "once everyone else is emailed".
  assert.deepEqual(deferredRetry({ ...refused, deferred_retrying: true }, now), { count: 9, retry: { when: "now" } });
});

test("an announcement being emailed says how far it got, why it waits and what is left", () => {
  const now = at("2026-09-30T09:10:00+00:00");
  assert.deepEqual(inFlightStatus({ ...IN_FLIGHT, remaining: 40 }, 20, now), {
    kind: "emailing",
    done: 87,
    total: 127,
    paused: null,
    minutesLeft: 2,
    deferred: null,
  });
  // Only the retries left, waiting for their time: no estimate, the retry instead.
  const retryWait = { ...IN_FLIGHT, deferred: 9, deferred_retry_at: "2026-09-30T09:20:00+00:00" };
  assert.deepEqual(inFlightStatus(retryWait, 20, now), {
    kind: "emailing",
    done: 87,
    total: 96,
    paused: null,
    minutesLeft: null,
    deferred: { count: 9, retry: { when: "at", time: "2026-09-30T09:20:00+00:00" } },
  });
  // Paused after a server error, with addresses refused for now besides: both, and no estimate.
  const paused = { ...IN_FLIGHT, remaining: 40, deferred: 3, retry_at: "2026-09-30T09:12:00+00:00", failed_attempts: 1 };
  assert.deepEqual(inFlightStatus(paused, 20, now), {
    kind: "emailing",
    done: 87,
    total: 127,
    paused: { when: "at", time: "2026-09-30T09:12:00+00:00" },
    minutesLeft: null,
    deferred: { count: 3, retry: { when: "after_others" } },
  });
  assert.deepEqual(inFlightStatus(paused, 20, at("2026-09-30T09:12:00+00:00")), {
    kind: "emailing",
    done: 87,
    total: 127,
    paused: { when: "now" },
    minutesLeft: null,
    deferred: { count: 3, retry: { when: "after_others" } },
  });
  // Queued behind an older one, or held: nothing about its own progress.
  assert.deepEqual(inFlightStatus({ ...paused, queued_behind: 2, blocked_until: "2026-09-30T09:15:00+00:00" }, 20, now), {
    kind: "queued",
    count: 2,
    blockedUntil: "2026-09-30T09:15:00+00:00",
  });
  assert.deepEqual(inFlightStatus({ ...IN_FLIGHT, held_until: "2026-09-30T09:30:00+00:00" }, 20, now), {
    kind: "held",
    until: "2026-09-30T09:30:00+00:00",
  });
});

test("an env change is held only until the time the backend set", () => {
  const latest = {
    id: "a",
    source: "env",
    status: "pending",
    skip_reason: null,
    default_model: "claude-sonnet-5-5",
    previous_default_model: "claude-sonnet-5",
    added_models: [],
    removed_models: [],
    capabilities: [],
    created_at: "2026-09-30T09:00:00+00:00",
    sent_at: null,
    delivered: 0,
    refused: 0,
    unknown: 0,
    remaining: 3,
    queued_behind: 0,
    retry_at: null,
    failed_attempts: 0,
    last_error: null,
    held_until: "2026-09-30T09:30:00+00:00",
    blocked_until: "2026-09-30T09:20:00+00:00",
    deferred: 0,
    deferred_retry_at: null,
    deferred_retrying: false,
    last_refusal: null,
  } satisfies ModelAnnouncementSummary;
  assert.equal(heldUntil(latest, at("2026-09-30T09:10:00+00:00")), "2026-09-30T09:30:00+00:00");
  assert.equal(heldUntil(latest, at("2026-09-30T09:30:00+00:00")), null);
  assert.equal(heldUntil({ ...latest, held_until: null }, at("2026-09-30T09:10:00+00:00")), null);
  // Behind an older one: its next attempt, until it is due.
  assert.equal(blockedUntil(latest, at("2026-09-30T09:10:00+00:00")), "2026-09-30T09:20:00+00:00");
  assert.equal(blockedUntil(latest, at("2026-09-30T09:20:00+00:00")), null);
  assert.equal(blockedUntil({ ...latest, blocked_until: null }, at("2026-09-30T09:10:00+00:00")), null);
});

test("canEmailUsers needs SMTP and the emails not turned off platform-wide", () => {
  assert.equal(canEmailUsers({ email_configured: true, emails_disabled: false }), true);
  assert.equal(canEmailUsers({ email_configured: true, emails_disabled: true }), false);
  assert.equal(canEmailUsers({ email_configured: false, emails_disabled: false }), false);
  assert.equal(canEmailUsers({ email_configured: false, emails_disabled: true }), false);
  assert.equal(canEmailUsers(undefined), false);
  assert.equal(canEmailUsers(null), false);
});
