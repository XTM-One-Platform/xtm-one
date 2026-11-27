/**
 * CaseOwnerTeam: a cut team name opens ONE tooltip, naming the team in full and
 * saying what a team owner means (UX r1 B1 on #2587: TruncatedText inside the Tip
 * painted two design-system tooltips on the same spot).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { CaseOwnerTeam } from "./cases-shared";

const LONG = "Global infrastructure and network security operations for the EMEA region";

async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
}

describe("CaseOwnerTeam", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("draws nothing when a person owns the case", () => {
    renderWithProviders(<CaseOwnerTeam name={null} />);
    expect(screen.queryByTestId("ctem-case-owner-team")).toBeNull();
  });

  it("a cut team name opens one tooltip naming the team in full", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(546);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(200);
    renderWithProviders(<CaseOwnerTeam name={LONG} />);
    const trigger = screen.getByTestId("ctem-case-owner-team");
    act(() => trigger.focus());
    fireEvent.focus(trigger);
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips).toHaveLength(1);
    expect(tips[0].textContent).toContain(LONG);
    expect(trigger.querySelectorAll("[tabindex]")).toHaveLength(0);
  });
});
