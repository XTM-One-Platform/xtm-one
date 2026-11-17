// The Modify with AI panel's cards (#10162 R8/R9, UX round 1 on #10181 B4/B5):
// the confirm card names its one action (title, "Pause all", Cancel first) and
// the raise refusal brings the page to its Approval tab before focusing the strip.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FleetChatCard } from "@/components/fleets/FleetChatCard";
import { FLEET_AUTONOMY_STRIP_ID } from "@/components/fleets/FleetAutonomyStrip";
import { renderWithProviders } from "@/testing/providers";

describe("FleetChatCard", () => {
  it("titles the confirm card and names its one action, Cancel first", async () => {
    const send = vi.fn();
    renderWithProviders(
      <FleetChatCard card={{ kind: "confirm", messageId: "m1", count: 2 }} send={send} canSend onShowApprovalTab={vi.fn()} />,
    );
    const card = screen.getByRole("alert", { name: "Pause all automations of this fleet?" });
    const buttons = Array.from(card.querySelectorAll("button")).map((b) => b.textContent);
    expect(buttons).toEqual(["Cancel", "Pause all"]);
    expect(card.textContent).not.toMatch(/Apply \d+ change/);
    await userEvent.click(screen.getByTestId("fleet.studio.confirm-apply"));
    expect(send).toHaveBeenCalledWith("Confirmed: apply it.");
  });

  it("opens the Approval tab, then focuses the autonomy strip once it is there", async () => {
    const strip = document.createElement("section");
    strip.id = FLEET_AUTONOMY_STRIP_ID;
    strip.tabIndex = -1;
    strip.scrollIntoView = vi.fn();
    const onShowApprovalTab = vi.fn(() => document.body.appendChild(strip));
    renderWithProviders(
      <FleetChatCard card={{ kind: "raise_refused", messageId: "m1" }} send={vi.fn()} canSend onShowApprovalTab={onShowApprovalTab} />,
    );
    await userEvent.click(screen.getByTestId("fleet.studio.open-autonomy"));
    expect(onShowApprovalTab).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(document.activeElement).toBe(strip));
    // The ring is drawn from data-landed (a script focus after a click never matches :focus-visible), until blur.
    expect(strip.dataset.landed).toBe("true");
    strip.blur();
    expect(strip.dataset.landed).toBeUndefined();
    strip.remove();
  });
});
