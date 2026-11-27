/**
 * A browser profile's sign-in settings in its form - `yarn test:unit`.
 *
 * `keep_storage_keys` is a list in the profile and one comma-separated text in the form: each key once, its case kept;
 * the other settings stay as typed, a blank one left out.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { appsFrom, appsSummary, appUrlInvalid, loginConfigFrom, loginConfigTexts } from "./browser-profile-format.ts";

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

test("an application URL is refused in the form as the save would refuse it", () => {
  assert.equal(appUrlInvalid("risk.example"), true);
  assert.equal(appUrlInvalid("ftp://risk.example.com/"), true);
  assert.equal(appUrlInvalid("https://user:pass@risk.example.com/"), true);
  assert.equal(appUrlInvalid(`https://risk.example.com/${"a".repeat(2000)}`), true);
  assert.equal(appUrlInvalid("https://risk.example.com/"), false);
  assert.equal(appUrlInvalid("  "), false);
});

test("the card names two applications, then how many more, a nameless one by its host", () => {
  assert.deepEqual(appsSummary([{ name: "Risk", url: "https://risk.example.com/" }]), { names: "Risk", more: 0 });
  assert.deepEqual(
    appsSummary([
      { name: "", url: "https://risk.example.com/x" },
      { name: "Intel", url: "https://intel.example.com/" },
      { name: "Third", url: "https://third.example.com/" },
    ]),
    { names: "risk.example.com, Intel", more: 1 },
  );
});
