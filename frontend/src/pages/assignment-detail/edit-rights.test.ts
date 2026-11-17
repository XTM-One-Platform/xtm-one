/**
 * Unit tests for what the assignment page lets a viewer do - `yarn test:unit`.
 *
 * A built-in agent's assignments stay read-only in XTM One; an administrator
 * may start a run on any of them, and turn on and point the run-as account of
 * a seeded opt-in one - nothing more.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { assignmentEditRights } from "./edit-rights.ts";

const base = {
  agentIsBuiltin: false,
  isXtmOne: true,
  hasManageRights: true,
  isAdmin: false,
  builtinAdminOptIn: false,
};

test("an agent the viewer manages is fully editable", () => {
  assert.deepEqual(assignmentEditRights(base), {
    canManage: true,
    canToggle: true,
    canChooseRunAs: false,
    canRunNow: true,
  });
});

test("an admin on a built-in's opt-in assignment toggles it and picks its run-as, nothing else", () => {
  assert.deepEqual(
    assignmentEditRights({ ...base, agentIsBuiltin: true, isAdmin: true, builtinAdminOptIn: true }),
    { canManage: false, canToggle: true, canChooseRunAs: true, canRunNow: true },
  );
});

test("an admin on any other built-in assignment may only run it", () => {
  assert.deepEqual(
    assignmentEditRights({ ...base, agentIsBuiltin: true, isAdmin: true }),
    { canManage: false, canToggle: false, canChooseRunAs: false, canRunNow: true },
  );
});

test("a non-admin gets nothing on a built-in, opt-in or not", () => {
  for (const builtinAdminOptIn of [false, true]) {
    assert.deepEqual(
      assignmentEditRights({ ...base, agentIsBuiltin: true, hasManageRights: false, builtinAdminOptIn }),
      { canManage: false, canToggle: false, canChooseRunAs: false, canRunNow: false },
    );
  }
});

test("outside XTM One a built-in is an ordinary agent", () => {
  assert.deepEqual(
    assignmentEditRights({ ...base, agentIsBuiltin: true, isXtmOne: false, builtinAdminOptIn: true }),
    { canManage: true, canToggle: true, canChooseRunAs: false, canRunNow: true },
  );
});
