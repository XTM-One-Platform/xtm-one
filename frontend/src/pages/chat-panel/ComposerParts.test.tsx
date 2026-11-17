// The voice conversation sits in the right group of the composer toolbar, next
// to dictation, with the panel's icon-button look (the microphone's): the
// design system's IconButton, default variant, tertiary priority, size md and
// no colour override, so it takes the accent of the mode. The left group keeps
// the session tools and Deep Investigation only: the agentic quota is the
// panel's own indicator, served through CHAT_PANEL_ENDPOINTS.quota.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import type { ComponentProps } from "react";

import { ComposerToolbar, ComposerVoiceCallButton } from "@/pages/chat-panel/ComposerParts";
import { renderWithProviders } from "@/testing/providers";

const iconButtons = vi.hoisted(() => [] as Record<string, unknown>[]);

vi.mock("@filigran/design-system", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@filigran/design-system")>();
  const Original = actual.IconButton;
  function RecordingIconButton(props: ComponentProps<typeof Original>) {
    iconButtons.push({ ...props });
    return <Original {...props} />;
  }
  return { ...actual, IconButton: RecordingIconButton };
});
vi.mock("@/pages/chat/SessionToolPicker", () => ({ SessionToolPicker: () => <span>tools</span> }));

describe("ComposerVoiceCallButton", () => {
  it("starts the voice conversation from a button named for it", () => {
    const onStart = vi.fn();
    renderWithProviders(<ComposerVoiceCallButton disabled={false} onStart={onStart} />);
    fireEvent.click(screen.getByRole("button", { name: "Start voice conversation" }));
    expect(onStart).toHaveBeenCalledTimes(1);
  });

  it("uses the microphone's icon-button look (default, tertiary, md)", () => {
    iconButtons.length = 0;
    renderWithProviders(<ComposerVoiceCallButton disabled={false} onStart={() => {}} />);
    const props = iconButtons.find((p) => p["aria-label"] === "Start voice conversation");
    expect(props).toBeDefined();
    expect(props?.variant).toBe("default");
    expect(props?.priority).toBe("tertiary");
    expect(props?.size).toBe("md");
  });

  it("is disabled with the composer's other controls", () => {
    renderWithProviders(<ComposerVoiceCallButton disabled onStart={() => {}} />);
    expect(screen.getByRole("button", { name: "Start voice conversation" })).toBeDisabled();
  });
});

describe("ComposerToolbar", () => {
  it("no longer holds the voice conversation in the left group", () => {
    renderWithProviders(
      <ComposerToolbar
        disabled={false}
        tools={{} as ComponentProps<typeof ComposerToolbar>["tools"]}
        toolCount={0}
        deepInvestigation={false}
      />,
    );
    expect(screen.queryByRole("button", { name: "Start voice conversation" })).toBeNull();
  });

  it("draws no quota of its own: the panel's indicator is the one", () => {
    renderWithProviders(
      <ComposerToolbar
        disabled={false}
        tools={{} as ComponentProps<typeof ComposerToolbar>["tools"]}
        toolCount={0}
        deepInvestigation={false}
      />,
    );
    expect(screen.queryByText(/agentic actions/i)).toBeNull();
  });
});
