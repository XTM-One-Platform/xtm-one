/**
 * Applies the design-system token rule table to product code.
 *
 *   node scripts/fds-migration/migrate.mjs --dry     # report only
 *   node scripts/fds-migration/migrate.mjs           # rewrite in place
 *
 * The per-rule counts are the point. A migration of this size cannot be
 * reviewed line by line, so what gets reviewed instead is the rule table plus
 * these numbers: a rule that fires 3 000 times deserves a second look at its
 * mapping, and a rule that fires zero times is either dead, or shadowed by an
 * earlier rule that already consumed its matches.
 *
 * Two properties keep it safe to re-run:
 *
 *   1. It regenerates from the committed content of each file (`git show HEAD:`)
 *      rather than from the working tree, so running it twice produces the same
 *      tree instead of compounding. Pass `--from-worktree` to override, which is
 *      only useful once the migration itself is committed.
 *
 *      The flip side, and it bites: any UNCOMMITTED edit to a tracked file under
 *      src/ is discarded the next time this runs, because HEAD is the baseline.
 *      Commit hand-written work before re-running — the theme bridge in
 *      `useThemeEffect.ts` was silently reverted this way once, and the symptom
 *      appeared two steps later as "light mode does nothing".
 *   2. In TypeScript sources it rewrites ONLY string literals and comments,
 *      never code. Utility names look like ordinary identifiers — an early
 *      version of this script renamed a `rounded` variable to `rounded-sm` and
 *      broke the build — and comments in this codebase quote the class names
 *      they explain, so leaving comments out would strand every one of those
 *      references on a token that no longer exists.
 */
import { readdir, readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import { hoverStep, rules } from "./token-map.mjs";

const run = promisify(execFile);
const SRC = path.resolve("src");
const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);
const dryRun = process.argv.includes("--dry");
const fromWorktree = process.argv.includes("--from-worktree");

/** Files whose colours are not product styling and must not be rewritten. */
const EXCLUDE = [
  // The token layer itself: it defines the product tokens the rules point AT,
  // so rewriting it would chase its own tail.
  path.join(SRC, "index.css"),
];

async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (EXTENSIONS.has(path.extname(entry.name))) yield full;
  }
}

/** The committed content of a file, or null when it is not tracked yet. */
async function committed(file) {
  const rel = path.relative(process.cwd(), file);
  try {
    const { stdout } = await run("git", ["show", `HEAD:./${rel}`], { maxBuffer: 64 * 1024 * 1024 });
    return stdout;
  } catch {
    return null;
  }
}

/**
 * Spans of a TypeScript source that may contain utility class names: string
 * literals (single, double, template) and comments (line, block).
 *
 * Interpolations are descended into for their own string literals, but their
 * expressions are not rewritten: `${rounded}` is code that happens to sit
 * inside a literal, and that distinction is what makes this safe where a plain
 * regex over the whole file is not.
 */
function rewritableSpans(source) {
  /** Each span is `[start, end, kind]`, kind being "comment" or "literal". */
  const spans = [];
  let i = 0;
  const n = source.length;

  while (i < n) {
    const ch = source[i];
    const next = source[i + 1];

    // Comments
    if (ch === "/" && next === "/") {
      const end = source.indexOf("\n", i);
      spans.push([i + 2, end === -1 ? n : end, "comment"]);
      i = end === -1 ? n : end;
      continue;
    }
    if (ch === "/" && next === "*") {
      const end = source.indexOf("*/", i + 2);
      spans.push([i + 2, end === -1 ? n : end, "comment"]);
      i = end === -1 ? n : end + 2;
      continue;
    }

    // Quoted strings
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < n && source[j] !== ch) {
        if (source[j] === "\\") j += 1;
        else if (source[j] === "\n") break;
        j += 1;
      }
      spans.push([i + 1, Math.min(j, n), "literal"]);
      i = j + 1;
      continue;
    }

    // Template literals: every run of literal text between interpolations.
    if (ch === "`") {
      let j = i + 1;
      let literalStart = j;
      while (j < n && source[j] !== "`") {
        if (source[j] === "\\") {
          j += 2;
          continue;
        }
        if (source[j] === "$" && source[j + 1] === "{") {
          spans.push([literalStart, j, "literal"]);
          // Recurse into the interpolation rather than skipping it: a
          // conditional class list (`${active ? "text-accent" : ""}`) is the
          // most common way this codebase writes state-dependent styling, and
          // skipping it left ~200 utilities behind on the first pass. Only the
          // string literals INSIDE it get rewritten, so the expression's own
          // identifiers stay untouched.
          let depth = 1;
          const exprStart = j + 2;
          j += 2;
          while (j < n && depth > 0) {
            if (source[j] === "{") depth += 1;
            else if (source[j] === "}") depth -= 1;
            j += 1;
          }
          const exprEnd = j - 1;
          for (const [start, end, kind] of rewritableSpans(source.slice(exprStart, exprEnd))) {
            spans.push([exprStart + start, exprStart + end, kind]);
          }
          literalStart = j;
          continue;
        }
        j += 1;
      }
      spans.push([literalStart, Math.min(j, n), "literal"]);
      i = j + 1;
      continue;
    }

    i += 1;
  }

  // Recursion appends nested spans as it finds them; the consumer walks them in
  // order and drops anything that overlaps what it has already emitted.
  return spans.sort((a, b) => a[0] - b[0]);
}

/**
 * A side-specific border or radius (`border-l-amber-400/60`, `rounded-t-lg`):
 * the rule table is written for the side-less utility, so the side is taken
 * off, the rest mapped like any other class, and the side put back. Without
 * this the palette and radius rules missed every directional utility, which
 * then compiled to nothing under the library's `--color-*: initial` - the
 * role bars of the trace inspector, a notification's urgency stripe.
 *
 * Nothing changes when the side-less form maps to itself: a width
 * (`border-l-2`) or a class already migrated (`rounded-t-sm`).
 */
const SIDED = /\b(border|rounded)-([trblxyse]{1,2})-([^\s"'`{}]+)/g;

function applySided(text, tally, kind) {
  return text.replace(SIDED, (match, utility, side, rest) => {
    const plain = `${utility}-${rest}`;
    const mapped = applyRuleTable(plain, tally, kind);
    if (mapped === plain || !mapped.startsWith(`${utility}-`)) return match;
    return `${utility}-${side}-${mapped.slice(utility.length + 1)}`;
  });
}

function applyRules(text, tally, kind = "literal") {
  const mapped = applyRuleTable(applySided(text, tally, kind), tally, kind);
  return kind === "comment" ? mapped : stepUpHovers(mapped);
}

/** A hover the mapping collapsed onto its rest state moves one step up (`hoverStep`). */
function stepUpHovers(text) {
  const resting = new Set(text.split(/\s+/).filter((token) => !token.includes(":")));
  return text.replace(/(^|\s)hover:([^\s"'`]+)/g, (match, lead, cls) => {
    if (!resting.has(cls)) return match;
    const next = hoverStep(cls);
    return next ? `${lead}hover:${next}` : match;
  });
}

function applyRuleTable(text, tally, kind = "literal") {
  let out = text;
  for (const rule of rules) {
    // A rule whose pattern is also an ordinary English word must not rewrite
    // prose. See `literalsOnly` in the rule table.
    if (rule.literalsOnly && kind === "comment") continue;
    out = out.replace(rule.from, (...args) => {
      tally.set(rule, tally.get(rule) + 1);
      // A rule whose replacement depends on what it matched declares `to` as a
      // function; the others declare a template and get their `$1` expanded
      // here, because `String.replace` with a function does not expand it.
      if (typeof rule.to === "function") return rule.to(...args);
      return rule.to.replace(/\$(\d)/g, (_, index) => args[Number(index)]);
    });
  }
  return out;
}

/** Rewrites only the spans that may hold class names, leaving code untouched. */
function applyRulesToSpans(source, tally) {
  const spans = rewritableSpans(source);
  let out = "";
  let cursor = 0;
  for (const [start, end, kind] of spans) {
    if (start < cursor || start > end) continue;
    out += source.slice(cursor, start);
    out += applyRules(source.slice(start, end), tally, kind);
    cursor = end;
  }
  return out + source.slice(cursor);
}

const tally = new Map(rules.map((rule) => [rule, 0]));
const touched = [];
let regenerated = 0;

for await (const file of walk(SRC)) {
  if (EXCLUDE.includes(file)) continue;

  const worktree = await readFile(file, "utf8");
  const baseline = fromWorktree ? worktree : ((await committed(file)) ?? worktree);
  if (baseline !== worktree) regenerated += 1;

  const isStyleSheet = path.extname(file) === ".css";
  const after = isStyleSheet ? applyRules(baseline, tally) : applyRulesToSpans(baseline, tally);

  if (after !== worktree) {
    touched.push(path.relative(process.cwd(), file));
    if (!dryRun) await writeFile(file, after);
  }
}

const total = [...tally.values()].reduce((sum, n) => sum + n, 0);
const width = Math.max(...rules.map((rule) => String(rule.from).length));

console.log(`${dryRun ? "would rewrite" : "rewrote"} ${touched.length} files, ${total} replacements`);
if (!fromWorktree && regenerated) {
  console.log(`(${regenerated} regenerated from HEAD rather than from the working tree)`);
}
console.log();
for (const [rule, count] of tally) {
  const to = rule.label ?? (typeof rule.to === "function" ? "(computed)" : rule.to);
  console.log(`${String(count).padStart(5)}  ${String(rule.from).padEnd(width)} → ${to}${count === 0 ? "  ← never fired" : ""}`);
}
