/**
 * The command center's lifecycle header and its Export CSV button (D3-5,
 * #9477).
 * - UX round 1 on #9478, B2: while the cards show the sample the button is
 *   disabled, and a disabled button swallows hover and focus, so the reason
 *   lives on the `wrap="auto"` wrapper, a tab stop exactly while the button
 *   is off.
 * - UX round 2 on #9478, B1: at 1024 px the widget's container is about
 *   780 px; the stage path subtitle took the width the fr, es and ja title
 *   needs. It is shown only from the `@4xl` container (896 px), never from
 *   `@2xl` (672 px). Container queries do not resolve in jsdom, so the
 *   widths are checked rendered by the UX review; this file asserts text and
 *   roles only (frontend/AGENTS.md, src/no-class-name-assertions.test.ts).
 */
import { describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { LifecycleHeader } from "./LifecycleHeader";

const REASON = "Available once the posture is computed from a connected platform";
const nextFrame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

describe("the Export CSV button on the sample", () => {
  it("shows why it is disabled when focused from the keyboard", async () => {
    renderWithProviders(<LifecycleHeader live={false} covered={0} total={5} onExport={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Export CSV" });
    expect(button).toBeDisabled();
    const wrapper = button.parentElement!;
    expect(wrapper.getAttribute("tabindex")).toBe("0");
    act(() => wrapper.focus());
    await nextFrame();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(REASON);
  });

  it("is the button's own tab stop once the posture is live", () => {
    const onExport = vi.fn();
    renderWithProviders(<LifecycleHeader live covered={4} total={5} onExport={onExport} />);
    const button = screen.getByRole("button", { name: "Export CSV" });
    expect(button.parentElement!.hasAttribute("tabindex")).toBe(false);
    act(() => button.click());
    expect(onExport).toHaveBeenCalledTimes(1);
  });
});

describe("the lifecycle header content", () => {
  it("renders the stage path subtitle", () => {
    renderWithProviders(<LifecycleHeader live covered={4} total={5} onExport={vi.fn()} />);
    expect(screen.getByTestId("lifecycle-stage-path")).toBeInTheDocument();
  });

  it("writes the stage count", () => {
    renderWithProviders(<LifecycleHeader live covered={4} total={5} onExport={vi.fn()} />);
    expect(screen.getByText("4 of 5 stages covered")).toBeInTheDocument();
  });
});
