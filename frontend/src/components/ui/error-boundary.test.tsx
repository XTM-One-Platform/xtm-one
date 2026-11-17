import { render, screen } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "@/components/ui/error-boundary";

function Boom(): never {
  throw new Error("render failed");
}

describe("ErrorBoundary fallback strings", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the English ids when no translation provider is mounted", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Failed to load this section")).toBeTruthy();
    expect(screen.getByText("An unexpected error occurred while loading this page.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("translates the fallback through the active locale", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <IntlProvider
        locale="fr-fr"
        messages={{
          "Failed to load this section": "Impossible de charger cette section",
          "Try again": "Réessayer",
        }}
      >
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>
      </IntlProvider>,
    );
    expect(screen.getByText("Impossible de charger cette section")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Réessayer" })).toBeTruthy();
  });
});
