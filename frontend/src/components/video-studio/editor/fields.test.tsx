import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { SelectField, SwitchField } from "@/components/video-studio/editor/fields";
import { renderWithProviders } from "@/testing/providers";

const OPTIONS = [{ value: "none" as const, label: "Full bleed" }];

describe("SelectField", () => {
  it("shows the unset label in the trigger while the value is unset", () => {
    renderWithProviders(
      <SelectField<"window" | "none"> label="Frame" value={undefined} options={OPTIONS} unsetLabel="Browser window" onChange={vi.fn()} />,
    );
    expect(screen.getByRole("combobox", { name: "Frame" })).toHaveTextContent("Browser window");
  });

  it("shows the unset label when the stored value is the default the list leaves out", () => {
    renderWithProviders(
      <SelectField<"window" | "none"> label="Frame" value="window" options={OPTIONS} unsetLabel="Browser window" onChange={vi.fn()} />,
    );
    expect(screen.getByRole("combobox", { name: "Frame" })).toHaveTextContent("Browser window");
  });

  it("shows the picked option's label", () => {
    renderWithProviders(
      <SelectField<"window" | "none"> label="Frame" value="none" options={OPTIONS} unsetLabel="Browser window" onChange={vi.fn()} />,
    );
    expect(screen.getByRole("combobox", { name: "Frame" })).toHaveTextContent("Full bleed");
  });
});

describe("SwitchField", () => {
  it("puts the help line under the label and switch row, not beside the switch", () => {
    renderWithProviders(
      <SwitchField label="Drop shadow" checked onChange={vi.fn()} hint="A soft shadow under the browser window." />,
    );
    const row = screen.getByRole("switch", { name: "Drop shadow" }).parentElement;
    expect(row).toHaveTextContent("Drop shadow");
    expect(row).not.toHaveTextContent("A soft shadow under the browser window.");
    expect(row?.parentElement).toHaveTextContent("A soft shadow under the browser window.");
  });
});
