/**
 * A Tip focused from the keyboard - `yarn test:dom`.
 *
 * Radix closes a tooltip when an ancestor of its trigger scrolls, and focusing
 * a row below the fold of a scrolling list scrolls that list: without the
 * deferred open, the tooltip a focus opened was closed by the scroll the same
 * focus caused, so a read-only picker row could not be read from the keyboard.
 */
import { describe, expect, it } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { Tip } from "@/components/ui/tooltip";
import { capTipText } from "@/lib/tip-text";

const nextFrame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

function renderRow() {
  renderWithProviders(
    <div data-testid="list" className="max-h-10 overflow-y-auto">
      <Tip content="Fetch and read a web page." wrap>
        <button type="button" disabled>
          Web fetch
        </button>
      </Tip>
    </div>,
  );
  return screen.getByRole("button", { name: "Web fetch" }).parentElement!;
}

describe("a Tip focused from the keyboard", () => {
  it("stays open through the scroll that brought its trigger into view", async () => {
    const row = renderRow();
    act(() => row.focus());
    fireEvent.scroll(screen.getByTestId("list"));
    await nextFrame();

    expect(row).toHaveAttribute("data-state", expect.stringMatching(/open/));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Fetch and read a web page.");
  });

  it("does not open when a pointer press focused it", async () => {
    const row = renderRow();
    fireEvent.pointerDown(row);
    act(() => row.focus());
    await nextFrame();

    expect(row).toHaveAttribute("data-state", "closed");
  });

  it("does not open when focus left before the frame", async () => {
    const row = renderRow();
    act(() => row.focus());
    act(() => row.blur());
    await nextFrame();

    expect(row).toHaveAttribute("data-state", "closed");
  });
});

/** A long text stays a few lines tall: cut at a word with an ellipsis. */
describe("capTipText", () => {
  it("keeps a short text whole", () => {
    expect(capTipText("Fetch and read a web page.")).toBe("Fetch and read a web page.");
  });

  it("cuts a long text at a word, under the cap, with an ellipsis", () => {
    const long = "How to make a video from a script. ".repeat(30);
    const capped = capTipText(long);
    expect(capped.length).toBeLessThanOrEqual(280);
    expect(capped.endsWith("\u2026")).toBe(true);
    expect(capped).not.toMatch(/\s\u2026$/);
    expect(long.startsWith(capped.slice(0, -1))).toBe(true);
  });

  it("shows the capped text in the tooltip of a Tip given a long string", async () => {
    const long = "word ".repeat(200);
    renderWithProviders(
      <Tip content={long}>
        <button type="button">Long</button>
      </Tip>,
    );
    const button = screen.getByRole("button", { name: "Long" });
    act(() => button.focus());
    await nextFrame();
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent!.length).toBeLessThanOrEqual(280);
    expect(tooltip.textContent!.endsWith("\u2026")).toBe(true);
  });
});

/**
 * `wrap="auto"` around a control that is disabled only now and then: one tab
 * stop, the control's own, while it is enabled; the wrapper takes over while it is
 * disabled; and the control is never re-created, so a click that disables it keeps it.
 */
describe('a Tip with wrap="auto"', () => {
  function Busy({ busy }: { busy: boolean }) {
    return (
      <Tip content="Re-scores this run." wrap="auto">
        <button type="button" disabled={busy}>
          Re-score
        </button>
      </Tip>
    );
  }

  it("is the button's own tab stop while enabled, and the wrapper's while disabled", () => {
    const { rerender } = renderWithProviders(<Busy busy={false} />);
    const button = screen.getByRole("button", { name: "Re-score" });
    const wrapper = button.parentElement!;
    expect(wrapper.hasAttribute("tabindex")).toBe(false);
    expect(wrapper.hasAttribute("aria-disabled")).toBe(false);

    rerender(<Busy busy />);
    expect(wrapper.getAttribute("tabindex")).toBe("0");
    expect(wrapper.getAttribute("aria-disabled")).toBe("true");
  });

  it("keeps the same button when it turns disabled, so it is not re-created", () => {
    const { rerender } = renderWithProviders(<Busy busy={false} />);
    const before = screen.getByRole("button", { name: "Re-score" });
    rerender(<Busy busy />);
    expect(screen.getByRole("button", { name: "Re-score" })).toBe(before);
  });
});
