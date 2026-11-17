/**
 * No class-name assertions in the frontend tests - `yarn test`.
 *
 * frontend/AGENTS.md: "Assert on text, roles and alt text, never on snapshots
 * or class names." A test that reads `.className`, `classList`,
 * `getAttribute("class")` or calls `toHaveClass(` pins a styling detail, not
 * what the user meets: it breaks on every restyle and passes on a broken
 * screen. This gate scans every test file under src/ and tests_unit/ and fails
 * on any of these four forms outside the allow-list below.
 *
 * The allow-list is explicit and can only shrink:
 *   - CONTRACT entries read a class on purpose: the deck renderer's `deck-*`
 *     structure classes are the HTML export's contract (the exported deck's
 *     stylesheet selects on them), the theme classes on the document root or
 *     a canvas are the theme switch itself, and a fake DOM stub implements
 *     `classList` / `className` for the code under test.
 *   - LEGACY entries assert Tailwind utility classes and predate this gate;
 *     migrate them to text, roles, accessible names or computed layout, then
 *     delete the entry. Never add a LEGACY entry.
 * An entry whose file is gone or no longer uses any of the four forms fails
 * the gate too, so the list never goes stale.
 *
 * Design-system guards that read class strings from SOURCE files with
 * readFileSync (shadow-overlay-floating, warning-text-contrast,
 * input-feedback-border, ...) do not use these forms and need no entry.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const ROOT = process.cwd();
const ROOTS = ["src", "tests_unit"];
const SELF = "src/no-class-name-assertions.test.ts";
const TEST_FILE = /\.test\.(ts|tsx)$/;

/** The four forms a test uses to read or assert a class name. */
const CLASS_NAME_ASSERTION = /\.className\b|\btoHaveClass\(|\bclassList\b|getAttribute\(\s*["']class["']\s*\)/;

const CONTRACT = "contract: reads a class on purpose";
const LEGACY = "legacy: asserts utility classes, migrate then delete this entry";

/** Path from frontend/ -> why it may use the forms. Only ever shrinks. */
const ALLOWED: Record<string, string> = {
  // Fake DOM stub implementing classList / className for the engine under test.
  "src/components/arcade/engine/screen-damage.test.ts": CONTRACT,
  // Deck renderer: deck-* structure classes are the HTML export contract.
  "src/components/presentation/blocks/architecture-bare.test.tsx": CONTRACT,
  "src/components/presentation/blocks/architecture-zone-chips.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-butterfly.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-fill.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-gauge.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-legend-position.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-map.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-surface.test.tsx": CONTRACT,
  "src/components/presentation/blocks/chart-title.test.tsx": CONTRACT,
  "src/components/presentation/blocks/diagram-theme.test.tsx": CONTRACT,
  "src/components/presentation/blocks/metrics.test.tsx": CONTRACT,
  "src/components/presentation/blocks/showcase.test.tsx": CONTRACT,
  "src/components/presentation/blocks/table.test.tsx": CONTRACT,
  // Theme switch: the light / dark classes on the root or the canvas are the feature.
  "src/hooks/useThemeEffect.test.ts": CONTRACT,
  "src/index-html-prepaint.test.ts": CONTRACT,
  "src/pages/new-agent-wizard/WizardCanvas.test.tsx": CONTRACT,
  // Legacy: files that still assert on class names.
  "src/components/investigation/SourcePackPicker.test.tsx": LEGACY,
  "src/components/layout/ImpersonationBanner.test.tsx": LEGACY,
  "src/components/ui/charts/chart-scroller.test.tsx": LEGACY,
  "src/components/ui/entity-table.test.tsx": LEGACY,
  "src/components/ui/run-card.test.tsx": LEGACY,
  "src/components/ui/schedule-row.test.tsx": LEGACY,
  "src/components/work-items/ScheduledDetails.test.tsx": LEGACY,
  "src/components/work-items/WorkItemDetailDialog.test.tsx": LEGACY,
  "src/pages/assignment-detail/AssignmentStatus.test.tsx": LEGACY,
  "src/pages/operations/WorkItemsTab.test.tsx": LEGACY,
  // SearchableSelect: TWO_LINE_LIST_CLASS is the exported class the DS select content takes (#11596).
  "src/components/ui/searchable-select.test.tsx": CONTRACT,
};

function* testFiles(dir: string): Generator<string> {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== "dist") yield* testFiles(full);
    } else if (TEST_FILE.test(entry.name)) {
      yield relative(ROOT, full).split(sep).join("/");
    }
  }
}

const offendingLines = (file: string): string[] =>
  readFileSync(join(ROOT, file), "utf8")
    .split("\n")
    .flatMap((line, index) => (CLASS_NAME_ASSERTION.test(line) ? [`${file}:${index + 1}: ${line.trim()}`] : []));

test("the pattern catches the four forms and nothing else", () => {
  for (const line of [
    'expect(note.className).toContain("text-default-secondary");',
    'expect(region).toHaveClass("overflow-x-auto");',
    'expect(el.classList.contains("sr-only")).toBe(true);',
    "expect(el.getAttribute('class')).toBe(\"truncate\");",
  ]) {
    assert.match(line, CLASS_NAME_ASSERTION, line);
  }
  for (const line of [
    'render(<Badge className="shrink-0" />);',
    'expect(screen.getByRole("button", { name: "Save" })).toBeVisible();',
    'expect(link.getAttribute("href")).toBe("/fleets");',
  ]) {
    assert.doesNotMatch(line, CLASS_NAME_ASSERTION, line);
  }
});

test("no test file asserts on class names outside the allow-list", () => {
  const offenders: string[] = [];
  for (const root of ROOTS) {
    for (const file of testFiles(join(ROOT, root))) {
      if (file === SELF || file in ALLOWED) continue;
      offenders.push(...offendingLines(file));
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `Assert on text, roles and alt text, never on class names (frontend/AGENTS.md, ${SELF}):\n${offenders.join("\n")}`,
  );
});

test("every allow-list entry still exists and still needs its entry", () => {
  const stale = Object.keys(ALLOWED).filter((file) => !existsSync(join(ROOT, file)) || offendingLines(file).length === 0);
  assert.deepEqual(stale, [], `remove these entries from ALLOWED in ${SELF}: ${stale.join(", ")}`);
});
