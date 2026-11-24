/**
 * TruncatedText: a cut text names itself whole in the design-system tooltip on hover AND
 * on keyboard focus (UX r6 B1 on #10581): through the focusable element it sits in (a
 * table row link), or as its own tab stop when nothing around it takes the focus.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { TruncatedText } from "@/components/ui/truncated-text";

const LONG = "Lab low-activity user with a deliberately long display name for the table";

function cutEverything() {
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(400);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(100);
}

async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
}

describe("TruncatedText", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("draws no tooltip and no tab stop when the text fits", () => {
    render(<TruncatedText text="Short" />);
    const span = screen.getByText("Short");
    expect(span.getAttribute("tabindex")).toBeNull();
  });

  it("a cut text outside any focusable element is a tab stop whose focus opens the tooltip", async () => {
    cutEverything();
    render(<TruncatedText text={LONG} />);
    const span = screen.getByText(LONG);
    expect(span.getAttribute("tabindex")).toBe("0");
    act(() => span.focus());
    fireEvent.focus(span);
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain(LONG);
  });

  it("an overflow under one pixel is a cut: tab stop and cut flag (UX r3 B3 on #1590)", () => {
    // 141.1 px of text in a 140 px box: the browser draws the ellipsis, the widths read 141 / 140.
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(141);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(140);
    render(<TruncatedText text={LONG} />);
    const span = screen.getByText(LONG);
    expect(span.getAttribute("tabindex")).toBe("0");
    expect(span.hasAttribute("data-cut-text")).toBe(true);
  });

  it("a text exactly as wide as its box is not a cut", () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(140);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(140);
    render(<TruncatedText text={LONG} />);
    const span = screen.getByText(LONG);
    expect(span.getAttribute("tabindex")).toBeNull();
    expect(span.hasAttribute("data-cut-text")).toBe(false);
  });

  it("a multi-line clamp is measured on the height and names the whole text in the tooltip", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(120);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    render(<TruncatedText text={LONG} lines={2} />);
    const span = screen.getByText(LONG);
    expect(span.getAttribute("tabindex")).toBe("0");
    act(() => span.focus());
    fireEvent.focus(span);
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain(LONG);
  });

  it("a multi-line clamp that fits draws no tab stop", () => {
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(40);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    render(<TruncatedText text={LONG} lines={3} />);
    expect(screen.getByText(LONG).getAttribute("tabindex")).toBeNull();
  });

  it("a cut text inside a link opens the tooltip when the link takes the focus", async () => {
    cutEverything();
    render(
      <a href="/users-and-groups/users/1">
        <TruncatedText text={LONG} />
      </a>,
    );
    const span = screen.getByText(LONG);
    expect(span.getAttribute("tabindex")).toBeNull();
    const link = screen.getByRole("link");
    act(() => link.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain(LONG);
  });

  it("a focusable host with several cut texts opens one tooltip naming them all (UX r1 B1 on #10947)", async () => {
    cutEverything();
    const handle = "@lab-agent-with-a-deliberately-long-handle";
    const description = "Description of the lab agent, long enough to be cut in a card";
    render(
      <button type="button">
        <TruncatedText text={LONG} />
        <TruncatedText text={handle} />
        <TruncatedText text={description} />
      </button>,
    );
    const button = screen.getByRole("button");
    act(() => button.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips).toHaveLength(1);
    expect(tips[0].textContent).toContain(LONG);
    expect(tips[0].textContent).toContain(handle);
    expect(tips[0].textContent).toContain(description);
  });
});
