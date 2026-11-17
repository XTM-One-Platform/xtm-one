// The Operations tabs: scheduled work items live in Work Items, so the
// list is the same six tabs as before. A change here is a product decision.

import { describe, expect, it } from "vitest";

import { OPERATIONS_TABS, operationsTabSearch } from "./tabs";

describe("Operations tabs", () => {
  it("are the six tabs, in order, with no Scheduled tab", () => {
    expect(OPERATIONS_TABS.map((t) => t.key)).toEqual([
      "executions",
      "background-tasks",
      "work-items",
      "state",
      "worksets",
      "workspaces",
    ]);
    expect(OPERATIONS_TABS.map((t) => t.label)).not.toContain("Scheduled");
  });
});

// The fleet a reader picked stays selected across the tab links.
describe("Operations tab links", () => {
  const current = new URLSearchParams("fleet=f1&agent=a1&q=x&status=open&page=3");

  it("carry the fleet to every list tab", () => {
    for (const key of ["background-tasks", "state", "worksets"] as const) {
      expect(operationsTabSearch(key, current)).toBe("?fleet=f1");
    }
  });

  it("carry the agent only to the tabs that filter by agent", () => {
    expect(operationsTabSearch("executions", current)).toBe("?fleet=f1&agent=a1");
    expect(operationsTabSearch("work-items", current)).toBe("?fleet=f1&agent=a1");
  });

  it("never carry page, search or status", () => {
    for (const { key } of OPERATIONS_TABS) {
      const search = new URLSearchParams(operationsTabSearch(key, current));
      expect(search.has("q")).toBe(false);
      expect(search.has("status")).toBe(false);
      expect(search.has("page")).toBe(false);
    }
  });

  // Workspaces ignores the fleet but its link keeps it in the URL.
  it("carry the fleet through Workspaces, without the agent", () => {
    expect(operationsTabSearch("workspaces", current)).toBe("?fleet=f1");
  });

  it("keep the fleet on a switch from Workspaces to a list tab", () => {
    const onWorkspaces = new URLSearchParams(
      operationsTabSearch("workspaces", new URLSearchParams("fleet=f1&page=2")),
    );
    expect(operationsTabSearch("work-items", onWorkspaces)).toBe("?fleet=f1");
    expect(operationsTabSearch("worksets", onWorkspaces)).toBe("?fleet=f1");
  });

  it("are unchanged when no fleet is selected", () => {
    for (const { key } of OPERATIONS_TABS) {
      expect(operationsTabSearch(key, new URLSearchParams("q=x&page=2"))).toBe("");
    }
  });
});
