/**
 * The review-rate figure and the list it opens read one query (#10570) -
 * `yarn test:unit`. A count that includes work items behind a runs-only list
 * (the agent Activity tab's `?review=unreviewed`) fails here.
 */
import { describe, expect, it } from "vitest";

import { agentReviewPath, fleetReviewPath } from "@/lib/roi-outcomes";
import { readRoiRecordsQuery, roiRecordHref, roiRecordsApiParams, roiReviewCounts } from "@/lib/roi-records";

/** The query the list reads back from the path the card links to. */
function listQueryOf(path: string, scope: "agent" | "fleet", id: string) {
  const url = new URL(path, "https://x.test");
  return readRoiRecordsQuery(scope, id, url.searchParams);
}

describe("the review-rate card and its list", () => {
  it("opens the ROI records list, never a runs-only list", () => {
    expect(agentReviewPath("a-1", "30d")).not.toContain("/activity?");
    expect(fleetReviewPath("f-1", "30d")).not.toContain("/operations/runs");
    expect(agentReviewPath("a-1", "30d").startsWith("/agents/a-1/activity/records?")).toBe(true);
    expect(fleetReviewPath("f-1", "30d").startsWith("/fleets/f-1/roi/records?")).toBe(true);
  });

  it("lists every kind of record under the card's window and filter", () => {
    for (const [path, scope, id] of [
      [agentReviewPath("a-1", "7d"), "agent", "a-1"],
      [fleetReviewPath("f-1", "90d"), "fleet", "f-1"],
    ] as const) {
      const query = listQueryOf(path, scope, id);
      expect(query.review).toBe("unreviewed");
      const api = roiRecordsApiParams(query);
      expect(api.get("outcome")).toBe("unreviewed");
      expect(api.get("window")).toBe(scope === "agent" ? "7d" : "90d");
      // No kind filter: runs and work items alike, as the report counts them.
      expect([...api.keys()].sort()).toEqual([scope === "agent" ? "agent_id" : "fleet_id", "outcome", "window"].sort());
    }
  });

  it("sends the search box to the list and its count alike", () => {
    const query = listQueryOf(agentReviewPath("a-1", "30d"), "agent", "a-1");
    expect(roiRecordsApiParams(query, "  triage ").get("search")).toBe("triage");
    expect(roiRecordsApiParams(query, "   ").has("search")).toBe(false);
    expect(roiRecordsApiParams(query).has("search")).toBe(false);
  });

  it("counts every record in the caption: runs and work items", () => {
    // 2,782 work-item records and no run: the caption's total is what the list holds.
    expect(roiReviewCounts({ runs: 2782, reviewed: 0, unreviewed: 2782 })).toEqual({ reviewed: 0, total: 2782, unreviewed: 2782 });
  });

  it("opens a work item's record on its work item and a run's on its run", () => {
    expect(roiRecordHref({ kind: "work_item", run_id: "r-1", work_item_id: "w-1" })).toBe("/tasks/work-items/w-1");
    expect(roiRecordHref({ kind: "run", run_id: "r-1", work_item_id: null })).toBe("/operations/executions/r-1");
  });
});
