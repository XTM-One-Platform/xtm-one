/**
 * `workspaceReachesOthers`: the FR-022 question is asked exactly when someone
 * else can search what is filed in the workspace.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { workspaceReachesOthers } from "./workspace-sharing.ts";

const ME = "me";

test("a workspace reaches others when someone else owns it or it is shared, never through the person alone", () => {
  const own = { user_id: ME };
  assert.equal(workspaceReachesOthers(own, ME), false);
  assert.equal(workspaceReachesOthers({ ...own, shared_user_ids: [ME] }, ME), false);
  assert.equal(workspaceReachesOthers({ ...own, shared_user_ids: [ME, "bob"] }, ME), true);
  assert.equal(workspaceReachesOthers({ ...own, shared_group_ids: ["g"] }, ME), true);
  assert.equal(workspaceReachesOthers({ ...own, is_company_managed: true }, ME), true);
  assert.equal(workspaceReachesOthers({ user_id: "bob" }, ME), true);
  assert.equal(workspaceReachesOthers({ user_id: null }, ME), false);
});
