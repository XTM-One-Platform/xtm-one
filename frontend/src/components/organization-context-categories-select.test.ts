import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The picker renders "Primary assets" (#1591, #1844): no string it renders, in any locale,
 * may still name the old "Crown jewels" category, and no translation of a string that names
 * primary assets may keep the old term (UX round 1 B1 on #1926).
 */
const source = readFileSync(
  join(process.cwd(), "src", "components", "organization-context-categories-select.tsx"),
  "utf8",
);
const keys = [...source.matchAll(/t_i18n\(\s*"((?:[^"\\]|\\.)*)"/g)].map((m) => JSON.parse(`"${m[1]}"`) as string);

const OLD_WORDS: Record<string, RegExp> = {
  en: /crown jewel/i,
  fr: /actifs? critiques?|joyaux de la couronne/i,
  es: /activos? cr[ií]ticos?/i,
  ja: /重要資産/,
};

const catalog = (locale: string) =>
  JSON.parse(readFileSync(join(process.cwd(), "src", "i18n", "lang", `${locale}.json`), "utf8")) as Record<string, string>;

describe("organization context categories picker wording", () => {
  it("finds the strings the picker renders", () => {
    expect(keys.length).toBeGreaterThan(0);
  });

  it("the source never names Crown jewels", () => {
    expect(source).not.toMatch(/crown jewel/i);
  });

  for (const [locale, old] of Object.entries(OLD_WORDS)) {
    it(`${locale}: no rendered string names the old category`, () => {
      const strings = catalog(locale);
      for (const key of keys) {
        expect(strings[key] ?? key).not.toMatch(old);
      }
    });
  }
});

describe("primary assets wording across the catalogs", () => {
  const primaryKeys = Object.keys(catalog("en")).filter((key) => /primary asset/i.test(key));

  it("finds English strings that name primary assets", () => {
    expect(primaryKeys.length).toBeGreaterThan(0);
  });

  for (const locale of ["fr", "es", "ja"]) {
    it(`${locale}: a string that names primary assets never keeps the old term`, () => {
      const strings = catalog(locale);
      const stale = primaryKeys.filter((key) => OLD_WORDS[locale].test(strings[key] ?? ""));
      expect(stale).toEqual([]);
    });
  }
});
