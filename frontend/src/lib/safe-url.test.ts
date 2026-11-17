/**
 * `safeHttpUrl()` - the one rule an `href` fed by a third party goes through.
 * `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { HOSTILE_URLS } from "../testing/hostile-urls.ts";
import { safeHttpUrl } from "./safe-url.ts";

test("an http(s) address is returned parsed", () => {
  assert.equal(safeHttpUrl("https://example.com/a?b=1#c"), "https://example.com/a?b=1#c");
  assert.equal(safeHttpUrl("http://intranet.local"), "http://intranet.local/");
  assert.equal(safeHttpUrl(" HTTPS://Example.com/Path"), "https://example.com/Path");
});

for (const [label, value] of HOSTILE_URLS) {
  test(`refused: ${label}`, () => {
    assert.equal(safeHttpUrl(value), null);
  });
}

test("no value, no link", () => {
  assert.equal(safeHttpUrl(""), null);
  assert.equal(safeHttpUrl("   "), null);
  assert.equal(safeHttpUrl("not a url"), null);
  assert.equal(safeHttpUrl(null), null);
  assert.equal(safeHttpUrl(undefined), null);
});
