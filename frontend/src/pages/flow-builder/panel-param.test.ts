import assert from "node:assert/strict";
import { test } from "node:test";

import { ownerAccessUrl, wantsOwnerAccessPanel } from "./panel-param.ts";

test("the paused-flow notice's link is exactly the contract the builder reads", () => {
  const flowId = "5b0f1a52-7c1e-4d3a-9f64-2f0c7a1d9e10";
  // The notice spells this URL on its own side; changing either
  // end breaks the other, so the string itself is pinned.
  assert.equal(ownerAccessUrl(flowId), `/flows/library/${flowId}?panel=owner-access`);
  const url = new URL(ownerAccessUrl(flowId), "https://xtm.example");
  assert.equal(wantsOwnerAccessPanel(url.searchParams), true);
});

test("only the owner-access value opens the drawer", () => {
  assert.equal(wantsOwnerAccessPanel(new URLSearchParams("")), false);
  assert.equal(wantsOwnerAccessPanel(new URLSearchParams("panel=versions")), false);
  assert.equal(wantsOwnerAccessPanel(new URLSearchParams("run=abc")), false);
  assert.equal(wantsOwnerAccessPanel(new URLSearchParams("panel=owner-access&run=abc")), true);
});
