// Parallel slots of a work-item inbox: what the trigger card shows and
// what the editor accepts, the same bound as the API (1 to 10).

import { describe, expect, it } from "vitest";

import { WORK_ITEM_MAX_ITEMS_IN_PARALLEL, inboxSlots, parseItemsInParallel } from "./inbox-slots";

describe("inboxSlots", () => {
  it("is the serial default when the key is absent or unreadable", () => {
    expect(inboxSlots(undefined)).toBe(1);
    expect(inboxSlots({})).toBe(1);
    expect(inboxSlots({ max_concurrent_runs: "4" })).toBe(1);
    expect(inboxSlots({ max_concurrent_runs: 2.5 })).toBe(1);
    expect(inboxSlots({ max_concurrent_runs: 0 })).toBe(1);
  });

  it("reads a stored whole number", () => {
    expect(inboxSlots({ max_concurrent_runs: 1 })).toBe(1);
    expect(inboxSlots({ max_concurrent_runs: 4 })).toBe(4);
  });
});

describe("parseItemsInParallel", () => {
  it("accepts a whole number from 1 to the API's bound", () => {
    expect(parseItemsInParallel("1")).toBe(1);
    expect(parseItemsInParallel(" 3 ")).toBe(3);
    expect(parseItemsInParallel(String(WORK_ITEM_MAX_ITEMS_IN_PARALLEL))).toBe(10);
  });

  it("refuses what the API refuses", () => {
    for (const text of ["", "0", "11", "-1", "2.5", "two", "1e1"]) {
      expect(parseItemsInParallel(text), text).toBeNull();
    }
  });
});
