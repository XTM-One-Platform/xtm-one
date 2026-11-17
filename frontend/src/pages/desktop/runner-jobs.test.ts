/**
 * Unit tests for the Machines page jobs list - `yarn test:unit`.
 *
 * Running jobs come first so their Stop is at hand, and a machine that cannot
 * list its jobs says why instead of showing an empty list.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { jobsUnavailableMessage, orderJobs } from "./runner-jobs.ts";
import type { RunnerJob } from "@/types";

const job = (job_id: string, status: RunnerJob["status"], started_at: string): RunnerJob => ({
  job_id,
  command: "sleep 600",
  pid: 1,
  started_at,
  status,
  ended_at: status === "running" ? null : started_at,
  exit_code: status === "exited" ? 0 : null,
  output_bytes: 0,
});

test("running jobs come first, then the newest ended ones", () => {
  const ordered = orderJobs([
    job("old-exit", "exited", "2026-11-03T00:00:00Z"),
    job("run", "running", "2026-11-02T00:00:00Z"),
    job("new-stop", "stopped", "2026-11-03T01:00:00Z"),
  ]);
  assert.deepEqual(
    ordered.map((j) => j.job_id),
    ["run", "new-stop", "old-exit"],
  );
});

test("each reason the server answers has its own sentence", () => {
  const reasons = ["offline", "not_allowed", "busy", "no_answer"];
  const sentences = new Set(reasons.map(jobsUnavailableMessage));
  assert.equal(sentences.size, reasons.length);
  assert.equal(jobsUnavailableMessage(null), jobsUnavailableMessage("no_answer"));
});

// An unknown reason (a newer server) and an inherited property name
// fall back to the no-answer sentence on purpose, never to undefined.
test("an unknown reason reads as no answer", () => {
  const noAnswer = jobsUnavailableMessage("no_answer");
  assert.equal(jobsUnavailableMessage("rate_limited"), noAnswer);
  assert.equal(jobsUnavailableMessage("toString"), noAnswer);
  assert.equal(jobsUnavailableMessage(""), noAnswer);
});
