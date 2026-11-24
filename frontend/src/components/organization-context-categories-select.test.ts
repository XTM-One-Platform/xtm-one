import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The picker renders "Primary assets" (#1591): no string it renders, in any locale,
 * may still name the old "Primary assets" category.
 */
const source = readFileSync(
  join(process.cwd(), "src", "components", "organization-context-categories-select.tsx"),
  "utf8",
);
const keys = [...source.matchAll(/t_i18n\(\s*"((?:[^"\\]|\\.)*)"/g)].map((m) => JSON.parse(`"${m[1]}"`) as string);

const OLD_WORDS: Record<string, RegExp> = {
  en: /primary asset/i,
  fr: /actifs critiques/i,
  es: /activos críticos/i,
  ja: /重要資産/,
};

describe("organization context categories picker wording", () => {
  it("finds the strings the picker renders", () => {
    expect(keys.length).toBeGreaterThan(0);
  });

  it("the source never names Primary assets", () => {
    expect(source).not.toMatch(/primary asset/i);
  });

  for (const [locale, old] of Object.entries(OLD_WORDS)) {
    it(`${locale}: no rendered string names the old category`, () => {
      const catalog = JSON.parse(
        readFileSync(join(process.cwd(), "src", "i18n", "lang", `${locale}.json`), "utf8"),
      ) as Record<string, string>;
      for (const key of keys) {
        expect(catalog[key] ?? key).not.toMatch(old);
      }
    });
  }
});
