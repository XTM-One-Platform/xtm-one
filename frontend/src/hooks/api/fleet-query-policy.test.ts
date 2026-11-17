import { test } from "vitest";
import assert from "node:assert/strict";

import { BackendUnavailableError } from "@/lib/api";
import { FLEET_QUERY_POLICY } from "./useFleetApi";

const LICENCE_WALL = new BackendUnavailableError("Enterprise license required. Contact your administrator.");

function withStatus(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

test("the Copilot-mode licence wall is rendered by the page: no retry, no ErrorBoundary", () => {
  assert.equal(FLEET_QUERY_POLICY.retry(0, LICENCE_WALL), false);
  assert.equal(FLEET_QUERY_POLICY.throwOnError(LICENCE_WALL), false);
});

test("a 403 or a 404 from a fleet read is rendered by the page", () => {
  for (const status of [403, 404]) {
    assert.equal(FLEET_QUERY_POLICY.retry(0, withStatus(status)), false);
    assert.equal(FLEET_QUERY_POLICY.throwOnError(withStatus(status)), false);
  }
});

test("a gateway error is retried, then thrown to the ErrorBoundary", () => {
  const gateway = new BackendUnavailableError("Bad gateway");
  assert.equal(FLEET_QUERY_POLICY.retry(0, gateway), true);
  assert.equal(FLEET_QUERY_POLICY.retry(2, gateway), false);
  assert.equal(FLEET_QUERY_POLICY.throwOnError(gateway), true);
  assert.equal(FLEET_QUERY_POLICY.throwOnError(withStatus(500)), true);
});
