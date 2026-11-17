// The company preset editor of Settings > Fleets (R4 of #9162): the draft,
// the values it saves and why it cannot be saved.
import { describe, expect, it } from "vitest";

import { presetDraft, presetDraftError, presetDraftValues } from "./members-view";

const company = [
  { id: "builtin:own-summary", name: "Own organization, a summary" },
  { id: "p1", name: "Regional SOC" },
];

describe("presetDraft", () => {
  it("starts a new preset with no setting on", () => {
    const draft = presetDraft();
    expect(draft.name).toBe("");
    expect(draft.sets).toEqual({ context_inclusion: false, collaboration_instruction: false, organization_scope: false });
    expect(presetDraftValues(draft)).toEqual({});
  });

  it("reads an existing preset back with the settings it writes on", () => {
    const draft = presetDraft({ name: "Regional SOC", values: { context_inclusion: "full", organization_scope: { mode: "monitored" } } });
    expect(draft.sets).toEqual({ context_inclusion: true, collaboration_instruction: false, organization_scope: true });
    expect(presetDraftValues(draft)).toEqual({ context_inclusion: "full", organization_scope: { mode: "monitored" } });
  });

  it("saves only the settings turned on, an empty instruction included", () => {
    const draft = { ...presetDraft(), sets: { context_inclusion: false, collaboration_instruction: true, organization_scope: false } };
    expect(presetDraftValues(draft)).toEqual({ collaboration_instruction: "" });
  });
});

describe("presetDraftError", () => {
  const on = { context_inclusion: true, collaboration_instruction: false, organization_scope: false };

  it("needs a name", () => {
    expect(presetDraftError({ ...presetDraft(), name: "  ", sets: on }, company)?.field).toBe("name");
  });

  it("refuses a name taken by another preset or a built-in, without regard to case", () => {
    expect(presetDraftError({ ...presetDraft(), name: "regional soc", sets: on }, company)?.message).toBe("A preset with this name exists");
    expect(presetDraftError({ ...presetDraft(), name: "OWN ORGANIZATION, A SUMMARY", sets: on }, company)?.field).toBe("name");
  });

  it("keeps the edited preset's own name", () => {
    expect(presetDraftError({ ...presetDraft(), name: "Regional SOC", sets: on }, company, "p1")).toBeNull();
  });

  it("needs at least one setting", () => {
    expect(presetDraftError({ ...presetDraft(), name: "New" }, company)?.field).toBe("settings");
  });
});
