/**
 * Unit tests for the provider logo `src` - `yarn test:unit`.
 *
 * `providerLogoSrc()` is `providerLogoSrcFrom()` against the page's prefix and
 * the curated `PROVIDER_LOGOS` map; `constants.ts` itself needs Vite (the `@/`
 * alias, `import.meta.env`), so the pure half is tested here, against a
 * curated map shaped like the real one: values already under the prefix.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { providerLogoSrcFrom } from "./provider-logo-src.ts";

const PREFIX = "/xtm-one";
const CURATED: Record<string, string> = {
  github: `${PREFIX}/logos/github.svg`,
  filigran_ai: `${PREFIX}/logos/filigran.svg`,
  databricks: `${PREFIX}/logos/databricks.png`,
  slack_bot: `${PREFIX}/logos/slack.svg`,
};

test("a curated provider gets its entry as is, never prefixed a second time", () => {
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "github"), "/xtm-one/logos/github.svg");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "filigran_ai"), "/xtm-one/logos/filigran.svg");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "databricks"), "/xtm-one/logos/databricks.png");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "slack_bot"), "/xtm-one/logos/slack.svg");
});

test("any other provider gets the conventional path, slug unchanged, prefixed once", () => {
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "logrhythm"), "/xtm-one/logos/logrhythm.svg");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "elastic_security"), "/xtm-one/logos/elastic_security.svg");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "n8n"), "/xtm-one/logos/n8n.svg");
  assert.equal(providerLogoSrcFrom("", CURATED, "logrhythm"), "/logos/logrhythm.svg");
});

test("a channel bot without a curated entry borrows its platform's logo", () => {
  assert.equal(providerLogoSrcFrom("", CURATED, "matrix_bot"), "/logos/matrix.svg");
  assert.equal(providerLogoSrcFrom(PREFIX, CURATED, "matrix_bot"), "/xtm-one/logos/matrix.svg");
  // Only a trailing `_bot` is the bot suffix.
  assert.equal(providerLogoSrcFrom("", CURATED, "bot_builder"), "/logos/bot_builder.svg");
});

test("a provider slug cannot shape the request path", () => {
  const slugs = [
    "a/b",
    "../../api/v1/users",
    "..",
    ".",
    "a?b=1",
    "a#frag",
    "a b",
    "a\\..\\b",
    "%2e%2e",
    "%2F",
    "https://evil.example/x",
    "//evil.example/x",
  ];
  for (const basePath of ["", PREFIX]) {
    const directory = [...basePath.split("/").filter(Boolean), "logos"];
    for (const slug of slugs) {
      const src = providerLogoSrcFrom(basePath, CURATED, slug);
      // Resolved the way the browser resolves an `<img src>`: dot segments
      // removed, backslashes read as slashes, query and fragment split off.
      const url = new URL(src, "https://platform.example");
      assert.equal(url.origin, "https://platform.example", slug);
      assert.equal(url.search, "", slug);
      assert.equal(url.hash, "", slug);
      const segments = url.pathname.split("/").slice(1);
      assert.deepEqual(segments.slice(0, -1), directory, slug);
      // One file name, which is the slug itself.
      assert.equal(decodeURIComponent(segments[segments.length - 1]), `${slug}.svg`, slug);
    }
  }
});

test("only the curated map's own keys are curated entries", () => {
  for (const slug of ["constructor", "toString", "hasOwnProperty", "__proto__"]) {
    assert.equal(providerLogoSrcFrom(PREFIX, CURATED, slug), `/xtm-one/logos/${slug}.svg`, slug);
  }
});
