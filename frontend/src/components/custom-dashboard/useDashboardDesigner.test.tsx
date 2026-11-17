// The studio's design history as the page drives it: a change is shown at once
// and saved as the difference; undo and redo save it back; a run of changes of
// one field is one step and one save; a change too large for one edit is saved
// whole; a revision someone else wrote starts the history again; a refused save
// says why and goes back to what is stored. Copy, paste and duplicate give new
// ids. Asserted on the edits handed to the page's queue.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { testQueryClient, withProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

import { addElement, moveItems, setElementOptions } from "./canvas";
import { COALESCE_MS, useDashboardDesigner } from "./useDashboardDesigner";

type Definition = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

function spec(): Definition {
  return {
    version: 2,
    title: "Ops",
    visualizations: { box: { type: "xtm.shape.rectangle", options: { rx: 4 } } },
    layout: {
      tabs: { items: [{ layoutId: "wall", label: "Wall" }] },
      layoutDefinitions: {
        wall: { type: "absolute", options: { width: 800, height: 400 }, structure: [{ item: "box", type: "block", position: { x: 0, y: 0, w: 100, h: 50 } }] },
      },
    },
  };
}

function setup(initial: Partial<CustomDashboard> = {}) {
  const edit = vi.fn();
  const replace = vi.fn();
  const onRefused = vi.fn();
  const client = testQueryClient();
  let dashboard = { id: "d1", revision: 3, spec: spec(), ...initial } as CustomDashboard;
  client.setQueryData(["custom-dashboards", "d1"], dashboard);
  const view = renderHook(({ current }) => useDashboardDesigner({ dashboard: current, edit, replace, onRefused }), {
    wrapper: withProviders(client),
    initialProps: { current: dashboard },
  });
  const serve = (next: Partial<CustomDashboard>) => {
    dashboard = { ...dashboard, ...next } as CustomDashboard;
    client.setQueryData(["custom-dashboards", "d1"], dashboard);
    view.rerender({ current: dashboard });
  };
  return { ...view, edit, replace, onRefused, serve, client };
}

afterEach(() => vi.useRealTimers());

describe("useDashboardDesigner", () => {
  it("shows a change at once and saves its difference; undo and redo save it back", () => {
    const { result, edit } = setup();
    act(() => result.current.change((d) => moveItems(d, "wall", ["box"], 10, 20)));
    expect((result.current.definition as Definition).layout.layoutDefinitions.wall.structure[0].position).toEqual({ x: 10, y: 20, w: 100, h: 50 });
    expect(edit.mock.calls[0][0]).toEqual([{ op: "set", path: "layout", value: (result.current.definition as Definition).layout }]);
    expect(result.current.canUndo).toBe(true);
    act(() => result.current.undo());
    expect((result.current.definition as Definition).layout.layoutDefinitions.wall.structure[0].position).toEqual({ x: 0, y: 0, w: 100, h: 50 });
    expect(edit.mock.calls[1][0]).toEqual([{ op: "set", path: "layout", value: spec().layout }]);
    act(() => result.current.redo());
    expect(edit.mock.calls.length).toBe(3);
    expect(edit.mock.calls[2][0][0].value.layoutDefinitions.wall.structure[0].position).toEqual({ x: 10, y: 20, w: 100, h: 50 });
  });

  it("makes a run of changes of one field one step and one save, once it pauses", () => {
    vi.useFakeTimers();
    const { result, edit } = setup();
    for (const rx of [5, 6, 7]) act(() => result.current.change((d) => setElementOptions(d, "box", { rx }), "box.rx"));
    expect(edit).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(COALESCE_MS));
    expect(edit.mock.calls).toEqual([
      [[{ op: "set", path: "visualizations.box", value: { type: "xtm.shape.rectangle", options: { rx: 7 } } }], expect.any(Object)],
    ]);
    act(() => result.current.undo());
    expect((result.current.definition as Definition).visualizations.box.options).toEqual({ rx: 4 });
  });

  it("saves a change too large for one edit as a whole definition", () => {
    const { result, edit, replace } = setup();
    act(() =>
      result.current.change((d) => {
        let next = d;
        for (let i = 0; i < 41; i += 1) next = addElement(next, "wall", "rectangle", { x: i, y: i }).definition;
        return next;
      }),
    );
    expect(edit).not.toHaveBeenCalled();
    expect(Object.keys(replace.mock.calls[0][0].visualizations).length).toBe(42);
  });

  it("starts again from a revision someone else wrote, not from its own", () => {
    const { result, edit, serve } = setup();
    act(() => result.current.change((d) => moveItems(d, "wall", ["box"], 10, 0)));
    // Its own save answers revision 4: the history stays.
    const saved = { ...spec(), layout: (result.current.definition as Definition).layout };
    act(() => edit.mock.calls[0][1].onSuccess({ revision: 4, spec: saved }));
    serve({ revision: 4, spec: saved });
    expect(result.current.canUndo).toBe(true);
    // The assistant writes revision 5: the history starts again from it.
    const theirs = setElementOptions(spec(), "box", { rx: 40 });
    serve({ revision: 5, spec: theirs });
    expect(result.current.canUndo).toBe(false);
    expect((result.current.definition as Definition).visualizations.box.options).toEqual({ rx: 40 });
  });

  it("says why a save was refused and goes back to what is stored", () => {
    const { result, edit, onRefused } = setup();
    act(() => result.current.change((d) => moveItems(d, "wall", ["box"], 10, 0)));
    const error = Object.assign(new Error("no"), { status: 422 });
    act(() => edit.mock.calls[0][1].onError(error));
    expect(onRefused).toHaveBeenCalledWith(error);
    expect(result.current.definition).toEqual(spec());
    expect(result.current.canUndo).toBe(false);
  });

  it("copies and pastes, duplicates with a new id, and cuts", () => {
    const { result } = setup();
    act(() => result.current.select(["box"]));
    act(() => result.current.copy("wall"));
    expect(result.current.canPaste).toBe(true);
    act(() => result.current.paste("wall"));
    expect(result.current.selection).toEqual(["box_2"]);
    expect((result.current.definition as Definition).layout.layoutDefinitions.wall.structure[1].position).toEqual({ x: 20, y: 20, w: 100, h: 50 });
    act(() => result.current.duplicate("wall"));
    expect(result.current.selection).toEqual(["box_3"]);
    act(() => result.current.cut("wall"));
    expect(Object.keys((result.current.definition as Definition).visualizations ?? {})).toEqual(["box", "box_2"]);
    expect(result.current.selection).toEqual([]);
  });

  it("answers the canvas's shortcuts", () => {
    const { result, edit } = setup();
    act(() => result.current.change((d) => moveItems(d, "wall", ["box"], 10, 0)));
    const key = (init: Partial<KeyboardEvent>) =>
      ({ key: "z", metaKey: false, ctrlKey: true, shiftKey: false, altKey: false, preventDefault: vi.fn(), ...init }) as unknown as React.KeyboardEvent;
    const undo = key({});
    act(() => result.current.onShortcut("wall")(undo));
    expect(undo.preventDefault).toHaveBeenCalled();
    expect(result.current.canRedo).toBe(true);
    act(() => result.current.onShortcut("wall")(key({ key: "y" })));
    expect(result.current.canRedo).toBe(false);
    expect(edit.mock.calls.length).toBe(3);
    const other = key({ key: "q" });
    act(() => result.current.onShortcut("wall")(other));
    expect(other.preventDefault).not.toHaveBeenCalled();
  });
});
