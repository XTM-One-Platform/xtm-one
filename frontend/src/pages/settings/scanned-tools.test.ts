/**
 * Unit tests for the "Tools screened by the prompt guard" editor - `yarn test:unit`.
 *
 * An entry is shown by its tool's current name, flagged once the list says no
 * tool carries it, and the draft stays sorted and each entry once, the form
 * the platform stores, so an unchanged selection is never sent as a change.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { ScannableToolGroup } from "@/types";
import {
  CUSTOM_TOOL_ENTRY_PREFIX,
  SCANNABLE_SEARCH_LIMIT,
  describeEntry,
  indexCandidates,
  isCustomToolEntry,
  searchCandidates,
  toggleEntry,
} from "./scanned-tools.ts";

const groups: ScannableToolGroup[] = [
  {
    key: "builtin",
    label: "Built-in tools",
    tools: [
      { entry: "web_fetch", name: "web_fetch", description: "Fetch a public web page." },
      { entry: "search_knowledge", name: "search_knowledge", description: "Search the agent's knowledge bases." },
    ],
  },
  {
    key: "jira",
    label: "Jira",
    tools: [{ entry: "search_jira_issues", name: "search_jira_issues", description: "Search issues with JQL." }],
  },
];

test("an entry is named by its tool and its group", () => {
  const index = indexCandidates(groups);
  assert.deepEqual(describeEntry("search_jira_issues", index), {
    entry: "search_jira_issues",
    name: "search_jira_issues",
    group: "Jira",
    stale: false,
    custom: false,
  });
});

test("an entry no tool carries is flagged once the list is known, never while it loads", () => {
  assert.deepEqual(describeEntry("gone_tool", indexCandidates(groups)), {
    entry: "gone_tool",
    name: "gone_tool",
    group: null,
    stale: true,
    custom: false,
  });
  assert.equal(describeEntry("gone_tool", null).stale, false);
});

const CUSTOM_ID = "0f3b3a5e-3c5d-4d1e-9d0b-2d1b6f4f7a10";

test("a custom tool is listed by id and shown by its current name, whatever it was called before", () => {
  const renamed: ScannableToolGroup[] = [
    ...groups,
    {
      key: "custom",
      label: "Custom tools",
      tools: [{ entry: `${CUSTOM_TOOL_ENTRY_PREFIX}${CUSTOM_ID}`, name: "crm_lookup_v2", description: "" }],
    },
  ];
  assert.deepEqual(describeEntry(`custom:${CUSTOM_ID}`, indexCandidates(renamed)), {
    entry: `custom:${CUSTOM_ID}`,
    name: "crm_lookup_v2",
    group: "Custom tools",
    stale: false,
    custom: true,
  });
  // Found by its current name, or by the id it is listed under.
  assert.deepEqual(
    searchCandidates(renamed, "crm_lookup").matches.map((match) => match.tool.entry),
    [`custom:${CUSTOM_ID}`],
  );
});

test("a deleted custom tool's entry is flagged for removal", () => {
  const described = describeEntry(`custom:${CUSTOM_ID}`, indexCandidates(groups));
  assert.equal(described.stale, true);
  assert.equal(described.custom, true);
  assert.equal(isCustomToolEntry("web_fetch"), false);
});

test("the first group that offers an entry names it", () => {
  const index = indexCandidates([
    ...groups,
    { key: "mcp:1", label: "MCP: Web", tools: [{ entry: "web_fetch", name: "web_fetch", description: "" }] },
  ]);
  assert.equal(index.get("web_fetch")?.group.label, "Built-in tools");
});

test("the search matches a name, a group or a description, and lists nothing for an empty query", () => {
  assert.deepEqual(searchCandidates(groups, "   "), { matches: [], total: 0 });
  assert.deepEqual(
    searchCandidates(groups, "JIRA").matches.map((match) => match.tool.entry),
    ["search_jira_issues"],
  );
  assert.deepEqual(
    searchCandidates(groups, "web page").matches.map((match) => match.tool.entry),
    ["web_fetch"],
  );
});

test("the search lists at most its limit and counts the rest", () => {
  const many: ScannableToolGroup[] = [
    {
      key: "builtin",
      label: "Built-in tools",
      tools: Array.from({ length: SCANNABLE_SEARCH_LIMIT + 7 }, (_, i) => ({
        entry: `tool_${i}`,
        name: `tool_${i}`,
        description: "",
      })),
    },
  ];
  const { matches, total } = searchCandidates(many, "tool_");
  assert.equal(matches.length, SCANNABLE_SEARCH_LIMIT);
  assert.equal(total, SCANNABLE_SEARCH_LIMIT + 7);
});

test("toggling keeps the entries sorted and each once", () => {
  assert.deepEqual(toggleEntry(["web_fetch"], "search_knowledge", true), ["search_knowledge", "web_fetch"]);
  assert.deepEqual(toggleEntry(["search_knowledge", "web_fetch"], "web_fetch", true), ["search_knowledge", "web_fetch"]);
  assert.deepEqual(toggleEntry(["search_knowledge", "web_fetch"], "web_fetch", false), ["search_knowledge"]);
  // On, then off again: the draft is the value it started from.
  const start = ["a_tool", "b_tool"];
  assert.deepEqual(toggleEntry(toggleEntry(start, "c_tool", true), "c_tool", false), start);
});
