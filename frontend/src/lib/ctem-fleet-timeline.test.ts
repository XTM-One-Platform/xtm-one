/**
 * Unit tests for the events timeline of the CTEM command center - `yarn test:unit`.
 *
 * Newest first across fleets, a run listed once, the module named from the
 * integration event, archived fleets left out, a name not read yet `null`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { fleetTimeline, timelineOutcome, timelineSource, type TimelineRun } from "./ctem-fleet-timeline.ts";

const run = (id: string, at: string, extra: Partial<TimelineRun> = {}): TimelineRun => ({
  id,
  agent_id: "a1",
  assignment_id: null,
  status: "completed",
  created_at: at,
  completed_at: null,
  outcome: "ok",
  ...extra,
});

const fleet = (id: string, name: string, is_archived = false) => ({ id, name, is_archived });

test("an integration event names the XTM module, any other provider is an integration", () => {
  assert.equal(timelineSource({ trigger_type: "integration_event", trigger_provider: "OpenCTI" }), "opencti");
  assert.equal(timelineSource({ trigger_type: "integration_event", trigger_provider: "openaev" }), "openaev");
  assert.equal(timelineSource({ trigger_type: "integration_event", trigger_provider: "opencrq" }), "opencrq");
  assert.equal(timelineSource({ trigger_type: "integration_event", trigger_provider: "microsoft" }), "integration");
  assert.equal(timelineSource({ trigger_type: "schedule" }), "schedule");
  assert.equal(timelineSource({ trigger_type: "chain" }), "delegation");
  assert.equal(timelineSource({ trigger_type: null }), "other");
  assert.equal(timelineSource({}), "other");
});

test("every trigger_context.type the backend writes has its source", () => {
  const cases: [string, string][] = [
    ["schedule", "schedule"],
    ["schedule_fanout", "schedule"],
    ["manual", "other"],
    ["webhook", "webhook"],
    ["work_item", "work_item"],
    ["scheduled_work_item", "work_item"],
    ["chat_handover", "chat"],
    ["chain", "delegation"],
    ["delegation", "delegation"],
    ["event", "other"],
  ];
  for (const [type, source] of cases) assert.equal(timelineSource({ trigger_type: type }), source, type);
  assert.equal(timelineSource({ trigger_type: "integration_event", trigger_provider: "opencti" }), "opencti");
});

test("the badge reads the API's outcomes (run_outcome.py), the status only as a fallback", () => {
  assert.equal(timelineOutcome("ok", "completed"), "succeeded");
  assert.equal(timelineOutcome("failed", "failed"), "failed");
  assert.equal(timelineOutcome("interrupted", "failed"), "interrupted");
  assert.equal(timelineOutcome("active", "running"), "running");
  assert.equal(timelineOutcome("active", "awaiting_human"), "running");
  assert.equal(timelineOutcome("active", "pending"), "pending");
  assert.equal(timelineOutcome("active", "queued"), "pending");
  assert.equal(timelineOutcome("other", "cancelled"), "cancelled");
  assert.equal(timelineOutcome("other", "skipped"), null);
  assert.equal(timelineOutcome(undefined, "completed"), "succeeded");
  assert.equal(timelineOutcome(null, "dismissed"), null);
});

test("rows merge across fleets, newest first, a run seen twice is listed once", () => {
  const rows = fleetTimeline(
    [fleet("f1", "Exposure"), fleet("f2", "Validation")],
    {
      f1: { fleet_id: "f1", runs: [run("r1", "2026-11-06T10:00:00Z"), run("r3", "2026-11-06T08:00:00Z")] },
      f2: { fleet_id: "f2", runs: [run("r2", "2026-11-06T09:00:00Z", { agent_id: "a2" }), run("r1", "2026-11-06T10:00:00Z")] },
    },
    { f1: { members: [{ agent_id: "a1", name: "OpenCTI Assistant" }] } },
  );
  assert.deepEqual(
    rows.map((r) => [r.runId, r.fleetName, r.agentName]),
    [
      ["r1", "Exposure", "OpenCTI Assistant"],
      ["r2", "Validation", null],
      ["r3", "Exposure", "OpenCTI Assistant"],
    ],
  );
});

test("the event is kept for an integration event only, archived fleets are left out, the limit applies", () => {
  const rows = fleetTimeline(
    [fleet("f1", "Exposure"), fleet("f9", "Old", true)],
    {
      f1: {
        fleet_id: "f1",
        runs: [
          run("r1", "2026-11-06T10:00:00Z", { trigger_type: "integration_event", trigger_provider: "opencti", trigger_event: "report.created" }),
          run("r2", "2026-11-06T09:00:00Z", { trigger_type: "schedule", trigger_event: "ignored" }),
          run("r3", "2026-11-06T08:00:00Z"),
        ],
      },
      f9: { fleet_id: "f9", runs: [run("r9", "2026-11-06T11:00:00Z")] },
    },
    {},
    2,
  );
  assert.deepEqual(
    rows.map((r) => [r.runId, r.source, r.event, r.outcome]),
    [
      ["r1", "opencti", "report.created", "succeeded"],
      ["r2", "schedule", null, "succeeded"],
    ],
  );
});

test("a run a case event started names its case, any other run none (#915)", () => {
  const rows = fleetTimeline(
    [fleet("f1", "Exposure")],
    {
      f1: {
        fleet_id: "f1",
        runs: [
          run("r1", "2026-11-06T10:00:00Z", { trigger_type: "platform_event", case_id: "c-1" }),
          run("r2", "2026-11-06T09:00:00Z", { trigger_type: "schedule" }),
        ],
      },
    },
    {},
  );
  assert.deepEqual(
    rows.map((r) => [r.runId, r.caseId]),
    [
      ["r1", "c-1"],
      ["r2", null],
    ],
  );
});

test("no fleet or no activity read yet gives an empty timeline", () => {
  assert.deepEqual(fleetTimeline(undefined, {}, {}), []);
  assert.deepEqual(fleetTimeline([fleet("f1", "Exposure")], {}, {}), []);
});
