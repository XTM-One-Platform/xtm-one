/**
 * The coordination access form stores what the server stores - `yarn test:unit`.
 * The same cases as `test_normalize_*` in backend/tests/test_coordination_access.py.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  accessDraft,
  normalizeAccess,
  readScopeAllowed,
  sameAccess,
  withWriteScope,
  type Access,
} from "./coordination-access.ts";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const SELF = "33333333-3333-4333-8333-333333333333";

function access(over: Partial<Access>): Access {
  return { read: "all", write: "all", read_agent_ids: [], write_agent_ids: [], ...over };
}

test("the default stores nothing", () => {
  assert.equal(normalizeAccess(access({})), null);
  assert.equal(normalizeAccess(accessDraft(null)), null);
});

test("writing everywhere means reading everywhere", () => {
  assert.equal(normalizeAccess(access({ read: "self", write: "all" })), null);
});

test("every writer can read", () => {
  assert.deepEqual(normalizeAccess(access({ read: "self", write: "agents", write_agent_ids: [A] })), {
    read: "agents",
    write: "agents",
    read_agent_ids: [A],
    write_agent_ids: [A],
  });
  assert.deepEqual(
    normalizeAccess(access({ read: "agents", write: "agents", read_agent_ids: [B], write_agent_ids: [A] })),
    { read: "agents", write: "agents", read_agent_ids: [B, A], write_agent_ids: [A] },
  );
});

test("the agent itself is never listed and lists count only for their scope", () => {
  assert.deepEqual(
    normalizeAccess(access({ read: "agents", write: "self", read_agent_ids: [A, SELF, A], write_agent_ids: [B] }), SELF),
    { read: "agents", write: "self", read_agent_ids: [A], write_agent_ids: [] },
  );
  assert.deepEqual(normalizeAccess(access({ read: "all", write: "self", read_agent_ids: [A] })), {
    read: "all",
    write: "self",
    read_agent_ids: [],
    write_agent_ids: [],
  });
});

test("a read scope is never narrower than the write scope", () => {
  assert.equal(readScopeAllowed("self", "all"), false);
  assert.equal(readScopeAllowed("agents", "all"), false);
  assert.equal(readScopeAllowed("self", "agents"), false);
  assert.equal(readScopeAllowed("agents", "agents"), true);
  assert.equal(readScopeAllowed("all", "self"), true);
  assert.equal(withWriteScope(access({ read: "self", write: "self" }), "all").read, "all");
  assert.equal(withWriteScope(access({ read: "all", write: "all" }), "self").read, "all");
});

test("an unknown stored scope reads as the narrowest, like the server", () => {
  const draft = accessDraft({ read: "fleet" as never, write: "self", read_agent_ids: [], write_agent_ids: [] });
  assert.equal(draft.read, "self");
});

test("sameAccess ignores list order", () => {
  const one = access({ read: "agents", write: "self", read_agent_ids: [A, B] });
  const two = access({ read: "agents", write: "self", read_agent_ids: [B, A] });
  assert.equal(sameAccess(one, two), true);
  assert.equal(sameAccess(one, null), false);
  assert.equal(sameAccess(null, null), true);
});
