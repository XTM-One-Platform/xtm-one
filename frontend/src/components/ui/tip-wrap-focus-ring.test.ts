/**
 * The focusable `wrap` span of the shared Tip owns its keyboard focus ring - `yarn test`.
 *
 * A disabled control sits in Tip's `wrap` / `wrap="auto"` span, which becomes the
 * tab stop. Call sites used to write the ring into `wrapClassName` by hand, four
 * copies at three styles, none matching the 2 px offset plus 2 px ring of the
 * enabled Button beside them. Tip now draws the ring
 * itself (TIP_WRAP_FOCUS_RING in components/ui/tooltip.tsx); `wrapClassName` is
 * layout only and never carries a focus ring of its own.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as focusable-badge-trigger.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** A `wrapClassName` value (string literal or template) carrying a focus variant ring or outline. */
const WRAP_WITH_FOCUS = /wrapClassName=(?:"[^"]*|\{`[^`]*|\{"[^"]*)\bfocus(?:-visible|-within)?:(?:ring|outline)/g;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

test("the pattern catches a hand-written ring on a Tip wrapper", () => {
  const sample = '<Tip wrap="auto" wrapClassName="inline-flex rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus">';
  expect(sample.match(WRAP_WITH_FOCUS)).toHaveLength(1);
  expect('<Tip wrap wrapClassName="inline-flex w-full">'.match(WRAP_WITH_FOCUS)).toBeNull();
});

test("no wrapClassName carries its own focus ring", () => {
  const offenders = sourceFiles(SRC).flatMap((path) => {
    const hits = readFileSync(path, "utf8").match(WRAP_WITH_FOCUS) ?? [];
    return hits.map((hit) => `${path.slice(SRC.length)}: ${hit}`);
  });
  expect(offenders).toEqual([]);
});

test("Tip draws the Button's ring (2 px offset plus 2 px ring-focus) on its wrapper", () => {
  const tooltip = readFileSync(join(SRC, "components", "ui", "tooltip.tsx"), "utf8");
  const ring = tooltip.match(/const TIP_WRAP_FOCUS_RING =\s*"([^"]+)"/)?.[1] ?? "";
  for (const cls of [
    "focus-visible:ring-2",
    "focus-visible:ring-focus",
    "focus-visible:ring-offset-2",
    "focus-visible:ring-offset-focus",
  ]) {
    expect(ring.split(/\s+/)).toContain(cls);
  }
  expect(tooltip.split("cn(TIP_WRAP_FOCUS_RING, wrapClassName)").length - 1).toBe(2);
});
