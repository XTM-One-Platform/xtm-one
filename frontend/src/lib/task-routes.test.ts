// SPDX-License-Identifier: Apache-2.0
// Every link to a work item, the Value report or a workspace is built here (#775):
// the URIs mirror the menu, Tasks > Work Items / Value / Workspaces.
import { describe, expect, it } from "vitest";

import {
  VALUE_METHODOLOGY_PATH, WORKSPACES_PATH, operationsPath, valuePath, workItemPath, workItemsPath, workspacePath,
} from "./task-routes";

describe("task routes", () => {
  it("open one work item as a path segment of the Work Items tab", () => {
    expect(workItemPath("0b1c2d3e")).toBe("/tasks/work-items/0b1c2d3e");
    expect(workItemPath("0b1c2d3e", { view: "waiting" })).toBe("/tasks/work-items/0b1c2d3e?view=waiting");
  });

  it("list the work items with their filters", () => {
    expect(workItemsPath()).toBe("/tasks/work-items");
    expect(workItemsPath({ view: "waiting" })).toBe("/tasks/work-items?view=waiting");
    expect(workItemsPath("?vis=mine")).toBe("/tasks/work-items?vis=mine");
  });

  it("put Value, its tables and its methodology under Tasks", () => {
    expect(valuePath()).toBe("/tasks/value");
    expect(valuePath("fleets")).toBe("/tasks/value");
    expect(valuePath("agents", { scope: "organization" })).toBe("/tasks/value/agents?scope=organization");
    expect(VALUE_METHODOLOGY_PATH).toBe("/tasks/value/methodology");
  });

  it("put the workspaces under Tasks", () => {
    expect(WORKSPACES_PATH).toBe("/tasks/workspaces");
    expect(workspacePath("w-1")).toBe("/tasks/workspaces/w-1");
    expect(workspacePath("w-1", "team")).toBe("/tasks/workspaces/w-1/team");
  });

  it("name the Operations tabs as their labels", () => {
    expect(operationsPath("shared-state")).toBe("/operations/shared-state");
    expect(operationsPath("executions", { agent: "a" })).toBe("/operations/executions?agent=a");
  });
});
