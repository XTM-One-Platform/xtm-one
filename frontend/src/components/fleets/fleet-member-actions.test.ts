/**
 * One behaviour for a member action wherever it starts: the graph's
 * node menu and the members panel share the menu, the change-parent dialog
 * and the remove action, and removing always asks first. The "no hand-rolled
 * menu" guard covers all of src/: src/no-hand-rolled-menu.test.ts.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { removeMemberCopy } from "./fleet-edition";

// Vitest runs from frontend/ (as the architecture block tests read their fixtures).
const DIR = resolve(process.cwd(), "src/components/fleets");
const read = (name: string) => readFileSync(join(DIR, name), "utf8");

test("a home member is told it goes back to the playground; a connection is not", () => {
  const home = removeMemberCopy({ name: "Triage", role: "member" });
  assert.equal(home.title, "Remove {name} from the fleet?");
  assert.deepEqual(home.titleValues, { name: "Triage" });
  assert.equal(home.description, "The agent is back in the playground, unchanged.");
  assert.equal(removeMemberCopy({ name: "Intel", role: "connected" }).description, undefined);
});

test("the node menu and the members panel share the change-parent dialog and the remove action", () => {
  for (const file of ["FleetNodeMenu.tsx", "FleetMembersPanel.tsx"]) {
    const source = read(file);
    assert.match(source, /<ChangeParentDialog\b/, file);
    assert.match(source, /useRemoveFleetMemberAction\(fleet\.id\)/, file);
    assert.match(source, /\{confirmElement\}/, file);
    // No second copy of the parent picker or of the removal call.
    assert.doesNotMatch(source, /parentCandidates\(/, file);
    assert.doesNotMatch(source, /useRemoveFleetMember\(/, file);
  }
});

test("removing asks first", () => {
  const actions = read("useRemoveFleetMemberAction.tsx");
  const confirmAt = actions.indexOf("await confirm(");
  const mutateAt = actions.indexOf("remove.mutate(");
  assert.ok(confirmAt > 0 && mutateAt > confirmAt);
  assert.match(actions, /if \(!ok\) return;/);
});
