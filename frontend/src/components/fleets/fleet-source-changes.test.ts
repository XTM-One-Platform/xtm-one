// The "source changed since you forked it" notice's pure parts (#9174, slice 3b):
// when it shows, and the line each structural change reads as.
import { test } from "vitest";
import assert from "node:assert/strict";

import type { FleetSourceChange, FleetSourceChanges } from "@/hooks/api/useFleetApi";

import {
  changedFieldLabels,
  currentSourceName,
  describeSourceChange,
  showSourceChanges,
  sourceChanged,
  sourceChangeNames,
  sourceSuccessor,
} from "./fleet-source-changes";

const t = (text: string, values?: Record<string, string | number>) =>
  values ? text.replace(/\{(\w+)\}/g, (_, k: string) => String(values[k])) : text;

const base: FleetSourceChanges = { is_fork: true, source_exists: true, changed: true, diff_available: true, changes: [] };

test("shows only on a fork whose existing source changed", () => {
  assert.equal(showSourceChanges(undefined), false);
  assert.equal(showSourceChanges({ ...base, is_fork: false, changed: null }), false);
  assert.equal(showSourceChanges({ ...base, source_exists: false, changed: null }), false);
  assert.equal(showSourceChanges({ ...base, changed: false }), false);
  assert.equal(showSourceChanges(base), true);
  // An old fork: changed is known, the detail is not; the notice still shows.
  assert.equal(showSourceChanges({ ...base, diff_available: false }), true);
});

const successor = { fleet_id: "f-loop", slug: "ctem-loop", name: "CTEM loop" };

test("a fork of a retired built-in fleet shows its successor, with or without a diff (CTEM-31)", () => {
  // The retired source is gone: no diff, but the successor is named.
  const gone: FleetSourceChanges = { ...base, source_exists: false, changed: null, diff_available: false, successor };
  assert.equal(showSourceChanges(gone), true);
  assert.equal(sourceChanged(gone), false);
  assert.deepEqual(sourceSuccessor(gone), successor);
  // The source row was adopted under the new slug and also changed: both apply.
  const adopted: FleetSourceChanges = { ...base, successor };
  assert.equal(sourceChanged(adopted), true);
  assert.deepEqual(sourceSuccessor(adopted), successor);
  // Not a fork: never a notice, even with a successor.
  assert.equal(showSourceChanges({ ...gone, is_fork: false }), false);
  assert.equal(sourceSuccessor({ ...base, successor: null }), null);
});

test("each change names the object and its action", () => {
  assert.equal(describeSourceChange({ object: "fleet", name: "F2", action: "changed", fields: ["name"] }, t), "The fleet's card changed");
  assert.equal(describeSourceChange({ object: "member", name: "triage", action: "added", fields: [] }, t), "Member triage added");
  assert.equal(describeSourceChange({ object: "member", name: "triage", action: "removed", fields: [] }, t), "Member triage removed");
  assert.equal(describeSourceChange({ object: "member", name: "triage", action: "changed", fields: ["persona"] }, t), "Member triage changed");
});

const names = sourceChangeNames(
  [
    { id: "a-1", slug: "lab-sample-researcher", name: "Copy of Lab sample Researcher" },
    { id: "a-2", slug: "triage", name: "Triage analyst" },
  ],
  [{ id: "f-1", name: "Intel fleet" }],
);

test("a member reads by its agent's display name, the slug only when no name is known", () => {
  const changed: FleetSourceChange = { object: "member", name: "lab-sample-researcher", action: "changed", fields: ["role_label"] };
  assert.equal(describeSourceChange(changed, t, names), "Member Copy of Lab sample Researcher changed");
  assert.equal(describeSourceChange({ ...changed, name: "unknown-slug" }, t, names), "Member unknown-slug changed");
});

test("a connected agent or fleet reads by its name and never by its id", () => {
  assert.equal(describeSourceChange({ object: "connected", name: "a-2", action: "removed", fields: [] }, t, names), "Connected agent Triage analyst removed");
  assert.equal(describeSourceChange({ object: "connected", name: "f-1", action: "added", fields: [] }, t, names), "Connected fleet Intel fleet added");
  assert.equal(
    describeSourceChange({ object: "connected", name: "f-1", action: "changed", fields: ["inclusion"] }, t, names),
    "Connected fleet Intel fleet changed",
  );
  const id = "44d08170-72ff-4817-9c1b-b69208befe1c";
  for (const action of ["added", "removed", "changed"] as const) {
    const line = describeSourceChange({ object: "connected", name: id, action, fields: [] }, t, names);
    assert.ok(!line.includes(id), line);
    assert.ok(line.startsWith("A connected agent or fleet you cannot open"), line);
  }
});

test("the title names the source as it reads now after a rename", () => {
  assert.equal(currentSourceName({ changes: [] }, "Kept"), "Kept");
  assert.equal(currentSourceName({ changes: [{ object: "fleet", name: "Renamed", action: "changed", fields: ["name"] }] }, "Kept"), "Renamed");
  assert.equal(currentSourceName({ changes: [{ object: "fleet", name: "Kept", action: "changed", fields: ["skills"] }] }, "Kept"), "Kept");
});

test("field labels are translated, deduplicated, and an unknown field is shown as sent", () => {
  assert.deepEqual(changedFieldLabels({ fields: ["persona", "parent"] }, t), ["Persona", "Reports to"]);
  // skills (the card) and skill_ids (a member) both read "Skills": listed once.
  assert.deepEqual(changedFieldLabels({ fields: ["skills", "skill_ids"] }, t), ["Skills"]);
  assert.deepEqual(changedFieldLabels({ fields: ["future_field"] }, t), ["future_field"]);
});
