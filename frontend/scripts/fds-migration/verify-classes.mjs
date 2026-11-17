/**
 * Checks that every design-system class the product code uses actually compiles
 * to a rule.
 *
 *   yarn build && node scripts/fds-migration/verify-classes.mjs
 *   node scripts/fds-migration/verify-classes.mjs --compile
 *
 * The first form reads the compiled CSS of `dist/assets` (CI). `--compile`
 * compiles `src/index.css` in-process with the same Tailwind plugin instead
 * (`compiled-css.mjs`): no tsc, no vite, seconds, so the sandbox runs it before
 * every push that touches a class (the `frontend-design-system-classes` pre-push
 * guard).
 *
 * This exists because of a specific, silent failure mode. FDS exposes its design
 * language two different ways: `--color-*` tokens, which Tailwind turns into
 * utilities automatically, and `@utility` blocks, which are hand-written and
 * exist only for the exact prefixes the library chose to write. `border-
 * elevation-subtle` is one of those blocks; `ring-elevation-subtle` and
 * `divide-elevation-subtle` look identical, are just as plausible, and do not
 * exist. Nothing complains: Tailwind emits no rule, the class name sits in the
 * markup, and the element renders unstyled.
 *
 * The library's primitives (`--gray-100`, `--grayblue-850`) have the same trap
 * from the other direction: they are declared outside the `--color-*` namespace,
 * so `bg-gray-100` is not a class either — those have to be referenced as
 * arbitrary values.
 *
 * So: collect the classes used, collect the classes built, and report the
 * difference for anything that looks like it came from the design system.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { carriesClasses, designSystemDist, plausibleClass } from "./design-system-dist.mjs";
import { compileStylesheets, selectorsOf, unresolvedClasses } from "./compiled-css.mjs";

const SRC = path.resolve("src");
const DIST = path.resolve("dist/assets");
/** Compile the stylesheets in-process instead of reading a build's `dist/assets`. */
const COMPILE = process.argv.includes("--compile");
/**
 * The library's compiled bundle. Its components carry their own classes, and
 * this host compiles them: Tailwind only emits a utility it detected in a
 * scanned source, so a class the library uses and the scanner misses produces
 * an unstyled library component — with nothing in this app's own code to
 * explain why. That is exactly how `bg-gradient-default` went missing, leaving
 * the Navbar with no surface at all.
 */
const LIBRARY_DIST = designSystemDist() ?? "";

async function* walk(dir, extensions) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full, extensions);
    else if (extensions.has(path.extname(entry.name))) yield full;
  }
}

/** Every whitespace-separated token that appears inside a quoted string. */
async function usedClasses() {
  const used = new Map();
  const record = (token, origin) => {
    if (!token || !/^[a-z0-9:[\]()\-_/.,%&#]+$/i.test(token)) return;
    if (!used.has(token)) used.set(token, []);
    used.get(token).push(origin);
  };

  const collect = async (file) => {
    const source = await readFile(file, "utf8");
    const origin = path.relative(process.cwd(), file);
    for (const [, doubleQuoted, singleQuoted] of source.matchAll(/"([^"\n]*)"|'([^'\n]*)'/g)) {
      const literal = doubleQuoted ?? singleQuoted;
      if (!literal) continue;
      for (const token of literal.split(/\s+/)) record(token, origin);
    }
  };

  for await (const file of walk(SRC, new Set([".ts", ".tsx"]))) await collect(file);
  try {
    // Every chunk, not just the entry: tsup splits the bundle and the
    // components live beside it.
    for await (const file of walk(LIBRARY_DIST, new Set([".mjs"]))) {
      if (carriesClasses(file)) await collect(file);
    }
  } catch {
    console.error("library bundle not found — install dependencies first");
    process.exit(2);
  }
  return used;
}

/** The selectors the build emitted: `dist/assets`, or compiled in-process with `--compile`. */
async function builtSelectors() {
  if (COMPILE) return selectorsOf(await compileStylesheets(process.cwd()));
  const sheets = [];
  for await (const file of walk(DIST, new Set([".css"]))) sheets.push(await readFile(file, "utf8"));
  return selectorsOf(sheets);
}

const used = await usedClasses();
const built = await builtSelectors();

/**
 * Second check, and the one that matters most.
 *
 * The report below only judges classes matching a design-system SHAPE, which
 * left a hole: the Switch paints its track with
 * `group-data-[state=checked]:bg-filigran-brand-primary-transparency-50` and its
 * thumb with `size-5` — a bracketed variant and a plain core utility. Neither is
 * "DS-shaped", so neither was checked, neither was safelisted, and the control
 * rendered as a transparent box. Reported by a human, not by this script.
 *
 * So the safelist is verified against the bundle directly: every plausible class
 * token the library carries must be in it. That comparison is exact and has no
 * false positives to tune, because both sides are token lists rather than one
 * being compiled CSS.
 */
const safelisted = new Set();
{
  const entry = await readFile(path.resolve("src/index.css"), "utf8");
  for (const [, list] of entry.matchAll(/@source inline\("([^"]+)"\)/g)) {
    for (const token of list.split(/\s+/)) if (token) safelisted.add(token);
  }
}
const unsafelisted = [];
for await (const file of walk(LIBRARY_DIST, new Set([".mjs"]))) {
  if (!carriesClasses(file)) continue;
  const source = await readFile(file, "utf8");
  for (const [, doubleQuoted, singleQuoted] of source.matchAll(/"([^"\n]*)"|'([^'\n]*)'/g)) {
    const literal = doubleQuoted ?? singleQuoted;
    if (!literal) continue;
    for (const token of literal.split(/\s+/)) {
      if (plausibleClass(token) && !safelisted.has(token)) unsafelisted.push(token);
    }
  }
}
// And the other way round: the safelist holds classes only. Prose or a URL in it
// means it was carried forward from a scan that let them in.
const notClasses = [...safelisted].filter((token) => !plausibleClass(token));
if (notClasses.length) {
  console.log(`${notClasses.length} safelist entr(y/ies) that cannot be a class — run generate-safelist.mjs:\n`);
  for (const token of notClasses.slice(0, 20)) console.log(`      ${token}`);
  process.exit(1);
}
if (unsafelisted.length) {
  const unique = [...new Set(unsafelisted)];
  console.log(`${unique.length} library class(es) not safelisted — run generate-safelist.mjs:\n`);
  for (const token of unique.slice(0, 20)) console.log(`      ${token}`);
  if (unique.length > 20) console.log(`      … and ${unique.length - 20} more`);
  process.exit(1);
}

if (built.size === 0) {
  console.error(
    COMPILE
      ? "no CSS compiled from src/index.css — install dependencies first"
      : "no compiled CSS found in dist/assets — run `yarn build` first, or pass --compile",
  );
  process.exit(2);
}

const missing = unresolvedClasses(used, built);
if (missing.length === 0) {
  console.log(`every design-system class used in src resolves (${built.size} selectors built)`);
  process.exit(0);
}

console.log(`${missing.length} design-system class(es) used but never compiled:\n`);
for (const { token, count, example } of missing) {
  console.log(`${String(count).padStart(5)} × ${token.padEnd(36)} e.g. ${example}`);
}
process.exit(1);
