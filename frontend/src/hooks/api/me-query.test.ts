/**
 * One definition of the `/auth/me` read - `yarn test:unit`.
 *
 * Pages read it through `useMe`, the hosts mounted above the routes (the
 * locale, the waiting arcade) through `useMeAboveRoutes`, both in
 * `useAuthApi.ts`. An inline copy of the `["me"]` query drifts from them: a
 * different `enabled`, `retry` or `throwOnError` on one observer changes what
 * every other one sees.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

const SRC = new URL("../../", import.meta.url);

test("only useAuthApi.ts reads /auth/me", () => {
  const readers = readdirSync(SRC, { recursive: true })
    .map((path) => String(path).replace(/\\/g, "/"))
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => /\bapi\.get(<[^>]*>)?\(\s*["'`]\/auth\/me["'`]/.test(readFileSync(new URL(path, SRC), "utf8")))
    .sort();
  assert.deepEqual(readers, ["hooks/api/useAuthApi.ts"]);
});
