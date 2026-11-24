import { act, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pageBarsAbove, useFitToViewport } from "./useFitToViewport";

describe("pageBarsAbove (#1294 UX r1 B1)", () => {
  it("keeps only the sticky page bars placed before the table, never one that comes after it", () => {
    const main = document.createElement("main");
    const before = document.createElement("div");
    const area = document.createElement("div");
    const after = document.createElement("div");
    main.append(before, area, after);
    expect(pageBarsAbove(area, [before, after])).toEqual([before]);
  });

  it("ignores a bar that holds the table itself", () => {
    const bar = document.createElement("div");
    const area = document.createElement("div");
    bar.append(area);
    expect(pageBarsAbove(area, [bar])).toEqual([]);
  });
});

/*
 * Guard for #11467: a records table in a dashboard grid row, beside a taller card, blinked
 * and re-rendered forever. Its room was measured with its own bound on: the neighbour kept
 * the page's bottom where it was, so each bound shrank the room again, down to the floor,
 * then the table went unbounded and the cycle started over. The geometry below is that
 * page: the area is 500 px tall unbounded, its top at 300 px, the neighbour ends at 1000 px,
 * the window is 900 px. The room must settle after one write and stay.
 *
 * Since #11470 such a table is a widget, not the page's list: it gets no bound at all and
 * hugs its rows. The same geometry with the other block ABOVE the table (a page header, the
 * table is the page's last block) still fits the window and must settle the same way.
 */

const NATURAL = 500;
const TOP = 300;
const NEIGHBOUR_BOTTOM = 1000;

function areaHeight(el: HTMLElement): number {
  const bound = Number.parseFloat(el.style.maxHeight);
  return Number.isFinite(bound) ? Math.min(NATURAL, bound) : NATURAL;
}

function rect(top: number, height: number): DOMRect {
  return { top, bottom: top + height, height, left: 0, right: 600, width: 600, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

let frames = new Map<number, FrameRequestCallback>();
let nextFrame = 0;
let observers: ResizeObserverCallback[] = [];

function Probe({ header, beside = false, onRender }: { header: boolean; beside?: boolean; onRender: (room: number | null | undefined) => void }) {
  const [area, setArea] = useState<HTMLDivElement | null>(null);
  const room = useFitToViewport(area);
  onRender(room);
  const cell = (
    <div data-testid="cell" key="cell">
      <div ref={setArea} data-testid="area" data-scroll-fit="" style={typeof room === "number" ? { maxHeight: room } : undefined} />
    </div>
  );
  return (
    <div id="app-content">
      <div data-testid="grid">{header ? [<div data-testid="header" key="header" />, cell] : [cell, <div data-testid="neighbour" key="neighbour" {...(beside ? { "data-fit-beside": "" } : {})} />]}</div>
    </div>
  );
}

function settle(header: boolean, beside = false) {
  const rooms: (number | null | undefined)[] = [];
  const { getByTestId } = render(<Probe header={header} beside={beside} onRender={(room) => rooms.push(room)} />);
  const area = getByTestId("area");
  let lastHeight = areaHeight(area);
  // Thirty layout rounds: run the queued frame, then report a size change the bound made.
  for (let round = 0; round < 30; round += 1) {
    act(() => {
      const queued = [...frames.values()];
      frames = new Map();
      for (const frame of queued) frame(performance.now());
    });
    const height = areaHeight(area);
    if (height !== lastHeight) {
      lastHeight = height;
      act(() => {
        for (const cb of observers) cb([], {} as ResizeObserver);
      });
    }
  }
  return { rooms, area };
}

describe("useFitToViewport in a row set by a taller neighbour (#11467, #11470)", () => {
  beforeEach(() => {
    frames = new Map();
    nextFrame = 0;
    observers = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      nextFrame += 1;
      frames.set(nextFrame, cb);
      return nextFrame;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      frames.delete(id);
    });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: ResizeObserverCallback) {
          observers.push(cb);
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("innerHeight", 900);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const testId = this.dataset.testid;
      if (testId === "area" || testId === "cell") return rect(TOP, areaHeight(this.querySelector<HTMLElement>("[data-testid=area]") ?? this));
      if (testId === "neighbour") return rect(TOP, NEIGHBOUR_BOTTOM - TOP);
      if (testId === "header") return rect(0, TOP);
      if (testId === "grid") return rect(0, NEIGHBOUR_BOTTOM);
      return rect(0, 0);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("gives a table beside a dashboard card no bound, so it hugs its rows, and settles", () => {
    const { rooms, area } = settle(false);
    expect(rooms.at(-1)).toBeNull();
    expect(area.style.maxHeight).toBe("");
    expect(new Set(rooms.filter((room) => room !== undefined))).toEqual(new Set([null]));
    // Mount, the area ref, the one room: never a render per layout round.
    expect(rooms.length).toBeLessThanOrEqual(4);
  });

  it("still fits a list whose neighbour is a self-bounded side panel (facet sidebar, #1501 UX r1 B1)", () => {
    const { rooms } = settle(false, true);
    expect(typeof rooms.at(-1)).toBe("number");
  });

  it("fits the page's last block to the window and settles on one room", () => {
    const { rooms, area } = settle(true);
    // 900 - 300 - (1000 - 800) = 400, measured unbounded, whatever bound is on.
    expect(rooms.at(-1)).toBe(400);
    expect(area.style.maxHeight).toBe("400px");
    expect(new Set(rooms.filter((room) => room !== undefined))).toEqual(new Set([400]));
    expect(rooms.length).toBeLessThanOrEqual(4);
  });

  it("shrinks a bound 1 px over the room, so the page does not scroll (#1300)", () => {
    vi.stubGlobal("innerHeight", 901);
    const { rooms, area } = settle(true);
    expect(rooms.at(-1)).toBe(401);
    vi.stubGlobal("innerHeight", 900);
    act(() => {
      for (const cb of observers) cb([], {} as ResizeObserver);
    });
    act(() => {
      const queued = [...frames.values()];
      frames = new Map();
      for (const frame of queued) frame(performance.now());
    });
    expect(rooms.at(-1)).toBe(400);
    expect(area.style.maxHeight).toBe("400px");
  });

  it("keeps the bound on a 1 px growth of the room", () => {
    const { rooms, area } = settle(true);
    expect(rooms.at(-1)).toBe(400);
    vi.stubGlobal("innerHeight", 901);
    act(() => {
      for (const cb of observers) cb([], {} as ResizeObserver);
    });
    act(() => {
      const queued = [...frames.values()];
      frames = new Map();
      for (const frame of queued) frame(performance.now());
    });
    expect(rooms.at(-1)).toBe(400);
    expect(area.style.maxHeight).toBe("400px");
  });
});
