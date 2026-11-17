/**
 * Opening the speaker view while presenting never ends presenting (xtm-one
 * #10552, slice A1): Chrome ends element fullscreen when the page opens a
 * pop-up, and the stage must fall back to the viewport mode instead of the
 * normal viewer. A second `S` focuses the open speaker window, never a second
 * one.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useElementFullscreen, VIEWPORT_FULLSCREEN_ATTR } from "./fullscreen";
import { openSpeakerView } from "./speaker-url";

let fullscreenElement: Element | null = null;

function setFullscreen(element: Element | null) {
  fullscreenElement = element;
  document.dispatchEvent(new Event("fullscreenchange"));
}

Object.defineProperty(document, "fullscreenElement", {
  configurable: true,
  get: () => fullscreenElement,
});

function presentNatively() {
  const element = document.createElement("div");
  document.body.appendChild(element);
  const ref = { current: element };
  const hook = renderHook(() => useElementFullscreen(ref));
  act(() => setFullscreen(element));
  expect(hook.result.current.mode).toBe("native");
  return { element, hook };
}

afterEach(() => {
  fullscreenElement = null;
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("keepPresenting", () => {
  it("keeps the deck presented in the viewport mode when opening a window ends fullscreen", () => {
    const { element, hook } = presentNatively();
    act(() => hook.result.current.keepPresenting(() => setFullscreen(null)));
    expect(hook.result.current.isFullscreen).toBe(true);
    expect(hook.result.current.mode).toBe("viewport");
    expect(element.hasAttribute(VIEWPORT_FULLSCREEN_ATTR)).toBe(true);
  });

  it("still leaves presenting on a plain exit (Esc) outside keepPresenting", () => {
    const { hook } = presentNatively();
    act(() => setFullscreen(null));
    expect(hook.result.current.isFullscreen).toBe(false);
  });

  it("leaves presenting on exit() even right after keepPresenting", () => {
    const { hook } = presentNatively();
    const exitFullscreen = vi.fn(() => {
      setFullscreen(null);
      return Promise.resolve();
    });
    Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exitFullscreen });
    act(() => hook.result.current.keepPresenting(() => undefined));
    act(() => hook.result.current.exit());
    expect(exitFullscreen).toHaveBeenCalledOnce();
    expect(hook.result.current.isFullscreen).toBe(false);
  });

  it("does nothing special when the deck is not presented", () => {
    const element = document.createElement("div");
    const ref = { current: element };
    const { result } = renderHook(() => useElementFullscreen(ref));
    const action = vi.fn();
    act(() => result.current.keepPresenting(action));
    expect(action).toHaveBeenCalledOnce();
    expect(result.current.isFullscreen).toBe(false);
  });
});

describe("openSpeakerView", () => {
  it("focuses the open speaker window on a second call instead of opening another", () => {
    const focus = vi.fn();
    const speaker = { closed: false, focus } as unknown as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(speaker);
    expect(openSpeakerView("deck-repeat")).toBe(true);
    expect(openSpeakerView("deck-repeat")).toBe(true);
    expect(open).toHaveBeenCalledOnce();
    expect(focus).toHaveBeenCalledTimes(2);
  });

  it("opens a new window once the previous one was closed", () => {
    const first = { closed: false, focus: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(first as unknown as Window);
    openSpeakerView("deck-closed");
    first.closed = true;
    openSpeakerView("deck-closed");
    expect(open).toHaveBeenCalledTimes(2);
  });

  it("reports a blocked pop-up", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(openSpeakerView("deck-blocked")).toBe(false);
  });
});
