// Settings > Fleets > Member presets (R4 of #9162): built-ins read only,
// company presets managed by administrators.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { FleetMemberPresetsSection as Section } from "./FleetMemberPresetsSection";

/** The section's table keeps its search and Type filter in the URL, so it renders inside a router. */
function FleetMemberPresetsSection({ url = "/" }: { url?: string }) {
  return (
    <MemoryRouter initialEntries={[url]}>
      <Section />
    </MemoryRouter>
  );
}

let admin = true;
const createMutate = vi.fn();
const presets = [
  { id: "builtin:own-summary", name: "Own organization, a summary", builtin: true, values: { context_inclusion: "summary", organization_scope: { mode: "primary" } } },
  { id: "p1", name: "Regional SOC", builtin: false, values: { context_inclusion: "full" } },
  {
    id: "p2",
    name: "Weekend desk",
    builtin: false,
    values: { context_inclusion: "full", organization_scope: { mode: "primary" }, collaboration_instruction: "Cite every source" },
  },
];

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: admin }) }));
vi.mock("@/hooks/api/useOrganizationsApi", () => ({ useOrganizations: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetMemberPresets: () => ({ data: presets, isLoading: false }),
  useCreateFleetMemberPreset: () => ({ mutate: createMutate, isPending: false }),
  useUpdateFleetMemberPreset: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteFleetMemberPreset: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("FleetMemberPresetsSection", () => {
  beforeEach(() => {
    admin = true;
    createMutate.mockReset();
  });

  it("lists the built-ins read only and lets an administrator edit a company preset", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    expect(screen.getAllByTestId("settings-fleet-member-preset")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Edit Regional SOC" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete Regional SOC" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Edit Own organization/ })).toBeNull();
  });

  it("shows no action to a reader who is not an administrator", () => {
    admin = false;
    renderWithProviders(<FleetMemberPresetsSection />);
    expect(screen.queryByRole("button", { name: "New preset" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Regional SOC" })).toBeNull();
  });

  it("refuses to save a new preset without a name or a setting, then saves the settings turned on", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    fireEvent.click(screen.getByRole("button", { name: "New preset" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Give the preset a name")).toBeTruthy();
    expect(createMutate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Night shift" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Choose at least one setting the preset writes", { selector: "[role=alert]" })).toBeTruthy();

    fireEvent.click(screen.getByTestId("preset-set-context_inclusion"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(createMutate).toHaveBeenCalledWith({ name: "Night shift", values: { context_inclusion: "summary" } }, expect.anything());
  });

  it("lists the presets on the shared data table, searched by name", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    expect(screen.getByRole("table")).toBeTruthy();
    // The shared table applies a typed search after a 250 ms pause.
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the presets by name" }), { target: { value: "regional" } });
    act(() => vi.advanceTimersByTime(300));
    vi.useRealTimers();
    expect(screen.getAllByTestId("settings-fleet-member-preset")).toHaveLength(1);
    expect(screen.getByText("Regional SOC")).toBeTruthy();
  });

  it("gives the name a help line with its length and keeps the settings inside the form", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    fireEvent.click(screen.getByRole("button", { name: "New preset" }));
    expect(screen.getByText("The name fleets see in Apply a preset; unique, 120 characters at most (0/120)")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "x".repeat(130) } });
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toHaveLength(120);
    const fieldset = screen.getByRole("group", { name: "Settings the preset writes" });
    expect(fieldset.closest("form")).toBeTruthy();
  });

  it("gives every setting of the editor its own info bubble (UX round 3, B1)", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    fireEvent.click(screen.getByRole("button", { name: "New preset" }));
    for (const label of ["Conversation shared", "Organizations", "Collaboration instruction"]) {
      // The shared InfoTip is a focusable icon named for assistive tech (role img).
      expect(screen.getAllByRole("img", { name: `About ${label}` })).toHaveLength(1);
    }
  });

  it("filters the presets by type with the table's filter pill, then clears it", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    const pill = screen.getAllByRole("button", { name: /Type/ }).find((button) => button.hasAttribute("aria-expanded"));
    fireEvent.click(pill as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "Company" }));
    expect(screen.getAllByTestId("settings-fleet-member-preset")).toHaveLength(2);
    expect(screen.queryByTestId("settings-fleet-member-preset-builtin")).toBeNull();
    expect(screen.getByText("Regional SOC")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters" })[0]);
    expect(screen.getAllByTestId("settings-fleet-member-preset")).toHaveLength(3);
  });

  it("shows two settings per row at most, the rest named behind +N (UX round 5 of #10980, B2)", () => {
    renderWithProviders(<FleetMemberPresetsSection />);
    const more = screen.getByTestId("apply-preset-values-more");
    expect(more.textContent).toBe("+1");
    expect(more.getAttribute("aria-label")).toBe("Collaboration instruction: Cite every source");
    expect(screen.getAllByTestId("apply-preset-values-more")).toHaveLength(1);
  });

  it("opens on the built-ins when the URL names that type", () => {
    renderWithProviders(<FleetMemberPresetsSection url="/?presets_f_type=builtin" />);
    expect(screen.getAllByTestId("settings-fleet-member-preset")).toHaveLength(1);
    expect(screen.queryByText("Regional SOC")).toBeNull();
    // The Built-in badge sits in its own non-shrinking wrapper next to the name (UX round 2, B2).
    expect(screen.getByTestId("settings-fleet-member-preset-builtin")).toBeTruthy();
  });
});
