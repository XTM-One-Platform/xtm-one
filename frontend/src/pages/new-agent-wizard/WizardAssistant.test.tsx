// The wizard assistant shows that it works: its bar while it is busy, and the
// field it is filling while that field is active (the active border).

import { beforeAll, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { AssistFrame, WizardAssistantBar } from "./WizardAssistant";

beforeAll(() => {
  // happy-dom has no layout: the frame scrolls itself into view when active.
  Element.prototype.scrollIntoView = vi.fn();
});

const barProps = {
  turns: [],
  pending: false,
  playing: false,
  status: null,
  started: true,
  disabled: false,
  canUndo: false,
  onSend: vi.fn(),
  onStop: vi.fn(),
  onUndo: vi.fn(),
};

describe("AssistFrame", () => {
  it("renders its field untouched and unmarked while the assistant is not on it", () => {
    const { container } = renderWithProviders(
      <AssistFrame active={false}>
        <input aria-label="Persona" />
      </AssistFrame>,
    );

    expect(screen.getByLabelText("Persona")).toBeTruthy();
    expect(container.querySelector("[data-assist-active]")).toBeNull();
    expect(screen.queryByText("AI")).toBeNull();
  });

  it("marks the field the assistant is filling, with its AI tag", () => {
    const { container } = renderWithProviders(
      <AssistFrame active>
        <input aria-label="Persona" />
      </AssistFrame>,
    );

    expect(container.querySelector("[data-assist-active]")).not.toBeNull();
    expect(screen.getByText("AI")).toBeTruthy();
  });
});

describe("WizardAssistantBar", () => {
  it("is not busy at rest", () => {
    const { container } = renderWithProviders(<WizardAssistantBar {...barProps} />);

    expect(container.querySelector("[data-busy]")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is busy while it prepares the changes, and says so", () => {
    const { container } = renderWithProviders(<WizardAssistantBar {...barProps} pending />);

    expect(container.querySelector("[data-busy]")).not.toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Preparing the changes...");
  });

  it("is busy while it plays the answer onto the wizard", () => {
    const { container } = renderWithProviders(
      <WizardAssistantBar {...barProps} playing status="Filling in Identity..." />,
    );

    expect(container.querySelector("[data-busy]")).not.toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Filling in Identity...");
  });
});
