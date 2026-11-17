/**
 * Who a "with your assistant" hand-off opens the chat on - `yarn test:unit`.
 *
 * The Video Studio opened every chat on the General Assistant, even for a user
 * whose default agent carries the Video Studio tool; the Presentations module
 * already opened on the default agent. Both read these two functions now.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import type { Agent } from "../types/index.ts";
import { aliasIndex } from "./builtin-tool-aliases.ts";
import { agentsCarrying, carriesJob, firstToolAgent } from "./tool-agents.ts";

const TOOL = "video_studio";

function agent(id: string, overrides: Partial<Agent> = {}): Agent {
  return {
    id,
    name: id,
    enabled: true,
    disable_chat: false,
    is_company_managed: false,
    builtin_tool_ids: [TOOL],
    ...overrides,
  } as Agent;
}

test("only the enabled chat agents carrying the tool are offered, by name", () => {
  const agents = [
    agent("zed"),
    agent("alpha"),
    agent("no-tool", { builtin_tool_ids: ["generate_presentation"] }),
    agent("off", { enabled: false }),
    agent("api-only", { disable_chat: true }),
  ];
  assert.deepEqual(
    agentsCarrying(agents, TOOL).map((a) => a.id),
    ["alpha", "zed"],
  );
});

test("the user's default agent comes first when it carries the tool", () => {
  const offered = agentsCarrying([agent("mine"), agent("other")], TOOL);
  assert.equal(firstToolAgent(offered, "mine", true), "mine");
});

test("a default agent without the tool falls back on the General Assistant", () => {
  const offered = agentsCarrying([agent("mine", { builtin_tool_ids: [] }), agent("other")], TOOL);
  assert.equal(firstToolAgent(offered, "mine", true), null);
});

// The platform group: agent_builder, fleets and flow_builder merged.
const ALIASES = aliasIndex([{ id: "platform", aliases: ["agent_builder", "fleets", "flow_builder"] }]);
const FLEETS = { suite: "platform", fn: "create_fleet" };

test("a job of a merged group is carried through the group or a former id", () => {
  assert.equal(carriesJob(["platform"], null, FLEETS, ALIASES), true);
  assert.equal(carriesJob(["fleets"], null, FLEETS, ALIASES), true);
  assert.equal(carriesJob(["agent_builder"], null, "platform", ALIASES), true);
  assert.equal(carriesJob(["web"], null, FLEETS, ALIASES), false);
});

test("an allow list that drops the job's function does not carry it", () => {
  assert.equal(carriesJob(["platform"], { platform: ["list_agents"] }, FLEETS, ALIASES), false);
  assert.equal(carriesJob(["platform"], { platform: ["list_agents", "create_fleet"] }, FLEETS, ALIASES), true);
  // A list keyed by a former id narrows that id only.
  assert.equal(carriesJob(["fleets"], { fleets: ["list_fleets"] }, FLEETS, ALIASES), false);
  assert.equal(carriesJob(["fleets", "agent_builder"], { agent_builder: ["list_agents"] }, FLEETS, ALIASES), true);
});

test("an agent migrated to the platform group is still offered for fleets", () => {
  const migrated = agent("migrated", { builtin_tool_ids: ["platform"] });
  const builderOnly = agent("builder", {
    builtin_tool_ids: ["platform"],
    builtin_allowed_tools: { platform: ["list_agents", "create_agent"] },
  });
  assert.deepEqual(
    agentsCarrying([migrated, builderOnly], FLEETS, ALIASES).map((a) => a.id),
    ["migrated"],
  );
});

test("without the General Assistant, a company-managed agent carrying the tool, else any", () => {
  const offered = agentsCarrying([agent("a"), agent("company", { is_company_managed: true })], TOOL);
  assert.equal(firstToolAgent(offered, null, false), "company");
  assert.equal(firstToolAgent(agentsCarrying([agent("a")], TOOL), null, false), "a");
  assert.equal(firstToolAgent([], null, false), null);
});
