// The instruction editor's prefill source and its no-overwrite rule (dev-docs/ctem-fleets.md 10.5, A4).
import { describe, expect, it } from "vitest";

import type { FleetDocumentation } from "@/hooks/api/useFleetApi";

import { INSTRUCTION_MAX, documentationLines, fillDecision, instructionPrefill } from "./instruction-editor";

function doc(members?: string): FleetDocumentation {
  return {
    section_keys: ["purpose", "members"],
    sections: members === undefined ? {} : { members },
    version: 1,
    updated_at: null,
    updated_by: null,
    editable: true,
    out_of_date: false,
  };
}

describe("documentationLines", () => {
  it("takes the member's bullet without the markdown and the name", () => {
    const text = doc("- **Desk Lead**: routes each alert to the analyst.\n- **SOC Analyst**: triages the alert.");
    expect(documentationLines(text, "SOC Analyst")).toBe("triages the alert.");
  });

  it("matches the name whatever its case and keeps every line naming it", () => {
    const text = doc("* soc analyst - triages.\n1. The Desk Lead hands off to SOC Analyst when needed.");
    expect(documentationLines(text, "SOC Analyst")).toBe("triages.\nThe Desk Lead hands off to SOC Analyst when needed.");
  });

  it("answers null without a members section or a line naming the member", () => {
    expect(documentationLines(undefined, "SOC Analyst")).toBeNull();
    expect(documentationLines(doc(), "SOC Analyst")).toBeNull();
    expect(documentationLines(doc("- **Desk Lead**: routes."), "SOC Analyst")).toBeNull();
  });

  it("keeps the text within the instruction limit", () => {
    const long = `- **Analyst**: ${"x".repeat(INSTRUCTION_MAX + 50)}`;
    expect(documentationLines(doc(long), "Analyst")).toHaveLength(INSTRUCTION_MAX);
  });
});

describe("instructionPrefill", () => {
  it("prefers the documentation, then the fleet default, then nothing", () => {
    expect(instructionPrefill(doc("- **Analyst**: triages."), "Analyst", "Default")).toBe("triages.");
    expect(instructionPrefill(doc(), "Analyst", "  Ask for a summary  ")).toBe("Ask for a summary");
    expect(instructionPrefill(undefined, "Analyst", "")).toBeNull();
    expect(instructionPrefill(undefined, "Analyst", null)).toBeNull();
  });
});

describe("fillDecision", () => {
  it("fills an empty field or one holding the same text", () => {
    expect(fillDecision("", "Draft")).toBe("fill");
    expect(fillDecision("   ", "Draft")).toBe("fill");
    expect(fillDecision("Draft ", "Draft")).toBe("fill");
  });

  it("asks before replacing typed text", () => {
    expect(fillDecision("Typed", "Draft")).toBe("ask");
  });
});
