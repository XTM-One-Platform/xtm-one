/**
 * Catalog guard for the five built-in group names - `yarn test:unit`.
 *
 * A group card is named in every locale; a name that reuses the word a locale
 * already uses for another surface reads as that surface. ja once rendered
 * "Coordination" as the word it uses for "Integration", so a tool card named
 * after the work queue read as the integrations section.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const LANG_DIR = join(process.cwd(), "src", "i18n", "lang");
const LOCALES = ["en", "es", "fr", "ja"] as const;
const GROUP_NAMES = ["Design", "Web", "Threat Intelligence", "Coordination", "Files"];
const RESERVED = ["Integration", "integration", "Integrations", "integrations"];

const catalog = (locale: string): Record<string, string> =>
  JSON.parse(readFileSync(join(LANG_DIR, `${locale}.json`), "utf8"));

for (const locale of LOCALES) {
  const strings = catalog(locale);

  test(`${locale}: every built-in group name is translated`, () => {
    for (const name of GROUP_NAMES) {
      assert.equal(typeof strings[name], "string", `${locale} misses "${name}"`);
      assert.notEqual(strings[name].trim(), "", `${locale} leaves "${name}" empty`);
    }
  });

  test(`${locale}: no two built-in group names share a value`, () => {
    const values = GROUP_NAMES.map((name) => strings[name]);
    assert.equal(new Set(values).size, values.length, `${locale}: ${values.join(", ")}`);
  });

  test(`${locale}: no built-in group name reuses the word for integrations`, () => {
    const reserved = new Set(RESERVED.map((key) => strings[key]).filter(Boolean));
    for (const name of GROUP_NAMES) {
      if (locale === "en" || name === "Web") continue;
      assert.ok(!reserved.has(strings[name]), `${locale}: "${name}" is "${strings[name]}", the word for integrations`);
    }
  });
}
