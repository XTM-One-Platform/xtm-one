/**
 * Saving the profile refreshes the canonical assets - `yarn test:unit`.
 *
 * Read off the source: the hook needs React and the `@/` alias, and
 * `yarn test:unit` has neither. The Inventory Assets tab and the asset page
 * read ["organization-assets", ...] (useOrganizationAssetsApi); Add, Update and
 * Delete save the profile, so the save must invalidate that prefix (#1375 review).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const hook = readFileSync(new URL("./useOrganizationContextApi.ts", import.meta.url), "utf8");
const assets = readFileSync(new URL("./useOrganizationAssetsApi.ts", import.meta.url), "utf8");
const start = hook.indexOf("export function useSaveOrganizationContext(");
const save = hook.slice(start, start + hook.slice(start).search(/\r?\n\}\r?\n/));

test("the canonical assets live under the organization-assets prefix", () => {
  assert.match(assets, /const KEY = \["organization-assets"\] as const;/);
});

test("a saved profile updates its own cache and invalidates the canonical assets", () => {
  assert.notEqual(start, -1);
  assert.match(save, /qc\.setQueryData\(KEY, saved\)/);
  assert.match(save, /invalidateQueries\(\{ queryKey: \["organization-assets"\] \}\)/);
});
