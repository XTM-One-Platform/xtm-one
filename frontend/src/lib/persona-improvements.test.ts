/**
 * Unit tests for the persona self-improvement loop's UI rules - `yarn test:unit`.
 *
 * The scheduler settles a cycle server-side; the list's poll is what notices,
 * and a settled cycle is when the agent's persona and version history may
 * have changed under the page.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { ImprovementProposal, ImprovementProposalList, ImprovementProposalStatus } from "@/types";
import { cycleSettled, isPersonaRewrite } from "./persona-improvements.ts";

function list(...cycles: [string, ImprovementProposalStatus][]): ImprovementProposalList {
  return {
    can_propose: false,
    items: cycles.map(([id, status]) => ({ id, status }) as ImprovementProposal),
  };
}

test("a change that leaves its watch is settled, whatever the verdict", () => {
  for (const verdict of ["rolled_back", "kept", "regression_kept", "failed"] as const) {
    assert.equal(cycleSettled(list(["a", "watching"]), list(["a", verdict])), true, verdict);
    assert.equal(cycleSettled(list(["a", "shadow"]), list(["a", verdict])), true, verdict);
  }
});

test("the first read and a change still being judged settle nothing", () => {
  assert.equal(cycleSettled(undefined, list(["a", "rolled_back"])), false);
  assert.equal(cycleSettled(list(["a", "watching"]), list(["a", "watching"])), false);
  assert.equal(cycleSettled(list(["a", "applying"]), list(["a", "shadow"])), false);
  assert.equal(cycleSettled(list(["a", "pending_approval"]), list(["a", "watching"])), false);
});

test("the persona card keeps the rewrites a person asked for, never the Concierge's proposals", () => {
  assert.equal(isPersonaRewrite({ proposal_type: "persona", author_agent_id: null }), true);
  assert.equal(isPersonaRewrite({ proposal_type: "persona", author_agent_id: "concierge" }), false);
  assert.equal(isPersonaRewrite({ proposal_type: "new_flow", author_agent_id: null }), false);
  assert.equal(isPersonaRewrite({ proposal_type: "skill", author_agent_id: "concierge" }), false);
});
