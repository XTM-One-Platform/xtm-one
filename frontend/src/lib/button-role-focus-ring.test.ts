/**
 * One ring and one hover treatment for elements that act as buttons - `yarn test`.
 *
 * Pinned here:
 * - `BUTTON_ROLE_FOCUS_RING` (lib/button-role.ts) draws the ring of the
 *   design-system Button and of the shared Tip wrapper, 2 px offset plus 2 px
 *   `ring-focus`, so Tab moving from a button to a clickable card keeps one style.
 * - A clickable `StatCard` sets its own pointer cursor and hover surface;
 *   a caller passing `onClick` never adds `cursor-pointer` or a `hover:` class.
 * - A KPI tile fills the DashboardGrid widget scroller, whose
 *   overflow clips the tile's ring, so the widget's unclipped wrapper draws the
 *   same ring while its button-role root has keyboard focus.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as tip-wrap-focus-ring.test.ts.
const SRC = join(process.cwd(), "src") + sep;

const STAT_CARD = /<StatCard\b[\s\S]*?\/>/g;
const CLICK_STYLE = /className=(?:"[^"]*|\{`[^`]*|\{"[^"]*)(?:\bcursor-pointer\b|\bhover:)/;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : [];
  });
}

/** The `<StatCard ... />` elements of a source that pass `onClick` with a click style of their own. */
function offendingCards(source: string): string[] {
  return (source.match(STAT_CARD) ?? []).filter((el) => /\bonClick=/.test(el) && CLICK_STYLE.test(el));
}

test("button-role elements draw the Button's ring (2 px offset plus 2 px ring-focus)", () => {
  const lib = readFileSync(join(SRC, "lib", "button-role.ts"), "utf8");
  const ring = lib.match(/export const BUTTON_ROLE_FOCUS_RING =\s*"([^"]+)"/)?.[1] ?? "";
  for (const cls of [
    "focus-visible:ring-2",
    "focus-visible:ring-focus",
    "focus-visible:ring-offset-2",
    "focus-visible:ring-offset-focus",
  ]) {
    expect(ring.split(/\s+/)).toContain(cls);
  }
});

test("the dashboard widget wrapper draws the button-role ring outside the clipping scroller", () => {
  const grid = readFileSync(join(SRC, "components", "dashboard-grid", "DashboardGrid.tsx"), "utf8");
  const wrapper = grid.match(/const WIDGET_WRAPPER =\s*"([^"]+)"/)?.[1] ?? "";
  const has = "has-[>*>[role=button]:focus-visible]:";
  for (const cls of ["relative", "h-full", `${has}ring-2`, `${has}ring-focus`, `${has}ring-offset-2`, `${has}ring-offset-focus`]) {
    expect(wrapper.split(/\s+/)).toContain(cls);
  }
  // The wrapper is the element around the widget scroller.
  expect(grid).toMatch(/<div className=\{WIDGET_WRAPPER\}>[\s\S]*?overflow-y-auto overflow-x-hidden/);
});

test("a clickable StatCard draws the button-role ring itself (#9837 N1)", () => {
  const statCard = readFileSync(join(SRC, "components/ui/stat-card.tsx"), "utf8");
  expect(statCard).toMatch(/onClick && \[CLICKABLE_CARD, BUTTON_ROLE_FOCUS_RING\]/);
});

test("the dashboard notice copy target and whole-notice button draw the button-role ring (#10877 B1)", () => {
  const notice = readFileSync(join(SRC, "components/ui/dashboard-notice.tsx"), "utf8");
  expect(notice).toMatch(/const COPY_TARGET = cn\([^)]*BUTTON_ROLE_FOCUS_RING\)/);
  expect(notice).toMatch(/hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING, s\.container/);
  // No local ring without the offset.
  expect(notice).not.toMatch(/focus-visible:ring-2 focus-visible:ring-focus"/);
});

/** The class string literals of a source that draw a 2 px focus ring offset
 *  without the `ring-offset-focus` colour: Tailwind then paints the offset white,
 *  a white halo around the ring in dark mode (#10956 UX r1 B1). */
function whiteOffsetRings(source: string): string[] {
  return (source.match(/"[^"\n]*"|`[^`]*`/g) ?? []).filter(
    (literal) => /\bfocus-visible:ring-offset-2\b/.test(literal) && !/\bring-offset-focus\b/.test(literal),
  );
}

test("the pattern catches a focus ring offset without its colour token", () => {
  expect(
    whiteOffsetRings('className="flex w-full rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2"'),
  ).toHaveLength(1);
  expect(whiteOffsetRings('className="rounded-sm ring-offset-focus focus-visible:ring-2 focus-visible:ring-offset-2"')).toHaveLength(0);
  expect(whiteOffsetRings('className="focus-visible:ring-offset-2 focus-visible:ring-offset-focus"')).toHaveLength(0);
});

test("no page draws a focus ring offset without ring-offset-focus (use BUTTON_ROLE_FOCUS_RING)", () => {
  const offenders = sourceFiles(join(SRC, "pages")).flatMap((path) =>
    whiteOffsetRings(readFileSync(path, "utf8")).map((literal) => `${path.slice(SRC.length)}: ${literal.slice(0, 80)}`),
  );
  expect(offenders).toEqual([]);
});

test("the pattern catches a caller styling a clickable StatCard", () => {
  expect(offendingCards('<StatCard icon={X} label="A" value={1} subtitle="b" onClick={go} className="cursor-pointer" />')).toHaveLength(1);
  expect(offendingCards('<StatCard icon={X} label="A" value={1} subtitle="b" className="hover:bg-elevation-hover" onClick={go} />')).toHaveLength(1);
  expect(offendingCards('<StatCard icon={X} label="A" value={1} subtitle="b" onClick={go} className="col-span-2" />')).toHaveLength(0);
  expect(offendingCards('<StatCard icon={X} label="A" value={1} subtitle="b" />')).toHaveLength(0);
});

test("no caller adds its own cursor or hover to a clickable StatCard", () => {
  const offenders = sourceFiles(SRC).flatMap((path) =>
    offendingCards(readFileSync(path, "utf8")).map((el) => `${path.slice(SRC.length)}: ${el.slice(0, 80)}`),
  );
  expect(offenders).toEqual([]);
});
