/**
 * Every runner command kind the backend can record has a label - `yarn test:unit`.
 *
 * The Machines page "Recent commands" list and the approval card show a kind
 * through `RUNNER_COMMAND_KIND_LABELS`; a kind missing from it rendered as the
 * raw backend value ("shell_job completed"). The kinds are read
 * from the backend's `COMMAND_KIND_CAPABILITY`, so a new kind without a label
 * fails here.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const MODEL = read("../../../backend/app/models/runner.py");
const CONSTANTS = read("./constants.ts");
const EN = JSON.parse(read("../i18n/lang/en.json")) as Record<string, string>;

function backendKinds(): string[] {
  const block = MODEL.match(/COMMAND_KIND_CAPABILITY[^=]*=\s*\{([\s\S]*?)\n\}/)?.[1];
  assert.ok(block, "COMMAND_KIND_CAPABILITY not found in backend/app/models/runner.py");
  return [...block.matchAll(/^\s*"([a-z_]+)"\s*:/gm)].map((m) => m[1]);
}

function frontendLabels(): Record<string, string> {
  const block = CONSTANTS.match(/RUNNER_COMMAND_KIND_LABELS[^=]*=\s*\{([\s\S]*?)\n\};/)?.[1];
  assert.ok(block, "RUNNER_COMMAND_KIND_LABELS not found in constants.ts");
  return Object.fromEntries([...block.matchAll(/^\s*([a-z_]+)\s*:\s*"([^"]+)"/gm)].map((m) => [m[1], m[2]]));
}

test("every backend command kind has a label", () => {
  const kinds = backendKinds();
  const labels = frontendLabels();
  assert.ok(kinds.includes("shell_job"), "shell_job is a backend kind");
  for (const kind of kinds) assert.ok(labels[kind], `no label for the runner command kind ${kind}`);
});

test("the background job label is translated", () => {
  const label = frontendLabels().shell_job;
  assert.ok(EN[label], `the shell_job label ("${label}") is not in en.json`);
});
