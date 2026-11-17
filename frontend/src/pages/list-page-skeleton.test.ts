// List pages load on their own list or card grid with the content greyed out
// (#11345): the page header and toolbar stay mounted, the rows or cards load in
// the loaded anatomy. These pages left the generic PageSkeleton in slices 5d and
// 5e and must not go back to it.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { cardSkeletonCount } from "@/components/ui/card-grid";

const MIGRATED = [
  "AdminObjects.tsx",
  "BrowserProfiles.tsx",
  "Datasets.tsx",
  "Evaluations.tsx",
  "Relays.tsx",
  "TaskHistory.tsx",
  "Templates.tsx",
  "TraceSessions.tsx",
  "Traces.tsx",
];

describe("list page skeletons", () => {
  it("keeps the migrated list pages off the generic PageSkeleton", () => {
    // vitest runs from frontend/, as detail-page-skeleton.test.tsx.
    const offenders = MIGRATED.filter((name) =>
      readFileSync(join(process.cwd(), "src", "pages", name), "utf8").includes("PageSkeleton"),
    );
    expect(offenders).toEqual([]);
  });

  it("caps a list with no pager at ten rows of cards", () => {
    expect(cardSkeletonCount(undefined, 5000)).toBe(30);
    expect(cardSkeletonCount(undefined, 700)).toBe(15);
  });
});
