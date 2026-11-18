// SPDX-License-Identifier: Apache-2.0

// A `?card=` landing keeps its card at the top while the page grows (UX round 1 B1 on xtm-one #108):
// the last card of a tab was left at the bottom of the view because the one scroll ran on a short page.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { landOnCard } from "./land-on-card";

let resize: (() => void) | null = null;
class FakeResizeObserver {
  constructor(callback: () => void) {
    resize = callback;
  }
  observe() {}
  disconnect() {
    resize = null;
  }
}

describe("landOnCard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    resize = null;
    document.body.innerHTML = "";
  });

  const card = () => {
    const element = document.createElement("div");
    element.scrollIntoView = vi.fn();
    document.body.appendChild(element);
    return element;
  };

  it("scrolls the card to the top of the view at once", () => {
    const element = card();
    landOnCard(element);
    expect(element.scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("scrolls it again each time the page grows, the last card of a tab included", () => {
    const element = card();
    landOnCard(element);
    resize?.();
    resize?.();
    expect(element.scrollIntoView).toHaveBeenCalledTimes(3);
  });

  it("gives the page back once it has settled", () => {
    const element = card();
    landOnCard(element, 1000);
    vi.advanceTimersByTime(1000);
    expect(resize).toBeNull();
  });

  it("gives the page back as soon as the reader scrolls, so a landing never fights them", () => {
    const element = card();
    landOnCard(element);
    window.dispatchEvent(new Event("wheel"));
    expect(resize).toBeNull();
    expect(element.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("stops when the next landing or the page ends it", () => {
    const element = card();
    const end = landOnCard(element);
    end();
    expect(resize).toBeNull();
  });
});
