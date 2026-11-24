// The voice conversation sits in the right group of the composer toolbar, next
// to dictation, with the panel's icon-button look (the microphone's): the
// design system's IconButton, default variant, tertiary priority, size md and
// no colour override, so it takes the accent of the mode. The left group keeps
// the session tools and Deep Investigation only: the agentic quota is the
// panel's own indicator, served through CHAT_PANEL_ENDPOINTS.quota.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";

import { ComposerHeader, ComposerToolbar, ComposerVoiceCallButton } from "@/pages/chat-panel/ComposerParts";
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
vi.mock("@/pages/chat/RunnerApprovalCard", () => ({
  RunnerApprovalCard: ({ request }: { request: { command_id: string } }) => (
    <div data-testid="runner-card">{request.command_id}</div>
  ),
}));

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

// The held runner commands are full decision cards: they sit in one labelled
// region whose height is capped and which scrolls inside, so the composer and
// the latest message stay visible (#842 S8, UX round 1 B1). The cap itself is
// layout, checked in the UX lab: the test reads the region by role.
describe("ComposerHeader runner approvals", () => {
  const request = (id: string) =>
    ({
      command_id: id,
      runner_id: "r-1",
      runner_name: "LAB",
      kind: "run_shell",
      summary: "ls",
      expires_in_seconds: 60,
      received_at_ms: 0,
    }) as ComponentProps<typeof ComposerHeader>["runnerApprovals"][number];

  it("holds every waiting command in one capped, scrolling stack", () => {
    renderWithProviders(
      <ComposerHeader
        deepInvestigation={false}
        runnerApprovals={[request("a"), request("b")]}
        onRunnerApprovalDecided={() => {}}
        desktop={null}
      />,
    );
    const stack = screen.getByRole("region", { name: "Commands waiting for your approval" });
    expect(within(stack).getAllByTestId("runner-card").map((card) => card.textContent)).toEqual(["a", "b"]);
    expect(screen.getAllByTestId("runner-card")).toHaveLength(2);
  });

  it("draws no stack when no command waits", () => {
    renderWithProviders(
      <ComposerHeader deepInvestigation runnerApprovals={[]} onRunnerApprovalDecided={() => {}} desktop={null} />,
    );
    expect(screen.queryByRole("region", { name: "Commands waiting for your approval" })).toBeNull();
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
