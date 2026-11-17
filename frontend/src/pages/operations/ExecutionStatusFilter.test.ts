// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { statusesFromParams } from "./ExecutionStatusFilter";
import { addRunStatusCounts } from "@/hooks/api/useRunStatusCounts";

describe("statusesFromParams", () => {
  it("reads the multi-select status param in the life order", () => {
    expect(statusesFromParams("failed,running", null)).toEqual(["running", "failed"]);
  });

  it("keeps an old link's single filter param readable", () => {
    expect(statusesFromParams(null, "running")).toEqual(["running"]);
    expect(statusesFromParams(null, "awaiting_human")).toEqual(["awaiting_human"]);
  });

  it("prefers status over the legacy filter and drops unknown values", () => {
    expect(statusesFromParams("completed,nope", "running")).toEqual(["completed"]);
    expect(statusesFromParams(null, "notifications")).toEqual([]);
    expect(statusesFromParams("", null)).toEqual([]);
  });
});

describe("addRunStatusCounts", () => {
  it("adds the feeds shown status by status", () => {
    expect(addRunStatusCounts({ running: 2, failed: 1 }, { running: 3, awaiting_human: 1 })).toEqual({
      running: 5,
      failed: 1,
      awaiting_human: 1,
    });
  });

  it("skips a feed not shown and answers undefined when none answered", () => {
    expect(addRunStatusCounts({ running: 2 }, false)).toEqual({ running: 2 });
    expect(addRunStatusCounts(undefined, false)).toBeUndefined();
  });
});
