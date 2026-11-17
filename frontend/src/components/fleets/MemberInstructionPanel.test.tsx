// The instruction panel of a connected member (R5): the counter, the
// documentation prefill that fills an empty field and asks before replacing
// typed text (A4), and the save that hands the text (or a reset) to the caller.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Fleet, FleetMember } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { renderWithProviders as render } from "@/testing/providers";

import { MemberInstructionPanel } from "./MemberInstructionPanel";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

const fleet = { id: "f", name: "Fleet", member_defaults: { collaboration_instruction: "Ask for a summary" } } as unknown as Fleet;

function member(patch: Partial<FleetMember> = {}): FleetMember {
  return {
    id: "m",
    fleet_id: "f",
    role: "connected",
    agent_id: "a",
    name: "SOC Analyst",
    target_fleet_id: null,
    parent_member_id: null,
    is_orchestrator: false,
    is_exposed: false,
    exposed_explicitly: false,
    role_label: "",
    collaboration_instruction: "",
    context_inclusion: "summary",
    overridden: [],
    created_at: "2026-11-07T00:00:00Z",
    ...patch,
  };
}

function panel(m: FleetMember, onSave = vi.fn()) {
  render(
    <MemberInstructionPanel fleet={fleet} member={m} canEdit saving={false} enabledAutomations={2} onClose={vi.fn()} onSave={onSave} />,
  );
  return onSave;
}

const editor = () => screen.getByRole("textbox", { name: "Collaboration instruction" });
const prefill = () => screen.getByRole("button", { name: "Use the fleet documentation" });

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({
    section_keys: ["members"],
    sections: { members: "- **SOC Analyst**: triages the alert." },
    version: 1,
    updated_at: null,
    updated_by: null,
    editable: true,
    out_of_date: false,
  });
});

describe("MemberInstructionPanel", () => {
  it("shows the member's instruction, its counter and its automations", () => {
    panel(member({ collaboration_instruction: "Hand off" }));
    expect(editor()).toHaveValue("Hand off");
    expect(screen.getByTestId("fleet-member-instruction-count")).toHaveTextContent("8 / 4000 characters");
    expect(screen.getByText("2 enabled")).toBeInTheDocument();
  });

  it("fills an empty field from the fleet documentation", async () => {
    panel(member());
    await waitFor(() => expect(get).toHaveBeenCalledWith("/fleets/f/documentation"));
    // Let the documentation query settle, so the prefill reads it rather than the fleet default.
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    fireEvent.click(prefill());
    await waitFor(() => expect(editor()).toHaveValue("triages the alert."));
  });

  it("asks before replacing typed text and keeps it on Cancel", async () => {
    panel(member());
    await waitFor(() => expect(get).toHaveBeenCalled());
    fireEvent.change(editor(), { target: { value: "Typed" } });
    fireEvent.click(prefill());
    expect(await screen.findByText("Replace the instruction?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Replace the instruction?")).not.toBeInTheDocument());
    expect(editor()).toHaveValue("Typed");
  });

  it("saves the typed text, and resets an overridden instruction", () => {
    const onSave = panel(member({ collaboration_instruction: "Old", overridden: ["collaboration_instruction"] }));
    fireEvent.change(editor(), { target: { value: "New" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ id: "m" }), "New");
    fireEvent.click(screen.getByRole("button", { name: "Reset collaboration instruction to the fleet default" }));
    expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ id: "m" }), null);
  });

  it("keeps the whole reset label in its button, never cut", () => {
    // The longest translation (es, 419 px) overflowed a truncating span (UX round 2, B1): the row wraps instead.
    panel(member({ collaboration_instruction: "Old", overridden: ["collaboration_instruction"] }));
    const reset = screen.getByRole("button", { name: "Reset collaboration instruction to the fleet default" });
    expect(reset).toHaveTextContent(/^Reset collaboration instruction to the fleet default$/);
    expect(reset.parentElement).toContainElement(screen.getByTestId("fleet-member-instruction-count"));
  });
});
