/**
 * Unit tests for the published dashboard page's address - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PUBLIC_DASHBOARD_ROUTE, isPublicDashboardPath } from "./public-dashboard.ts";

test("a published dashboard page is recognised, and nothing else", () => {
  assert.equal(PUBLIC_DASHBOARD_ROUTE, "/public/dashboards");
  assert.equal(isPublicDashboardPath("/public/dashboards/Ab-_9"), true);
  assert.equal(isPublicDashboardPath("/public/dashboards"), false);
  assert.equal(isPublicDashboardPath("/dashboards/0b8f7c1e"), false);
  assert.equal(isPublicDashboardPath("/public/presentations/Ab"), false);
});
