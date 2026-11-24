/**
 * A settings card saves its own fields, never the asset rows - `yarn test:unit` (#1738, #1752).
 *
 * Read off the source, like organization-context-save-refresh.test.ts: the hook
 * and the cards need React and the `@/` alias. Every card of Settings >
 * Organization context and of CTEM > Inventory saves through PATCH with its own
 * fields; a synced asset row a sync moved since the last version must never
 * travel with them (a whole-profile PUT took minutes on a 3,000-asset
 * inventory), and a refused save shows the card's toast alone.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const hook = read("./useOrganizationContextApi.ts");
const start = hook.indexOf("export function usePatchOrganizationContext(");
const patch = hook.slice(start, start + hook.slice(start).search(/\r?\n\}\r?\n/));

test("the patch hook sends PATCH and leaves the error toast to the card", () => {
  assert.notEqual(start, -1);
  assert.match(patch, /api\.patch\("\/organization-context", body\)/);
  assert.match(patch, /suppressGlobalErrorToast: true/);
});

test("the outside lens card patches its three fields and no asset row", () => {
  const card = read("../../pages/settings/OrganizationOutsideLensCard.tsx");
  assert.match(card, /usePatchOrganizationContext\(\)/);
  assert.doesNotMatch(card, /useSaveOrganizationContext/);
  assert.match(card, /fields: \{ owned_domains, address_ranges, attack_surface \}/);
  assert.doesNotMatch(card, /assets_exposure|asset_groups/);
});

/** The cards that save one or a few profile fields: each patches them alone (#1752). */
const CARDS: Record<string, RegExp> = {
  "../../pages/settings/OrganizationEvidenceHealthCard.tsx": /fields: \{ evidence_health: /,
  "../../pages/xtm-one/ctem/InventorySourcesCard.tsx": /fields: \{ asset_identity: /,
  "../../pages/xtm-one/ctem/loop/SignalPathSourcesSheet.tsx": /fields: next,/,
  "../../pages/xtm-one/ctem/CaseAttentionWeightsDialog.tsx": /fields: \{ case_attention: /,
  "../../pages/xtm-one/CtemGovernance.tsx": /fields: \{ posture_target: value \}/,
};

for (const [path, fields] of Object.entries(CARDS)) {
  test(`${path.split("/").pop()} patches its own fields and never PUTs the profile`, () => {
    const card = read(path);
    assert.match(card, /usePatchOrganizationContext\(\)/);
    assert.doesNotMatch(card, /useSaveOrganizationContext/);
    assert.match(card, fields);
    assert.match(card, /based_on_version: /);
    assert.doesNotMatch(card, /assets_exposure|asset_groups/);
  });
}

test("the settings list and policy cards patch their field; the asset rows are never written from settings", () => {
  const section = read("../../pages/settings/OrganizationSection.tsx");
  assert.match(section, /patch\.mutate\(\{ fields: \{ \[key\]: parsed\.profile\[key\] \}/);
  // The Prioritization policy card carries the PIR list with its field (#1889); every other card patches its field alone.
  assert.match(section, /patch\.mutate\(\{ fields: \{ \[field\]: parsed\.profile\[field\], \.\.\.withPirs \}/);
  assert.match(section, /const withPirs = field === "prioritization_policy" \? \{ pir_ids: parsed\.profile\.pir_ids, pir_names: parsed\.profile\.pir_names \} : \{\};/);
  // The asset and group rows are written through their row APIs on CTEM > Inventory (#1752 slice 3b).
  assert.match(section, /if \(field === "assets_exposure" \|\| field === "asset_groups"\) return;/);
  assert.doesNotMatch(section, /field === "asset_groups"\) save\.mutate/);
});
