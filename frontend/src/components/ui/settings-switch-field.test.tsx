// SettingsSwitchField (#9173, S1c): a setting that is a switch carries the same
// label, "About this field" bubble and helper as SettingsField, and the switch is
// named by that label and described by that helper.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { SettingsSwitchField } from "./settings-form";

describe("SettingsSwitchField", () => {
  it("names the switch by its label, describes it by its helper and offers the info bubble", () => {
    const onCheckedChange = vi.fn();
    renderWithProviders(
      <SettingsSwitchField
        id="wait-unbounded"
        label="Waits on named work are not counted"
        helper="Only blind waits are charged."
        info="On: named waits are free. Off: every wait is charged."
        checked={false}
        onCheckedChange={onCheckedChange}
      >
        <p>governed field</p>
      </SettingsSwitchField>,
    );
    const toggle = screen.getByRole("switch", { name: "Waits on named work are not counted" });
    expect(toggle).toHaveAccessibleDescription("Only blind waits are charged.");
    expect(screen.getByRole("img", { name: "About this field" })).toBeTruthy();
    expect(screen.getByText("governed field")).toBeTruthy();
    fireEvent.click(toggle);
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });

  it("does not toggle when disabled", () => {
    const onCheckedChange = vi.fn();
    renderWithProviders(
      <SettingsSwitchField id="dry-run" label="Dry run" checked onCheckedChange={onCheckedChange} disabled />,
    );
    fireEvent.click(screen.getByRole("switch", { name: "Dry run" }));
    expect(onCheckedChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("img", { name: "About this field" })).toBeNull();
  });

  it("dims its label while the switch is disabled", () => {
    const { rerender } = renderWithProviders(
      <SettingsSwitchField id="dim" label="Dim me" checked={false} onCheckedChange={() => {}} disabled />,
    );
    // data-disabled marks the dimmed label (frontend/AGENTS.md: no class-name assertions).
    expect(screen.getByText("Dim me").hasAttribute("data-disabled")).toBe(true);
    rerender(<SettingsSwitchField id="dim" label="Dim me" checked={false} onCheckedChange={() => {}} />);
    expect(screen.getByText("Dim me").hasAttribute("data-disabled")).toBe(false);
  });
});
