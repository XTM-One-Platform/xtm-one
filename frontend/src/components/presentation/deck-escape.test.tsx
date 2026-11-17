// Escape closes the deck's popup first, even before Radix registers it as the
// top layer: a document capture listener (the host dialog's) never sees the key
// while the popup is open.

import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { useEscapeClosesFirst } from "./deck-escape";

function pressEscape(target: EventTarget = document.body): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

describe("useEscapeClosesFirst", () => {
  const hostListener = vi.fn();

  afterEach(() => {
    document.removeEventListener("keydown", hostListener, { capture: true });
    hostListener.mockReset();
  });

  it("closes the open popup and keeps the key from the host dialog's document listener", () => {
    // The host dialog's listener is in place before the popup opens, as
    // Radix's is for the template preview.
    document.addEventListener("keydown", hostListener, { capture: true });
    const close = vi.fn();
    renderHook(() => useEscapeClosesFirst(true, close));

    const event = pressEscape();

    expect(close).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(hostListener).not.toHaveBeenCalled();
  });

  it("is in place from the first commit, with no render in between", () => {
    const close = vi.fn();
    renderHook(() => useEscapeClosesFirst(true, close));
    // No act(), no await: the listener exists as soon as the hook mounted.
    pressEscape();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("leaves Escape to the host while the popup is closed, and after it closes", () => {
    document.addEventListener("keydown", hostListener, { capture: true });
    const close = vi.fn();
    const hook = renderHook(({ open }) => useEscapeClosesFirst(open, close), { initialProps: { open: false } });

    pressEscape();
    expect(close).not.toHaveBeenCalled();
    expect(hostListener).toHaveBeenCalledTimes(1);

    hook.rerender({ open: true });
    hook.rerender({ open: false });
    const event = pressEscape();
    expect(close).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
    expect(hostListener).toHaveBeenCalledTimes(2);
  });

  it("ignores other keys and an Escape already handled", () => {
    const close = vi.fn();
    renderHook(() => useEscapeClosesFirst(true, close));

    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    const handled = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    handled.preventDefault();
    document.body.dispatchEvent(handled);

    expect(close).not.toHaveBeenCalled();
  });

  it("calls the latest close handler", () => {
    const first = vi.fn();
    const second = vi.fn();
    const hook = renderHook(({ close }) => useEscapeClosesFirst(true, close), { initialProps: { close: first } });
    hook.rerender({ close: second });

    pressEscape();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
