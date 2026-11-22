import { describe, expect, it } from "vitest";

import { flowDecisionView, flowResponse } from "@/components/decisions/FlowDecisionCard";

describe("FlowDecisionCard (#842, S6)", () => {
  it("shows a free-text park as the question alone", () => {
    const view = flowDecisionView("Publish the post?", null);
    expect(view.question).toBe("Publish the post?");
    expect(view.options).toEqual([]);
    expect(view.recommendation).toBeNull();
    expect(view.deadline).toBeNull();
  });

  it("shows the node's options, recommendation and deadline", () => {
    const view = flowDecisionView("Publish the post?", {
      question: "Publish the post?",
      options: [
        { key: "A", label: "Publish" },
        { key: "B", label: "Hold" },
      ],
      recommendation: "B",
      deadline: "2026-12-01T17:00:00+00:00",
    });
    expect(view.options.map((o) => o.key)).toEqual(["A", "B"]);
    expect(view.recommendation).toBe("B");
    expect(view.deadline).toBe("2026-12-01T17:00:00+00:00");
  });

  it("sends the option key, with the note after a colon", () => {
    expect(flowResponse({ option: "B" })).toBe("B");
    expect(flowResponse({ option: "A", text: " ship it " })).toBe("A: ship it");
    expect(flowResponse({ text: " my own words " })).toBe("my own words");
    expect(flowResponse({})).toBe("");
  });
});
