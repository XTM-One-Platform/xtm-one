/**
 * Segment counts on every visibility filter.
 *
 * The client-filtered lists get them from `useEntityFilter`, the paginated
 * ones from `useSegmentCounts` (`GET <list>/segment-counts`). A page that
 * draws the control without `counts` would show bare labels again: the guard
 * below reads every page that renders it, directly or through the records
 * table's `scope` prop (standard v2 toolbar, which draws the same control).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { addSegmentCounts } from "./useSegmentCounts";

const SRC = join(__dirname, "..", "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : [];
  });
}

describe("addSegmentCounts", () => {
  it("adds the feeds shown segment by segment", () => {
    expect(
      addSegmentCounts({ mine: 2, shared: 0, company: 3, all: 5 }, { mine: 1, shared: 4, company: 0, all: 5 }),
    ).toEqual({ mine: 3, shared: 4, company: 3, all: 10 });
  });

  it("ignores a feed not shown", () => {
    expect(addSegmentCounts(false, { mine: 1, shared: 0, company: 0, all: 1 })).toEqual({
      mine: 1,
      shared: 0,
      company: 0,
      all: 1,
    });
  });

  it("shows no number until every feed shown answered", () => {
    expect(addSegmentCounts({ mine: 1, shared: 0, company: 0, all: 1 }, undefined)).toBeUndefined();
    expect(addSegmentCounts(false, false)).toBeUndefined();
  });

  it("shows no number when a feed shown failed", () => {
    expect(addSegmentCounts({ mine: 1, shared: 0, company: 0, all: 1 }, null)).toBeNull();
    expect(addSegmentCounts(undefined, null)).toBeNull();
    expect(addSegmentCounts(null, false)).toBeNull();
  });
});

describe("every visibility filter shows its counts", () => {
  const usages = sources(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    // A page on the v2 toolbar hands the control to the table: `scope={{ value, onChange, counts }}`,
    // or behind a condition: `scope={lockedFleet ? undefined : { value, onChange, counts }}`.
    const pattern = /<VisibilityFilter\b[\s\S]*?\/>|\bscope=\{(?:[^{}]*?\?\s*(?:undefined\s*:\s*)?)?\{\s*value:[\s\S]*?\}/g;
    return [...text.matchAll(pattern)].map((match) => ({
      file: relative(SRC, path),
      tag: match[0],
    }));
  });

  it("finds the pages that draw it", () => {
    expect(usages.length).toBeGreaterThanOrEqual(24);
  });

  it.each(usages.map((usage) => [usage.file, usage.tag]))("%s passes counts", (_file, tag) => {
    // `counts={...}` on the control, `counts: x` or the shorthand `counts` in the scope object.
    expect(tag).toMatch(/\bcounts(=\{|\s*:|\s*[,}])/);
  });
});
