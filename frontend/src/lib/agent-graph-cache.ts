/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The computed agent graph, one per layout, for one set of inputs.
 *
 * The map computes a whole graph (positions, edges, counters) from its inputs
 * and a layout. A layout click computes the target before React draws it, and
 * the render that draws it must reuse that result; a switch back to a layout
 * already drawn must not compute it again (the force layout is the costly one,
 * about 90 ms at 149 agents). The inputs are carried by the compute function
 * itself (a memoised callback that changes when they change), so a new
 * function starts a new cache.
 */

export interface GraphCache<G> {
  compute: (layout: string | undefined) => G;
  byLayout: Map<string | undefined, G>;
}

/** Run `task` once the browser is idle; returns what cancels it. */
function whenIdle(task: () => void): () => void {
  if (typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(task);
    return () => window.cancelIdleCallback(handle);
  }
  const handle = window.setTimeout(task, 300);
  return () => window.clearTimeout(handle);
}

/**
 * Compute the graphs of `layouts` missing from the cache, one per idle period,
 * so the first switch to any layout is as quick as a switch back
 * (a cold Radial took 155 ms at 149 agents). Stops when the cache moves to new
 * inputs (`read()` no longer holds `compute`) or when cancelled.
 */
export function warmGraphs<L extends string | undefined, G>(
  read: () => GraphCache<G> | null,
  write: (cache: GraphCache<G>) => void,
  compute: (layout: L) => G,
  layouts: readonly L[],
  schedule: (task: () => void) => () => void = whenIdle,
): () => void {
  let cancel = () => {};
  let stopped = false;
  const step = () => {
    if (stopped) return;
    const cache = read();
    if (!cache || cache.compute !== (compute as (layout: string | undefined) => G)) return;
    const missing = layouts.filter((l) => !cache.byLayout.has(l));
    if (missing.length === 0) return;
    write(cachedGraph(cache, compute, missing[0]));
    cancel = schedule(step);
  };
  cancel = schedule(step);
  return () => {
    stopped = true;
    cancel();
  };
}

/** The cache holding `layout`'s graph for `compute`, computed when missing. */
export function cachedGraph<L extends string | undefined, G>(
  cache: GraphCache<G> | null,
  compute: (layout: L) => G,
  layout: L,
): GraphCache<G> {
  const fn = compute as (layout: string | undefined) => G;
  const current: GraphCache<G> = cache && cache.compute === fn ? cache : { compute: fn, byLayout: new Map() };
  if (!current.byLayout.has(layout)) current.byLayout.set(layout, compute(layout));
  return current;
}
