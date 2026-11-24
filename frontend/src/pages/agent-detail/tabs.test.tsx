// The agent page tabs: what an agent will do next is the Overview's
// "Coming up" card, so the page keeps at most the same eleven tabs. The ROI
// and the run budget share the Value tab (#11037).

import { describe, expect, it } from "vitest";

import { agentDetailTabs, isAgentTab } from "./tabs";

describe("agent page tabs", () => {
  it("are at most the eleven tabs, in order, with no Scheduled tab", () => {
    const tabs = agentDetailTabs({
      canManageMemory: true,
      canViewChats: true,
      canViewFeedback: true,
      evaluation: true,
      isAdmin: true,
    });
    expect(tabs.map((t) => t.key)).toEqual([
      "overview",
      "config",
      "value",
      "versioning",
      "memory",
      "observability",
      "feedback",
      "chats",
      "delegations",
      "evaluation",
      "activity",
    ]);
    expect(tabs.map((t) => t.label)).not.toContain("Scheduled");
  });

  it("name the third tab Value, and no tab Budget (#11037)", () => {
    const tabs = agentDetailTabs({
      canManageMemory: false,
      canViewChats: false,
      canViewFeedback: false,
      evaluation: false,
      isAdmin: false,
    });
    expect(tabs[2]).toMatchObject({ key: "value", label: "Value" });
    expect(tabs.map((t) => t.label)).not.toContain("Budget");
  });

  it("show a viewer with no extra rights the six tabs everyone has", () => {
    const tabs = agentDetailTabs({
      canManageMemory: false,
      canViewChats: false,
      canViewFeedback: false,
      evaluation: false,
      isAdmin: false,
    });
    expect(tabs.map((t) => t.key)).toEqual([
      "overview",
      "config",
      "value",
      "versioning",
      "delegations",
      "activity",
    ]);
  });

  it("answer not found for an unknown tab segment, the former budget included (#797)", () => {
    expect(isAgentTab(undefined)).toBe(true);
    expect(isAgentTab("value")).toBe(true);
    expect(isAgentTab("evaluation")).toBe(true);
    expect(isAgentTab("budget")).toBe(false);
    expect(isAgentTab("garbage")).toBe(false);
    expect(isAgentTab("constructor")).toBe(false);
  });

  it("show an administrator no Evaluation tab while evaluation is off", () => {
    const tabs = agentDetailTabs({
      canManageMemory: true,
      canViewChats: true,
      canViewFeedback: true,
      evaluation: false,
      isAdmin: true,
    });
    expect(tabs.map((t) => t.key)).not.toContain("evaluation");
  });
});
