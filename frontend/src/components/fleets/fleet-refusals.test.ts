/**
 * A refused fleet member write reads in the user's language - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { BackendUnavailableError } from "@/lib/api";

import { MEMBER_REFUSAL_SENTENCES, fleetRefusalMessage, matchFleetRefusal } from "./fleet-refusals";
import { describeFleetRefusal } from "./new-fleet-form";

const t = (text: string) => `t:${text}`;
const refused = (message: string, status: number) => Object.assign(new Error(message), { status });

test("a known server refusal becomes its catalog sentence", () => {
  assert.equal(
    fleetRefusalMessage(refused("This fleet is archived: restore it to change its members.", 409), t),
    "t:This fleet is archived: restore it to edit its members.",
  );
  assert.equal(
    fleetRefusalMessage(
      refused("'Scout' is already a member of the fleet 'SOC'. An agent has one home fleet: remove it there first, or add it here as a connected agent.", 409),
      t,
    ),
    "t:An agent you picked already has a home fleet: remove it there first, or connect it instead.",
  );
  assert.equal(
    fleetRefusalMessage(refused("'Scout' is already a member of this fleet.", 409), t),
    "t:The agent is already a member of this fleet.",
  );
  assert.equal(
    fleetRefusalMessage(refused("That parent reports to this member: the hierarchy would loop.", 400), t),
    "t:That parent reports to this member: the hierarchy would loop.",
  );
  assert.equal(fleetRefusalMessage(refused("Already connected to this fleet.", 409), t), "t:Already connected to this fleet.");
});

test("the refusals once shown in raw English have a catalog sentence", () => {
  const cases: [string, number, string][] = [
    ["A member reports to another member of the fleet, never to a connected one.", 400, MEMBER_REFUSAL_SENTENCES.parent_connected],
    ["Only a home member moves to another fleet; a connected row is removed.", 400, MEMBER_REFUSAL_SENTENCES.move_connected],
    ["That workspace is archived.", 400, MEMBER_REFUSAL_SENTENCES.archived_workspace],
    ["Agent 3f2a is not a member of this fleet.", 400, MEMBER_REFUSAL_SENTENCES.exposed_not_member],
    [
      "'Scout' is a member of the fleet 'SOC' and is not exposed to other fleets. Hand over to the fleet's orchestrator or one of its exposed agents, or ask the fleet's owner to expose this agent.",
      422,
      MEMBER_REFUSAL_SENTENCES.not_exposed,
    ],
  ];
  for (const [raw, status, sentence] of cases) assert.equal(fleetRefusalMessage(refused(raw, status), t), `t:${sentence}`, raw);
});

test("one matcher table serves both surfaces, each with its own sentence", () => {
  const twice = refused("'Scout' is already a member of this fleet.", 409);
  assert.equal(matchFleetRefusal(twice), "already_member");
  assert.equal(fleetRefusalMessage(twice, t), "t:The agent is already a member of this fleet.");
  assert.equal(describeFleetRefusal(twice, t).message, "t:An agent was picked twice.");
  const workspace = refused("That workspace is archived.", 400);
  assert.equal(describeFleetRefusal(workspace, t).message, fleetRefusalMessage(workspace, t));
  assert.equal(matchFleetRefusal(refused("HTTP 500", 500)), undefined);
});

test("a 403 is a localized refusal, an unknown 4xx keeps the server's words", () => {
  assert.equal(fleetRefusalMessage(refused("Forbidden", 403), t), "t:You are not allowed to change this fleet.");
  assert.equal(fleetRefusalMessage(refused("Member not found", 404), t), "Member not found");
  assert.equal(fleetRefusalMessage("boom", t), undefined);
});

test("a 5xx or a network error reads 'Try again in a moment.', never the server's raw text", () => {
  for (const error of [
    refused("Internal Server Error", 500),
    refused("HTTP 500", 500),
    refused("Upstream timed out", 503),
    new BackendUnavailableError("Server unavailable (HTTP 502)"),
    new BackendUnavailableError("Failed to fetch"),
    new TypeError("Failed to fetch"),
  ]) {
    assert.equal(fleetRefusalMessage(error, t), "t:Try again in a moment.", error.message);
  }
  // A known refusal still wins, whatever the status.
  assert.equal(
    fleetRefusalMessage(refused("That parent reports to this member: the hierarchy would loop.", 500), t),
    "t:That parent reports to this member: the hierarchy would loop.",
  );
});

test("a fetch rejection reads 'Try again in a moment.' in every browser's words", () => {
  for (const message of [
    "Failed to fetch",
    "NetworkError when attempting to fetch resource.",
    "Load failed",
    "Network request failed",
  ]) {
    assert.equal(fleetRefusalMessage(new TypeError(message), t), "t:Try again in a moment.", message);
  }
  // doFetch (lib/api.ts) wraps the rejection: whatever its words, it is a network error.
  assert.equal(fleetRefusalMessage(new BackendUnavailableError("Load failed"), t), "t:Try again in a moment.");
});

test("a client-side TypeError keeps its own words, never 'Try again in a moment.'", () => {
  for (const message of [
    "Cannot read properties of undefined (reading 'id')",
    "undefined is not a function",
    "Failed to fetch dynamically imported module: /assets/FleetPage.js",
  ]) {
    assert.equal(fleetRefusalMessage(new TypeError(message), t), message);
  }
});

// Vitest runs from frontend/.
const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

test("every fleet write leaves its refusal to its caller: one toast, never two", () => {
  const hooks = read("src/hooks/api/useFleetApi.ts");
  const mutations = hooks.split("useMutation<").slice(1);
  assert.ok(mutations.length >= 8);
  for (const body of mutations) assert.match(body.split("\n  });")[0], /meta: OWN_ERROR_TOAST/);
});

test("no fleet surface shows a member write's raw English refusal", () => {
  for (const file of ["FleetMembersPanel.tsx", "useRemoveFleetMemberAction.tsx", "useReparentFleetMember.tsx", "FleetSettingsPanel.tsx"]) {
    const source = read(`src/components/fleets/${file}`);
    assert.match(source, /fleetRefusalMessage\(err, t_i18n\)/, file);
    assert.doesNotMatch(source, /description: err instanceof Error \? err\.message/, file);
  }
  // Archive, restore and delete included.
  assert.doesNotMatch(read("src/components/fleets/FleetSettingsPanel.tsx"), /err instanceof Error \? err\.message/);
});

test("a settings save whose exposure is refused says the rest was saved", () => {
  const source = read("src/components/fleets/FleetSettingsPanel.tsx");
  const save = source.slice(source.indexOf("const save = async"), source.indexOf("const toggleArchive"));
  assert.ok(save.indexOf("updateFleet.mutateAsync(body)") < save.indexOf("setExposure.mutateAsync(exposure)"));
  assert.match(save, /The other settings were saved, but the exposure was not/);
});
