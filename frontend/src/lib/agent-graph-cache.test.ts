import { describe, expect, it, vi } from "vitest";

import { type GraphCache, cachedGraph, warmGraphs } from "./agent-graph-cache";

describe("cachedGraph", () => {
  it("computes a layout once for the same inputs", () => {
    const compute = vi.fn((layout: string | undefined) => ({ layout }));
    let cache = cachedGraph(null, compute, "force");
    cache = cachedGraph(cache, compute, "radial");
    cache = cachedGraph(cache, compute, "force");
    expect(compute).toHaveBeenCalledTimes(2);
    expect(cache.byLayout.get("force")).toEqual({ layout: "force" });
  });

  it("starts over when the inputs change", () => {
    const first = vi.fn((layout: string | undefined) => ({ layout, v: 1 }));
    const second = vi.fn((layout: string | undefined) => ({ layout, v: 2 }));
    let cache = cachedGraph(null, first, "force");
    cache = cachedGraph(cache, second, "force");
    expect(second).toHaveBeenCalledTimes(1);
    expect(cache.byLayout.get("force")).toEqual({ layout: "force", v: 2 });
    expect(cache.byLayout.size).toBe(1);
  });

  it("warms the missing layouts one task at a time", () => {
    const compute = vi.fn((layout: string | undefined) => ({ layout }));
    let cache: GraphCache<{ layout: string | undefined }> | null = cachedGraph(null, compute, "force");
    const tasks: Array<() => void> = [];
    warmGraphs(() => cache, (c) => { cache = c; }, compute, ["force", "radial", "hierarchy-tb"], (task) => {
      tasks.push(task);
      return () => {};
    });
    tasks.shift()!();
    expect(compute).toHaveBeenCalledTimes(2);
    tasks.shift()!();
    expect(compute).toHaveBeenCalledTimes(3);
    expect(tasks).toHaveLength(1);
    tasks.shift()!();
    expect(compute).toHaveBeenCalledTimes(3);
    expect([...cache!.byLayout.keys()]).toEqual(["force", "radial", "hierarchy-tb"]);
  });

  it("stops warming once cancelled or when the inputs change", () => {
    const first = vi.fn((layout: string | undefined) => ({ layout }));
    const second = vi.fn((layout: string | undefined) => ({ layout }));
    let cache: GraphCache<{ layout: string | undefined }> | null = cachedGraph(null, first, "force");
    const tasks: Array<() => void> = [];
    const schedule = (task: () => void) => {
      tasks.push(task);
      return () => {};
    };
    const cancel = warmGraphs(() => cache, (c) => { cache = c; }, first, ["force", "radial"], schedule);
    cancel();
    tasks.shift()!();
    expect(first).toHaveBeenCalledTimes(1);
    warmGraphs(() => cache, (c) => { cache = c; }, first, ["force", "radial"], schedule);
    cache = cachedGraph(cache, second, "force");
    tasks.shift()!();
    expect(first).toHaveBeenCalledTimes(1);
  });

  it("keeps the graph of no layout apart", () => {
    const compute = vi.fn((layout: string | undefined) => ({ layout }));
    const cache = cachedGraph(null, compute, undefined);
    expect(cache.byLayout.get(undefined)).toEqual({ layout: undefined });
  });
});
