// The agent page tabs: what an agent will do next is the Overview's
// "Coming up" card, so the page keeps at most the same eleven tabs. The ROI
// and the run budget share the Value tab (#11037).

import { describe, expect, it } from "vitest";

import { agentDetailTabs, legacyAgentTabTarget } from "./tabs";

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

describe("old agent page links (#11037)", () => {
  const base = "/agents/a-1";

  it("open the Value tab from the former Budget tab, query kept", () => {
    expect(legacyAgentTabTarget(base, "budget", "", "")).toBe("/agents/a-1/value");
    expect(legacyAgentTabTarget(base, "budget", "?window=7d", "")).toBe("/agents/a-1/value?window=7d");
  });

  it("open the Value tab from an Overview link to the ROI section", () => {
    expect(legacyAgentTabTarget(base, undefined, "", "#roi")).toBe("/agents/a-1/value");
    expect(legacyAgentTabTarget(base, undefined, "", "#agent-roi-title")).toBe("/agents/a-1/value");
    expect(legacyAgentTabTarget(base, "overview", "", "#agent-roi-section")).toBe("/agents/a-1/value");
    expect(legacyAgentTabTarget(base, undefined, "?window=90d", "")).toBe("/agents/a-1/value?window=90d");
    expect(legacyAgentTabTarget(base, undefined, "?chat_q=report", "")).toBe("/agents/a-1/value?chat_q=report");
  });

  it("leave every current link where it is", () => {
    expect(legacyAgentTabTarget(base, undefined, "", "")).toBeNull();
    expect(legacyAgentTabTarget(base, "value", "?window=7d", "")).toBeNull();
    expect(legacyAgentTabTarget(base, "config", "?pick=unit", "")).toBeNull();
    expect(legacyAgentTabTarget(base, "activity", "?window=7d", "#roi")).toBeNull();
    expect(legacyAgentTabTarget(base, undefined, "?other=1", "#coming-up")).toBeNull();
  });
});
