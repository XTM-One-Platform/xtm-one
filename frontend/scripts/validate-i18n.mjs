/**
 * Validate the i18n catalogs:
 *  1. every message in every locale parses as valid ICU;
 *  2. every fr/es/ja key exists in en.json (no orphans);
 *  3. translation placeholder names are a subset of the English key's;
 *  4. no catalog repeats a key (`JSON.parse` would keep the last one silently);
 *  5. every en.json key exists in fr/es/ja (a key missing there renders in English);
 *  6. every key the UI source asks for exists in every catalog: a
 *     literal passed to `t_i18n(...)` (or a literal branch of a conditional
 *     passed to it) and every value of an `@i18n-keys` table
 *     (`lib/i18n-source-keys.mjs`, read with the TypeScript parser), tests
 *     excluded. A missing key renders its English id, so the gap only ever
 *     shows up to fr/es/ja users.
 *  7. French typography: no plain space before "?", "!", ";" or
 *     ":" in fr.json. A narrow no-break space (U+202F) goes before "?", "!"
 *     and ";", a no-break space (U+00A0) before ":", so a wrapping title
 *     never leaves the mark alone at the start of a line.
 *
 * Usage: node scripts/validate-i18n.mjs
 */
import { readFileSync } from "node:fs";
import { relative } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "@formatjs/icu-messageformat-parser";

import { findDuplicateKeys } from "./lib/json-duplicate-keys.mjs";
import { frenchSpacingProblems } from "./lib/french-spacing.mjs";
import { scanSource, sourceFiles } from "./lib/i18n-source-keys.mjs";

const LOCALES = ["en", "fr", "es", "ja"];
const sources = Object.fromEntries(
  LOCALES.map((l) => [l, readFileSync(new URL(`../src/i18n/lang/${l}.json`, import.meta.url), "utf8")]),
);
const catalogs = Object.fromEntries(LOCALES.map((l) => [l, JSON.parse(sources[l])]));

function args(ast, out = new Set()) {
  for (const el of ast) {
    if (el.value && typeof el.value === "string" && el.type !== 0) out.add(el.value);
    if (el.options) for (const opt of Object.values(el.options)) args(opt.value, out);
    if (el.children) args(el.children, out);
  }
  return out;
}

let errors = 0;
for (const locale of LOCALES) {
  for (const { key, firstLine, line } of findDuplicateKeys(sources[locale])) {
    console.log(`DUPLICATE ${locale}: ${JSON.stringify(key)} in src/i18n/lang/${locale}.json at lines ${firstLine} and ${line}`);
    errors += 1;
  }
  for (const [key, message] of Object.entries(catalogs[locale])) {
    let ast;
    try {
      ast = parse(message);
    } catch (e) {
      console.log(`PARSE ${locale}: ${JSON.stringify(key)} -> ${e.message}`);
      errors += 1;
      continue;
    }
    if (locale === "en") continue;
    if (!(key in catalogs.en)) {
      console.log(`ORPHAN ${locale}: ${JSON.stringify(key)} (missing from en.json)`);
      errors += 1;
      continue;
    }
    let enArgs;
    try {
      enArgs = args(parse(catalogs.en[key]));
    } catch {
      continue; // reported in the en pass
    }
    for (const name of args(ast)) {
      if (!enArgs.has(name)) {
        console.log(`ARG ${locale}: ${JSON.stringify(key)} uses {${name}} not present in the English key`);
        errors += 1;
      }
    }
  }
}
for (const { key, mark } of frenchSpacingProblems(catalogs.fr)) {
  console.log(`SPACING fr: ${JSON.stringify(key)} has a plain space before "${mark}" (use ${mark === ":" ? "U+00A0" : "U+202F"})`);
  errors += 1;
}
for (const locale of LOCALES.slice(1)) {
  for (const key of Object.keys(catalogs.en)) {
    if (!(key in catalogs[locale])) {
      console.log(`UNTRANSLATED ${locale}: ${JSON.stringify(key)} (in en.json, missing from ${locale}.json)`);
      errors += 1;
    }
  }
}

// `URL.pathname` yields `/C:/...` on Windows; `fileURLToPath` is native everywhere.
const FRONTEND = fileURLToPath(new URL("../", import.meta.url));
const scan = scanSource(sourceFiles(fileURLToPath(new URL("../src/", import.meta.url))), (file) =>
  relative(FRONTEND, file).replace(/\\/g, "/"),
);
for (const problem of scan.problems) {
  console.log(`TABLE ${problem}`);
  errors += 1;
}
const reported = new Set();
for (const { key, where } of scan.keys) {
  const absent = LOCALES.filter((locale) => !(key in catalogs[locale]));
  if (absent.length === 0 || reported.has(key)) continue;
  reported.add(key);
  console.log(`MISSING ${absent.join(",")}: ${JSON.stringify(key)} (${where})`);
  errors += 1;
}
const used = new Set(scan.keys.map(({ key }) => key));
console.log(`Source: ${used.size} keys asked for by t_i18n literals and @i18n-keys tables.`);
console.log(errors === 0 ? "All catalogs valid." : `${errors} problem(s) found.`);
process.exit(errors === 0 ? 0 : 1);
