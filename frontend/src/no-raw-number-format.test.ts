/**
 * Numbers are compact on every surface, exact on hover and export (#10476) - `yarn test`.
 *
 * A figure drawn in the UI goes through the shared formatter set:
 * `lib/number-format.ts` (pure rules, the locale passed in), read in components
 * through `hooks/useNumberFormat.ts`, and drawn with `components/ui/compact-figure.tsx`
 * (compact text, exact value on hover). A render site that calls
 * `.toLocaleString(`, `new Intl.NumberFormat(` or `intl.formatNumber(` itself
 * prints "$316,151" or "3,470.6 h" where the rule writes "$316K" and "3.5K h".
 * This gate scans every .ts and .tsx source file under src/ (test files
 * excluded) and fails on any of these calls outside the formatter module.
 *
 * The sweep runs in slices, so the gate lands as a ratchet:
 * `no-raw-number-format.ceiling.json` names each file that still carries such
 * calls on the day the gate landed, with its count. A file may only go DOWN:
 * a file absent from the list must carry none, a listed file may not carry
 * more than its ceiling. A slice that moves a file to the shared formatters
 * lowers or deletes its entry; an entry is never added and never raised.
 * The ceiling also holds the legitimate exact forms that stay (a date's
 * `toLocaleString`, an input's own value, an exact figure built for a hover):
 * when such a file is touched, prefer `exactCount` / `exactCurrency` /
 * `exactPercent` from `lib/number-format.ts` over a raw call.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-text-under-10px.test.ts.
const ROOT = process.cwd();
const CEILING_FILE = "src/no-raw-number-format.ceiling.json";
const SOURCE_FILE = /\.(ts|tsx)$/;
const TEST_FILE = /\.test\.(ts|tsx)$/;

/** The shared formatter module: the one place a raw Intl call belongs. */
const FORMATTER_MODULES = new Set(["src/lib/number-format.ts"]);

const RAW_NUMBER_FORMAT = /\.toLocaleString\(|\bnew Intl\.NumberFormat\(|\bintl\.formatNumber\(/g;

/** Every raw number-format call the text carries, as the matched source. */
function rawNumberFormats(text: string): string[] {
  return [...text.matchAll(RAW_NUMBER_FORMAT)].map((match) => match[0]);
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

test("the pattern catches every raw number-format call and nothing else", () => {
  for (const line of [
    "{value.toLocaleString()}",
    "inputTokens.toLocaleString(undefined, { maximumFractionDigits: 0 })",
    "new Intl.NumberFormat(locale, { style: \"currency\", currency }).format(v)",
    "{intl.formatNumber(entry.best_score)}",
  ]) {
    assert.equal(rawNumberFormats(line).length, 1, line);
  }
  for (const line of [
    "{formatCompactNumber(total)}",
    "const fmt = useNumberFormat(currency);",
    "<CompactFigure exact={exact}>{short}</CompactFigure>",
    "formatCount(value, locale)",
    "Intl.NumberFormat.supportedLocalesOf(locales)",
  ]) {
    assert.deepEqual(rawNumberFormats(line), [], line);
  }
});

test("no source file draws a number with a raw formatter beyond its ceiling", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (FORMATTER_MODULES.has(file)) continue;
    const found = rawNumberFormats(readFileSync(join(ROOT, file), "utf8"));
    const ceiling = CEILING[file] ?? 0;
    if (found.length > ceiling) {
      offenders.push(`${file}: ${found.length} raw call(s), ceiling ${ceiling} (${found.join(", ")})`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "Numbers are compact on every surface, exact on hover and export (#10476): draw a figure with " +
      "useNumberFormat / CompactFigure (lib/number-format.ts), never toLocaleString, a raw Intl.NumberFormat " +
      `or intl.formatNumber at a render site. A ceiling in ${CEILING_FILE} only goes down:\n${offenders.join("\n")}`,
  );
});

/**
 * ROI surfaces: a count in an ICU plural is written by `#`, which formats the raw
 * number ("1,200 runs", #10770). On these screens the plural reads the number and
 * a separate argument draws it compact: `{count, plural, one {{shown} run} other
 * {{shown} runs}}` with `shown: formatCount(locale, count)`. The same ratchet: a
 * listed file may only go down, an unlisted one carries none.
 */
const ROI_SURFACES = ["src/components/roi/", "src/components/fleets/FleetRoiView.tsx", "src/pages/Value.tsx", "src/pages/analytics/OutcomesTab.tsx"];
const ROI_POUND_CEILING: Record<string, number> = {
  // Slides, pages, minutes of video and deliverables of one chat series, under 1,000 in practice.
  "src/components/roi/RoiChatSeriesCard.tsx": 4,
  // Units per run of one outcome definition, a small figure.
  "src/components/roi/OutcomesCard.tsx": 1,
  // Payback in months, a small figure.
  "src/components/roi/RoiFigure.tsx": 1,
};
const POUND_PLURAL = /"(?:[^"\\\n]|\\.)*\bplural\s*,(?:[^"\\\n]|\\.)*#(?:[^"\\\n]|\\.)*"/g;

function poundPlurals(text: string): string[] {
  return [...text.matchAll(POUND_PLURAL)].map((match) => match[0]);
}

test("the ROI pattern catches a # plural and nothing else", () => {
  assert.equal(poundPlurals('t_i18n("{count, plural, one {# run} other {# runs}}", { count })').length, 1);
  assert.deepEqual(poundPlurals('t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count, shown })'), []);
  assert.deepEqual(poundPlurals('t_i18n("Last 30 days") // #10770'), []);
});

test("no ROI surface writes a count with a raw ICU # beyond its ceiling", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (!ROI_SURFACES.some((surface) => file === surface || (surface.endsWith("/") && file.startsWith(surface)))) continue;
    const found = poundPlurals(readFileSync(join(ROOT, file), "utf8"));
    const ceiling = ROI_POUND_CEILING[file] ?? 0;
    if (found.length > ceiling) offenders.push(`${file}: ${found.length} # plural(s), ceiling ${ceiling}`);
  }
  assert.deepEqual(
    offenders,
    [],
    "An ROI figure is compact (#10770): pass the count to the plural and draw it with a {shown} argument " +
      `from formatCount, never #. A ceiling only goes down:\n${offenders.join("\n")}`,
  );
  for (const [file, count] of Object.entries(ROI_POUND_CEILING)) {
    assert.ok(existsSync(join(ROOT, file)) && count > 0, `remove or fix the ROI_POUND_CEILING entry of ${file}`);
  }
});

test("every ceiling entry names an existing file with a positive count", () => {
  const bad = Object.entries(CEILING).filter(
    ([file, count]) =>
      !Number.isInteger(count) || count <= 0 || !existsSync(join(ROOT, file)) || FORMATTER_MODULES.has(file),
  );
  assert.deepEqual(bad, [], `remove or fix these entries in ${CEILING_FILE}: ${bad.map(([file]) => file).join(", ")}`);
});
