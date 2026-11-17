// The reasoning window's gamepad: offered only when the arcade can take
// control, and launching it from the button through the waiting panel's own
// launch path, with the chat surface's wait.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { ReasoningArcadeAction } from "@/components/chat/ReasoningArcadeAction";
import { renderWithProviders } from "@/testing/providers";

const arcade = vi.hoisted(() => ({ canTakeControl: true, takeControl: vi.fn(), waitSources: [] as unknown[] }));

vi.mock("@/components/chat/waiting-arcade", () => ({
  useWaitingArcade: ({ waitSource }: { waitSource?: string | null }) => {
    arcade.waitSources.push(waitSource);
    return { reducedMotion: false, playMode: true, canTakeControl: arcade.canTakeControl, takeControl: arcade.takeControl };
  },
}));

const LABEL = "Play Space Invaders while the agent works";

describe("ReasoningArcadeAction", () => {
  it("launches the game from the button, waiting on the surface's wait", () => {
    arcade.canTakeControl = true;
    renderWithProviders(<ReasoningArcadeAction waitSource="conversation:c-1" />);

    fireEvent.click(screen.getByRole("button", { name: LABEL }));

    expect(arcade.takeControl).toHaveBeenCalledTimes(1);
    expect(arcade.takeControl.mock.calls[0][0]).toEqual({ x: expect.any(Number), y: expect.any(Number) });
    expect(arcade.waitSources).toContain("conversation:c-1");
  });

  it("offers nothing when the arcade cannot take control", () => {
    arcade.canTakeControl = false;
    renderWithProviders(<ReasoningArcadeAction waitSource="conversation:c-1" />);

    expect(screen.queryByRole("button", { name: LABEL })).toBeNull();
  });
});
