import { test } from "vitest";
import assert from "node:assert/strict";

import type { FleetCost } from "@/hooks/api/useFleetApi";
import { blockedLimits, limitsPayload, parseIterationCap } from "@/lib/run-budget-limits";
import type { BudgetPolicy } from "@/types";

import { costRows } from "./fleet-cost";
import { runDefaultsChanges, runDefaultsDraft } from "./fleet-run-defaults";

const POLICY: BudgetPolicy = {
  can_exceed_ceiling: false,
  ceilings: { max_cost_usd: 5, max_total_tokens: 1_000_000, max_tool_calls: null, max_duration_seconds: 3600 },
  final_answer_reserve: 0.1,
  fallback_enabled: false,
  fallback_model: null,
};

test("a fleet without run defaults seeds empty fields and saves nothing untouched", () => {
  const fleet = { budget_config: null, max_iterations: null };
  const draft = runDefaultsDraft(fleet);
  assert.equal(draft.limits.max_cost_usd, "");
  assert.equal(draft.maxIterations, "");
  assert.deepEqual(runDefaultsChanges(fleet, draft), {});
});

test("typing a limit and a cap sends both", () => {
  const fleet = { budget_config: null, max_iterations: null };
  const draft = runDefaultsDraft(fleet);
  draft.limits.max_cost_usd = "2.5";
  draft.maxIterations = "40";
  assert.deepEqual(runDefaultsChanges(fleet, draft), {
    budget_config: { max_cost_usd: 2.5 },
    max_iterations: 40,
  });
});

test("clearing every field sends null, which clears the fleet's defaults", () => {
  const fleet = { budget_config: { max_total_tokens: 5000 }, max_iterations: 12 };
  const draft = runDefaultsDraft(fleet);
  draft.limits.max_total_tokens = "";
  draft.maxIterations = "";
  assert.deepEqual(runDefaultsChanges(fleet, draft), { budget_config: null, max_iterations: null });
});

test("a re-save of the stored value is no change, key order aside", () => {
  const fleet = { budget_config: { max_tool_calls: 10, max_cost_usd: 1 }, max_iterations: 30 };
  assert.deepEqual(runDefaultsChanges(fleet, runDefaultsDraft(fleet)), {});
});

test("keys the panel does not edit are kept as stored", () => {
  const stored = { max_cost_usd: 1, final_answer_reserve: 0.2, fallback_enabled: true };
  const draft = runDefaultsDraft({ budget_config: stored });
  draft.limits.max_cost_usd = "";
  assert.deepEqual(limitsPayload(draft.limits, stored), { final_answer_reserve: 0.2, fallback_enabled: true });
});

test("a limit above the ceiling is blocked for a non-admin, not for an admin or a stored value", () => {
  const draft = runDefaultsDraft({ budget_config: null }).limits;
  draft.max_cost_usd = "9";
  assert.deepEqual([...blockedLimits(draft, POLICY, null)], ["max_cost_usd"]);
  assert.equal(blockedLimits(draft, { ...POLICY, can_exceed_ceiling: true }, null).size, 0);
  assert.equal(blockedLimits(draft, POLICY, { max_cost_usd: 9 }).size, 0);
});

test("the iteration cap is whole, positive and bounded by the ceiling", () => {
  assert.equal(parseIterationCap(""), null);
  assert.equal(parseIterationCap("0"), null);
  assert.equal(parseIterationCap("abc"), null);
  assert.equal(parseIterationCap("12.7"), 12);
  assert.equal(parseIterationCap("5000"), 1000);
});

test("cost rows are sorted by cost with a share of the most expensive agent", () => {
  const cost: FleetCost = {
    fleet_id: "f",
    days: 30,
    since: "2026-10-03T00:00:00Z",
    total_cost: 3,
    total_tokens: 30,
    trace_count: 6,
    agents: [
      { agent_id: "a", agent_name: "Analyst", trace_count: 2, total_cost: 1, total_tokens: 10 },
      { agent_id: null, agent_name: "", trace_count: 4, total_cost: 2, total_tokens: 20 },
    ],
  };
  const rows = costRows(cost);
  assert.deepEqual(rows.map((r) => [r.key, r.share]), [["unknown-0", 100], ["a", 50]]);
  assert.deepEqual(costRows(undefined), []);
});
