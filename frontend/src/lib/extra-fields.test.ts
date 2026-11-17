/**
 * Unit tests for the OAuth default form's stored secrets - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { EXTRA_CONFIG_MASK, extraConfigPatch, maskedStoredSecrets } from "./extra-fields.ts";

test("a stored secret starts as the mask, so saving the form untouched keeps it", () => {
  assert.deepEqual(maskedStoredSecrets(["webhook_secret"]), { webhook_secret: EXTRA_CONFIG_MASK });
});

test("nothing stored starts empty", () => {
  assert.deepEqual(maskedStoredSecrets([]), {});
  assert.deepEqual(maskedStoredSecrets(undefined), {});
});

test("the edit form's patch removes, as null, every stored key the form dropped", () => {
  assert.deepEqual(
    extraConfigPatch({ base_url: "https://b" }, { base_url: "https://a", credential_mode: "x" }),
    { base_url: "https://b", credential_mode: null },
  );
});

test("the edit form's patch keeps what it holds and adds nothing when nothing is stored", () => {
  assert.deepEqual(extraConfigPatch({ backend: "gw" }, null), { backend: "gw" });
  assert.deepEqual(extraConfigPatch({}, undefined), {});
});
