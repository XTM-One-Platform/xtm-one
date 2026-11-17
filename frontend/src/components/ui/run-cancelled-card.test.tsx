import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { RunCancelledCard } from "@/components/ui/run-cancelled-card";
import { renderWithProviders } from "@/testing/providers";

// The run page of a bulk-cancelled run reads
// neutral and in the app language, never a red "Error" card in English.
describe("RunCancelledCard", () => {
  it("reads a bulk cancel neutral and in French", () => {
    renderWithProviders(<RunCancelledCard run={{ status: "cancelled", error: "Cancelled by Admin" }} />, { locale: "fr" });
    const card = screen.getByTestId("run-cancelled-card");
    expect(card.getAttribute("data-tone")).toBe("neutral");
    expect(screen.getByText("Annulé")).toBeTruthy();
    expect(screen.getByText("Annulée par Admin")).toBeTruthy();
    expect(screen.queryByText("Erreur")).toBeNull();
    expect(screen.queryByText("Cancelled by Admin")).toBeNull();
  });

  it("shows another cancel reason as written", () => {
    renderWithProviders(<RunCancelledCard run={{ status: "cancelled", error: "Stopped by the emergency stop" }} />);
    expect(screen.getByText("Cancelled")).toBeTruthy();
    expect(screen.getByText("Stopped by the emergency stop")).toBeTruthy();
  });
});
