/**
 * Unit tests for a dashboard's scheduled exports - `yarn test:unit`.
 *
 * The assignment says what it sends in its trigger config (so the dashboard
 * lists it) and in its prompt (for the agent); recipients are read once each.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { exportOf, isEmailAddress, parseRecipients, scheduleAssignment } from "./schedule.ts";

const schedule = {
  dashboardId: "d-1",
  title: "Ops \"weekly\"",
  cron: "0 8 * * 1-5",
  format: "pdf" as const,
  recipients: ["ann@acme.test", "bob@acme.test"],
  subject: "Weekly ops",
  message: "",
};

test("recipients are read once each, whatever separates them", () => {
  assert.deepEqual(parseRecipients("Ann@acme.test, bob@acme.test\nann@acme.test; carl@acme.test  "), ["ann@acme.test", "bob@acme.test", "carl@acme.test"]);
  assert.deepEqual(parseRecipients("  "), []);
  assert.equal(isEmailAddress("ann@acme.test"), true);
  assert.equal(isEmailAddress("ann"), false);
});

test("the assignment says what it sends, in its trigger and its prompt", () => {
  const body = scheduleAssignment("agent-1", schedule);
  assert.deepEqual(
    { ...body, prompt: undefined },
    {
      agent_id: "agent-1",
      name: 'Send the dashboard Ops "weekly"',
      trigger_type: "schedule",
      trigger_config: { cron: "0 8 * * 1-5", dashboard_export: { dashboard_id: "d-1", format: "pdf", recipients: ["ann@acme.test", "bob@acme.test"] } },
      prompt: undefined,
    },
  );
  assert.deepEqual(body.prompt.split("\n"), [
    'Send the dashboard "Ops \\"weekly\\"" (dashboard_id d-1) as a PDF with the send_dashboard_export tool.',
    "Recipients: ann@acme.test, bob@acme.test.",
    'Subject: "Weekly ops".',
    "Then say in one line who received it and who did not.",
  ]);
});

test("a dashboard recognises its own scheduled exports only", () => {
  const stored = scheduleAssignment("agent-1", schedule);
  assert.deepEqual(exportOf(stored, "d-1"), { format: "pdf", recipients: ["ann@acme.test", "bob@acme.test"], cron: "0 8 * * 1-5" });
  assert.equal(exportOf(stored, "d-2"), null);
  assert.equal(exportOf({ trigger_type: "manual", trigger_config: stored.trigger_config }, "d-1"), null);
  assert.equal(exportOf({ trigger_type: "schedule", trigger_config: { cron: "0 8 * * *" } }, "d-1"), null);
});
