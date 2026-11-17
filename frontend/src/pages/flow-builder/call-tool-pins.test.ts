/**
 * Unit tests for the tool a Call-tool node picked - `yarn test:unit`.
 *
 * The drawer stores the id of the custom tool it picks next to its name; a
 * node written before stores the name alone and must keep showing, and
 * storing, what it did.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  candidateKey,
  pickCandidates,
  pinOf,
  readCandidates,
  shownTool,
  toolHints,
  toolKey,
} from "./call-tool-pins.ts";
import type { CallableTool } from "./call-tool-pins.ts";

const builtin = { name: "web_fetch", source: "builtin" };
const mine = { name: "lookup", source: "custom", id: "id-mine", designated: true };
const company = { name: "lookup", source: "custom", id: "id-company", designated: false };
const other = { name: "enrich", source: "custom", id: "id-enrich", designated: true };
const tools = [builtin, mine, company, other];

test("a custom tool is picked by its id and a built-in by its name", () => {
  assert.equal(toolKey(mine), "id-mine");
  assert.equal(toolKey(builtin), "web_fetch");
  assert.equal(pinOf(company), "id-company");
  assert.equal(pinOf(builtin), null);
});

test("a pinned node shows its pinned tool, even where another tool designates the name", () => {
  assert.equal(shownTool(tools, "lookup", "id-company"), company);
});

test("a node without a pin shows the tool its name designates", () => {
  assert.equal(shownTool(tools, "lookup", null), mine);
  assert.equal(shownTool(tools, "enrich", undefined), other);
});

test("a pin the list does not hold, or that no longer carries the name, shows nothing", () => {
  assert.equal(shownTool(tools, "lookup", "id-gone"), undefined);
  assert.equal(shownTool(tools, "lookup", "id-enrich"), undefined);
  assert.equal(shownTool(tools, "", "id-mine"), undefined);
});

test("a built-in wins its name, pin or not", () => {
  assert.equal(shownTool([...tools, { name: "web_fetch", source: "custom", id: "id-x", designated: true }], "web_fetch", "id-x"), builtin);
});

test("stored candidates read their pins by name, and ignore anything else", () => {
  assert.deepEqual(readCandidates(["lookup", "enrich"], { lookup: "id-company", stray: "id-x" }), [
    { name: "lookup", pin: "id-company" },
    { name: "enrich", pin: null },
  ]);
  assert.deepEqual(readCandidates(["lookup"], ["id-company"]), [{ name: "lookup", pin: null }]);
  assert.deepEqual(readCandidates(["lookup"], undefined), [{ name: "lookup", pin: null }]);
});

test("a newly picked candidate stores its id next to its name", () => {
  assert.deepEqual(pickCandidates(tools, [], ["id-company", "web_fetch"]), {
    candidate_tools: ["lookup", "web_fetch"],
    candidate_tool_ids: { lookup: "id-company" },
  });
});

test("a candidate stored without a pin keeps resolving its name when another is added", () => {
  const current = readCandidates(["lookup"], undefined);
  const keys = [candidateKey(tools, current[0]), "id-enrich"];
  assert.deepEqual(keys, ["id-mine", "id-enrich"]);
  assert.deepEqual(pickCandidates(tools, current, keys), {
    candidate_tools: ["lookup", "enrich"],
    candidate_tool_ids: { enrich: "id-enrich" },
  });
});

test("an unlisted candidate is kept, with its pin, until it is removed", () => {
  const current = readCandidates(["gone"], { gone: "id-gone" });
  assert.equal(candidateKey(tools, current[0]), "gone");
  assert.deepEqual(pickCandidates(tools, current, ["gone", "id-mine"]), {
    candidate_tools: ["gone", "lookup"],
    candidate_tool_ids: { gone: "id-gone", lookup: "id-mine" },
  });
  assert.deepEqual(pickCandidates(tools, current, []), {
    candidate_tools: [],
    candidate_tool_ids: null,
  });
});

test("tools sharing a name say whose each is, and a repeated hint adds the start of the id", () => {
  const listed: CallableTool[] = [
    builtin,
    mine,
    company,
    { name: "lookup", source: "custom", id: "abcdef12-0000", designated: false },
    { name: "lookup", source: "custom", id: "12345678-0000", designated: false },
    other,
  ];
  const whose: Record<string, string> = {
    "id-mine": "Your tool",
    "id-company": "Company-managed",
    "abcdef12-0000": "Shared by Alice",
    "12345678-0000": "Shared by Alice",
  };
  const hints = toolHints(listed, (tool) => whose[tool.id ?? ""] ?? "?");
  assert.deepEqual(Object.fromEntries(hints), {
    "id-mine": "Your tool",
    "id-company": "Company-managed",
    "abcdef12-0000": "Shared by Alice, abcdef12",
    "12345678-0000": "Shared by Alice, 12345678",
  });
});

test("one tool per name: picking a second tool of a name replaces the first", () => {
  const current = readCandidates(["lookup"], { lookup: "id-mine" });
  assert.deepEqual(pickCandidates(tools, current, ["id-mine", "id-company"]), {
    candidate_tools: ["lookup"],
    candidate_tool_ids: { lookup: "id-company" },
  });
});
