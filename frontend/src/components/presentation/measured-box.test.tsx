/**
 * `useSettledBox` - `yarn test:dom` (#11580).
 *
 * A figure that sizes its room from its own layout can be handed back a
 * different room every frame (an architecture in a `columns` slide under the
 * slide's shrink-to-fit). The box follows the room while it settles, and is
 * frozen once it keeps moving, with `unsettled` set for the layout check.
 */
import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { BOX_SETTLE_CHANGES, useSettledBox, type SettledBox } from "./measured-box";

type Callback = () => void;
let observers: Callback[] = [];

class FakeResizeObserver {
  private readonly callback: Callback;
  constructor(callback: Callback) {
    this.callback = callback;
  }
  observe() {
    observers.push(this.callback);
  }
  disconnect() {
    observers = observers.filter((cb) => cb !== this.callback);
  }
}

let size = { width: 800, height: 500 };
const resize = (width: number, height: number) => {
  size = { width, height };
  act(() => observers.forEach((cb) => cb()));
};

function Probe({ settleKey, onRead }: { settleKey: unknown; onRead: (value: SettledBox) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const value = useSettledBox(ref, settleKey);
  onRead(value);
  return <div ref={ref} />;
}

beforeEach(() => {
  observers = [];
  size = { width: 800, height: 500 };
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => size.width);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => size.height);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useSettledBox", () => {
  test("follows the room while it settles: a few changes are read as they come", () => {
    let last: SettledBox = { box: null, unsettled: false };
    render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    expect(last).toEqual({ box: { width: 800, height: 500 }, unsettled: false });
    resize(820, 520);
    resize(821, 689);
    expect(last).toEqual({ box: { width: 821, height: 689 }, unsettled: false });
  });

  test("a room that keeps moving is frozen at the last box taken, and says so", () => {
    let last: SettledBox = { box: null, unsettled: false };
    render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    // An oscillation between two rooms, as a feedback through the slide's fit draws it.
    for (let i = 0; i < BOX_SETTLE_CHANGES + 5; i += 1) resize(i % 2 ? 821 : 980, i % 2 ? 689 : 768);
    expect(last.unsettled).toBe(true);
    const frozen = last.box;
    expect(frozen).not.toBeNull();
    resize(700, 400);
    expect(last.box).toEqual(frozen);
  });

  test("a monotonic burst past the bound is not frozen: a cold render is not an oscillation", () => {
    let last: SettledBox = { box: null, unsettled: false };
    render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    // A deck arriving: fonts and pictures grow the room many times, one way (UX round 1 B1).
    for (let i = 1; i <= BOX_SETTLE_CHANGES * 2; i += 1) resize(800 + i * 5, 300 + i * 3);
    // Then the slide's fit hands it back once.
    resize(900, 360);
    expect(last).toEqual({ box: { width: 900, height: 360 }, unsettled: false });
  });

  test("a drifting oscillation is frozen even when no size repeats", () => {
    let last: SettledBox = { box: null, unsettled: false };
    render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    for (let i = 0; i < BOX_SETTLE_CHANGES + 5; i += 1) resize(i % 2 ? 821 + i : 980 + i, 600);
    expect(last.unsettled).toBe(true);
  });

  test("a new settle key counts again from zero and reads the room as it stands", () => {
    let last: SettledBox = { box: null, unsettled: false };
    const { rerender } = render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    for (let i = 0; i < BOX_SETTLE_CHANGES + 5; i += 1) resize(i % 2 ? 821 : 980, i % 2 ? 689 : 768);
    expect(last.unsettled).toBe(true);
    size = { width: 640, height: 400 };
    rerender(<Probe settleKey="b" onRead={(value) => (last = value)} />);
    expect(last).toEqual({ box: { width: 640, height: 400 }, unsettled: false });
  });

  test("being hidden and shown again is not counted as a change", () => {
    let last: SettledBox = { box: null, unsettled: false };
    render(<Probe settleKey="a" onRead={(value) => (last = value)} />);
    for (let i = 0; i < BOX_SETTLE_CHANGES + 5; i += 1) resize(i % 2 ? 0 : 800, i % 2 ? 0 : 500);
    expect(last).toEqual({ box: { width: 800, height: 500 }, unsettled: false });
  });
});
