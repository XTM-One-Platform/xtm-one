// The entries the platform could not run are left out of every headline: a run
// listed or compared beside others says how many.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { PlatformErrorBadge } from "./platform-error-badge";

describe("PlatformErrorBadge", () => {
  it("says how many entries the platform left out", () => {
    renderWithProviders(<PlatformErrorBadge run={{ platform_error_count: 3 }} />);
    expect(screen.getByText("3 left out")).toBeTruthy();
  });

  it.each([0, undefined, null])("renders nothing for %s", (count) => {
    const { container } = renderWithProviders(<PlatformErrorBadge run={{ platform_error_count: count }} />);
    expect(container.textContent).toBe("");
  });
});
