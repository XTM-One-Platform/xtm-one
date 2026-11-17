import { describe, expect, it } from "vitest";

import { readWorkspaceOpsList, resolveWorkspaceTab, workspaceTabs } from "./workspace-tabs";

describe("workspace tabs", () => {
  it("offers Team only when the workspace hosts a fleet", () => {
    expect(workspaceTabs(true).map((t) => t.key)).toEqual(["overview", "team", "operations", "conversations"]);
    expect(workspaceTabs(false).map((t) => t.key)).toEqual(["overview", "operations", "conversations"]);
  });

  it("opens the Overview for a tab the workspace does not have", () => {
    expect(resolveWorkspaceTab("team", false)).toBe("overview");
    expect(resolveWorkspaceTab("nope", true)).toBe("overview");
    expect(resolveWorkspaceTab("team", true)).toBe("team");
    expect(resolveWorkspaceTab("operations", false)).toBe("operations");
  });

  it("reads the Operations list from ?list=, work items by default", () => {
    expect(readWorkspaceOpsList(null)).toBe("work-items");
    expect(readWorkspaceOpsList("state")).toBe("state");
    expect(readWorkspaceOpsList("worksets")).toBe("worksets");
    expect(readWorkspaceOpsList("runs")).toBe("work-items");
  });
});
