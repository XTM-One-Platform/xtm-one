/**
 * Unit tests for the built-in tool offer rules - `yarn test:unit`.
 *
 * An agent chat's session picker: a session selection replaces the
 * agent's list, and the server drops an agent-only suite the agent does not
 * carry from the session of anyone who may not configure the agent. The
 * picker must never offer that suite.
 *
 * An agent's configuration grid: a suite whose deployment switch is
 * off must stay visible while the agent carries it, so it can be removed, and
 * must never be added.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  listedInAgentConfig,
  mayConfigureAgent,
  offeredInAgentChat,
  toggledInAgentConfig,
} from "./builtin-tool-offers.ts";

const suites = [
  { id: "web_fetch", agentOnly: false },
  { id: "generate_report", agentOnly: false },
  { id: "work_items", agentOnly: true },
  { id: "inspect_my_traces", agentOnly: true },
];
const ids = (offered: { id: string }[]) => offered.map((suite) => suite.id);

const sharedAgent = { builtin_tool_ids: ["web_fetch", "work_items"], user_id: null, is_company_managed: true };
const member = { userId: "u-1", isAdmin: false };

test("a user who cannot configure a shared agent is offered only the agent-only suites it carries", () => {
  assert.deepEqual(ids(offeredInAgentChat(suites, sharedAgent, member)), ["web_fetch", "generate_report", "work_items"]);
});

test("an admin configures every agent, so every suite is offered", () => {
  assert.deepEqual(ids(offeredInAgentChat(suites, sharedAgent, { userId: "u-1", isAdmin: true })), ids(suites));
});

test("the owner of a personal agent is offered every suite", () => {
  const own = { builtin_tool_ids: [], user_id: "u-1", is_company_managed: false };
  assert.equal(mayConfigureAgent(own, member), true);
  assert.deepEqual(ids(offeredInAgentChat(suites, own, member)), ids(suites));
});

test("owning a company-managed agent's row is not configuring it", () => {
  const managed = { builtin_tool_ids: [], user_id: "u-1", is_company_managed: true };
  assert.equal(mayConfigureAgent(managed, member), false);
});

test("an agent not loaded yet carries nothing and offers no agent-only suite", () => {
  assert.deepEqual(ids(offeredInAgentChat(suites, undefined, { userId: "u-1", isAdmin: true })), [
    "web_fetch",
    "generate_report",
  ]);
});

// ---------------------------------------------------------------------------
// The agent configuration grid
// ---------------------------------------------------------------------------

const webFetch = { id: "web_fetch", available: true };
const codeInterpreter = { id: "code_interpreter", available: false };
const computerUse = { id: "computer_use", available: false };
const workItems = { id: "work_items", available: true };
const registry = [webFetch, codeInterpreter, computerUse, workItems];

test("the grid lists every available suite and a switched-off one only while the agent carries it", () => {
  const saved = ["code_interpreter"];
  assert.deepEqual(ids(listedInAgentConfig(registry, saved, saved)), ["web_fetch", "code_interpreter", "work_items"]);
});

test("a switched-off suite no agent carries stays hidden, as before", () => {
  assert.deepEqual(ids(listedInAgentConfig(registry, [], [])), ["web_fetch", "work_items"]);
});

test("a switched-off suite removed from the draft stays listed until the save", () => {
  assert.deepEqual(ids(listedInAgentConfig(registry, ["code_interpreter"], [])), [
    "web_fetch",
    "code_interpreter",
    "work_items",
  ]);
});

test("a switched-off suite the draft holds is listed even if the saved agent dropped it meanwhile", () => {
  // Another session saved the agent without it while this draft was open: the
  // draft would still send it, so the grid shows it and it can be removed.
  assert.deepEqual(ids(listedInAgentConfig(registry, [], ["computer_use"])), [
    "web_fetch",
    "computer_use",
    "work_items",
  ]);
});

test("the grid removes a switched-off suite the agent carries", () => {
  const saved = ["web_fetch", "code_interpreter"];
  assert.deepEqual(toggledInAgentConfig([...saved], codeInterpreter, saved), ["web_fetch"]);
});

test("a switched-off suite removed before the save can be put back", () => {
  const saved = ["web_fetch", "code_interpreter"];
  assert.deepEqual(toggledInAgentConfig(["web_fetch"], codeInterpreter, saved), ["web_fetch", "code_interpreter"]);
});

test("a switched-off suite the agent does not carry is never added", () => {
  const draft = ["web_fetch"];
  assert.equal(toggledInAgentConfig(draft, computerUse, ["web_fetch"]), draft);
  assert.deepEqual(draft, ["web_fetch"]);
});

test("a non-administrator never adds a suite only an administrator binds", () => {
  const orgContext = { id: "organization_context", available: true, bindLocked: true };
  const draft = ["web_fetch"];
  assert.equal(toggledInAgentConfig(draft, orgContext, ["web_fetch"]), draft);
  // One an administrator already bound stays removable, and a removal can be undone before the save.
  const saved = ["organization_context"];
  assert.deepEqual(toggledInAgentConfig([...saved], orgContext, saved), []);
  assert.deepEqual(toggledInAgentConfig([], orgContext, saved), ["organization_context"]);
  // For an administrator the suite is not locked.
  assert.deepEqual(toggledInAgentConfig([], { ...orgContext, bindLocked: false }, []), ["organization_context"]);
});

test("an available suite is added and removed as before", () => {
  assert.deepEqual(toggledInAgentConfig([], webFetch, []), ["web_fetch"]);
  assert.deepEqual(toggledInAgentConfig(["web_fetch"], webFetch, ["web_fetch"]), []);
});

test("toggling other suites keeps a switched-off one the agent carries", () => {
  const saved = ["code_interpreter"];
  const added = toggledInAgentConfig([...saved], webFetch, saved);
  assert.deepEqual(added, ["code_interpreter", "web_fetch"]);
  assert.deepEqual(toggledInAgentConfig(added, webFetch, saved), ["code_interpreter"]);
});

test("no sequence of toggles adds a switched-off suite, and the grid lists every one a save would send", () => {
  const saved = ["web_fetch", "code_interpreter"];
  const switchedOff = new Set(registry.filter((suite) => !suite.available).map((suite) => suite.id));
  const walk = (draft: string[], depth: number) => {
    const listed = new Set(ids(listedInAgentConfig(registry, saved, draft)));
    for (const id of draft) {
      if (!switchedOff.has(id)) continue;
      assert.ok(saved.includes(id), `${id} was added while switched off`);
      assert.ok(listed.has(id), `${id} would be saved without being listed`);
    }
    if (depth === 0) return;
    // A click can only land on a listed card, but the rule refuses the others
    // too, so every suite of the registry is tried.
    for (const suite of registry) walk(toggledInAgentConfig(draft, suite, saved), depth - 1);
  };
  walk([...saved], 4);
});
