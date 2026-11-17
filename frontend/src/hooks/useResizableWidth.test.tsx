// The shared splitter resizes a height too (the video editor's timeline):
// a horizontal separator, Up grows a bottom panel, Left / Right do nothing.

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { KeyboardEvent } from "react";

import { useResizableWidth } from "./useResizableWidth";

const press = (key: string) => ({ key, shiftKey: false, preventDefault: () => {} }) as unknown as KeyboardEvent<HTMLElement>;

describe("useResizableWidth on the y axis", () => {
  it("is a horizontal splitter whose Up / Down arrows grow and shrink a bottom panel", () => {
    const { result } = renderHook(() =>
      useResizableWidth({ defaultWidth: 200, minWidth: 150, maxWidth: 400, edge: "start", axis: "y", step: 10 }),
    );
    expect(result.current.handleProps["aria-orientation"]).toBe("horizontal");
    expect(result.current.width).toBe(200);
    act(() => result.current.handleProps.onKeyDown(press("ArrowUp")));
    expect(result.current.width).toBe(210);
    act(() => result.current.handleProps.onKeyDown(press("ArrowDown")));
    act(() => result.current.handleProps.onKeyDown(press("ArrowDown")));
    expect(result.current.width).toBe(190);
    act(() => result.current.handleProps.onKeyDown(press("ArrowRight")));
    expect(result.current.width).toBe(190);
    act(() => result.current.handleProps.onKeyDown(press("End")));
    expect(result.current.width).toBe(400);
  });

  it("keeps the width splitter as it was: vertical, Left / Right only", () => {
    const { result } = renderHook(() => useResizableWidth({ defaultWidth: 360, minWidth: 320, maxWidth: 600, edge: "start", step: 10 }));
    expect(result.current.handleProps["aria-orientation"]).toBe("vertical");
    act(() => result.current.handleProps.onKeyDown(press("ArrowLeft")));
    expect(result.current.width).toBe(370);
    act(() => result.current.handleProps.onKeyDown(press("ArrowUp")));
    expect(result.current.width).toBe(370);
  });
});
