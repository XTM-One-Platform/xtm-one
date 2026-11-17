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

import { startTransition, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import {
  LAYOUT_TWEEN_MS,
  drawnState,
  easeCubicInOut,
  edgeKeys,
  frameRate,
  layoutFrame,
  samePlacement,
  type DrawnState,
  type Faded,
  type TweenEdge,
  type TweenNode,
} from "@/lib/agent-graph-tween";

type Keyed<E> = E & { tweenKey: string };
/**
 * `leaving`: drawn only until the change ends. `fade`: never set by React
 * renders (the frame loop paints fades on the DOM); kept optional so a
 * drawer can scale its rest opacity by it.
 */
type Leaving = { leaving?: boolean; fade?: number };

interface AnimatedLayout<N, E> {
  /** What React draws: the target nodes, plus the ones fading out during a change. */
  nodes: Array<N & Leaving>;
  edges: Array<Keyed<E> & Leaving>;
  /** True while a layout change is being drawn by a React render. */
  animating: boolean;
  /** Frames per second of the last layout change, 0 before the first one. */
  fps: number;
  /** Longest gap between two painted frames of the last change (ms), 0 before the first one. */
  worstFrameMs: number;
  /**
   * Time from the input that asked for the change (the click) to the first
   * painted frame of the move (ms), 0 before the first one.
   */
  firstFrameMs: number;
  /**
   * Starts moving the drawing on screen to a new target at once, before React
   * renders it: the frame loop writes the DOM
   * React drew last. `commit` is called once the move ends: it hands the
   * target to React (a low-priority update), whose render then finds the
   * drawing already in place. False when nothing is animated (reduced motion,
   * no drawing yet, same placement): the caller commits at once.
   */
  preview: (nodes: N[], edges: E[], commit: () => void) => boolean;
}

/** Wrapper of one node the frame loop moves and fades: `<g data-tween-node={id}>`. */
export const TWEEN_NODE_ATTR = "data-tween-node";
/** A path of one edge (by its tween key) whose `d` and opacity the frame loop redraws. */
export const TWEEN_EDGE_ATTR = "data-tween-edge";
/**
 * Opacity an element has at rest (focus and hover dimming included); the
 * frame loop paints it multiplied by the element's fade.
 */
export const BASE_OPACITY_ATTR = "data-base-opacity";
/** Set on the svg while a change is drawn: decorations pause, labels hide (CSS). */
const ANIMATING_ATTR = "data-animating";

function withKeys<E extends TweenEdge>(edges: readonly E[]): Array<Keyed<E>> {
  const keys = edgeKeys(edges);
  return edges.map((edge, i) => ({ ...edge, tweenKey: keys[i] }));
}

const keyOf = <E extends TweenEdge>(edge: Keyed<E>) => edge.tweenKey;

interface Drawing<N, E> {
  nodes: N[];
  edges: Array<Keyed<E>>;
}

interface Change<N, E> {
  start: DrawnState;
  target: Drawing<N, E>;
  leaving: Drawing<N, E>;
  /** When the change was asked for (the click's time stamp): its first frame is timed from it. */
  askedAt: number;
}

/** One target with its edge keys, and the change that moves the drawing to it (none at rest). */
interface Plan<N, E> {
  nodes: N[];
  edges: Array<Keyed<E>>;
  change: Change<N, E> | null;
  /** Rank of the change, so an edge leaving twice keeps distinct React keys. */
  generation: number;
}

/** A change started on the DOM ahead of React (see `preview`). */
interface Preview<N, E> {
  target: Drawing<N, E>;
  commit: () => void;
  generation: number;
  /** Its move ended: the drawing waits, in place, for React's render of the target. */
  done: boolean;
}

/** The last painted frame: a change and its eased progress (null: at rest, as React drew it). */
interface PaintedAt<N, E> {
  change: Change<N, E>;
  t: number;
}

interface LoopDom {
  rendered: Map<string, TweenNode>;
  nodeEls: Map<string, SVGElement[]>;
  edgeEls: Map<string, SVGElement[]>;
}

/**
 * Where everything is drawn now. Computed once when a change starts, never
 * per frame: a frame keeps its progress only.
 */
function drawnFrom<N extends TweenNode, E extends TweenEdge>(
  at: PaintedAt<N, E> | null,
  rest: Drawing<N, E> | null,
): DrawnState {
  if (at) {
    const frame = layoutFrame(at.change.start, at.change.target, at.change.leaving, at.t, keyOf);
    return drawnState(frame.nodes, frame.edges, keyOf);
  }
  return drawnState(rest?.nodes ?? [], rest?.edges ?? [], keyOf);
}

/**
 * Drawn now and not in the target: fades out from where it is. A leaving
 * element of an interrupted change keeps its key; one that faded out is gone.
 */
function leavingOf<N extends TweenNode, E extends TweenEdge>(
  drawn: Drawing<N, E>,
  wasLeaving: Drawing<N, E> | undefined,
  start: DrawnState,
  target: Drawing<N, E>,
  generation: number,
): Drawing<N, E> {
  const targetIds = new Set(target.nodes.map((n) => n.id));
  const targetKeys = new Set(target.edges.map(keyOf));
  const isPainted = (fade: number | undefined) => (fade ?? 0) > 0.01;
  return {
    nodes: [...drawn.nodes, ...(wasLeaving?.nodes ?? [])].filter(
      (n) => !targetIds.has(n.id) && isPainted(start.fades.get(n.id)),
    ),
    edges: [
      ...drawn.edges.map((e) => ({ ...e, id: `${e.id}-leaving-${generation}` })),
      ...(wasLeaving?.edges ?? []),
    ].filter((e) => !targetKeys.has(e.tweenKey) && isPainted(start.edgeFades.get(e.tweenKey))),
  };
}

/**
 * Draws a deterministic graph layout with every change animated: each
 * time the target nodes or edges move, appear or go away, the drawing moves
 * from what is on screen to the target in LAYOUT_TWEEN_MS, eased cubic in-out
 * (agent-graph-tween.ts).
 *
 * Two ways in. A new target rendered by React plans its change in that render
 * (edge keys, start, what leaves) and the loop starts at its commit. A target
 * handed to `preview` (a layout click) starts the loop at once on the DOM
 * React drew last, and reaches React only when the move ends, so the click
 * waits for no render and no render runs during the move.
 *
 * Every frame is written straight to the DOM under `svgRef`: a transform and
 * an opacity on each node wrapper (TWEEN_NODE_ATTR), the `d` and an opacity on
 * each edge path (TWEEN_EDGE_ATTR), opacities scaled from BASE_OPACITY_ATTR;
 * a value equal to the one written last is not written again. A target with
 * the same placement (running state, a radius at a new zoom) is drawn at once.
 * With `disabled` (reduced motion) every change is drawn at once.
 */
export function useAnimatedLayout<N extends TweenNode, E extends TweenEdge>(
  targetNodes: N[],
  targetEdges: E[],
  disabled: boolean,
  svgRef: RefObject<SVGSVGElement | null>,
): AnimatedLayout<N, E> {
  const [stats, setStats] = useState({ fps: 0, worstFrameMs: 0, firstFrameMs: 0 });
  // The plan last committed (what React drew), and the one whose change ended.
  const committed = useRef<Plan<N, E> | null>(null);
  const [ended, setEnded] = useState<Plan<N, E> | null>(null);
  const paintedAt = useRef<PaintedAt<N, E> | null>(null);
  const raf = useRef<number | null>(null);
  // The change the frame loop is drawing, and its rank.
  const looping = useRef<Change<N, E> | null>(null);
  const loopGeneration = useRef(-1);
  const previewing = useRef<Preview<N, E> | null>(null);
  // What React drew last (leaving elements included) and the DOM the loop paints.
  const drawnRows = useRef<{ nodes: Array<N & Leaving>; edges: Array<Keyed<E> & Leaving> } | null>(null);
  const loopDom = useRef<LoopDom | null>(null);

  useEffect(() => () => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
  }, []);

  const plan = useMemo<Plan<N, E>>(() => {
    const edges = withKeys(targetEdges);
    const target = { nodes: targetNodes, edges };
    const prev = committed.current;
    const pre = previewing.current;
    const atRest = { nodes: targetNodes, edges, change: null, generation: prev?.generation ?? 0 };
    if (!prev || disabled || typeof requestAnimationFrame !== "function") return atRest;
    const running = looping.current;
    if (pre && samePlacement(pre.target, target)) {
      // The target a preview moved (or moves) the drawing to.
      if (pre.done || !running) return { ...atRest, generation: pre.generation };
      return { nodes: targetNodes, edges, change: { ...running, target }, generation: pre.generation };
    }
    if (samePlacement(prev, target)) {
      // Same places: a change under way keeps running and ends on this target.
      if (!running || !prev.change) return atRest;
      return { ...prev, nodes: targetNodes, edges, change: { ...prev.change, target } };
    }
    const start = drawnFrom(paintedAt.current, prev);
    const next = Math.max(prev.generation, pre?.generation ?? 0, loopGeneration.current) + 1;
    const wasLeaving = running?.leaving;
    const leaving = leavingOf(prev, wasLeaving, start, target, next);
    return { nodes: targetNodes, edges, change: { start, target, leaving, askedAt: changeAskedAt() }, generation: next };
  }, [targetNodes, targetEdges, disabled]);
  // A change is drawn until its loop ends it.
  const animating = plan.change !== null && ended?.generation !== plan.generation;
  const shown = useMemo(() => {
    if (!animating || !plan.change) {
      return { nodes: plan.nodes as Array<N & Leaving>, edges: plan.edges as Array<Keyed<E> & Leaving> };
    }
    const { leaving } = plan.change;
    return {
      nodes: [...plan.nodes, ...leaving.nodes.map((n) => ({ ...n, leaving: true }))] as Array<N & Leaving>,
      edges: [...plan.edges, ...leaving.edges.map((e) => ({ ...e, leaving: true }))] as Array<Keyed<E> & Leaving>,
    };
  }, [plan, animating]);

  // One frame loop, whichever way the change came in: paints the start at
  // once, then one frame per animation frame, and ends on the target.
  const runLoop = (change: Change<N, E>, generation: number) => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
    looping.current = change;
    loopGeneration.current = generation;
    const written = new WeakMap<Element, Map<string, string>>();
    const paint = (t: number) => {
      const now = looping.current ?? change;
      const dom = loopDom.current;
      if (dom) {
        const frame = layoutFrame(now.start, now.target, now.leaving, t, keyOf);
        paintFrame(frame, dom, written);
      }
      paintedAt.current = { change: now, t };
    };
    paint(0);
    let begin: number | null = null;
    let last: number | null = null;
    let worst = 0;
    let frames = 0;
    let firstFrame = 0;
    const seenAt = change.askedAt;
    const step = (time: number) => {
      // The clock starts on the first frame the browser paints.
      if (begin === null) {
        begin = time;
        // performance.now(), not the frame's time stamp: Chrome stamps a
        // frame with its vsync, which can precede a long task that ran in it.
        firstFrame = seenAt > 0 ? Math.max(1, Math.round(performance.now() - seenAt)) : 0;
      }
      if (last !== null) worst = Math.max(worst, time - last);
      last = time;
      const elapsed = time - begin;
      frames += 1;
      if (elapsed < LAYOUT_TWEEN_MS) {
        paint(easeCubicInOut(elapsed / LAYOUT_TWEEN_MS));
        raf.current = requestAnimationFrame(step);
        return;
      }
      paint(1);
      raf.current = null;
      const done = looping.current ?? change;
      looping.current = null;
      paintedAt.current = { change: done, t: 1 };
      const result = { fps: frameRate(frames, elapsed), worstFrameMs: Math.round(worst), firstFrameMs: firstFrame };
      const pre = previewing.current;
      if (pre && pre.generation === generation && !pre.done) {
        pre.done = true;
        const held = committed.current;
        if (held && !held.change && samePlacement(held, pre.target)) {
          // Back to the drawing React holds (a click undone mid-move): no
          // render follows, so the loop settles the drawing itself.
          previewing.current = null;
          paintedAt.current = null;
          const svg = svgRef.current;
          if (svg) settle(svg);
          pre.commit();
          setStats(result);
          return;
        }
        // React draws the target once, now, as a low-priority update.
        startTransition(() => {
          setStats(result);
          setEnded(committed.current);
          pre.commit();
        });
        return;
      }
      setEnded(committed.current);
      setStats(result);
    };
    raf.current = requestAnimationFrame(step);
  };

  // After each commit: start the loop of a change React planned, or let one
  // that runs go on with the new DOM; at rest, clear what the loop wrote.
  useLayoutEffect(() => {
    committed.current = plan;
    drawnRows.current = shown;
    const svg = svgRef.current;
    const pre = previewing.current;
    const run = animating ? plan.change : null;
    if (!run) {
      if (pre && !pre.done) {
        // A preview runs on the DOM React kept: it goes on.
        if (svg) loopDom.current = domOf(svg, shown.nodes);
        return;
      }
      if (raf.current !== null) {
        cancelAnimationFrame(raf.current);
        raf.current = null;
      }
      looping.current = null;
      previewing.current = null;
      paintedAt.current = null;
      if (svg) settle(svg);
      return;
    }
    if (!svg) return;
    if (pre && pre.generation !== plan.generation) previewing.current = null;
    svg.pauseAnimations?.();
    loopDom.current = domOf(svg, shown.nodes);
    // The same change with a new target or new DOM: the loop runs on.
    if (raf.current !== null && loopGeneration.current === plan.generation) {
      looping.current = run;
      return;
    }
    runLoop(run, plan.generation);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- shown follows plan and animating; runLoop uses refs only
  }, [plan, animating, svgRef]);

  const preview = (nodes: N[], edges: E[], commit: () => void): boolean => {
    const svg = svgRef.current;
    const prev = committed.current;
    if (!svg || !prev || disabled || typeof requestAnimationFrame !== "function") return false;
    const target = { nodes, edges: withKeys(edges) };
    if (!looping.current && samePlacement(prev, target)) return false;
    const pre = previewing.current;
    const start = drawnFrom(paintedAt.current, prev);
    const generation = Math.max(prev.generation, pre?.generation ?? 0, loopGeneration.current) + 1;
    const drawn = drawnRows.current ?? { nodes: prev.nodes as Array<N & Leaving>, edges: prev.edges as Array<Keyed<E> & Leaving> };
    const leaving = leavingOf<N, E>(
      { nodes: drawn.nodes.filter((n) => !n.leaving), edges: drawn.edges.filter((e) => !e.leaving) },
      looping.current?.leaving,
      start,
      target,
      generation,
    );
    // A newer preview replaces one under way: only its target reaches React.
    previewing.current = { target, commit, generation, done: false };
    svg.setAttribute(ANIMATING_ATTR, "true");
    svg.pauseAnimations?.();
    loopDom.current = domOf(svg, drawn.nodes);
    runLoop({ start, target, leaving, askedAt: changeAskedAt() }, generation);
    return true;
  };

  return { nodes: shown.nodes, edges: shown.edges, animating, ...stats, preview };
}

/**
 * When the change being drawn was asked for: the time stamp of the input
 * event in flight (same clock as performance.now), else now.
 */
function changeAskedAt(): number {
  const clickedAt = typeof window !== "undefined" ? window.event?.timeStamp : undefined;
  if (clickedAt !== undefined && clickedAt > 0) return clickedAt;
  return typeof performance !== "undefined" ? performance.now() : 0;
}

function domOf(svg: SVGSVGElement, rendered: readonly TweenNode[]): LoopDom {
  return {
    rendered: new Map(rendered.map((n) => [n.id, n])),
    nodeEls: byAttr(svg, TWEEN_NODE_ATTR),
    edgeEls: byAttr(svg, TWEEN_EDGE_ATTR),
  };
}

function byAttr(svg: SVGSVGElement, attr: string): Map<string, SVGElement[]> {
  const out = new Map<string, SVGElement[]>();
  svg.querySelectorAll<SVGElement>(`[${attr}]`).forEach((el) => {
    const id = el.getAttribute(attr);
    if (id === null) return;
    const list = out.get(id);
    if (list) list.push(el);
    else out.set(id, [el]);
  });
  return out;
}

function baseOpacity(el: Element): number | null {
  const raw = el.getAttribute(BASE_OPACITY_ATTR);
  if (raw === null) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** Writes an attribute unless the loop already wrote that value on it. */
function writeOnce(el: Element, attr: string, value: string, written: WeakMap<Element, Map<string, string>>): void {
  let seen = written.get(el);
  if (!seen) {
    seen = new Map();
    written.set(el, seen);
  }
  if (seen.get(attr) === value) return;
  seen.set(attr, value);
  el.setAttribute(attr, value);
}

function setFade(el: Element, fade: number, written?: WeakMap<Element, Map<string, string>>): void {
  const base = baseOpacity(el);
  // A hit-area path carries no base opacity: it stays as React drew it.
  if (base === null) return;
  const value = String(Math.round(base * fade * 1000) / 1000);
  if (written) writeOnce(el, "opacity", value, written);
  else el.setAttribute("opacity", value);
}

const r2 = (v: number) => Math.round(v * 100) / 100;

function paintFrame<N extends TweenNode, E extends TweenEdge>(
  frame: { nodes: Array<N & Faded>; edges: Array<Keyed<E> & Faded> },
  dom: LoopDom,
  written: WeakMap<Element, Map<string, string>>,
): void {
  for (const node of frame.nodes) {
    const els = dom.nodeEls.get(node.id);
    const at = dom.rendered.get(node.id);
    if (!els || !at) continue;
    // The node's body is drawn about its own origin, for its rendered radius:
    // move it to the frame's point and scale it by the frame's radius.
    const s = at.r > 0 ? r2(node.r / at.r) : 1;
    const transform = s === 1 ? `translate(${r2(node.x)} ${r2(node.y)})` : `translate(${r2(node.x)} ${r2(node.y)}) scale(${s})`;
    for (const el of els) {
      writeOnce(el, "transform", transform, written);
      setFade(el, node.fade, written);
    }
  }
  for (const edge of frame.edges) {
    const els = dom.edgeEls.get(edge.tweenKey);
    if (!els) continue;
    const d = edge.arc
      ? `M ${r2(edge.arc.a.x)},${r2(edge.arc.a.y)} Q ${r2(edge.arc.control.x)},${r2(edge.arc.control.y)} ${r2(edge.arc.b.x)},${r2(edge.arc.b.y)}`
      : edge.path;
    for (const el of els) {
      writeOnce(el, "d", d, written);
      setFade(el, edge.fade, written);
    }
  }
}

/** A change ended: opacities at rest, the animating flag off, SMIL decorations running again. */
function settle(svg: SVGSVGElement): void {
  clearTween(svg);
  // A preview set the flag itself: React, which never drew it, never clears it.
  svg.removeAttribute(ANIMATING_ATTR);
  svg.unpauseAnimations?.();
}

/**
 * After a change: every opacity is back at rest. The node transforms stay:
 * the last frame left each node at its rendered place (`translate(x y)`, the
 * transform React draws), and React does not rewrite an unchanged prop.
 */
function clearTween(svg: SVGSVGElement): void {
  svg.querySelectorAll(`[${TWEEN_NODE_ATTR}]`).forEach((el) => setFade(el, 1));
  svg.querySelectorAll(`[${TWEEN_EDGE_ATTR}]`).forEach((el) => setFade(el, 1));
}
