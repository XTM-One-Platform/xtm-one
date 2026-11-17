/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Machine activity - what an agent did on a runner, as the chat shows it.
 *
 * The `computer_use` tools report every command they send to a machine as a
 * display record (`backend/app/services/runner_activity.py`): live through
 * `tool_progress` frames (`runner_command_started` / `runner_command_finished`,
 * each carrying the FULL record under `command` - an upsert keyed by
 * `command_id`), then persisted on the assistant message as `runner_activity`.
 * The transcript renders one card per record: the command line and its output
 * in a small terminal window, the file, the folder, the capture.
 *
 * Pure helpers only (`node --test`): parsing, merging and the wording inputs.
 */
import type {
  RunnerActivity,
  RunnerActivityEntry,
  RunnerActivityInput,
  RunnerActivityOutput,
  RunnerActivityStatus,
  RunnerActivityStopper,
  RunnerPlatform,
} from "@/types";

/** `event` of the `tool_progress` frame that opens or updates a card. */
export const RUNNER_COMMAND_STARTED_EVENT = "runner_command_started";
/** `event` of the `tool_progress` frame that settles a card. */
export const RUNNER_COMMAND_FINISHED_EVENT = "runner_command_finished";

/** Every status a record may carry - mirrors `RECORD_STATUSES` on the backend (tripwire-tested).
 * @public
 */
export const RUNNER_ACTIVITY_STATUSES = [
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "timeout",
  "rejected",
  "not_approved",
  "refused",
  "busy",
  "unavailable",
  "interrupted",
  "stopped",
] as const;

/** The statuses of a card whose tool is still waiting on the machine.
 * @public
 */
export const LIVE_RUNNER_ACTIVITY_STATUSES: readonly RunnerActivityStatus[] = ["running", "awaiting_approval"];

const KINDS = new Set([
  "run_shell",
  "read_file",
  "write_file",
  "list_dir",
  "screenshot",
  "computer_action",
  "browser_control",
]);
const PLATFORMS: readonly RunnerPlatform[] = ["windows", "macos", "linux"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The fields a card reads, grouped by the type each is read as. */
interface FieldTypes {
  strings?: readonly string[];
  numbers?: readonly string[];
  /** Numbers for which `null` is a value ("no exit code"), not junk. */
  nullableNumbers?: readonly string[];
  flags?: readonly string[];
}

const INPUT_FIELDS: FieldTypes = {
  strings: ["command", "cwd", "path", "content", "display", "action", "button", "browser", "keys", "url", "selector"],
  numbers: ["content_bytes", "x", "y", "x2", "y2", "amount", "text_length"],
  flags: ["command_truncated", "content_truncated"],
};

const OUTPUT_FIELDS: FieldTypes = {
  strings: ["stdout", "stderr", "content", "file_id", "url", "title", "job_id", "job_status"],
  numbers: ["entry_count", "width", "height", "content_chars"],
  nullableNumbers: ["exit_code", "size", "bytes_written", "pid"],
  flags: ["stdout_truncated", "stderr_truncated", "content_truncated", "binary", "entries_truncated", "background"],
};

/** The fields of `raw` a card reads, each kept only with the type it is read as. */
function pickFields(raw: Record<string, unknown>, fields: FieldTypes): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of fields.strings ?? []) if (typeof raw[key] === "string") out[key] = raw[key];
  for (const key of fields.numbers ?? []) if (numberOrNull(raw[key]) !== null) out[key] = raw[key];
  for (const key of fields.nullableNumbers ?? []) {
    if (raw[key] === null || numberOrNull(raw[key]) !== null) out[key] = raw[key];
  }
  for (const key of fields.flags ?? []) if (typeof raw[key] === "boolean") out[key] = raw[key];
  return out;
}

function readEntry(raw: unknown): RunnerActivityEntry | null {
  if (!isRecord(raw) || typeof raw.name !== "string") return null;
  const entry: RunnerActivityEntry = { name: raw.name, is_dir: raw.is_dir === true };
  const size = numberOrNull(raw.size);
  if (size !== null && !entry.is_dir) entry.size = size;
  return entry;
}

function readOutput(raw: unknown): RunnerActivityOutput | null {
  if (!isRecord(raw)) return null;
  const output = pickFields(raw, OUTPUT_FIELDS) as RunnerActivityOutput;
  if (Array.isArray(raw.entries)) {
    output.entries = raw.entries.map(readEntry).filter((e): e is RunnerActivityEntry => e !== null);
  }
  return output;
}

/**
 * A record as the wire / the API delivers it, or `null` when malformed. The
 * nested `input` / `output` keep only the fields a card reads, with the type
 * it reads them as: a malformed row costs its odd fields, never the page.
 */
export function readRunnerActivity(raw: unknown): RunnerActivity | null {
  if (!isRecord(raw)) return null;
  const commandId = raw.command_id;
  const kind = raw.kind;
  const status = raw.status;
  if (typeof commandId !== "string" || !commandId) return null;
  if (typeof kind !== "string" || !KINDS.has(kind)) return null;
  if (typeof status !== "string" || !(RUNNER_ACTIVITY_STATUSES as readonly string[]).includes(status)) return null;
  const platform = stringOrNull(raw.platform);
  return {
    command_id: commandId,
    kind,
    status: status as RunnerActivityStatus,
    runner_id: stringOrNull(raw.runner_id),
    runner_name: stringOrNull(raw.runner_name),
    platform: platform && (PLATFORMS as readonly string[]).includes(platform) ? (platform as RunnerPlatform) : null,
    local: raw.local === true,
    started_at: stringOrNull(raw.started_at),
    duration_ms: numberOrNull(raw.duration_ms),
    input: isRecord(raw.input) ? (pickFields(raw.input, INPUT_FIELDS) as RunnerActivityInput) : {},
    output: readOutput(raw.output),
    error: stringOrNull(raw.error),
    stopped_by: status === "stopped" ? readStopper(raw.stopped_by) : null,
  };
}

function readStopper(raw: unknown): RunnerActivityStopper | null {
  if (!isRecord(raw) || typeof raw.user_id !== "string" || !raw.user_id) return null;
  return { user_id: raw.user_id, name: typeof raw.name === "string" && raw.name ? raw.name : null };
}

/**
 * Who a stopped card says stopped it: `you` when the reader did,
 * `named` with the person's name when someone else did, `unknown` when the
 * record does not say (saved before the platform kept it, or the name is gone).
 */
export type RunnerActivityStoppedBy = { who: "you" } | { who: "named"; name: string } | { who: "unknown" };

export function runnerActivityStoppedBy(
  record: Pick<RunnerActivity, "stopped_by">,
  readerId: string | null | undefined,
): RunnerActivityStoppedBy {
  const stopper = record.stopped_by;
  if (!stopper) return { who: "unknown" };
  if (readerId && stopper.user_id === readerId) return { who: "you" };
  return stopper.name ? { who: "named", name: stopper.name } : { who: "unknown" };
}

/** Consult wrappers read through before a frame is ignored - mirrors `MAX_CONSULT_DEPTH` on the backend. */
const MAX_CONSULT_DEPTH = 8;

/**
 * The frame a consulted agent's loop emitted, read through its wrappers
 * (`consult_progress` -> `inner`, once per consult level), or `null` when a
 * wrapper carries no frame.
 */
export function unwrapConsultFrame(frame: Record<string, unknown>): Record<string, unknown> | null {
  let current = frame;
  for (let depth = 0; depth < MAX_CONSULT_DEPTH && current.status === "consult_progress"; depth += 1) {
    if (!isRecord(current.inner)) return null;
    current = current.inner;
  }
  return current;
}

/**
 * The record a `tool_progress` frame carries, or `null` when it is not a
 * machine-activity frame. A command a CONSULTED agent ran arrives wrapped
 * and is read through (`unwrapConsultFrame`).
 */
export function readRunnerActivityFrame(frame: Record<string, unknown>): RunnerActivity | null {
  const current = unwrapConsultFrame(frame);
  if (!current) return null;
  if (current.event !== RUNNER_COMMAND_STARTED_EVENT && current.event !== RUNNER_COMMAND_FINISHED_EVENT) return null;
  return readRunnerActivity(current.command);
}

/** A page address a card may link to (the URL comes from the agent's own command). */
export { safeHttpUrl } from "./safe-url.ts";

/** Fold one record into a list: an update keeps the card where it first appeared. */
export function upsertRunnerActivity(list: readonly RunnerActivity[], record: RunnerActivity): RunnerActivity[] {
  const index = list.findIndex((r) => r.command_id === record.command_id);
  if (index === -1) return [...list, record];
  const next = list.slice();
  next[index] = record;
  return next;
}

export function isLiveRunnerActivity(record: Pick<RunnerActivity, "status">): boolean {
  return LIVE_RUNNER_ACTIVITY_STATUSES.includes(record.status);
}

/**
 * The turn ended while these cards were still live (a stop, an error, a tool
 * that never reported back): stop the spinners. Returns the same array when
 * nothing changed, so a React state update is a no-op.
 */
export function closeLiveRunnerActivity(list: RunnerActivity[]): RunnerActivity[] {
  if (!list.some(isLiveRunnerActivity)) return list;
  return list.map((r) => (isLiveRunnerActivity(r) ? { ...r, status: "interrupted" as const } : r));
}

/** How a card reads at a glance - drives its accent colour and status icon. */
export type RunnerActivityTone = "live" | "success" | "warning" | "error" | "muted";

export function runnerActivityTone(record: RunnerActivity): RunnerActivityTone {
  switch (record.status) {
    case "running":
    case "awaiting_approval":
      return "live";
    case "completed": {
      // A background job the user stopped reads like any stopped card.
      if (backgroundJobState(record)?.state === "stopped") return "muted";
      const exit = record.kind === "run_shell" ? record.output?.exit_code : 0;
      return exit === 0 || exit === null || exit === undefined ? "success" : "error";
    }
    case "failed":
    case "rejected":
    case "refused":
      return "error";
    case "timeout":
    case "not_approved":
    case "busy":
    case "unavailable":
      return "warning";
    case "interrupted":
    case "stopped":
    default:
      return "muted";
  }
}

/**
 * Where a background shell job stood when its card was written, or
 * null for any other card: `running` keeps the spinner, `exited` reads as an
 * exit code (or "Ended" without one), `stopped` as "Stopped by you". The
 * badge never says a job runs when the record says it ended.
 */
export function backgroundJobState(
  record: Pick<RunnerActivity, "kind" | "status" | "output">,
): { state: "running" | "exited" | "stopped"; exitCode: number | null } | null {
  if (record.kind !== "run_shell" || record.status !== "completed" || !record.output?.background) return null;
  const exitCode = typeof record.output.exit_code === "number" ? record.output.exit_code : null;
  switch (record.output.job_status) {
    case "exited":
      return { state: "exited", exitCode };
    case "stopped":
      return { state: "stopped", exitCode };
    default:
      return { state: "running", exitCode: null };
  }
}

/** A background shell job the card last saw running on its machine. */
export function isRunningBackgroundJob(record: Pick<RunnerActivity, "kind" | "status" | "output">): boolean {
  return (
    record.kind === "run_shell" && record.status === "completed" && !!record.output?.background && record.output?.job_status === "running"
  );
}

/**
 * The error line a card shows under its body, or null. A command the user
 * stopped shows none: its status note already says so, and the backend
 * string is neither translated nor an error.
 */
export function runnerActivityErrorLine(record: Pick<RunnerActivity, "status" | "error">): string | null {
  if (record.status === "stopped") return null;
  return record.error || null;
}

// ESC [ ... final byte (CSI), ESC ] ... BEL / ESC \ (OSC), and lone ESC sequences.
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;

/** Terminal output as text: colour codes and cursor controls removed, CRLF normalised. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "").replace(/\r\n/g, "\n");
}

/**
 * `320 ms`, `4.2 s`, `1 min 04 s` - how long a command ran. Each unit is
 * chosen AFTER rounding, so a boundary rolls over (`59 999 ms` is `1 min 00 s`,
 * never `60 s`). The decimal follows the reader's locale: `4,2 s`
 * in French, `4.2 s` in English.
 */
export function formatActivityDuration(ms: number | null | undefined, locale = "en"): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  const millis = Math.round(ms);
  if (millis < 1000) return `${millis} ms`;
  const tenths = Math.round(ms / 100) / 10;
  if (tenths < 10) {
    const decimal = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    return `${decimal.format(tenths)} s`;
  }
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}

/** The command as an accessible name reads it: one line, clipped to `max` characters. */
export function clipCommandForLabel(command: string | null | undefined, max = 40): string {
  const line = (command ?? "").replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 3)}...` : line;
}

/** `1.2 KB`-style size for a card line (`null` for an unknown size). */
export function formatActivityBytes(bytes: number | null | undefined): string | null {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** The prompt a shell card draws before the command, like the machine's own terminal. */
export function shellPrompt(platform: RunnerPlatform | null, cwd: string | null | undefined): string {
  if (platform === "windows") return cwd ? `PS ${cwd}>` : "PS>";
  return cwd ? `${cwd} $` : "$";
}

/** Counts for the header of a block of cards (`Ran 2 commands, read 1 file`). */
interface RunnerActivitySummary {
  commands: number;
  reads: number;
  writes: number;
  listings: number;
  captures: number;
  actions: number;
  /** Distinct machine names, in first-seen order. */
  machines: string[];
}

export function summarizeRunnerActivity(records: readonly RunnerActivity[]): RunnerActivitySummary {
  const summary: RunnerActivitySummary = {
    commands: 0,
    reads: 0,
    writes: 0,
    listings: 0,
    captures: 0,
    actions: 0,
    machines: [],
  };
  for (const record of records) {
    if (record.kind === "run_shell") summary.commands += 1;
    else if (record.kind === "read_file") summary.reads += 1;
    else if (record.kind === "write_file") summary.writes += 1;
    else if (record.kind === "list_dir") summary.listings += 1;
    else if (record.kind === "screenshot" || (record.kind === "browser_control" && record.input.action === "screenshot"))
      summary.captures += 1;
    else summary.actions += 1;
    if (record.runner_name && !summary.machines.includes(record.runner_name)) summary.machines.push(record.runner_name);
  }
  return summary;
}

/** A listing's entries, folders first then names - the order a file manager shows. */
export function sortListingEntries(entries: readonly RunnerActivityEntry[]): RunnerActivityEntry[] {
  return entries
    .slice()
    .sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

/** The command ids a list of messages already renders - the live list hides those. */
export function renderedCommandIds(messages: readonly { runner_activity?: readonly RunnerActivity[] | null }[]): Set<string> {
  const ids = new Set<string>();
  for (const message of messages) {
    for (const record of message.runner_activity ?? []) ids.add(record.command_id);
  }
  return ids;
}
