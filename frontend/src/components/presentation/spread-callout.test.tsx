// A `fill` callout around a capped `fill` list: the callout follows
// its run instead of stretching its tint past the list's cap. happy-dom lays
// nothing out, so the boxes are stubbed: every element measures the height
// its `data-h` attribute names while the callout carries `flex: none` (its
// natural height), and `data-stretched` once the room stretches it.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { measureCalloutRun } from "./spread";

function box(tag: string, attrs: Record<string, string> = {}, children: HTMLElement[] = []): HTMLElement {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  for (const child of children) el.appendChild(child);
  return el;
}

/** A callout whose run holds `children`, natural height 260, stretched height 900. */
function callout(children: HTMLElement[]): HTMLElement {
  const run = box("div", { class: "deck-blocks", "data-h": "200" }, children);
  const body = box("div", { class: "deck-callout-body", "data-h": "220" }, [run]);
  const el = box("div", { class: "deck-callout", "data-fill": "true", "data-h": "260", "data-stretched": "900" }, [body]);
  el.style.boxSizing = "border-box";
  document.body.appendChild(el);
  return el;
}

/** A slide run (`.deck-blocks`) whose room is `room` px tall. */
function slideRun(room: number): HTMLElement {
  const run = box("div", { class: "deck-blocks", "data-client": String(room) });
  document.body.appendChild(run);
  return run;
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return Number(this.getAttribute("data-client") ?? 0);
    },
  });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      const stretched = this.getAttribute("data-stretched");
      // `flex: none` reads back as its longhands, so the grow factor is checked.
      if (stretched && this.style.flexGrow !== "0") return Number(stretched);
      return Number(this.getAttribute("data-h") ?? 0);
    },
  });
});

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "offsetHeight");
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  document.body.replaceChildren();
});

describe("measureCalloutRun", () => {
  it("caps a fill callout at its natural height plus what its capped list may still take", () => {
    // The list measures 180 now and is capped at 260: 80 more it may grow.
    const list = box("ul", { class: "deck-list", "data-fill": "true", "data-h": "180", "data-spread-cap": "260" });
    const el = callout([list]);
    const measured = measureCalloutRun(el);
    expect(measured).toEqual({ rows: [260 + 80], gap: 0, frame: 0, filler: false });
    // The callout's own styles are put back after the read.
    expect(el.style.flex).toBe("");
    expect(el.style.maxHeight).toBe("");
  });

  it("leaves a callout around a filler (a fill chart) uncapped", () => {
    const list = box("ul", { class: "deck-list", "data-fill": "true", "data-h": "180", "data-spread-cap": "260" });
    const chart = box("figure", { class: "deck-chart", "data-fill": "true", "data-h": "160" });
    expect(measureCalloutRun(callout([list, chart]))?.filler).toBe(true);
  });

  it("caps a short callout with no growing child at its natural height", () => {
    // Alone in a body run 796 tall, 260 of content: a third of the room.
    const text = box("p", { class: "deck-text", "data-h": "40" });
    const el = callout([text]);
    slideRun(796).appendChild(el);
    expect(measureCalloutRun(el)).toEqual({ rows: [260], gap: 0, frame: 0, filler: false });
  });

  it("keeps a lone callout whose content takes most of its room stretched", () => {
    // 260 of content in a run 400 tall: 65%, over the 60% floor.
    const text = box("p", { class: "deck-text", "data-h": "40" });
    const el = callout([text]);
    slideRun(400).appendChild(el);
    expect(measureCalloutRun(el)?.filler).toBe(true);
  });

  it("caps a callout that shares its run with another block", () => {
    const text = box("p", { class: "deck-text", "data-h": "40" });
    const el = callout([text]);
    const run = slideRun(300);
    run.appendChild(box("p", { class: "deck-text", "data-h": "40" }));
    run.appendChild(el);
    expect(measureCalloutRun(el)?.filler).toBe(false);
  });

  it("leaves every fill callout of a run the slide asked to stretch uncapped, siblings or not", () => {
    // `vertical_align: stretch` is an explicit request: it wins over the
    // hug-content default, so two short callouts share the body again.
    const run = slideRun(796);
    run.setAttribute("data-valign", "stretch");
    const first = callout([box("p", { class: "deck-text", "data-h": "40" })]);
    const second = callout([box("p", { class: "deck-text", "data-h": "40" })]);
    run.appendChild(first);
    run.appendChild(second);
    expect(measureCalloutRun(first)?.filler).toBe(true);
    expect(measureCalloutRun(second)?.filler).toBe(true);
  });

  it("still caps the same callouts when the run asks nothing", () => {
    const run = slideRun(796);
    run.setAttribute("data-valign", "start");
    const first = callout([box("p", { class: "deck-text", "data-h": "40" })]);
    run.appendChild(first);
    run.appendChild(callout([box("p", { class: "deck-text", "data-h": "40" })]));
    expect(measureCalloutRun(first)?.filler).toBe(false);
  });

  it("leaves a callout in a column stretched: the cards of a row share one height", () => {
    const list = box("ul", { class: "deck-list", "data-fill": "true", "data-h": "180", "data-spread-cap": "260" });
    const el = callout([list]);
    const column = box("div", { class: "deck-column" });
    document.body.appendChild(column);
    column.appendChild(el);
    expect(measureCalloutRun(el)?.filler).toBe(true);
  });
});
