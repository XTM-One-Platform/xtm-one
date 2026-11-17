/**
 * The tolerant A2A verification read - `yarn test:unit`.
 *
 * Read off the source: the hook needs React and the `@/` alias, and
 * `yarn test:unit` has neither. The A2A e2e suite drives both properties
 * through the real pages.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const hook = readFileSync(new URL("./useA2AClientApi.ts", import.meta.url), "utf8");
const start = hook.indexOf("export function useA2AVerificationState(");
const read = hook.slice(start, start + hook.slice(start).search(/\r?\n\}\r?\n/));

test("a tolerant read overrides the strict policy rather than being overridden by it", () => {
  assert.notEqual(start, -1);
  const strict = read.indexOf("...unavailableIsOrdinary");
  const override = read.indexOf("...(tolerant ? { throwOnError: false } : {})");
  assert.notEqual(strict, -1);
  assert.ok(override > strict, "spread before the strict policy, `throwOnError: false` is overwritten");
});

test("a tolerant read keeps a cache entry of its own, under the strict key's prefix", () => {
  // A strict reader mounting on an error it would throw does not refetch, so a
  // shared entry would replay the badge's failure on the integration page. The
  // prefix is what accepting a changed card invalidates.
  assert.match(
    read,
    /queryKey: \["a2a-client-verification", integrationId, \.\.\.\(tolerant \? \["tolerant"\] : \[\]\)\]/,
  );
  assert.match(hook, /invalidateQueries\(\{ queryKey: \["a2a-client-verification", integrationId\] \}\)/);
});

test("a read that needs one answer turns the poll off, and every other read keeps it", () => {
  assert.match(read, /refetchInterval: options\.refetchInterval \?\? REFETCH_INTERVAL_SLOW,/);
});
