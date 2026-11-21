/**
 * One in-page section index in the frontend (#9539, UX B3) - `yarn test`.
 *
 * The sticky "On this page" list that jumps to the sections of a long page is
 * `SectionIndex` (components/ui/section-index.tsx), beside the page (vertical)
 * or as a bar above it (horizontal): it sticks below the shell header and marks
 * the current entry. A page-local copy drifts (the first one stuck at 16 px
 * under the 68 px header; #12271 hid a card title under a two-row bar), so the
 * gate fails on any other `nav` named "On this page", its attributes on one line
 * or on several, and on a hand-written scroll margin under the header.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { currentLine, pickCurrentSection } from "@/components/ui/section-index";

const SRC = join(process.cwd(), "src");
const OWNER = join(SRC, "components", "ui", "section-index.tsx");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx$/.test(entry.name) && full !== OWNER) {
      yield full;
    }
  }
}

/** The line numbers of each `<nav ...>` opening tag named "On this page", whatever its line breaks. */
function pageLocalIndexes(source: string): number[] {
  const lines: number[] = [];
  for (const match of source.matchAll(/<nav\b[^>]*>/g)) {
    if (/t_i18n\(\s*["']On this page["']\s*\)/.test(match[0])) lines.push(source.slice(0, match.index).split("\n").length);
  }
  return lines;
}

/** A scroll margin under the shell header written by hand: the anchor classes of SectionIndex own it. */
const HAND_ANCHOR = /scroll-mt-\[calc\(var\(--fds-header-height,68px\)_\+_(?!1rem\)\])[^\]]*\]/;

it("finds a page-local index on one line and on several", () => {
  expect(pageLocalIndexes('<nav aria-label={t_i18n("On this page")}>')).toEqual([1]);
  expect(pageLocalIndexes('x\n<nav\n  aria-label={t_i18n("On this page")}\n  className="sticky"\n>')).toEqual([2]);
  expect(pageLocalIndexes('<nav aria-label={t_i18n("Breadcrumb")}>')).toEqual([]);
  expect(HAND_ANCHOR.test('"scroll-mt-[calc(var(--fds-header-height,68px)_+_4.5rem)]"')).toBe(true);
  expect(HAND_ANCHOR.test('"scroll-mt-[calc(var(--fds-header-height,68px)_+_1rem)]"')).toBe(false);
});

it("every in-page section index is SectionIndex", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const source = readFileSync(file, "utf8");
    const name = relative(SRC, file).split(sep).join("/");
    pageLocalIndexes(source).forEach((line) => offenders.push(`${name}:${line}`));
    source.split("\n").forEach((line, i) => {
      if (HAND_ANCHOR.test(line)) offenders.push(`${name}:${i + 1} (scroll margin)`);
    });
  }
  expect(offenders).toEqual([]);
});

it("SectionIndex sticks below the header and marks the current entry", () => {
  const source = readFileSync(OWNER, "utf8");
  expect(source).toMatch(/sticky top-\[calc\(var\(--fds-header-height,68px\)_\+_1rem\)\]/);
  expect(source).toMatch(/scroll-mt-\[calc\(var\(--fds-header-height,68px\)_\+_1rem\)\]/);
  expect(source).toMatch(/aria-current=\{isCurrent \? "location" : undefined\}/);
  // The horizontal bar lands a jump below its measured height, never a fixed guess.
  expect(source).toMatch(/scroll-mt-\[calc\(var\(--fds-header-height,68px\)_\+_var\(--section-bar-height,49px\)_\+_1rem\)\]/);
});

describe("pickCurrentSection (#794 UX r2 and r3 B1)", () => {
  const page = { bandTop: 140, viewportBottom: 1117, viewportHeight: 1117, atTop: false, remaining: 5000 };

  it("is the first section at the top, even when its top is below the band", () => {
    expect(pickCurrentSection({ ...page, tops: [429, 700, 1500], atTop: true })).toBe(0);
  });

  it("is the last section whose top passed the band", () => {
    expect(pickCurrentSection({ ...page, tops: [-900, 100, 900] })).toBe(1);
    expect(pickCurrentSection({ ...page, tops: [200, 900, 1500] })).toBe(0);
  });

  it("is the last section in view at the bottom, when short last sections never reach the band", () => {
    // The UX Director's measure at 1728x1117: Alerts at 652 px, Companion pets at 814 px.
    expect(pickCurrentSection({ ...page, tops: [-1844, -1573, -773, 652, 814], remaining: 0 })).toBe(4);
  });

  /** The entries current at some scroll position, sweeping the page from top to bottom one pixel at a time. */
  function currentAtSomeScroll(docTops: readonly number[], maxScroll: number, bandTop: number, viewportHeight: number): Set<number> {
    const seen = new Set<number>();
    for (let scrollTop = 0; scrollTop <= maxScroll; scrollTop += 1) {
      seen.add(
        pickCurrentSection({
          tops: docTops.map((top) => top - scrollTop),
          bandTop,
          viewportBottom: viewportHeight,
          viewportHeight,
          atTop: scrollTop <= 0,
          remaining: Math.max(0, maxScroll - scrollTop - 2),
        }),
      );
    }
    return seen;
  }

  it("marks every Platform Health entry current at some scroll, Alerts before Companion pets (UX r3 B1)", () => {
    // Settings > Platform Health bar at 1728x1117, max scroll 2273: Alerts and pets are short and last.
    expect(currentAtSomeScroll([429, 700, 1500, 2925, 3087], 2273, 140, 1117)).toEqual(new Set([0, 1, 2, 3, 4]));
  });

  it("marks every Value methodology entry current at some scroll (UX r3 B1)", () => {
    // Vertical index at 1728x1117, max scroll 920: What is counted, Who sees what and How to change
    // a default sit at 185, 520 and 801 px at scroll 880 and were never current with a fixed band.
    expect(currentAtSomeScroll([100, 800, 1065, 1400, 1681, 1900], 920, 84, 1117)).toEqual(new Set([0, 1, 2, 3, 4, 5]));
  });

  it("slides the line from the band to the bottom of the screen over the last viewport of scroll", () => {
    expect(currentLine({ bandTop: 140, viewportBottom: 1117, viewportHeight: 1117, remaining: 3000 })).toBe(140);
    expect(currentLine({ bandTop: 140, viewportBottom: 1117, viewportHeight: 1117, remaining: 0 })).toBe(1117);
  });

  it("has none without sections", () => {
    expect(pickCurrentSection({ ...page, tops: [] })).toBe(-1);
  });
});
