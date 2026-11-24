// The Operations and Tasks tabs (#775): Operations holds what is running or
// past, Tasks what to do and follow. A change here is a product decision.

import { describe, expect, it } from "vitest";

import { OPERATIONS_TABS, TASKS_TABS, operationsTabSearch, tasksTabOf, tasksTabSearch } from "./tabs";

describe("Operations tabs", () => {
  it("are the four tabs, in order, with no Scheduled, Work Items or Workspaces tab", () => {
    expect(OPERATIONS_TABS.map((t) => t.key)).toEqual(["executions", "background-tasks", "shared-state", "worksets"]);
    expect(OPERATIONS_TABS.map((t) => t.label)).toEqual(["Executions", "Background Tasks", "Shared State", "Worksets"]);
  });
});

describe("Tasks tabs", () => {
  it("are Work Items (the default), Value and Workspaces, each its own route", () => {
    expect(TASKS_TABS.map((t) => [t.key, t.to])).toEqual([
      ["work-items", "/tasks/work-items"],
      ["value", "/tasks/value"],
      ["workspaces", "/tasks/workspaces"],
    ]);
  });

  it("read the tab off the path, /tasks alone being Work Items", () => {
    expect(tasksTabOf("/tasks")).toBe("work-items");
    expect(tasksTabOf("/tasks/work-items")).toBe("work-items");
    expect(tasksTabOf("/tasks/work-items/0b1c")).toBe("work-items");
    expect(tasksTabOf("/tasks/value")).toBe("value");
    expect(tasksTabOf("/tasks/value/agents")).toBe("value");
    expect(tasksTabOf("/tasks/workspaces")).toBe("workspaces");
  });
});

// The fleet a reader picked stays selected across the tab links.
describe("tab links", () => {
  const current = new URLSearchParams("fleet=f1&agent=a1&q=x&status=open&page=3");

  it("carry the fleet to every Operations list tab", () => {
    for (const key of ["background-tasks", "shared-state", "worksets"] as const) {
      expect(operationsTabSearch(key, current)).toBe("?fleet=f1");
    }
  });

  it("carry the agent only to the tabs that filter by agent", () => {
    expect(operationsTabSearch("executions", current)).toBe("?fleet=f1&agent=a1");
    expect(tasksTabSearch("work-items", current)).toBe("?fleet=f1&agent=a1");
    expect(tasksTabSearch("workspaces", current)).toBe("?fleet=f1");
  });

  it("carry nothing to Value, whose scope and window are its own", () => {
    expect(tasksTabSearch("value", current)).toBe("");
  });

  it("never carry page, search or status", () => {
    for (const { key } of OPERATIONS_TABS) {
      const search = new URLSearchParams(operationsTabSearch(key, current));
      expect(search.has("q")).toBe(false);
      expect(search.has("status")).toBe(false);
      expect(search.has("page")).toBe(false);
    }
  });

  it("are unchanged when no fleet is selected", () => {
    for (const { key } of OPERATIONS_TABS) {
      expect(operationsTabSearch(key, new URLSearchParams("q=x&page=2"))).toBe("");
    }
    for (const { key } of TASKS_TABS) {
      expect(tasksTabSearch(key, new URLSearchParams("q=x&page=2"))).toBe("");
    }
  });
});
