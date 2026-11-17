/**
 * Keyboard focus rings use the `ring-focus` token, never the accent - `yarn test`.
 *
 * `focus-visible:ring-1 focus-visible:ring-accent/50` measured 2.40:1 (light)
 * and 2.30:1 (dark) on the hover panel, under the 3:1 a focus indicator needs.
 * A badge whose tooltip opens from the
 * keyboard goes through `BadgeFocusTrigger` (components/ui/badge.tsx); every
 * other focus indicator is `ring-2 ring-focus` under its `focus-visible:` or
 * `focus-within:` variant (dev-docs/reference/frontend-design-system.md).
 * The token at 1 px (`focus-visible:ring-1 focus-visible:ring-focus`) drew one
 * pattern at two widths, and `focus:ring-focus` lit the ring on a mouse click:
 * both are forbidden too.
 *
 * `ring-accent` stays legitimate as a selection, active or highlight state
 * (a selected row, a selected asset, the transient message highlight): those
 * carry no focus variant and are not matched here, so no allowlist is needed.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** A focus variant (`focus:`, `focus-visible:`, `focus-within:`) drawing its ring with the accent, any opacity. */
const ACCENT_FOCUS_RING = /\bfocus(?:-visible|-within)?:ring-accent(?:\/\d+)?(?![\w-])/g;

/** A 1 px ring paired with the focus token under a focus variant, in either order within one class string. */
const THIN_FOCUS_RING =
  /\bfocus(?:-visible|-within)?:ring-1(?![\w-])(?=[^"'`]*\bfocus(?:-visible|-within)?:ring-focus(?![\w-]))|\bfocus(?:-visible|-within)?:ring-focus(?![\w-])(?=[^"'`]*\bfocus(?:-visible|-within)?:ring-1(?![\w-]))/g;

/** The focus token under the bare `focus:` variant, which also fires on a mouse click. */
const MOUSE_FOCUS_RING = /\bfocus:ring-focus(?![\w-])/g;

/** One JSX opening tag carrying both `tabIndex={0}` and `ring-accent/50`, in either order. */
const FAINT_TAB_STOP = /<[A-Za-z][^<>]*?(?:tabIndex=\{0\}[^<>]*?ring-accent\/50|ring-accent\/50[^<>]*?tabIndex=\{0\})/g;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

function offenders(pattern: RegExp): string[] {
  return sourceFiles(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    // Whole-file match: a JSX opening tag can span several lines.
    return [...text.matchAll(pattern)].map((hit) => {
      const line = text.slice(0, hit.index).split("\n").length;
      return `${path.slice(SRC.length)}:${line} ${hit[0].slice(-60)}`;
    });
  });
}

test("no focus indicator is drawn with ring-accent", () => {
  expect(offenders(ACCENT_FOCUS_RING)).toEqual([]);
});

test("no focus ring is drawn at 1 px with ring-focus", () => {
  expect(offenders(THIN_FOCUS_RING)).toEqual([]);
});

test("no focus ring uses focus: instead of focus-visible:", () => {
  expect(offenders(MOUSE_FOCUS_RING)).toEqual([]);
});

test("no tab stop is drawn with the faint ring-accent/50 focus ring", () => {
  expect(offenders(FAINT_TAB_STOP)).toEqual([]);
});

test("the patterns catch the markup they forbid and nothing else", () => {
  for (const forbidden of [
    "focus-visible:ring-1 focus-visible:ring-accent/50",
    "focus-visible:ring-2 focus-visible:ring-accent",
    "focus:ring-1 focus:ring-accent/40",
    "focus-within:ring-2 focus-within:ring-accent",
  ]) {
    expect(forbidden.match(ACCENT_FOCUS_RING)).toHaveLength(1);
  }
  for (const allowed of [
    "focus-visible:ring-2 focus-visible:ring-focus",
    "focus-within:ring-2 focus-within:ring-focus",
    "ring-1 ring-accent/30",
    'isSelected && "ring-1 ring-accent"',
    "focus:ring-accent-foreground",
  ]) {
    expect(allowed.match(ACCENT_FOCUS_RING)).toBeNull();
  }
  for (const thin of [
    "focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-focus",
    "focus:outline-none focus:ring-1 focus:ring-focus",
    "focus-within:ring-focus focus-within:ring-1",
  ]) {
    expect(thin.match(THIN_FOCUS_RING)).toHaveLength(1);
  }
  for (const allowed of [
    "focus-visible:ring-2 focus-visible:ring-focus",
    "focus-within:ring-2 focus-within:ring-focus",
    "rounded-full ring-1 ring-focus",
    "focus-visible:ring-1 focus-visible:ring-feedback-warning-primary/50",
    'focus-visible:ring-1" + "x focus-visible:ring-focus',
  ]) {
    expect(allowed.match(THIN_FOCUS_RING)).toBeNull();
  }
  expect("focus:outline-hidden focus:ring-2 focus:ring-focus".match(MOUSE_FOCUS_RING)).toHaveLength(1);
  expect("focus-visible:ring-2 focus-visible:ring-focus".match(MOUSE_FOCUS_RING)).toBeNull();
  const sample = '<span tabIndex={0} className="inline-flex rounded-sm outline-none focus-visible:ring-1 focus-visible:ring-accent/50">';
  expect(sample.match(FAINT_TAB_STOP)).toHaveLength(1);
  expect('<span tabIndex={0} className="focus-visible:ring-2 focus-visible:ring-focus">'.match(FAINT_TAB_STOP)).toBeNull();
});
