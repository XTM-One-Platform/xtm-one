import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// #11477: a row of four KPI cards that each carry a range picker goes four
// per line only from xl; below it, two per line, so the labels stay whole
// and the picker stays on one line at 1080 px. jsdom has no layout, so the
// breakpoint is pinned on the source; the lab render is the layout proof.
describe("AI adoption four-card KPI rows", () => {
  // vitest runs from frontend/ (CI and `yarn test`).
  const DIR = join(process.cwd(), "src/pages/ai-adoption");
  const ROW = (testId: string, file: string) =>
    readFileSync(join(DIR, file), "utf8")
      .split("\n")
      .find((line) => line.includes(`data-testid="${testId}"`)) ?? "";

  it.each([
    ["overview-kpi-row", "OverviewTab.tsx"],
    ["gaps-kpi-row", "GapsTab.tsx"],
  ])("%s goes four per line from xl only", (testId, file) => {
    const row = ROW(testId, file);
    expect(row).toContain("sm:grid-cols-2");
    expect(row).toContain("xl:grid-cols-4");
    expect(row).not.toContain("lg:grid-cols-4");
  });
});
