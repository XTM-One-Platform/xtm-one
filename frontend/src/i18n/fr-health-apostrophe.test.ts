// SPDX-License-Identifier: Apache-2.0
/**
 * French typographic apostrophes on Platform Health - `yarn test`.
 *
 * #798 UX r1 B2: the Settings > Platform Health section rendered in fr showed
 * "n'est pas activé", "Pic d'échecs" and "Retard d'ingestion" next to curly
 * apostrophes. French copy writes the apostrophe U+2019 (’). This fails on a
 * straight apostrophe in the fr value of any string a Health source file
 * (a .ts / .tsx file whose path names "health", tests excluded) quotes, so the
 * Health screens cannot drift back into a mix.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(process.cwd(), "src");
const fr = JSON.parse(readFileSync(join(SRC, "i18n", "lang", "fr.json"), "utf8")) as Record<string, string>;

/** Every Health .ts / .tsx source file under src/, tests excluded. */
function healthFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) healthFiles(path, out);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) && /health/i.test(path.slice(SRC.length))) out.push(path);
  }
  return out;
}

/** The string literals of a source file that are fr catalog keys. */
function catalogKeys(source: string): string[] {
  const keys: string[] = [];
  for (const quote of ['"', "'", "`"]) {
    const literal = new RegExp(`${quote}((?:[^${quote}\\\\\\n]|\\\\.)+)${quote}`, "g");
    for (const match of source.matchAll(literal)) {
      const key = match[1].split(`\\${quote}`).join(quote);
      if (key in fr) keys.push(key);
    }
  }
  return keys;
}

test("Health fr values write the typographic apostrophe, never a straight one", () => {
  const files = healthFiles(SRC);
  expect(files.length).toBeGreaterThan(10);
  const straight = new Set<string>();
  for (const file of files) {
    for (const key of catalogKeys(readFileSync(file, "utf8"))) {
      if (fr[key].includes("'")) straight.add(`${JSON.stringify(key)} => ${fr[key]}`);
    }
  }
  expect([...straight], "write ’ (U+2019) in French copy").toEqual([]);
});

test("the strings the UX review named read with ’", () => {
  expect(fr["{name} is not enabled."]).toBe("{name} n’est pas activé.");
  expect(fr["Failure spike"]).toBe("Pic d’échecs");
  expect(fr["Ingestion lag above"]).toBe("Retard d’ingestion au-delà de");
  expect(fr["Log every write instead of executing it"]).toBe("Journaliser chaque écriture au lieu de l’exécuter");
});
