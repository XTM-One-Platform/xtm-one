/**
 * Unit tests for the ordering and matching rules every picker shares -
 * `yarn test:unit`.
 *
 * The provider list put ElevenLabs last because it followed the key order of a
 * constant; a picker of 300 agents could not be searched at all. These pin the
 * one rule both now go through.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  foldForSearch,
  labelCollator,
  matchesQuery,
  sortByGroupThenLabel,
  sortByLabel,
  sortFilterOptions,
  sortWithinGroups,
} from "./select-options.ts";

const label = (item: { label: string }) => item.label;

test("sorts the provider names alphabetically, ElevenLabs among them", () => {
  const providers = [
    "Anthropic",
    "OpenAI",
    "Google Gemini",
    "OpenRouter",
    "Ollama",
    "Mistral AI",
    "AWS Bedrock",
    "LiteLLM",
    "Custom OpenAI-Compatible",
    "Embedding Server (/embed)",
    "ElevenLabs",
  ].map((name) => ({ label: name }));
  assert.deepEqual(sortByLabel(providers, label, "en").map(label), [
    "Anthropic",
    "AWS Bedrock",
    "Custom OpenAI-Compatible",
    "ElevenLabs",
    "Embedding Server (/embed)",
    "Google Gemini",
    "LiteLLM",
    "Mistral AI",
    "Ollama",
    "OpenAI",
    "OpenRouter",
  ]);
});

test("ignores case and reads numbers by value", () => {
  const items = ["agent 10", "Agent 2", "AGENT 1", "beta"].map((name) => ({ label: name }));
  assert.deepEqual(sortByLabel(items, label, "en").map(label), ["AGENT 1", "Agent 2", "agent 10", "beta"]);
});

test("files accented letters with their base letter", () => {
  const items = ["Zulu", "Équipe", "Echo", "Alpha"].map((name) => ({ label: name }));
  assert.deepEqual(sortByLabel(items, label, "fr").map(label), ["Alpha", "Echo", "Équipe", "Zulu"]);
});

test("keeps the given order for labels that compare equal and never mutates the input", () => {
  const items = [{ label: "same", id: 1 }, { label: "Same", id: 2 }, { label: "a", id: 3 }];
  const sorted = sortByLabel(items, label, "en");
  assert.deepEqual(sorted.map((item) => item.id), [3, 1, 2]);
  assert.deepEqual(items.map((item) => item.id), [1, 2, 3]);
});

test("orders groups, then labels inside each group, ungrouped entries first", () => {
  const items = [
    { group: "OpenAI", label: "gpt-4o" },
    { group: "Anthropic", label: "claude-sonnet" },
    { group: undefined, label: "zeta" },
    { group: "OpenAI", label: "gpt-4.1" },
    { group: "Anthropic", label: "claude-haiku" },
  ];
  assert.deepEqual(
    sortByGroupThenLabel(items, (item) => item.group, label, "en").map((item) => `${item.group ?? "-"}/${item.label}`),
    ["-/zeta", "Anthropic/claude-haiku", "Anthropic/claude-sonnet", "OpenAI/gpt-4.1", "OpenAI/gpt-4o"],
  );
});

test("keeps the groups in their given order and sorts the labels inside each", () => {
  const voices = [
    { group: "Cloned", label: "Sam" },
    { group: "Premade", label: "Rachel" },
    { group: "Cloned", label: "Ada" },
    { group: "Premade", label: "Adam" },
  ];
  assert.deepEqual(
    sortWithinGroups(voices, (voice) => voice.group, label, "en").map((voice) => `${voice.group}/${voice.label}`),
    ["Cloned/Ada", "Cloned/Sam", "Premade/Adam", "Premade/Rachel"],
  );
});

const values = (options: { value: string }[]) => options.map((option) => option.value);

test("sorts a filter's options by the label shown, whatever the declaration order", () => {
  const types = [
    { value: "agent", label: "Agents" },
    { value: "knowledge_base", label: "Knowledge Bases" },
    { value: "prompt", label: "Prompts" },
    { value: "skill", label: "Skills" },
    { value: "mcp_server", label: "MCP Servers" },
    { value: "custom_tool", label: "Tools" },
    { value: "integration", label: "Integrations" },
  ];
  assert.deepEqual(
    sortFilterOptions(types, { locale: "en" }).map((option) => option.label),
    ["Agents", "Integrations", "Knowledge Bases", "MCP Servers", "Prompts", "Skills", "Tools"],
  );
});

test("sorts filter options on the translated label, case and accents ignored", () => {
  const statuses = [
    { value: "online", label: "En ligne" },
    { value: "archived", label: "Archivé" },
    { value: "enabled", label: "activé" },
    { value: "queued", label: "En file d'attente" },
    { value: "failed", label: "Échoué" },
  ];
  assert.deepEqual(values(sortFilterOptions(statuses, { locale: "fr" })), ["enabled", "archived", "failed", "queued", "online"]);
});

test("keeps the pinned values first, in the order given, then the rest by label", () => {
  const owners = [
    { value: "u2", label: "Zoé" },
    { value: "none", label: "None" },
    { value: "u1", label: "alice" },
    { value: "me", label: "Me" },
  ];
  assert.deepEqual(values(sortFilterOptions(owners, { locale: "en", pinned: ["me", "none", "absent"] })), ["me", "none", "u1", "u2"]);
});

test("an ordinal scale keeps its order", () => {
  const priorities = [
    { value: "critical", label: "Critical" },
    { value: "high", label: "High" },
    { value: "medium", label: "Medium" },
    { value: "low", label: "Low" },
  ];
  assert.deepEqual(values(sortFilterOptions(priorities, { locale: "en", ordered: true })), ["critical", "high", "medium", "low"]);
});

test("builds one collator per locale", () => {
  assert.equal(labelCollator("en"), labelCollator("en"));
  assert.notEqual(labelCollator("en"), labelCollator("ja"));
});

test("folds case and accents for search", () => {
  assert.equal(foldForSearch("Équipe ÉTÉ"), "equipe ete");
});

test("matches every word of the query, in any order, across the option's texts", () => {
  const texts = ["[EMT & ExCo] Executive Assistant", "Prepares the board pack"];
  assert.equal(matchesQuery("exec assist", texts), true);
  assert.equal(matchesQuery("assistant emt", texts), true);
  assert.equal(matchesQuery("board", texts), true);
  assert.equal(matchesQuery("exec finder", texts), false);
});

test("matches an accentless query against an accented label, and the reverse", () => {
  assert.equal(matchesQuery("equipe", ["Équipe rouge"]), true);
  assert.equal(matchesQuery("équipe", ["Equipe rouge"]), true);
});

test("an empty or blank query matches everything, missing texts match nothing", () => {
  assert.equal(matchesQuery("", ["anything"]), true);
  assert.equal(matchesQuery("   ", []), true);
  assert.equal(matchesQuery("a", [null, undefined, ""]), false);
});
