/**
 * A settings card saves its own fields, never the asset rows - `yarn test:unit` (#1738).
 *
 * Read off the source, like organization-context-save-refresh.test.ts: the hook
 * and the card need React and the `@/` alias. The outside lens card saves the
 * owned domains, address ranges and attack surface through PATCH; a synced
 * asset row a sync moved since the last version must never travel with them,
 * and a refused save shows the card's toast alone.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const hook = readFileSync(new URL("./useOrganizationContextApi.ts", import.meta.url), "utf8");
const card = readFileSync(new URL("../../pages/settings/OrganizationOutsideLensCard.tsx", import.meta.url), "utf8");
const start = hook.indexOf("export function usePatchOrganizationContext(");
const patch = hook.slice(start, start + hook.slice(start).search(/\r?\n\}\r?\n/));

test("the patch hook sends PATCH and leaves the error toast to the card", () => {
  assert.notEqual(start, -1);
  assert.match(patch, /api\.patch\("\/organization-context", body\)/);
  assert.match(patch, /suppressGlobalErrorToast: true/);
});

test("the outside lens card patches its three fields and no asset row", () => {
  assert.match(card, /usePatchOrganizationContext\(\)/);
  assert.doesNotMatch(card, /useSaveOrganizationContext/);
  assert.match(card, /fields: \{ owned_domains, address_ranges, attack_surface \}/);
  assert.doesNotMatch(card, /assets_exposure|asset_groups/);
});
