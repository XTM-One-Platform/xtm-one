// A run's version chips read the same on the evaluation, run and compare pages.

import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { RunVersionChips } from "./run-version-chips";

async function tooltipOf(text: string) {
  fireEvent.focus(screen.getByText(text));
  return (await screen.findByRole("tooltip")).textContent;
}

describe("RunVersionChips", () => {
  it("names the persona version a run used", async () => {
    renderWithProviders(<RunVersionChips run={{ persona_version_number: 4 }} />);
    expect(await tooltipOf("v4")).toContain("The version of the agent's persona this run used.");
  });

  it("names each agent setting, its platform default, and its prompts", async () => {
    renderWithProviders(
      <RunVersionChips
        run={{ agent_config_versions: { temperature: 2, guardrails: null }, prompt_versions: { triage: 3, summary: 1 } }}
      />,
    );
    expect(await tooltipOf("v2")).toContain("Temperature: version 2 of this agent setting.");
    fireEvent.blur(screen.getByText("v2"));
    expect(await tooltipOf("default")).toContain("Guardrails: the platform default, with no saved version.");
    fireEvent.blur(screen.getByText("default"));
    expect(await tooltipOf("2")).toContain("The prompt versions this run used: triage v3, summary v1.");
  });

  it("shows a setting it has no name for as recorded, an inherited property name included", async () => {
    renderWithProviders(<RunVersionChips run={{ agent_config_versions: { constructor: 1 } }} />);
    expect(await tooltipOf("v1")).toContain("constructor: version 1 of this agent setting.");
  });

  it("is reached with Tab, as a keyboard user reaches it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RunVersionChips run={{ persona_version_number: 4 }} />);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByText("v4").closest('[tabindex="0"]'));
    expect((await screen.findByRole("tooltip")).textContent).toContain("persona this run used");
  });

  it("renders the fallback when the run recorded no version", () => {
    renderWithProviders(
      <RunVersionChips run={{ persona_version_number: null, agent_config_versions: {}, prompt_versions: null }} fallback="—" />,
    );
    expect(screen.getByText("—")).toBeTruthy();
  });
});
