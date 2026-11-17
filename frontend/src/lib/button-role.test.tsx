// `buttonRoleProps()`: an element that acts as a button is focusable,
// announced as a button, and activated by a click, Enter or Space - never by a
// key that bubbles up from a control inside it.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { buttonRoleProps } from "./button-role";

const onActivate = vi.fn();
const onInfo = vi.fn();

function card({ activate }: { activate: (() => void) | undefined } = { activate: onActivate }) {
  render(
    <div data-testid="card" {...buttonRoleProps(activate)}>
      <span>Agents</span>
      <button type="button" onClick={onInfo}>How is it computed?</button>
    </div>,
  );
  return screen.getByTestId("card");
}

describe("buttonRoleProps", () => {
  beforeEach(() => {
    onActivate.mockReset();
    onInfo.mockReset();
  });

  it("makes the element a focusable button", () => {
    const el = card();
    expect(el.getAttribute("role")).toBe("button");
    expect(el.tabIndex).toBe(0);
    el.focus();
    expect(document.activeElement).toBe(el);
  });

  it("activates on click, Enter and Space, and on no other key", () => {
    const el = card();
    fireEvent.keyDown(el, { key: "a" });
    fireEvent.keyDown(el, { key: "Tab" });
    fireEvent.keyDown(el, { key: "Escape" });
    expect(onActivate).toHaveBeenCalledTimes(0);

    fireEvent.click(el);
    expect(onActivate).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(el, { key: "Enter" });
    expect(onActivate).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(el, { key: " " });
    expect(onActivate).toHaveBeenCalledTimes(3);
  });

  it("keeps Space from scrolling the page and Enter from submitting a form", () => {
    const el = card();
    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    el.dispatchEvent(space);
    expect(space.defaultPrevented).toBe(true);
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    el.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
  });

  it("leaves Enter and Space pressed on a control inside it to that control", () => {
    card();
    const info = screen.getByRole("button", { name: "How is it computed?" });
    fireEvent.keyDown(info, { key: "Enter" });
    fireEvent.keyDown(info, { key: " " });
    expect(onActivate).toHaveBeenCalledTimes(0);
  });

  it("gives nothing to an element with nothing to activate", () => {
    const el = card({ activate: undefined });
    expect(el.getAttribute("role")).toBeNull();
    expect(el.hasAttribute("tabindex")).toBe(false);
    expect(buttonRoleProps(undefined)).toEqual({});
  });
});
