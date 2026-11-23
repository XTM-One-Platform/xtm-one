// One header for every titled fold of a fleet page (UX round 1 of #1393, B1): a card
// whose heading is followed by an aria-expanded toggle draws it through
// FleetFoldHeader, never a page-local copy (the Capabilities "Show slots" button and
// the Members "Show" button drifted apart in icon, heading level and label).
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = join(__dirname);
const SHARED = "FleetFoldHeader.tsx";

/** Whether a source holds a card heading (h2/h3 inside CardContent) followed within 30 lines by an aria-expanded toggle. */
function hasLocalFoldHeader(source: string): boolean {
  const lines = source.split("\n");
  return lines.some((line, i) => {
    if (!line.includes("aria-expanded")) return false;
    const before = lines.slice(Math.max(0, i - 30), i).join("\n");
    return /<h[23]\b/.test(before) && before.includes("<CardContent");
  });
}

describe("fleet fold header gate (#1393)", () => {
  it("finds a page-local fold header", () => {
    expect(hasLocalFoldHeader('<CardContent>\n<h3>Title</h3>\n<Button aria-expanded={open}>Show</Button>')).toBe(true);
    expect(hasLocalFoldHeader('<CardContent>\n<FleetFoldHeader title="Members" fold={fold} />')).toBe(false);
  });

  it("every fleet card fold draws its header through FleetFoldHeader", () => {
    const offenders = readdirSync(DIR)
      .filter((name) => name.endsWith(".tsx") && !name.includes(".test.") && name !== SHARED)
      .filter((name) => hasLocalFoldHeader(readFileSync(join(DIR, name), "utf8")));
    expect(offenders).toEqual([]);
  });
});
