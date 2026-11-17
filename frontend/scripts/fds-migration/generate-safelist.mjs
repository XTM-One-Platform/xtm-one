/**
 * Generates the Tailwind safelist that closes the library's class-detection gap.
 *
 *   yarn build && node scripts/fds-migration/generate-safelist.mjs   # then build again
 *
 * WHY
 *
 * A Tailwind v4 host compiles the library's components with its own pipeline, so
 * every class those components carry must be DETECTED in a scanned source. With
 * `@source` pointing at the compiled bundle that detection is unreliable:
 * `bg-gradient-default` and `w-45` were emitted while `text-input-placeholder`,
 * `content-compact-medium` and `bg-filigran-brand-primary-transparency-10` —
 * plain string literals in the same file — were not. Safelisting them emits them
 * immediately, which puts the fault in detection rather than in the tokens.
 *
 * The failure is silent in the worst way: layout intact, colours and typography
 * gone, nothing in the host's own code to explain it.
 *
 * HOW, and why not the two obvious alternatives
 *
 * The list is derived from the build, not hand-written and not scraped: it is
 * exactly the set `verify-classes.mjs` reports as "used by the library, never
 * compiled". Scraping every hyphenated string out of the bundle was tried first
 * and produced 1 385 candidates that were mostly icon names and ratios.
 *
 * Importing the library's pre-compiled `dist/index.css` alongside the tokens was
 * also tried, and rejected for a concrete reason: an UNLAYERED stylesheet beats
 * every layered rule in CSS, so its own copies of core utilities (`p-4`, which
 * its build safelists for Paper's padding scale) silently beat this app's
 * responsive `lg:p-8`. The symptom was a 16px horizontal scrollbar on the chat
 * page. Moving it into a cascade layer broke Tailwind's own emission instead.
 *
 * TWO-PASS, by nature: it reads what the last build emitted, so run build →
 * generate → build. The committed output means a normal `yarn dev` needs neither
 * pass, and `verify-classes.mjs` fails the day it goes stale.
 *
 * It writes into `src/index.css` rather than its own file because
 * `@source inline()` is only honoured in Tailwind's ENTRY stylesheet — from an
 * `@import` it is silently ignored, which is exactly what made two earlier
 * attempts at this look like safelisting did not work at all.
 */
import { existsSync } from "node:fs";
import { readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { carriesClasses, designSystemDist, plausibleClass } from "./design-system-dist.mjs";

const SRC = path.resolve("src");
const DIST = path.resolve("dist/assets");
const LIBRARY_DIST = designSystemDist() ?? "";
const ENTRY = path.resolve("src/index.css");
const START = "/* fds-safelist:start";
const END = "/* fds-safelist:end */";

/** Class shapes that belong to the design system, and must therefore resolve. */
const DESIGN_SYSTEM_SHAPED =
  /^(?:[a-z-]+:)*(?:bg|text|border|ring|ring-offset|outline|fill|stroke|divide|from|via|to|shadow|font|rounded|placeholder|caret|decoration)-(?:elevation|default|negative|input|alert|feedback|filigran|entities|accent|scrim|overlay|gradient|tooltip|sans|mono|focus)[a-z0-9-]*$|^layer-[0-3]$|^(?:title-(?:jumbo|2xl|xl|lg|md|sm|xs)|content-(?:base|compact|caption|highlight|button|code)(?:-(?:bold|medium|link))?)$/;

async function* walk(dir, extensions) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full, extensions);
    else if (extensions.has(path.extname(entry.name))) yield full;
  }
}

/** Every class-shaped token in a file's string literals. */
async function tokensIn(file) {
  const source = await readFile(file, "utf8");
  const tokens = new Set();
  for (const [, doubleQuoted, singleQuoted] of source.matchAll(/"([^"\n]*)"|'([^'\n]*)'/g)) {
    const literal = doubleQuoted ?? singleQuoted;
    if (!literal) continue;
    for (const token of literal.split(/\s+/)) {
      if (token) tokens.add(token);
    }
  }
  return tokens;
}

const used = new Set();
for await (const file of walk(LIBRARY_DIST, new Set([".mjs"]))) {
  if (!carriesClasses(file)) continue;
  for (const token of await tokensIn(file)) used.add(token);
}
for await (const file of walk(SRC, new Set([".ts", ".tsx"]))) {
  for (const token of await tokensIn(file)) used.add(token);
}

const built = new Set();
for await (const file of existsSync(DIST) ? walk(DIST, new Set([".css"])) : []) {
  const css = await readFile(file, "utf8");
  for (const [, selector] of css.matchAll(/\.((?:\\.|[\w-])+)/g)) {
    const unescaped = selector.replace(/\\/g, "");
    built.add(unescaped);
    const slash = unescaped.indexOf("/");
    const bare = slash === -1 ? unescaped : unescaped.slice(0, slash);
    built.add(bare);
    built.add(bare.replace(/^(?:[a-z-]+:)+/, ""));
  }
}

if (built.size === 0) {
  console.error("no compiled CSS in dist/assets — run `yarn build` first");
  process.exit(2);
}

// Additive. Once a class is safelisted the next build EMITS it, so it is no
// longer "missing" — regenerating from scratch would write an empty block and
// undo itself. Previously safelisted classes are therefore carried forward, and
// the script converges on the second run instead of oscillating.
const entryBefore = await readFile(ENTRY, "utf8");
const previous = new Set();
{
  const start = entryBefore.indexOf(START);
  const end = entryBefore.indexOf(END);
  if (start !== -1 && end !== -1) {
    for (const [, list] of entryBefore.slice(start, end).matchAll(/@source inline\("([^"]+)"\)/g)) {
      for (const token of list.split(/\s+/)) if (token) previous.add(token);
    }
  }
}

/**
 * Every plausible class token the library's bundle carries, verbatim.
 *
 * The narrow, DS-shaped list this started as was not enough, and the Switch
 * proved it: its track is styled with
 * `group-data-[state=checked]:bg-filigran-brand-primary-transparency-50` and its
 * thumb with `size-5`. Neither matches a "design-system-shaped" pattern — one is
 * a bracketed variant, the other a plain core utility — so neither was
 * safelisted, and the control rendered as a 36×20 transparent box with a
 * full-width thumb. Layout intact, everything else gone, no error: the same
 * silent shape as every other detection failure here.
 *
 * So the token is taken as written, prefix and all. Unknown candidates cost
 * nothing — Tailwind emits no rule for them — while a missed one costs an
 * invisible control, and that asymmetry decides the filter's width
 * (`plausibleClass`, shared with `verify-classes.mjs`).
 */
const bundleTokens = new Set();
for await (const file of walk(LIBRARY_DIST, new Set([".mjs"]))) {
  if (!carriesClasses(file)) continue;
  for (const token of await tokensIn(file)) {
    if (plausibleClass(token)) bundleTokens.add(token);
  }
}

// Newly missing: a design-system class the bundle or product code uses and this
// build did not emit.
const bareClass = (token) => token.replace(/^(?:[a-z-]+:)+/, "").replace(/\/.*$/, "");
// Layer-suffixed elevation names are CSS variables, never classes: the leading
// `--` is lost when `var(--bg-…)` is split into tokens.
const designSystemShaped = (token) =>
  !/-layer-[0-3]$/.test(bareClass(token)) && DESIGN_SYSTEM_SHAPED.test(bareClass(token));
const newlyMissing = [...used].filter((token) => designSystemShaped(token) && !built.has(bareClass(token)));

// The previous list is carried over, and that is the whole point: a safelisted
// class is emitted, so it is no longer "missing", so re-deriving the list from
// scratch would drop it and the next build would lose it again. What is carried
// is what something still uses, though: an entry neither the bundle nor the
// product code carries any more (a class the library dropped in a version bump,
// prose an older scan let in) goes, so the list shrinks as well as grows.
const carried = [...previous].filter(
  (token) => bundleTokens.has(token) || (used.has(token) && designSystemShaped(token)),
);
const missing = [...new Set([...carried, ...newlyMissing, ...bundleTokens])].sort();

const CHUNK = 12;
const directives = [];
for (let index = 0; index < missing.length; index += CHUNK) {
  directives.push(`@source inline("${missing.slice(index, index + CHUNK).join(" ")}");`);
}

const block = [
  `${START} — generated by scripts/fds-migration/generate-safelist.mjs.`,
  "   Do not edit by hand; regenerate after a pin bump.",
  "   It lives HERE rather than in an imported file because `@source inline()` is",
  "   only honoured in the Tailwind ENTRY stylesheet — from an `@import` it is",
  "   silently ignored, which is what made two earlier attempts at this look like",
  "   the safelist itself was not working.",
  `   ${missing.length} classes, from the pinned library build. */`,
  ...directives,
  END,
].join("\n");

const entry = entryBefore;
const start = entry.indexOf(START);
const end = entry.indexOf(END);
if (start === -1 || end === -1) {
  console.error(`markers not found in ${path.relative(process.cwd(), ENTRY)}`);
  process.exit(2);
}
await writeFile(ENTRY, entry.slice(0, start) + block + entry.slice(end + END.length));

console.log(`safelisted ${missing.length} classes in ${path.relative(process.cwd(), ENTRY)}`);
