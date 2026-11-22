/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

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
