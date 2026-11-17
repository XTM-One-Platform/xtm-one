/**
 * No text size under 10 px in the frontend - `yarn test`.
 *
 * The smallest type token of the design system is `content-caption` (10 px on
 * a 15 px line). A raw `text-[9px]`, `text-[0.5rem]`, a CSS `font-size: 9px`
 * or an inline / SVG `fontSize` under 10 sits below it: the badges of one list
 * page read at 9 px while their siblings read at 10. This gate scans
 * every .ts, .tsx and .css source file under src/ (test files excluded) and
 * fails on any of these forms under 10 px.
 *
 * The sweep runs in slices, so the gate lands as a ratchet:
 * `no-text-under-10px.ceiling.json` names each file that still carries such
 * sizes on the day the gate landed, with its count. A file may only go DOWN:
 * a file absent from the list must carry none, a listed file may not carry
 * more than its ceiling. A sweep slice lowers or deletes the entries of the
 * files it fixes; an entry is never added and never raised. A count below
 * its ceiling passes (a sibling pull request may fix a listed file first, and
 * main must never go red on that); the sweep slice that touches the file
 * writes the new count. The last slice empties the list and the gate is
 * strict.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const ROOT = process.cwd();
const SELF = "src/no-text-under-10px.test.ts";
const CEILING_FILE = "src/no-text-under-10px.ceiling.json";
const SOURCE_FILE = /\.(ts|tsx|css)$/;
const TEST_FILE = /\.test\.(ts|tsx)$/;
const MIN_PX = 10;

/** Tailwind arbitrary size: text-[9px], text-[0.5rem]. */
const TAILWIND_SIZE = /\btext-\[(\d+(?:\.\d+)?)(px|rem)\]/g;
/** CSS declaration: font-size: 9px, font-size: 0.5rem. */
const CSS_SIZE = /font-size\s*:\s*(\d+(?:\.\d+)?)(px|rem)\b/g;
/** JSX / style object / SVG attribute: fontSize={9}, fontSize="7", fontSize: "9px". */
const INLINE_SIZE = /\bfontSize\s*[=:]\s*\{?\s*["'`]?(\d+(?:\.\d+)?)(px|rem)?\b/g;

const toPx = (value: string, unit: string | undefined): number => Number(value) * (unit === "rem" ? 16 : 1);

/** Every size under 10 px the text carries, as the matched source. */
function sizesUnder10px(text: string): string[] {
  const found: string[] = [];
  for (const pattern of [TAILWIND_SIZE, CSS_SIZE, INLINE_SIZE]) {
    for (const match of text.matchAll(pattern)) {
      if (toPx(match[1], match[2]) < MIN_PX) found.push(match[0]);
    }
  }
  return found;
}

function* sourceFiles(dir: string): Generator<string> {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== "dist") yield* sourceFiles(full);
    } else if (SOURCE_FILE.test(entry.name) && !TEST_FILE.test(entry.name)) {
      yield relative(ROOT, full).split(sep).join("/");
    }
  }
}

const CEILING: Record<string, number> = JSON.parse(readFileSync(join(ROOT, CEILING_FILE), "utf8"));

test("the pattern catches every form under 10 px and nothing else", () => {
  for (const line of [
    '<Badge className="text-[9px] px-1.5">',
    '<span className="text-[7.5px]">',
    '<p className="text-[0.5rem]">',
    '<p className="text-[0.5625rem]">',
    "  font-size: 9px;",
    '<text fontSize="6">',
    "<text fontSize={5.5}>",
    'style={{ fontSize: "9px" }}',
    "style={{ fontSize: 8 }}",
  ]) {
    assert.equal(sizesUnder10px(line).length, 1, line);
  }
  for (const line of [
    '<Badge className="content-caption px-1.5">',
    '<span className="text-[10px]">',
    '<p className="text-[0.625rem]">',
    "  font-size: 10px;",
    "  font-size: 0.88em;",
    "  gap: 9px;",
    '<text fontSize="11">',
    "<text fontSize={12}>",
    '<div className="w-[9px] h-[8px]">',
  ]) {
    assert.deepEqual(sizesUnder10px(line), [], line);
  }
});

test("no source file carries a text size under 10 px beyond its ceiling", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (file === SELF) continue;
    const found = sizesUnder10px(readFileSync(join(ROOT, file), "utf8"));
    const ceiling = CEILING[file] ?? 0;
    if (found.length > ceiling) {
      offenders.push(`${file}: ${found.length} size(s) under 10 px, ceiling ${ceiling} (${found.join(", ")})`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `Use the content-caption token (10 px / 15 px), never a text size under 10 px (${SELF}). ` +
      `A ceiling in ${CEILING_FILE} only goes down:\n${offenders.join("\n")}`,
  );
});

test("every ceiling entry names an existing file with a positive count", () => {
  const bad = Object.entries(CEILING).filter(
    ([file, count]) => !Number.isInteger(count) || count <= 0 || !existsSync(join(ROOT, file)),
  );
  assert.deepEqual(bad, [], `remove or fix these entries in ${CEILING_FILE}: ${bad.map(([file]) => file).join(", ")}`);
});
