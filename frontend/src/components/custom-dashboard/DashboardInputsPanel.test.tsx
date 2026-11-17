// The studio's Inputs panel: an input created as a `set` of the input and of
// the layout placing it above the tabs, the Submit switch, and the token
// manager listing each token with its value now and writing its default.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(async () => [{ id: "aev-1", provider: "openaev", name: "OpenAEV lab" }]) }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardInputsPanel } from "./DashboardInputsPanel";

const SPEC = {
  version: 2,
  title: "Board",
  dataSources: { picked: { type: "derived", options: { extends: "base", steps: [{ op: "where", value: "$sev$" }] } } },
  visualizations: {},
  inputs: { sev: { type: "input.text", token: "sev", title: "Severity", defaultValue: "high" } },
  layout: { tabs: { items: [{ layoutId: "main", label: "Main" }] }, layoutDefinitions: { main: { structure: [] } }, globalInputs: ["sev"] },
};
const DASHBOARD = { id: "d1", revision: 2, can_manage: true, spec: SPEC, widgets: [] } as unknown as CustomDashboard;

function open(edit = vi.fn()) {
  renderWithProviders(<DashboardInputsPanel dashboard={DASHBOARD} tokens={{ sev: "low" }} edit={edit} onEditInput={vi.fn()} />);
  return edit;
}

describe("DashboardInputsPanel", () => {
  it("creates a dropdown placed above the tabs", () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = screen.getByTestId("dashboard.inputs.new");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Token" }), { target: { value: "team" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Title (heading)" }), { target: { value: "Team" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Choices, one per line (label=value)" }), {
      target: { value: "Blue team=blue\nred" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(edit.mock.calls[0][0]).toEqual([
      {
        op: "set",
        path: "inputs.input_team",
        value: {
          type: "input.dropdown",
          title: "Team",
          token: "team",
          options: { items: [{ label: "Blue team", value: "blue" }, { label: "red", value: "red" }] },
        },
      },
      { op: "set", path: "layout", value: { ...SPEC.layout, globalInputs: ["sev", "input_team"] } },
    ]);
  });

  it("creates an OpenAEV simulation input that reads every simulation while none is picked", async () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = screen.getByTestId("dashboard.inputs.new");
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Type" }));
    fireEvent.click(await screen.findByRole("option", { name: "Entity" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Token" }), { target: { value: "simulation" } });
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Searched in" }));
    fireEvent.click(await screen.findByRole("option", { name: "OpenAEV lab" }));
    // Entity types is a TagInput: the default Stix-Core-Object tag goes, simulation is typed then committed with Enter.
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove tag" }));
    const types = within(dialog).getByRole("textbox", { name: "Entity types" });
    fireEvent.change(types, { target: { value: "simulation" } });
    fireEvent.keyDown(types, { key: "Enter" });
    fireEvent.click(within(dialog).getByRole("switch", { name: "Read every entity while none is picked" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(edit).toHaveBeenCalledTimes(1));
    expect(edit.mock.calls[0][0][0]).toEqual({
      op: "set",
      path: "inputs.input_simulation",
      value: {
        type: "input.entity",
        token: "simulation",
        options: { source: "openaev", integration_id: "aev-1", entityTypes: ["simulation"], required: false },
      },
    });
  });

  it("refuses a token name it cannot hold", () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = screen.getByTestId("dashboard.inputs.new");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Token" }), { target: { value: "1 bad" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^A token name is/);
    expect(edit).not.toHaveBeenCalled();
  });

  it("turns the Submit button on", () => {
    const edit = open();
    fireEvent.click(screen.getByRole("switch", { name: "Submit button" }));
    expect(edit.mock.calls[0][0]).toEqual([
      { op: "set", path: "layout", value: { ...SPEC.layout, options: { submitButton: true } } },
    ]);
  });

  it("lists each token with its value now, and writes its default", () => {
    const edit = open();
    const row = document.querySelector('[data-token="sev"]') as HTMLElement;
    expect(row.textContent).toContain("Now: low");
    expect(row.textContent).toContain("Used by: dataSources.picked");
    expect(row.textContent).toContain("Set by: inputs.sev");
    const field = within(row).getByRole("textbox", { name: "Default of sev" });
    fireEvent.change(field, { target: { value: "medium" } });
    fireEvent.blur(field);
    // Set by an input: its own default changes.
    expect(edit.mock.calls[0][0]).toEqual([
      { op: "set", path: "inputs.sev", value: { type: "input.text", token: "sev", title: "Severity", defaultValue: "medium" } },
    ]);
  });

  it("writes a token's default in the definition when no input sets it", () => {
    const edit = open();
    const row = document.querySelector('[data-token="picked"]') as HTMLElement | null;
    expect(row).toBeNull();
    renderWithProviders(
      <DashboardInputsPanel
        dashboard={{ ...DASHBOARD, spec: { ...SPEC, visualizations: { v: { type: "xtm.text", title: "$team$", options: { body: "x" } } } } } as CustomDashboard}
        tokens={{}}
        edit={edit}
        onEditInput={vi.fn()}
      />,
    );
    const team = document.querySelectorAll('[data-token="team"]');
    const field = within(team[team.length - 1] as HTMLElement).getByRole("textbox", { name: "Default of team" });
    fireEvent.change(field, { target: { value: "blue" } });
    fireEvent.blur(field);
    expect(edit.mock.calls[0][0]).toEqual([{ op: "set", path: "defaults", value: { tokens: { default: { team: { value: "blue" } } } } }]);
  });
});
