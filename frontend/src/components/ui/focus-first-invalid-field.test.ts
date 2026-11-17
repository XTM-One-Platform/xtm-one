// focusFirstInvalidField (#9173, S1c, UX r3 B1 on #9895): a refused Save focuses
// the first invalid field and scrolls it to the middle of its scroll container,
// so the sticky SettingsSaveBar never covers the field or its error line.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { focusFirstInvalidField } from "./settings-form";

describe("focusFirstInvalidField", () => {
  let scrollIntoView: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, writable: true, value: scrollIntoView });
    document.body.innerHTML = '<input id="first-ok" /><input id="second-bad" /><input id="third-bad" />';
  });

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
    delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  });

  it("focuses the first set id without the browser's own scroll, then centres it", () => {
    const target = document.getElementById("second-bad") as HTMLInputElement;
    const focus = vi.spyOn(target, "focus");

    focusFirstInvalidField([false, "second-bad", "third-bad"]);
    vi.runAllTimers();

    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(document.activeElement).toBe(target);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", inline: "nearest" });
    expect(scrollIntoView.mock.instances[0]).toBe(target);
  });

  it("does nothing when no id is set or the field is not in the document", () => {
    focusFirstInvalidField([false, null, undefined]);
    focusFirstInvalidField(["missing-field"]);
    vi.runAllTimers();

    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(document.body);
  });
});
