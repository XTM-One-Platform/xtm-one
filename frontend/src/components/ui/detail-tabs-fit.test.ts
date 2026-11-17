// Which tabs stay in a detail tab bar narrower than its tabs.
import { describe, expect, it, vi } from "vitest";
import { dropLastTab, fitTabs, listOverflows, revealTabInBar, scrollLeftToReveal } from "@/components/ui/detail-tabs-fit";

const base = { gap: 4, chrome: 8, more: 40 };

describe("fitTabs", () => {
  it("keeps every tab when they fit, with no menu", () => {
    // 8 + 6 * 100 + 5 * 4 = 628
    expect(fitTabs({ ...base, widths: [100, 100, 100, 100, 100, 100], available: 628, activeIndex: 0 })).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("moves the trailing tabs into the menu, leaving room for its trigger", () => {
    // 600 - 40 = 560 of room: 8 + 5 * 100 + 4 * 4 = 524 fits, so five tabs stay.
    expect(fitTabs({ ...base, widths: [100, 100, 100, 100, 100, 100], available: 600, activeIndex: 0 })).toEqual([0, 1, 2, 3, 4]);
    // 500 - 40 = 460: four tabs (8 + 400 + 12 = 420) fit, five (524) do not.
    expect(fitTabs({ ...base, widths: [100, 100, 100, 100, 100, 100], available: 500, activeIndex: 2 })).toEqual([0, 1, 2, 3]);
  });

  it("keeps the active tab in the bar when it would fall in the menu", () => {
    expect(fitTabs({ ...base, widths: [100, 100, 100, 100, 100, 100], available: 500, activeIndex: 5 })).toEqual([0, 1, 2, 5]);
  });

  it("measures the active tab's own width when it takes a place", () => {
    // Room 460: [0, 1, 2, 5] spans 8 + 300 + 200 + 12 = 520, too wide; [0, 1, 5] spans 8 + 400 + 8 = 416.
    expect(fitTabs({ ...base, widths: [100, 100, 100, 100, 100, 200], available: 500, activeIndex: 5 })).toEqual([0, 1, 5]);
  });

  it("keeps the active tab alone when not even one tab fits beside the menu", () => {
    expect(fitTabs({ ...base, widths: [300, 300, 300], available: 200, activeIndex: 1 })).toEqual([1]);
    expect(fitTabs({ ...base, widths: [300, 300, 300], available: 200, activeIndex: -1 })).toEqual([0]);
  });

  it("keeps every tab when nothing is laid out (no width known)", () => {
    expect(fitTabs({ gap: 0, chrome: 0, more: 40, widths: [0, 0, 0], available: 0, activeIndex: 0 })).toEqual([0, 1, 2]);
  });
});

describe("listOverflows", () => {
  it("is the library's chevron test: more than 1 px of overflow", () => {
    // 1149 in 1124 px scrolls.
    expect(listOverflows({ scrollWidth: 1149, clientWidth: 1124 })).toBe(true);
    // B3: es agent bar with nine tabs, 1148 px in the 1156 px list once no
    // chevron narrows it, fits: the ninth tab stays in the bar.
    expect(listOverflows({ scrollWidth: 1148, clientWidth: 1156 })).toBe(false);
    // A sub-pixel rounding the library ignores does not cost a tab.
    expect(listOverflows({ scrollWidth: 1157, clientWidth: 1156 })).toBe(false);
  });
});

describe("dropLastTab", () => {
  it("drops the last kept tab that is not the active one", () => {
    // es agent bar at 1440: nine tabs kept, the list
    // still scrolls (1149 in 1124 px), so the ninth goes to the menu.
    expect(dropLastTab([0, 1, 2, 3, 4, 5, 6, 7, 8], 0)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(dropLastTab([0, 1, 2, 9], 9)).toEqual([0, 1, 9]);
  });

  it("keeps a single tab", () => {
    expect(dropLastTab([4], 4)).toEqual([4]);
    expect(dropLastTab([0], -1)).toEqual([0]);
  });
});

describe("scrollLeftToReveal", () => {
  const view = { left: 100, right: 400 };

  it("leaves a tab already in view where it is", () => {
    expect(scrollLeftToReveal(view, { left: 150, right: 250 }, 30)).toBe(30);
  });

  it("scrolls left by the hidden part of a tab cut on the left", () => {
    expect(scrollLeftToReveal(view, { left: 80, right: 180 }, 50)).toBe(30);
  });

  it("scrolls right by the hidden part of a tab cut on the right", () => {
    expect(scrollLeftToReveal(view, { left: 350, right: 450 }, 0)).toBe(50);
  });

  it("aligns a tab wider than the view on its start", () => {
    expect(scrollLeftToReveal(view, { left: 380, right: 800 }, 0)).toBe(280);
  });
});

describe("revealTabInBar", () => {
  it("moves the list's scrollLeft only, never the window", () => {
    const bar = document.createElement("div");
    const list = document.createElement("div");
    const tab = document.createElement("a");
    list.appendChild(tab);
    bar.appendChild(list);
    document.body.appendChild(bar);
    const rect = (left: number, right: number) =>
      ({ left, right, top: 0, bottom: 32, width: right - left, height: 32, x: left, y: 0, toJSON: () => ({}) }) as DOMRect;
    Object.defineProperty(list, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(list, "clientWidth", { configurable: true, value: 300 });
    // jsdom keeps scrollLeft at 0: a plain backing field stands in for it.
    let listScrollLeft = 0;
    Object.defineProperty(list, "scrollLeft", {
      configurable: true,
      get: () => listScrollLeft,
      set: (value: number) => {
        listScrollLeft = value;
      },
    });
    list.getBoundingClientRect = () => rect(100, 400);
    tab.getBoundingClientRect = () => rect(350, 450);
    const intoView = vi.fn();
    tab.scrollIntoView = intoView;
    const windowScroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});

    revealTabInBar(tab, bar);

    expect(list.scrollLeft).toBe(50);
    expect(intoView).not.toHaveBeenCalled();
    expect(windowScroll).not.toHaveBeenCalled();
    bar.remove();
    windowScroll.mockRestore();
  });
});
