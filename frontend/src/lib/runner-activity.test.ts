/**
 * Unit tests for the machine-activity helpers - `yarn test:unit`.
 *
 * The record arrives from two places - a live `tool_progress` frame and the
 * persisted message - and both must parse to the same card; the live list
 * must merge updates in place and stop spinning when the turn ends.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  RUNNER_COMMAND_FINISHED_EVENT,
  RUNNER_COMMAND_STARTED_EVENT,
  backgroundJobState,
  closeLiveRunnerActivity,
  formatActivityBytes,
  formatActivityDuration,
  isRunningBackgroundJob,
  readRunnerActivity,
  readRunnerActivityFrame,
  renderedCommandIds,
  runnerActivityErrorLine,
  runnerActivityStoppedBy,
  runnerActivityTone,
  safeHttpUrl,
  shellPrompt,
  sortListingEntries,
  stripAnsi,
  summarizeRunnerActivity,
  unwrapConsultFrame,
  upsertRunnerActivity,
} from "./runner-activity.ts";

const record = (overrides: Record<string, unknown> = {}) => ({
  command_id: "cmd-1",
  kind: "run_shell",
  status: "completed",
  runner_id: "r-1",
  runner_name: "SAMUEL-LAPTOP",
  platform: "windows",
  local: true,
  started_at: "2026-09-26T07:06:00+00:00",
  duration_ms: 1234,
  input: { command: "Get-PSDrive", cwd: "C:\\" },
  output: { exit_code: 0, stdout: "C 120 GB\n", stderr: "" },
  error: null,
  ...overrides,
});

test("a background job card keeps its job fields and stays stoppable while it runs", () => {
  const raw = record({
    output: { background: true, job_id: "cmd-1", pid: 4242, job_status: "running", exit_code: null, stdout: "first\n" },
  });
  const parsed = readRunnerActivity(raw);
  assert.ok(parsed);
  assert.equal(parsed.output?.background, true);
  assert.equal(parsed.output?.job_id, "cmd-1");
  assert.equal(parsed.output?.pid, 4242);
  assert.equal(isRunningBackgroundJob(parsed), true);
  const exited = readRunnerActivity(record({ output: { background: true, job_id: "cmd-1", job_status: "exited" } }));
  assert.ok(exited);
  assert.equal(isRunningBackgroundJob(exited), false);
  // A foreground command that completed is not a job.
  assert.equal(isRunningBackgroundJob(readRunnerActivity(record())!), false);
});

test("a background job card reads the job's state, never a loader on an ended job", () => {
  const card = (output: Record<string, unknown>) => readRunnerActivity(record({ output: { background: true, job_id: "cmd-1", ...output } }))!;
  assert.deepEqual(backgroundJobState(card({ job_status: "running", exit_code: null })), { state: "running", exitCode: null });
  assert.deepEqual(backgroundJobState(card({ job_status: "exited", exit_code: 2 })), { state: "exited", exitCode: 2 });
  assert.deepEqual(backgroundJobState(card({ job_status: "exited" })), { state: "exited", exitCode: null });
  assert.deepEqual(backgroundJobState(card({ job_status: "stopped", exit_code: null })), { state: "stopped", exitCode: null });
  // A foreground command is no job.
  assert.equal(backgroundJobState(readRunnerActivity(record())!), null);
});

test("only machine-activity frames carry a card", () => {
  assert.equal(readRunnerActivityFrame({ event: "presentation_saved", command: record() }), null);
  assert.ok(readRunnerActivityFrame({ event: RUNNER_COMMAND_STARTED_EVENT, command: record({ status: "running" }) }));
});

test("a consulted agent's command is read through the consult wrappers", () => {
  const consult = (inner: Record<string, unknown>, depth = 1): Record<string, unknown> => {
    let frame = inner;
    for (let i = 0; i < depth; i += 1) frame = { status: "consult_progress", agent_name: "Ops", agent_id: "a-1", inner: frame };
    return frame;
  };
  const frame = { status: "tool_progress", event: RUNNER_COMMAND_FINISHED_EVENT, command: record() };
  assert.equal(readRunnerActivityFrame(consult(frame))?.command_id, record().command_id);
  assert.equal(readRunnerActivityFrame(consult(frame, 3))?.command_id, record().command_id);
  assert.equal(readRunnerActivityFrame(consult({ status: "tool_start", tools: ["run_shell"] })), null);
  assert.equal(readRunnerActivityFrame({ status: "consult_progress", inner: "nope" }), null);
  assert.equal(readRunnerActivityFrame(consult(frame, 9)), null);
});

test("a peer consulted in turn reports its own frame through both wrappers", () => {
  const own = { status: "tool_start", tools: ["read_figures"] };
  const nested = {
    status: "consult_progress",
    agent_name: "Clerk",
    agent_id: "a-1",
    inner: { status: "consult_progress", agent_name: "Analyst", agent_id: "a-2", inner: own },
  };
  assert.equal(unwrapConsultFrame(nested), own);
  assert.equal(unwrapConsultFrame(own), own, "a frame without a wrapper is its own");
  assert.equal(unwrapConsultFrame({ status: "consult_progress", inner: "nope" }), null);
});

test("a card links to a web page only, never to another scheme", () => {
  assert.equal(safeHttpUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
  assert.equal(safeHttpUrl("http://intranet.local/"), "http://intranet.local/");
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl(" JavaScript:alert(1)"), null);
  assert.equal(safeHttpUrl("data:text/html,<b>x</b>"), null);
  assert.equal(safeHttpUrl("file:///C:/Users/me/notes.txt"), null);
  assert.equal(safeHttpUrl("/relative/path"), null);
  assert.equal(safeHttpUrl(""), null);
  assert.equal(safeHttpUrl(undefined), null);
});

test("an update keeps the card where it first appeared", () => {
  const a = readRunnerActivity(record({ command_id: "a", status: "running" }))!;
  const b = readRunnerActivity(record({ command_id: "b", status: "running" }))!;
  let list = upsertRunnerActivity([], a);
  list = upsertRunnerActivity(list, b);
  list = upsertRunnerActivity(list, { ...a, status: "completed" });
  assert.deepEqual(list.map((r) => [r.command_id, r.status]), [["a", "completed"], ["b", "running"]]);
});

test("ending the turn stops the spinners and leaves settled cards alone", () => {
  const live = readRunnerActivity(record({ command_id: "a", status: "awaiting_approval" }))!;
  const done = readRunnerActivity(record({ command_id: "b" }))!;
  const closed = closeLiveRunnerActivity([live, done]);
  assert.deepEqual(closed.map((r) => r.status), ["interrupted", "completed"]);
  const settled = [done];
  assert.equal(closeLiveRunnerActivity(settled), settled, "unchanged list keeps its identity");
});

test("the tone follows the outcome, a non-zero exit code included", () => {
  const tone = (overrides: Record<string, unknown>) => runnerActivityTone(readRunnerActivity(record(overrides))!);
  assert.equal(tone({}), "success");
  assert.equal(tone({ output: { exit_code: 1, stdout: "", stderr: "boom" } }), "error");
  assert.equal(tone({ status: "running", output: null }), "live");
  assert.equal(tone({ status: "refused" }), "error");
  assert.equal(tone({ status: "not_approved" }), "warning");
  assert.equal(tone({ status: "interrupted" }), "muted");
  assert.equal(tone({ status: "stopped" }), "muted");
  assert.equal(tone({ kind: "read_file", output: { content: "x" } }), "success");
});

test("a stopped background job reads muted like an ordinary stopped card", () => {
  const job = (output: Record<string, unknown>) =>
    runnerActivityTone(readRunnerActivity(record({ status: "completed", output: { background: true, job_id: "cmd-1", ...output } }))!);
  assert.equal(job({ job_status: "stopped", exit_code: null }), "muted");
  assert.equal(job({ job_status: "exited", exit_code: 0 }), "success");
  assert.equal(job({ job_status: "exited", exit_code: 1 }), "error");
});

test("a stopped card shows no error line, a failed one does", () => {
  const line = (overrides: Record<string, unknown>) => runnerActivityErrorLine(readRunnerActivity(record(overrides))!);
  assert.equal(line({ status: "stopped", error: "command stopped by the user" }), null);
  assert.equal(line({ status: "failed", error: "boom" }), "boom");
  assert.equal(line({}), null);
});

test("a stopped card says who stopped it to each reader", () => {
  const stopped = readRunnerActivity(record({ status: "stopped", stopped_by: { user_id: "u-ada", name: "Ada" } }))!;
  assert.deepEqual(stopped.stopped_by, { user_id: "u-ada", name: "Ada" });
  assert.deepEqual(runnerActivityStoppedBy(stopped, "u-ada"), { who: "you" });
  assert.deepEqual(runnerActivityStoppedBy(stopped, "u-grace"), { who: "named", name: "Ada" });
  assert.deepEqual(runnerActivityStoppedBy(stopped, undefined), { who: "named", name: "Ada" });
  // Saved before the platform kept it, a malformed stopper, or a stopper whose name is gone: nobody named.
  assert.deepEqual(runnerActivityStoppedBy(readRunnerActivity(record({ status: "stopped" }))!, "u-ada"), { who: "unknown" });
  assert.equal(readRunnerActivity(record({ status: "stopped", stopped_by: { name: "Ada" } }))!.stopped_by, null);
  assert.deepEqual(
    runnerActivityStoppedBy(readRunnerActivity(record({ status: "stopped", stopped_by: { user_id: "u-ada" } }))!, "u-grace"),
    { who: "unknown" },
  );
  // Only a stopped card carries one.
  assert.equal(readRunnerActivity(record({ stopped_by: { user_id: "u-ada", name: "Ada" } }))!.stopped_by, null);
});

test("terminal colour codes and carriage returns are stripped", () => {
  assert.equal(stripAnsi("\u001b[32mok\u001b[0m done\r\n"), "ok done\n");
  assert.equal(stripAnsi("\u001b]0;title\u0007plain"), "plain");
  assert.equal(stripAnsi("no codes"), "no codes");
});

test("durations and sizes read like a person would say them", () => {
  assert.equal(formatActivityDuration(320), "320 ms");
  assert.equal(formatActivityDuration(4230), "4.2 s");
  assert.equal(formatActivityDuration(42_000), "42 s");
  assert.equal(formatActivityDuration(64_000), "1 min 04 s");
  // Rounded once, on the total: a boundary rolls over instead of reading 60.
  assert.equal(formatActivityDuration(999.6), "1.0 s");
  assert.equal(formatActivityDuration(9_999), "10 s");
  assert.equal(formatActivityDuration(59_999), "1 min 00 s");
  assert.equal(formatActivityDuration(119_999), "2 min 00 s");
  assert.equal(formatActivityDuration(null), null);
  assert.equal(formatActivityDuration(-5), null);
  // The decimal follows the reader's locale.
  assert.equal(formatActivityDuration(999.6, "fr"), "1,0 s");
  assert.equal(formatActivityDuration(4230, "fr"), "4,2 s");
  assert.equal(formatActivityDuration(4230, "es"), "4,2 s");
  assert.equal(formatActivityDuration(4230, "ja"), "4.2 s");
  assert.equal(formatActivityDuration(4230, "en"), "4.2 s");
  assert.equal(formatActivityDuration(42_000, "fr"), "42 s");
  assert.equal(formatActivityBytes(512), "512 B");
  assert.equal(formatActivityBytes(1536), "1.5 KB");
  assert.equal(formatActivityBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatActivityBytes(undefined), null);
});

test("the prompt looks like the machine's own terminal", () => {
  assert.equal(shellPrompt("windows", "C:\\repo"), "PS C:\\repo>");
  assert.equal(shellPrompt("windows", null), "PS>");
  assert.equal(shellPrompt("linux", "/srv"), "/srv $");
  assert.equal(shellPrompt(null, undefined), "$");
});

test("a block of cards is summarised by kind and machine", () => {
  const summary = summarizeRunnerActivity(
    [
      record(),
      record({ command_id: "2", kind: "read_file" }),
      record({ command_id: "3", kind: "list_dir", runner_name: "build-box" }),
      record({ command_id: "4", kind: "browser_control", input: { action: "screenshot" } }),
      record({ command_id: "5", kind: "computer_action", input: { action: "click" } }),
    ].map((r) => readRunnerActivity(r)!),
  );
  assert.equal(summary.commands, 1);
  assert.equal(summary.reads, 1);
  assert.equal(summary.listings, 1);
  assert.equal(summary.captures, 1);
  assert.equal(summary.actions, 1);
  assert.deepEqual(summary.machines, ["SAMUEL-LAPTOP", "build-box"]);
});

test("a listing shows folders first, names case-insensitively", () => {
  const sorted = sortListingEntries([
    { name: "zeta.txt", is_dir: false },
    { name: "downloads", is_dir: true },
    { name: "Alpha.txt", is_dir: false },
    { name: "Desktop", is_dir: true },
  ]);
  assert.deepEqual(sorted.map((e) => e.name), ["Desktop", "downloads", "Alpha.txt", "zeta.txt"]);
});

test("the live list hides what a message already renders", () => {
  const ids = renderedCommandIds([
    { runner_activity: [readRunnerActivity(record({ command_id: "a" }))!] },
    { runner_activity: null },
    {},
  ]);
  assert.deepEqual([...ids], ["a"]);
});

test("a malformed nested field never reaches the card", () => {
  const card = readRunnerActivity(
    record({
      input: { command: 42, cwd: ["C:\\"], path: { a: 1 }, x: "10", text_length: "5", url: 7, extra: "dropped" },
      output: {
        stdout: { evil: true },
        stderr: "boom",
        exit_code: "0",
        width: Number.NaN,
        entries: [{ name: 3, is_dir: false }, { name: "ok.txt", is_dir: false, size: "big" }, "junk", { name: "sub", is_dir: true }],
      },
    }),
  );
  assert.ok(card);
  assert.deepEqual(card.input, {});
  assert.deepEqual(card.output, {
    stderr: "boom",
    entries: [
      { name: "ok.txt", is_dir: false },
      { name: "sub", is_dir: true },
    ],
  });
  const settled = readRunnerActivity(record({ output: { exit_code: null, stdout: "", stderr: "" } }));
  assert.equal(settled?.output?.exit_code, null, "a null exit code is a value, not junk");
});
