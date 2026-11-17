/**
 * Unit tests for the goal-plan state the investigation block draws -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { InvestigationGoalPlan, InvestigationStep } from "../../types/index.ts";
import {
  awaitingReport,
  deriveGoalState,
  messageIsRunReport,
  REPORT_WAIT_MS,
  reportLead,
  reportPhase,
} from "./goal-state.ts";

const PLAN: InvestigationGoalPlan = {
  pack_slug: null,
  declared: false,
  objective: "Investigate {value}",
  predicates: [
    { slug: "subject_defined", label: "Subject defined" },
    { slug: "sources_read", label: "External sources read" },
    { slug: "leads_followed", label: "Leads followed" },
    { slug: "report_written", label: "Cited report written" },
  ],
  initial_state: ["subject_defined"],
  goal_state: ["sources_read", "leads_followed", "report_written"],
  actions: [
    {
      slug: "read_sources",
      label: "Read the external sources",
      description: "",
      cost: 1,
      preconditions: ["subject_defined"],
      effects: ["sources_read"],
      icon: "globe",
      produces_report: false,
    },
    {
      slug: "follow_leads",
      label: "Follow the leads",
      description: "",
      cost: 1,
      preconditions: ["subject_defined"],
      effects: ["leads_followed"],
      icon: "branch",
      produces_report: false,
    },
    {
      slug: "write_report",
      label: "Write the cited report",
      description: "",
      cost: 1,
      preconditions: ["sources_read", "leads_followed"],
      effects: ["report_written"],
      icon: "file",
      produces_report: true,
    },
  ],
  path_cost: 3,
};

let position = 0;
function step(action: string | null, step_status: InvestigationStep["step_status"], findings = 0): InvestigationStep {
  position += 1;
  return {
    step_id: `s${position}`,
    position,
    step_status,
    source_name: `source ${position}`,
    source_pack_slug: null,
    query: null,
    error_type: null,
    status_detail: null,
    detail_code: null,
    detail_params: null,
    findings_count: findings,
    confidence: null,
    evidence: [],
    iteration: null,
    lead_from_step_id: null,
    action,
  };
}

test("a planned run has every action ahead and only its initial state", () => {
  const state = deriveGoalState(PLAN, [step("read_sources", "pending")], { live: true, report: "waiting" });
  assert.deepEqual(
    state.actions.map((a) => a.status),
    ["pending", "pending", "pending"],
  );
  assert.equal(state.goalMet, 0);
  assert.equal(state.goalTotal, 3);
  assert.equal(state.holds.subject_defined, true);
  assert.equal(state.remaining.length, 3);
});

test("an action half answered is under way, and its effect waits for a finding", () => {
  const state = deriveGoalState(
    PLAN,
    [step("read_sources", "empty"), step("read_sources", "pending")],
    { live: true, report: "waiting" },
  );
  assert.equal(state.actions[0].status, "active");
  assert.equal(state.holds.sources_read, false);
  assert.equal(state.counts.active, 1);
});

test("an action whose sources found nothing never sets its predicate", () => {
  const state = deriveGoalState(PLAN, [step("read_sources", "empty"), step("read_sources", "empty")], {
    live: false,
    report: "missing",
  });
  assert.equal(state.actions[0].status, "empty");
  assert.equal(state.holds.sources_read, false);
  assert.equal(state.actions[1].status, "not_reached", "no lead was ever found");
  assert.equal(state.counts.unmet, 3);
});

test("a finding next to a failure is partial, and partial work still holds", () => {
  const state = deriveGoalState(
    PLAN,
    [step("read_sources", "completed", 3), step("read_sources", "error"), step("follow_leads", "completed", 1)],
    { live: false, report: "written" },
  );
  assert.deepEqual(
    state.actions.map((a) => a.status),
    ["partial", "completed", "completed"],
  );
  assert.equal(state.actions[0].findings, 3);
  assert.equal(state.goalMet, 3);
  assert.deepEqual(
    state.transitions.map((t) => t.predicate),
    ["sources_read", "leads_followed", "report_written"],
  );
});

test("the report action follows the report, not any step", () => {
  const writing = deriveGoalState(PLAN, [], { live: false, report: "writing" });
  assert.equal(writing.actions[2].status, "active");
  const missing = deriveGoalState(PLAN, [], { live: false, report: "missing" });
  assert.equal(missing.actions[2].status, "failed");
});

test("a step serving no action of the plan is listed apart, never dropped", () => {
  const state = deriveGoalState(PLAN, [step(null, "completed", 1), step("other", "empty")], {
    live: false,
    report: "missing",
  });
  assert.equal(state.unplanned.length, 2);
});

test("the report phase", () => {
  assert.equal(reportPhase({ status: "completed", report: "x" }, { live: false, awaiting: false }), "written");
  assert.equal(reportPhase({ status: "running", report: null }, { live: true, awaiting: false }), "waiting");
  assert.equal(reportPhase({ status: "completed", report: null }, { live: false, awaiting: true }), "writing");
  assert.equal(reportPhase({ status: "completed", report: null }, { live: false, awaiting: false }), "missing");
});

test("the executive summary is the report's first section", () => {
  const report = [
    "# Intelligence Report: yerevantimes.am",
    "",
    "## Site profile",
    "- The home page shows the Rank Math SEO plugin. [1]",
    "- It exposes the WordPress REST API. [2]",
    "",
    "## Hosting",
    "- It resolved to 208.109.215.186. [4]",
  ].join("\n");
  assert.equal(
    reportLead(report),
    "**Site profile**\n- The home page shows the Rank Math SEO plugin. [1]\n- It exposes the WordPress REST API. [2]",
  );
  assert.equal(reportLead("The site is hosted [1].\n\n## More\nx"), "The site is hosted [1].");
  const long = `## A\n${"- A sentence.\n".repeat(200)}`;
  const lead = reportLead(long, 100);
  assert.ok(lead.length <= 104);
  assert.ok(lead.endsWith("…"));
});

test("a message that is the run's report is recognised, an agent's prose is not", () => {
  const report = "# Intelligence Report: x\n\n## Site profile\n- It shows ![logo](u) the Rank Math plugin. [1]";
  const message = `${report.replace("![", "[")}\n\n## Sources queried\n\n- Site fingerprint\n\n[1] Rank Math`;
  assert.equal(messageIsRunReport(message, report), true);
  assert.equal(messageIsRunReport(`Here is what I found.\n\n${message}`, report), false);
  assert.equal(messageIsRunReport(message, null), false);
});

test("what the subject already satisfied is given, never counted as earned", () => {
  const plan: InvestigationGoalPlan = {
    ...PLAN,
    initial_state: ["subject_defined", "sources_read"],
    goal_state: ["sources_read", "leads_followed", "report_written"],
  };
  const state = deriveGoalState(plan, [], { live: true, report: "waiting" });
  assert.equal(state.holds.sources_read, true);
  assert.ok(state.given.has("sources_read"));
  assert.equal(state.goalTotal, 2);
  assert.equal(state.goalMet, 0);
});

test("a source the budget never reached keeps an action from reading complete", () => {
  const state = deriveGoalState(PLAN, [step("read_sources", "completed", 2), step("read_sources", "skipped")], {
    live: false,
    report: "missing",
  });
  assert.equal(state.actions[0].status, "partial");
  const stillPending = deriveGoalState(PLAN, [step("read_sources", "completed", 2), step("read_sources", "pending")], {
    live: false,
    report: "missing",
  });
  assert.equal(stillPending.actions[0].status, "partial");
});

test("a partial read that found nothing sets no predicate", () => {
  const state = deriveGoalState(PLAN, [step("read_sources", "degraded", 0)], { live: false, report: "missing" });
  assert.notEqual(state.actions[0].status, "partial");
  assert.equal(state.holds.sources_read, false);
  const found = deriveGoalState(PLAN, [step("read_sources", "degraded", 1)], { live: false, report: "missing" });
  assert.equal(found.actions[0].status, "partial");
});

test("an action no source of the run can serve is not reached from the start", () => {
  const plan: InvestigationGoalPlan = {
    ...PLAN,
    actions: PLAN.actions.map((a) => (a.slug === "follow_leads" ? { ...a, servable: false } : a)),
  };
  const state = deriveGoalState(plan, [step("read_sources", "active")], { live: true, report: "waiting" });
  assert.equal(state.actions[1].status, "not_reached");
  assert.equal(state.remaining.some((a) => a.slug === "follow_leads"), false);
});

test("the report wait follows the writer's word, and guesses only without it", () => {
  const ended = "2026-10-01T10:00:00Z";
  const later = Date.parse(ended) + REPORT_WAIT_MS + 1;
  const run = { goal_plan: PLAN, status: "completed" as const, report: null, completed_at: ended };
  assert.equal(awaitingReport({ ...run, report_status: "writing" }, later), true, "long after, still writing");
  assert.equal(awaitingReport({ ...run, report_status: "none" }, Date.parse(ended)), false, "no report is coming");
  assert.equal(awaitingReport({ ...run, report_status: "writing", status: "running" }), false, "a live run waits on its steps");
  assert.equal(awaitingReport({ ...run, report_status: "written", report: "# R" }), false);
  // A server that cannot say: the old bounded guess.
  assert.equal(awaitingReport({ ...run, report_status: "unknown" }, Date.parse(ended) + 1000), true);
  assert.equal(awaitingReport({ ...run, report_status: "unknown" }, later), false);
  assert.equal(awaitingReport({ ...run }, Date.parse(ended) + 1000), true);
});
