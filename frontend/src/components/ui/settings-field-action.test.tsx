// SettingsField `action` (#11585): a small action (a "Reset to default" link)
// sits at the end of the label row without changing how the label names the
// field or how the helper describes it, so a field with a reset keeps the DS
// Label at body size, the info bubble and the helper of every other field.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { Slider } from "@filigran/design-system";

import { renderWithProviders } from "@/testing/providers";

import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "./settings-form";

describe("SettingsField action", () => {
  it("keeps the label, the helper and the info bubble on the field and renders the action in the label row", () => {
    const onReset = vi.fn();
    renderWithProviders(
      <SettingsField
        id="max-iterations"
        label="Max Iterations"
        info="Rounds per message."
        helper="Platform default is 25."
        action={
          <button type="button" onClick={onReset}>
            Reset to default
          </button>
        }
      >
        <input id="max-iterations" type="number" aria-describedby={settingsFieldHintId("max-iterations")} />
      </SettingsField>,
    );
    const field = screen.getByRole("spinbutton", { name: "Max Iterations" });
    expect(field).toHaveAccessibleDescription("Platform default is 25.");
    expect(screen.getByRole("img", { name: "About this field" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("names a slider thumb by the field label", () => {
    renderWithProviders(
      <SettingsField id="temperature" label="Temperature" helper="Controls LLM randomness.">
        <Slider
          min={0}
          max={2}
          step={0.1}
          value={[0.7]}
          aria-labelledby={settingsFieldLabelId("temperature")}
          aria-describedby={settingsFieldHintId("temperature")}
        />
      </SettingsField>,
    );
    expect(screen.getByRole("slider", { name: "Temperature" })).toBeTruthy();
  });

  it("renders no action row when no action is given", () => {
    renderWithProviders(
      <SettingsField id="name" label="Name">
        <input id="name" />
      </SettingsField>,
    );
    expect(screen.queryByRole("button", { name: "Reset to default" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "Name" })).toBeTruthy();
  });
});
