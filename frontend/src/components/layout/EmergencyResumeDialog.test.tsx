import { describe, it, expect } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { EmergencyResumeDialog } from "./EmergencyResumeDialog";

/**
 * The resume dialog lifts an emergency stop: it says so in the banner's words,
 * for whom, and does not promise AI is switched on — a platform resume puts
 * the AI switch back to what it was before the stop, which may be off.
 */

function description(): string {
  const dialog = screen.getByTestId("emergency-resume-dialog");
  const id = dialog.getAttribute("aria-describedby");
  expect(id).not.toBeNull();
  return document.getElementById(id as string)?.textContent ?? "";
}

describe("EmergencyResumeDialog", () => {
  it("lifts the stop for the whole platform and restores AI to its pre-stop setting", () => {
    renderWithProviders(<EmergencyResumeDialog scope="platform" onClose={() => {}} />);
    expect(description()).toBe(
      "Lifts the emergency stop for the whole platform: re-enables every assignment it paused and sets AI back to what it was before the stop. Assignments paused by hand are left alone.",
    );
  });

  it("lifts the viewer's own stop", () => {
    renderWithProviders(<EmergencyResumeDialog scope="self" onClose={() => {}} />);
    expect(description()).toBe(
      "Lifts your emergency stop: re-enables the assignments it paused for you. Assignments paused by hand are left alone.",
    );
  });
});
