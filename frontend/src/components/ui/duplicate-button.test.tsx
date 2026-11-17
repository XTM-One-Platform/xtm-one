// The fleet and skill headers' Duplicate (#9174, UX round 1 B1 on #9209): with AI off the
// button is disabled, and the reason stays reachable by keyboard and pointer
// through the Tip wrapper, since a disabled button takes neither.

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { DuplicateButton } from "./duplicate-button";

const REASON = "AI is disabled on this platform. An administrator can enable it in Settings → Platform.";
const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", () => ({
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? REASON : null),
}));

const nextFrame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

describe("DuplicateButton", () => {
  it("opens the fork dialog with AI on, with no wrapper around the button", () => {
    ai.off = false;
    const onDuplicate = vi.fn();
    renderWithProviders(<DuplicateButton onDuplicate={onDuplicate} hint="Create an editable copy" />);
    const button = screen.getByRole("button", { name: "Duplicate" });
    expect(button).toBeEnabled();
    expect(button.parentElement?.getAttribute("tabindex")).toBeNull();
    fireEvent.click(button);
    expect(onDuplicate).toHaveBeenCalledTimes(1);
  });

  it("with AI off, is disabled and its reason is reachable from the keyboard", async () => {
    ai.off = true;
    const onDuplicate = vi.fn();
    renderWithProviders(<DuplicateButton onDuplicate={onDuplicate} hint="Create an editable copy" />);
    const button = screen.getByRole("button", { name: "Duplicate" });
    expect(button).toBeDisabled();

    const wrapper = button.parentElement!;
    expect(wrapper.getAttribute("tabindex")).toBe("0");
    expect(wrapper.getAttribute("aria-disabled")).toBe("true");

    act(() => wrapper.focus());
    await nextFrame();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(REASON);

    fireEvent.click(button);
    expect(onDuplicate).not.toHaveBeenCalled();
  });
});
