/**
 * Unit tests for the stale-bundle check - `yarn test:unit`.
 *
 * A tab running the bundle from before an upgrade reads a relayed turn as its
 * own until it is reloaded; comparing the build it booted with the build the
 * server reports is what tells it to reload.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { BUILD_ID_META_NAME, isStaleBuild, readPageBuildId, type MetaSource } from "./build-version.ts";

const page = (content: string | null): MetaSource => ({
  querySelector: (selector: string) =>
    selector === `meta[name="${BUILD_ID_META_NAME}"]` && content !== null
      ? { getAttribute: (name: string) => (name === "content" ? content : null) }
      : null,
});

test("the page's build is the meta the backend wrote into its index.html", () => {
  assert.equal(readPageBuildId(page("0123456789abcdef")), "0123456789abcdef");
  assert.equal(readPageBuildId(page("  0123456789abcdef \n")), "0123456789abcdef");
  // The Vite dev server writes no meta; outside a browser there is no document.
  assert.equal(readPageBuildId(page(null)), "");
  assert.equal(readPageBuildId(undefined), "");
});

test("a page is stale only when both builds are known and differ", () => {
  assert.equal(isStaleBuild("aaaa", "bbbb"), true);
  assert.equal(isStaleBuild("aaaa", "aaaa"), false);
  // Unknown on either side is never stale: a dev page, an older backend that
  // sends no build id, a server without a built frontend.
  assert.equal(isStaleBuild("", "bbbb"), false);
  assert.equal(isStaleBuild("aaaa", ""), false);
  assert.equal(isStaleBuild("aaaa", undefined), false);
  assert.equal(isStaleBuild("aaaa", null), false);
});
