import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";

import { SettingsCard } from "./settings-form";

/**
 * A settings card sized to its content (#1219 B2): an action-only card has no
 * body, so it draws no CardContent padding under its header.
 */
describe("SettingsCard", () => {
  it("draws no body for an action-only card", () => {
    const { container } = renderWithProviders(<SettingsCard title="Onboarding" description="One line." action={<button type="button">Act</button>} />);
    const card = container.firstElementChild as HTMLElement;
    expect(card.children).toHaveLength(1);
  });

  it("keeps its body when it has children", () => {
    const { container, getByText } = renderWithProviders(
      <SettingsCard title="Language">
        <span>Body</span>
      </SettingsCard>,
    );
    const card = container.firstElementChild as HTMLElement;
    expect(card.children).toHaveLength(2);
    expect(getByText("Body")).toBeTruthy();
  });
});
