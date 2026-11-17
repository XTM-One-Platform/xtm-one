/**
 * The records list's review filter sits in the DataTable toolbar, sized to its
 * segments (#10570, UX round 2 B1) - `yarn test:unit`. A SegmentedFilter
 * rendered above the table stretches to a full-width track with its segments
 * at the left of an empty bar; this fails if it moves back there.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SOURCE = readFileSync(join(process.cwd(), "src", "components", "roi", "RoiRecordsList.tsx"), "utf8");

describe("the ROI records list layout", () => {
  it("puts the review filter in the toolbar slot, sized to its content", () => {
    const toolbar = SOURCE.indexOf("toolbar={");
    const filter = SOURCE.indexOf("<SegmentedFilter");
    expect(toolbar).toBeGreaterThan(-1);
    expect(filter).toBeGreaterThan(toolbar);
    expect(SOURCE.indexOf("<DataTable")).toBeLessThan(filter);
    expect(SOURCE.slice(filter, SOURCE.indexOf("/>", filter))).toContain('className="w-fit"');
  });
});
