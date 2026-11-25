/**
 * Chips and badges take the design-system chip radius, never a full pill - `yarn test`.
 *
 * `@filigran/design-system` `Chip` draws `rounded-sm` (the 4 px step of the
 * radius scale) with a 1 px edge and `px-2`. The shared `Badge`
 * (components/ui/badge.tsx), and `ToneBadge` and `DiffBadge` on top of it,
 * follow that shape (#1333). This gate keeps it that way:
 *
 * 1. the shared chip components carry `rounded-sm` and no `rounded-full`;
 * 2. no `<Badge>` or `<ToneBadge>` overrides the radius at a call site;
 * 3. no new hand-made chip is drawn as a pill: a class string that carries
 *    `rounded-full` and an inline padding (`px-1` to `px-4`) is a chip (its
 *    type size may be inherited), unless it is a fixed square (`h-N w-N`,
 *    `size-N`: an avatar, a dot, a round button), a numeric counter
 *    (`min-w-*`) or a shape filling a sized box (`h-full w-full`, a graph node). Every hand-made chip has moved (#1333):
 *    the list of pending chips is empty and any new pill fails.
 *
 * 4. a `<Badge>` holding only a count is a `CountBadge` (round counter).
 *
 * Avatars, status dots, progress bars, toggles, spinners, round icon buttons
 * and counters are not chips and stay round.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as focusable-badge-trigger.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** One class string (between quotes or backticks) on one line. */
const CLASS_STRING = /["'`]([^"'`\n]*)["'`]/g;

/**
 * The `cn(...)` / `clsx(...)` calls of a file, each read as ONE class set: a
 * chip whose padding sits in a separate argument (`compact ? "px-1.5" :
 * "px-2.5"`) is still a chip (#1333, UX round 1 B3).
 */
function classCalls(text: string): { start: number; end: number; classes: string }[] {
  const calls: { start: number; end: number; classes: string }[] = [];
  for (const open of text.matchAll(/\b(?:cn|clsx)\(/g)) {
    const start = open.index + open[0].length;
    let depth = 1;
    let end = start;
    while (end < text.length && depth > 0) {
      if (text[end] === "(") depth += 1;
      else if (text[end] === ")") depth -= 1;
      end += 1;
    }
    const body = text.slice(start, end - 1);
    calls.push({ start, end, classes: [...body.matchAll(CLASS_STRING)].map((m) => m[1]).join(" ") });
  }
  return calls;
}

/** A pill-shaped chip: full radius and inline padding in one class set (the type size may be inherited). */
function isPillChip(classes: string): boolean {
  if (!/(?:^|\s)rounded-full(?=\s|$)/.test(classes)) return false;
  if (!/(?:^|\s)px-(?:1|1\.5|2|2\.5|3|3\.5|4)(?=\s|$)/.test(classes)) return false;
  // The type size may be inherited from the parent (#1333, UX round 2 B2): not required.
  // A fixed square (avatar, dot, round button) or a numeric counter is not a chip.
  // A shape filling a sized box (`h-full w-full`: a work-item graph node) is not a chip either.
  if (/(?:^|\s)h-full\s+(?:[^\s]+\s+)*?w-full(?=\s|$)/.test(classes) && !/(?:^|\s)inline-flex(?=\s|$)/.test(classes)) return false;
  if (/(?:^|\s)(?:h-\[?[\d.]+(?:px)?\]?\s+w-\[?[\d.]+(?:px)?\]?|size-[\d.]+|min-w-(?:[1-9][\d.]*|\[[1-9][^\]\s]*\]))(?=\s|$)/.test(classes)) return false;
  return true;
}

/**
 * Hand-made pill chips still to move: none since the #1333 sweep. Never add
 * a row: a new chip uses Badge or ToneBadge (or the rounded-sm chip shape),
 * a count uses CountBadge.
 */
const PENDING_PILL_CHIPS: Record<string, number> = {};

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : [];
  });
}

function pillChipCounts(): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const path of sourceFiles(SRC)) {
    const text = readFileSync(path, "utf8");
    const calls = classCalls(text);
    const inCall = (at: number) => calls.some((call) => at >= call.start && at < call.end);
    const hits =
      [...text.matchAll(CLASS_STRING)].filter((m) => !inCall(m.index) && isPillChip(m[1])).length +
      calls.filter((call) => isPillChip(call.classes)).length;
    if (hits > 0) counts[path.slice(SRC.length).split(sep).join("/")] = hits;
  }
  return counts;
}

test("the shared chip components draw the design-system radius", () => {
  for (const file of ["components/ui/badge.tsx", "components/ui/diff-badge.tsx"]) {
    const text = readFileSync(join(SRC, file), "utf8");
    const code = text.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
    expect(code, file).toMatch(/\brounded-sm\b/);
    expect(code, file).not.toMatch(/\brounded-full\b/);
  }
});

test("no Badge or ToneBadge overrides the chip radius at a call site", () => {
  const OVERRIDE = /<(?:Badge|ToneBadge)\b[^<>]*?\brounded-(?:full|none|xs|md|lg|xl|2xl|3xl|\[[^\]]*\])/g;
  const offenders = sourceFiles(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    return [...text.matchAll(OVERRIDE)].map((hit) => `${path.slice(SRC.length)}:${text.slice(0, hit.index).split("\n").length}`);
  });
  expect(offenders).toEqual([]);
});

/**
 * A `<Badge>` whose only child is a count (a number, or an expression ending in
 * `.length`, `.size`, `count` or `total`) is a counter, not a chip: it is drawn
 * with `CountBadge` (a 20 px circle, the design-system standalone count), so a
 * count never takes the square chip shape (#1333, UX round 1).
 */
const COUNT_ONLY_BADGE = /<Badge\b[^<>]*>\s*(?:\d+|\{[^{}]*(?:\.length|\.size|[cC]ount|[tT]otal\w*)\s*\})\s*<\/Badge>/g;

test("a count is drawn with CountBadge, never a chip-shaped Badge", () => {
  const offenders = sourceFiles(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    return [...text.matchAll(COUNT_ONLY_BADGE)].map((hit) => `${path.slice(SRC.length)}:${text.slice(0, hit.index).split("\n").length}`);
  });
  expect(offenders).toEqual([]);
  for (const count of ["<Badge variant=\"outline\">{items.length}</Badge>", "<Badge>{count}</Badge>", "<Badge variant=\"cyan\">3</Badge>", "<Badge>\n  {totalBound}\n</Badge>"]) {
    expect(count.match(COUNT_ONLY_BADGE), count).not.toBeNull();
  }
  for (const chip of ["<Badge variant=\"outline\">{t_i18n(\"Paused\")}</Badge>", "<Badge>{span.type}</Badge>"]) {
    expect(chip.match(COUNT_ONLY_BADGE), chip).toBeNull();
  }
});

test("CountBadge stays round", () => {
  const code = readFileSync(join(SRC, "components/ui/count-badge.tsx"), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
  expect(code).toMatch(/\brounded-full\b/);
  expect(code).toMatch(/\bmin-w-5\b/);
});

test("no new hand-made chip is drawn as a full pill", () => {
  expect(pillChipCounts()).toEqual(PENDING_PILL_CHIPS);
});

test("the pill pattern catches chips and leaves round shapes alone", () => {
  for (const chip of [
    "inline-flex items-center rounded-full border px-2.5 py-0.5 content-compact font-semibold",
    "rounded-full px-2 py-0.5 text-[11px] font-medium",
    "inline-flex items-center gap-1 rounded-full bg-elevation-hover px-2 py-0.5 text-xs",
    // A truncating chip (`min-w-0`) is not a counter (#1333, UX round 1 B2).
    "flex min-w-0 shrink items-center gap-1.5 rounded-full border border-elevation-subtle px-2.5 py-0.5 content-compact",
    // The type size may be inherited from the parent (#1333, UX round 2 B2).
    "inline-flex items-center gap-1 rounded-full border border-dashed border-accent/30 px-1.5 py-1 text-accent/80",
    "flex items-center gap-1.5 rounded-full bg-elevation-hover px-3 py-1.5 border border-elevation-subtle-soft",
  ]) {
    expect(isPillChip(chip), chip).toBe(true);
  }
  // A padding in a separate cn() argument still makes a chip (#1333, UX round 1 B1).
  const split = `cn(\n  "inline-flex items-center gap-1.5 rounded-full border border-elevation-subtle-soft py-1 text-[11px]",\n  compact ? "px-1.5" : "px-2.5",\n)`;
  expect(classCalls(split).filter((call) => isPillChip(call.classes))).toHaveLength(1);
  for (const round of [
    "inline-flex items-center rounded-sm border px-2 py-0.5 content-compact font-semibold",
    "h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent",
    "flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold",
    "h-1.5 w-full rounded-full bg-elevation-hover",
    "flex h-6 w-6 items-center justify-center rounded-full px-1 content-caption",
    "size-3.5 rounded-full border px-1 content-caption",
    "flex h-full w-full flex-col justify-center gap-0.5 rounded-full border border-elevation-subtle bg-elevation-hover px-3 text-start",
  ]) {
    expect(isPillChip(round), round).toBe(false);
  }
});
