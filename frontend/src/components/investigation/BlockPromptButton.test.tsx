/**
 * A block prompt button explains a wait on the conversation's tools -
 * `yarn test:dom`.
 */
import { describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { Database } from "lucide-react";

import { ChatActionsContext, type ChatActions } from "@/components/chat/chat-actions";
import { renderWithProviders } from "@/testing/providers";
import { BlockPromptButton } from "./BlockPromptButton";

const nextFrame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

function renderButton(actions: Partial<ChatActions> | null, requested = false) {
  const value: ChatActions | null = actions && { canSend: false, sendPrompt: vi.fn(), ...actions };
  renderWithProviders(
    <ChatActionsContext.Provider value={value}>
      <BlockPromptButton
        tip="The agent records the findings in a new OpenCTI draft"
        label="Send to OpenCTI (draft)"
        requestedLabel="Sent to OpenCTI"
        icon={<Database className="h-3.5 w-3.5" />}
        requested={requested}
        action="opencti-draft"
        onClick={() => {}}
      />
    </ChatActionsContext.Provider>,
  );
}

const button = () => screen.getByRole("button", { name: /OpenCTI/ });

describe("a block prompt button", () => {
  it("is busy and says the tools are loading while they resolve", async () => {
    renderButton({ canSend: false, toolsResolving: true });
    expect(button()).toBeDisabled();
    expect(button()).toHaveAttribute("aria-busy", "true");

    const trigger = button().parentElement!;
    expect(trigger).toHaveAttribute("aria-disabled", "true");
    act(() => trigger.focus());
    await nextFrame();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Loading tools…");
  });

  it("shows its own hint and no busy state once it can send", async () => {
    renderButton({ canSend: true, toolsResolving: false });
    expect(button()).toBeEnabled();
    expect(button()).not.toHaveAttribute("aria-busy");

    act(() => button().focus());
    await nextFrame();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("The agent records the findings");
  });

  it("is not busy when something other than the tools blocks the send", () => {
    renderButton({ canSend: false, toolsResolving: false });
    expect(button()).toBeDisabled();
    expect(button()).not.toHaveAttribute("aria-busy");
  });

  it("is not busy once pressed", () => {
    renderButton({ canSend: false, toolsResolving: true }, true);
    expect(screen.getByRole("button", { name: "Sent to OpenCTI" })).not.toHaveAttribute("aria-busy");
  });

  it("is not offered outside a chat that can send", () => {
    renderButton(null);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
