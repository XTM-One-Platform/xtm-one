/**
 * A browser profile's sign-in settings in its form - `yarn test:unit`.
 *
 * `keep_storage_keys` is a list in the profile and one comma-separated text in the form: each key once, its case kept;
 * the other settings stay as typed, a blank one left out.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { appsFrom, loginConfigFrom, loginConfigTexts } from "./browser-profile-format.ts";

test("the storage keys a profile keeps read as one text in the form", () => {
  assert.deepEqual(loginConfigTexts({ keep_storage_keys: ["auth", "msal.Token"], signed_in_selector: "#app" }), {
    keep_storage_keys: "auth, msal.Token",
    signed_in_selector: "#app",
  });
  assert.deepEqual(loginConfigTexts(null), {});
});

test("the form's text is saved as the list of storage keys", () => {
  assert.deepEqual(
    loginConfigFrom({ keep_storage_keys: "auth, msal.Token\nauth, ", signed_in_selector: "#app", submit_selector: "  " }),
    { keep_storage_keys: ["auth", "msal.Token"], signed_in_selector: "#app" },
  );
  assert.deepEqual(loginConfigFrom({ keep_storage_keys: " , " }), {});
});

test("the applications a profile also signs in are saved trimmed, a row without a URL left out", () => {
  assert.deepEqual(
    appsFrom([
      { name: " Risk ", url: " https://risk.example.com/ " },
      { name: "Blank", url: "  " },
    ]),
    [{ name: "Risk", url: "https://risk.example.com/" }],
  );
});
