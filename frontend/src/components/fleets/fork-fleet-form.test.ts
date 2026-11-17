// The "Duplicate fleet" dialog's pure parts (#9174): the request it sends,
// what a refusal says, the toast after a fork and the provenance line.
import { test } from "vitest";
import assert from "node:assert/strict";

import type { Fleet, FleetForkResult } from "@/hooks/api/useFleetApi";

import { describeForkRefusal, forkProvenance, forkSummary, initialForkForm, toForkRequest } from "./fork-fleet-form";

const t = (text: string, values?: Record<string, string | number>) =>
  values ? `${text} ${JSON.stringify(values)}` : text;

test("the form starts on Copy of the source's name, localized, every member copied, a new workspace", () => {
  const fr = (text: string, values?: Record<string, string | number>) =>
    text === "Copy of {name}" ? `Copie de ${values?.name}` : text;
  assert.deepEqual(initialForkForm({ name: "Vulnerability prioritization" }, fr), {
    name: "Copie de Vulnerability prioritization",
    members: "copy",
    workspaceId: null,
  });
});

test("the request trims the name and leaves an empty one to the server", () => {
  assert.deepEqual(toForkRequest({ name: "  My office  ", members: "keep", workspaceId: "w1" }), {
    name: "My office",
    workspace_id: "w1",
    members: "keep",
  });
  assert.equal(toForkRequest({ name: "   ", members: "copy", workspaceId: null }).name, null);
});

test("a refusal says why, with the API's words as a detail", () => {
  const forbidden = Object.assign(new Error("Not allowed"), { status: 403 });
  assert.deepEqual(describeForkRefusal(forbidden, (s) => s), {
    message: "You are not allowed to duplicate this fleet.",
    detail: "Not allowed",
  });
  const noOrchestrator = Object.assign(new Error("no orchestrator"), { status: 400 });
  assert.match(describeForkRefusal(noOrchestrator, (s) => s).message, /no orchestrator/);
  assert.deepEqual(describeForkRefusal("boom", (s) => s), { message: "The fleet was not duplicated.", detail: null });
});

function result(extra: Partial<FleetForkResult> = {}): FleetForkResult {
  return {
    fleet: { id: "f2", name: "My office" } as Fleet,
    copied_agent_ids: { a1: "b1" },
    kept_agent_ids: [],
    assignments_copied: 2,
    assignments_enabled: 0,
    warnings: [],
    ...extra,
  };
}

test("the toast says the copied automations are switched off, and lists what was left out", () => {
  const clean = forkSummary(result(), t);
  assert.equal(clean.variant, "success");
  assert.equal(clean.lines.length, 2);
  assert.match(clean.lines[1], /switched off/);
  assert.match(clean.lines[1], /"count":2/);

  const partial = forkSummary(result({ warnings: ["'Dev': 1 binding(s) you cannot use were not copied"] }), t);
  assert.equal(partial.variant, "info");
  assert.equal(partial.lines[partial.lines.length - 1], "'Dev': 1 binding(s) you cannot use were not copied");
});

test("only a fork carries a provenance line", () => {
  assert.equal(forkProvenance({ forked_from: null }), null);
  assert.equal(forkProvenance({}), null);
  assert.deepEqual(
    forkProvenance({
      forked_from: {
        fleet_id: "f1",
        slug: "f2-vuln",
        name: "Vulnerability prioritization",
        is_builtin: true,
        fingerprint: "0123456789abcdef",
        members: "copy",
        forked_at: "2026-11-06T12:00:00Z",
      },
    }),
    { name: "Vulnerability prioritization", isBuiltin: true, sourceId: "f1", forkedAt: "2026-11-06T12:00:00Z" },
  );
});
