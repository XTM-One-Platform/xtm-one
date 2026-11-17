/**
 * Unit tests for the provider-deletion wording - `yarn test:unit`.
 *
 * The confirmation must say, in plain words, what each route that used the
 * provider becomes - and must not claim a consequence the backend did not
 * report (a cleared image default that never routed anything, a fallback
 * embedding model that is in fact the same one).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { LLMProviderDeletionImpact, ProviderDeletionRoute, ProviderRouteTarget } from "@/types";
import { deletionNeedsReplacement, describeProviderDeletion } from "./provider-deletion.ts";

const target = (overrides: Partial<ProviderRouteTarget> = {}): ProviderRouteTarget => ({
  provider: "anthropic",
  provider_id: "anthropic-1",
  display_name: "Claude",
  model: "claude-sonnet-5",
  ...overrides,
});

const deleted = target({ provider: "openai", provider_id: "openai-1", display_name: "Production", model: "gpt-5.1" });

const unchanged = (before: ProviderRouteTarget | null = null): ProviderDeletionRoute => ({
  affected: false,
  action: "unchanged",
  before,
  after: before,
  env_managed: false,
});

const impact = (overrides: Partial<LLMProviderDeletionImpact> = {}): LLMProviderDeletionImpact => ({
  provider_id: "openai-1",
  provider: "openai",
  display_name: "Production",
  replacement: target(),
  replacement_explicit: false,
  default_replacement_id: "anthropic-1",
  candidates: [target(), target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "gpt-4o" })],
  chat_available: true,
  general_assistant: unchanged(),
  agent_default: unchanged(),
  image_default: unchanged(),
  embedding_default: { ...unchanged(), fallback: null, fallback_same_model: false, knowledge_base_count: 0 },
  routing_model: unchanged(),
  fallback_model: unchanged(),
  decision_model: unchanged(),
  agents: { count: 0, names: [] },
  users: { count: 0, names: [] },
  impact_fingerprint: "fp-1",
  ...overrides,
});

const nameOf = (t: ProviderRouteTarget) => t.display_name ?? t.provider ?? "?";

const messages = (i: LLMProviderDeletionImpact) => describeProviderDeletion(i, nameOf);

test("a platform default moved to the replacement's default names the model it could not keep", () => {
  const lines = messages(
    impact({
      agent_default: { affected: true, action: "reseeded", before: deleted, after: target(), env_managed: false, model_kept: false },
    }),
  );
  const line = lines.find((l) => l.key === "agent-default");
  assert.equal(
    line?.message,
    "The platform default model becomes {model}, the default model of {provider}, which does not list {previous}.",
  );
  assert.deepEqual(line?.values, { model: "claude-sonnet-5", provider: "Claude", previous: "gpt-5.1" });
});

test("a chat default the replacement also offers keeps its model, and says so", () => {
  const backup = target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "gpt-5.1" });
  const lines = messages(
    impact({
      agent_default: { affected: true, action: "reseeded", before: deleted, after: backup, env_managed: false, model_kept: true },
      general_assistant: { affected: true, action: "reseeded", before: deleted, after: backup, env_managed: false, model_kept: true },
    }),
  );
  const platform = lines.find((l) => l.key === "agent-default");
  assert.equal(platform?.message, "The platform default model keeps {model}, now on {provider}.");
  assert.deepEqual(platform?.values, { model: "gpt-5.1", provider: "Backup" });
  assert.equal(
    lines.find((l) => l.key === "general-assistant")?.message,
    "The General Assistant chat model keeps {model}, now on {provider}.",
  );
});

test("a pin without a model moves without inventing one", () => {
  const lines = messages(
    impact({
      agent_default: {
        affected: true,
        action: "reseeded",
        before: { ...deleted, model: null },
        after: target({ model: null }),
        env_managed: false,
      },
    }),
  );
  assert.equal(lines[0].message, "The platform default moves to {provider}, on its default model.");

  const assistant = messages(
    impact({
      general_assistant: {
        affected: true,
        action: "reseeded",
        before: { ...deleted, model: null },
        after: target({ model: null }),
        env_managed: false,
      },
    }),
  );
  assert.equal(
    assistant.find((l) => l.key === "general-assistant")?.message,
    "The General Assistant moves to {provider}, on its default model.",
  );
});

test("reset agents and users are counted and named", () => {
  const lines = messages(impact({ agents: { count: 12, names: ["A", "B"] }, users: { count: 1, names: ["Ada"] } }));
  const agents = lines.find((l) => l.key === "agents");
  assert.deepEqual(agents?.values, { count: 12 });
  assert.deepEqual(agents?.names, ["A", "B"]);
  assert.equal(agents?.more, 10);
  assert.equal(lines.find((l) => l.key === "users")?.more, 0);
});

test("on Community Edition the lines say what the configuration becomes, never that something stops until configured", () => {
  const cleared: ProviderDeletionRoute = { affected: true, action: "cleared", before: deleted, after: null, env_managed: false };
  const lines = describeProviderDeletion(
    impact({
      chat_available: false,
      candidates: [],
      replacement: null,
      agent_default: cleared,
      general_assistant: cleared,
      image_default: { ...cleared, before: { ...deleted, model: "gpt-image-1" } },
      embedding_default: {
        ...cleared,
        fallback: target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "text-embedding-3-large" }),
        fallback_same_model: false,
        knowledge_base_count: 3,
      },
    }),
    nameOf,
    { licensed: false },
  );
  assert.deepEqual(
    lines.map((l) => [l.key, l.message]),
    [
      ["agent-default", "The platform default model is cleared."],
      ["general-assistant", "The General Assistant chat model is cleared: it follows the platform default model."],
      ["image", "The image default is cleared."],
      ["embedding", "The embedding default is cleared."],
    ],
  );
  assert.ok(lines.every((l) => l.tone === "info"));
});

test("a survivor listing no model is named as not offered, and why", () => {
  const azure = target({ provider: "azure_openai", provider_id: "azure-1", display_name: "Azure", model: null });
  const line = messages(impact({ unlisted: [azure] })).find((l) => l.key === "unlisted");
  assert.equal(
    line?.message,
    "{count, plural, one {# remaining provider is} other {# remaining providers are}} not offered as the replacement: a provider that lists no model cannot take over a default that runs on one.",
  );
  assert.deepEqual(line?.names, ["Azure"]);
});

test("an implicit embedding fallback that moves is announced with where it goes", () => {
  const moving = (overrides: Partial<LLMProviderDeletionImpact["embedding_default"]>) =>
    impact({
      embedding_default: {
        ...unchanged(),
        affected: true,
        fallback: target({ provider: "gemini", provider_id: "gemini-1", display_name: "Gemini", model: "text-embedding-004" }),
        fallback_same_model: false,
        knowledge_base_count: 3,
        fallback_moves: true,
        previous_fallback: { ...deleted, model: "text-embedding-3-small" },
        ...overrides,
      },
    });
  const lines = messages(moving({}));
  const line = lines.find((l) => l.key === "embedding");
  assert.equal(line?.tone, "warning");
  assert.deepEqual(line?.values, { model: "text-embedding-004", provider: "Gemini", previous: "text-embedding-3-small" });
  assert.deepEqual(lines.find((l) => l.key === "embedding-reindex")?.values, { count: 3 });
  assert.equal(lines.some((l) => l.key === "nothing"), false);

  assert.match(messages(moving({ fallback: null })).find((l) => l.key === "embedding")?.message ?? "", /no remaining provider can compute them/);
  assert.equal(messages(moving({ fallback_same_model: true })).find((l) => l.key === "embedding")?.tone, "info");
  assert.equal(
    describeProviderDeletion(moving({}), nameOf, { licensed: false }).find((l) => l.key === "embedding")?.message,
    "Embeddings no longer fall back to this provider.",
  );
});

test("with no chat-capable provider left but a fallback landing, the dialog names it instead of saying chat stops", () => {
  const cleared: ProviderDeletionRoute = { affected: true, action: "cleared", before: deleted, after: null, env_managed: false };
  const landing = target({ provider: "openai", provider_id: "openai-3", display_name: "Images", model: "gpt-4o" });
  const lines = messages(
    impact({ chat_available: false, candidates: [], replacement: null, agent_default: cleared, implicit_chat_fallback: landing }),
  );
  assert.equal(lines[0].tone, "warning");
  assert.deepEqual(lines[0].values, { provider: "Images", model: "gpt-4o" });
});

test("with no chat default stored, the dialog says where chat that goes to this provider moves", () => {
  const landing = target({ provider: "anthropic", provider_id: "anthropic-2", display_name: "Claude", model: "claude-sonnet-5" });
  const lines = messages(impact({ implicit_chat_moves: true, implicit_chat_fallback: landing }));
  const line = lines.find((l) => l.key === "implicit-chat");
  assert.equal(line?.tone, "info");
  assert.deepEqual(line?.values, { provider: "Claude", model: "claude-sonnet-5" });
  assert.equal(lines.some((l) => l.key === "nothing"), false);

  // Not moving: nothing said about implicit chat.
  assert.equal(messages(impact({})).some((l) => l.key === "implicit-chat"), false);
});

test("with no chat provider left the dialog says chat stops, once", () => {
  const cleared: ProviderDeletionRoute = { affected: true, action: "cleared", before: deleted, after: null, env_managed: false };
  const lines = messages(
    impact({ chat_available: false, candidates: [], replacement: null, agent_default: cleared, general_assistant: cleared }),
  );
  assert.equal(lines[0].tone, "danger");
  assert.equal(lines.filter((l) => l.key === "agent-default" || l.key === "general-assistant").length, 0);
});

test("the image default names where it moves, and warns only when image generation stops", () => {
  const moved = messages(
    impact({
      image_default: {
        affected: true,
        action: "reseeded",
        before: { ...deleted, model: "gpt-image-1" },
        after: target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "gpt-image-1" }),
        env_managed: false,
        model_kept: true,
      },
    }),
  );
  const kept = moved.find((l) => l.key === "image");
  assert.equal(kept?.message, "Image generation keeps {model}, now on {provider}.");
  assert.deepEqual(kept?.values, { model: "gpt-image-1", provider: "Backup" });

  const switched = messages(
    impact({
      image_default: {
        affected: true,
        action: "reseeded",
        before: { ...deleted, model: "gpt-image-1" },
        after: target({ provider: "google", provider_id: "google-1", display_name: "Gemini", model: "imagen-4" }),
        env_managed: false,
        model_kept: false,
      },
    }),
  );
  assert.deepEqual(switched.find((l) => l.key === "image")?.values, {
    model: "imagen-4",
    provider: "Gemini",
    previous: "gpt-image-1",
  });

  const stopped = messages(
    impact({
      image_default: { affected: true, action: "cleared", before: { ...deleted, model: "gpt-image-1" }, after: null, env_managed: false },
    }),
  );
  assert.equal(stopped.find((l) => l.key === "image")?.tone, "warning");

  // A stale provider name with no image model routed nothing: no alarm.
  const stale = messages(
    impact({ image_default: { affected: true, action: "cleared", before: { ...deleted, model: null }, after: null, env_managed: false } }),
  );
  assert.equal(stale.find((l) => l.key === "image")?.message, "The image default is cleared.");
});

test("a different fallback embedding model is disclosed with the re-embedding it implies", () => {
  const lines = messages(
    impact({
      embedding_default: {
        affected: true,
        action: "cleared",
        before: { ...deleted, model: "text-embedding-3-small" },
        after: null,
        env_managed: false,
        fallback: target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "text-embedding-3-large" }),
        fallback_same_model: false,
        knowledge_base_count: 4,
      },
    }),
  );
  const embedding = lines.find((l) => l.key === "embedding");
  assert.equal(embedding?.tone, "warning");
  assert.deepEqual(embedding?.values, {
    model: "text-embedding-3-large",
    provider: "Backup",
    previous: "text-embedding-3-small",
  });
  assert.deepEqual(lines.find((l) => l.key === "embedding-reindex")?.values, { count: 4 });
});

test("a same-model embedding fallback is not presented as a problem", () => {
  const lines = messages(
    impact({
      embedding_default: {
        affected: true,
        action: "cleared",
        before: { ...deleted, model: "text-embedding-3-large" },
        after: null,
        env_managed: false,
        fallback: target({ provider: "openai", provider_id: "openai-2", display_name: "Backup", model: "text-embedding-3-large" }),
        fallback_same_model: true,
        knowledge_base_count: 4,
      },
    }),
  );
  assert.equal(lines.find((l) => l.key === "embedding")?.tone, "info");
  assert.equal(lines.find((l) => l.key === "embedding-reindex"), undefined);
});

test("no embedding provider left stops knowledge base search", () => {
  const lines = messages(
    impact({
      embedding_default: {
        affected: true,
        action: "cleared",
        before: deleted,
        after: null,
        env_managed: false,
        fallback: null,
        fallback_same_model: false,
        knowledge_base_count: 2,
      },
    }),
  );
  assert.match(lines.find((l) => l.key === "embedding")?.message ?? "", /no remaining provider can compute embeddings/);
});

test("an environment-owned default says it comes back at the next restart", () => {
  const lines = messages(
    impact({ agent_default: { affected: true, action: "reseeded", before: deleted, after: target(), env_managed: true } }),
  );
  assert.ok(lines.some((l) => l.key === "env"));
});

test("an unused provider says so, and offers no replacement to choose", () => {
  const i = impact();
  assert.deepEqual(
    messages(i).map((l) => l.key),
    ["nothing"],
  );
  assert.equal(deletionNeedsReplacement(i), false);
});

test("a replacement is offered only when a re-seedable route is affected and a survivor can take it", () => {
  const affected = impact({
    agent_default: { affected: true, action: "reseeded", before: deleted, after: target(), env_managed: false },
  });
  assert.equal(deletionNeedsReplacement(affected), true);
  assert.equal(deletionNeedsReplacement({ ...affected, candidates: [] }), false);
  // A stale name cleared regardless of the choice offers nothing to choose.
  assert.equal(
    deletionNeedsReplacement(
      impact({ agent_default: { affected: true, action: "cleared", before: { ...deleted, model: null }, after: null, env_managed: false } }),
    ),
    false,
  );
  // Clearing the routing model alone leaves nothing to re-seed.
  assert.equal(
    deletionNeedsReplacement(
      impact({ routing_model: { affected: true, action: "cleared", before: deleted, after: null, env_managed: false } }),
    ),
    false,
  );
});

test("a cleared decision model says agents can no longer act on pages", () => {
  const cleared: ProviderDeletionRoute = { affected: true, action: "cleared", before: deleted, after: null, env_managed: false };
  const lines = messages(impact({ decision_model: cleared }));
  assert.equal(
    lines.find((l) => l.key === "decision")?.message,
    "The decision model is cleared: agents can no longer act on the pages of embedded chats until another is set.",
  );
  // Nothing to re-seed, but still a change worth confirming.
  assert.equal(deletionNeedsReplacement(impact({ decision_model: cleared })), false);
  assert.ok(lines.length > 0);
});
