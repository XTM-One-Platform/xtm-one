// The fleet defaults bar re-reads the stored defaults when they change outside
// its own form (a preset applied on the defaults, the Undo of a change): the
// controls show the server values and no false dirty footer offers to write the
// old values back (UX round 1 of #9422, B1). Elements are found by role and text.
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Fleet } from "@/hooks/api/useFleetApi";
import { renderWithProviders as render } from "@/testing/providers";

import { FleetDefaultsBar } from "./FleetMembersView";

type FleetMemberDefaults = NonNullable<Fleet["member_defaults"]>;

function fleet(member_defaults: FleetMemberDefaults): Fleet {
  return { id: "f", name: "Fleet", member_defaults } as unknown as Fleet;
}

function bar(defaults: FleetMemberDefaults) {
  return (
    <FleetDefaultsBar
      fleet={fleet(defaults)}
      members={[]}
      organizations={[]}
      canEdit
      saving={false}
      onSave={vi.fn()}
      onApplyPreset={vi.fn()}
    />
  );
}

const instruction = () => screen.getByRole("textbox", { name: "Collaboration instruction" });
const context = () => screen.getByRole("combobox", { name: "Fleet default: Conversation shared" });
const saveDefaults = () => screen.queryByRole("button", { name: "Save defaults" });

describe("FleetDefaultsBar", () => {
  it("shows the server values after a preset is applied on the defaults, with no dirty footer", () => {
    const { rerender } = render(bar({ context_inclusion: "none", collaboration_instruction: "" }));
    expect(instruction()).toHaveValue("");
    expect(context()).toHaveTextContent("Nothing");

    rerender(bar({ context_inclusion: "summary", collaboration_instruction: "Hand off to the SOC" }));

    expect(instruction()).toHaveValue("Hand off to the SOC");
    expect(context()).toHaveTextContent("A summary");
    expect(saveDefaults()).not.toBeInTheDocument();
  });

  it("drops an unsaved edit and shows the restored values after an Undo", () => {
    const { rerender } = render(bar({ context_inclusion: "summary", collaboration_instruction: "Hand off to the SOC" }));
    fireEvent.change(instruction(), { target: { value: "Draft" } });
    expect(saveDefaults()).toBeInTheDocument();

    rerender(bar({ context_inclusion: "none", collaboration_instruction: "" }));

    expect(instruction()).toHaveValue("");
    expect(context()).toHaveTextContent("Nothing");
    expect(saveDefaults()).not.toBeInTheDocument();
  });

  it("keeps an unsaved edit while the stored defaults do not change", () => {
    const defaults: FleetMemberDefaults = { context_inclusion: "none", collaboration_instruction: "" };
    const { rerender } = render(bar(defaults));
    fireEvent.change(instruction(), { target: { value: "Draft" } });

    rerender(bar({ ...defaults }));

    expect(instruction()).toHaveValue("Draft");
    expect(saveDefaults()).toBeInTheDocument();
  });
});
