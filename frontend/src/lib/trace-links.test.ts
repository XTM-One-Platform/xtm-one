/**
 * Unit tests for the Trace Explorer deep links. Run with `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ASSIGNMENT_RUN_TRACE_PARAM,
  ASSIGNMENT_TRACE_PARAM,
  assignmentRunTracesPath,
  assignmentTracesPath,
  isTraceScopeId,
} from "./trace-links.ts";

function parse(path: string) {
  const url = new URL(path, "http://localhost");
  return { pathname: url.pathname, params: [...url.searchParams.entries()] };
}

test("scopes the explorer to an assignment", () => {
  assert.deepEqual(parse(assignmentTracesPath("5d1c")), {
    pathname: "/observability/traces",
    params: [[ASSIGNMENT_TRACE_PARAM, "5d1c"]],
  });
});

test("scopes the explorer to one run", () => {
  assert.deepEqual(parse(assignmentRunTracesPath("9f2e")), {
    pathname: "/observability/traces",
    params: [[ASSIGNMENT_RUN_TRACE_PARAM, "9f2e"]],
  });
});

test("an id never breaks out of its query value", () => {
  assert.deepEqual(parse(assignmentTracesPath("a&status=error#x")), {
    pathname: "/observability/traces",
    params: [[ASSIGNMENT_TRACE_PARAM, "a&status=error#x"]],
  });
});

test("only an id is taken as a scope to look up", () => {
  assert.equal(isTraceScopeId("5d1c0a7e-0000-4000-8000-000000000001"), true);
  assert.equal(isTraceScopeId("5D1C0A7E-0000-4000-8000-000000000001"), true);
  for (const value of ["", " ", "..", "../users/me", "5d1c0a7e", "5d1c0a7e-0000-4000-8000-000000000001/../x"]) {
    assert.equal(isTraceScopeId(value), false, value);
  }
});
