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
import { expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

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
