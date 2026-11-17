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
 * A dashboard tab drawn as a canvas (an `absolute` layout, `canvas.ts`): its
 * widgets and elements at their pixels, scaled to the container by the
 * canvas's display mode (or the studio's zoom), over its background.
 *
 * In edit mode (the studio) every item is picked by a click (Shift or
 * Ctrl/Cmd adds to the selection) or a marquee drawn from the empty canvas,
 * dragged to move, resized by its handles; a line's ends are dragged too and
 * snap to the middle of a side of a block they come near, which holds them
 * there. Keys on the focused canvas: arrows move by 10 px (Shift: 1 px), Alt +
 * arrows resize, Delete removes, Escape clears the selection, Ctrl/Cmd+A
 * selects everything; the rest (copy, paste, duplicate, undo, redo) goes to
 * the studio (`onShortcut`). The stage shows a gesture as it goes and hands
 * the change over once, when it ends (`onChange`): one undo step per gesture.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import { WidgetSizeContext } from "@/components/dashboard-grid/widget-size-context";
import { useAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { dashboardAssetUrl, useDashboardAccess } from "@/hooks/api/dashboard-access";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import {
  FINE_NUDGE,
  HANDLES,
  NUDGE,
  blockMap,
  canvasScale,
  cellOfBox,
  endPoint,
  itemsIn,
  moveItems,
  nearestPort,
  removeItems,
  resizeBox,
  resizeItems,
  setBox,
  setLineEnd,
  shiftItems,
  withBox,
  withLineEnd,
  type Box,
  type CanvasItem,
  type CanvasModel,
  type ElementKind,
  type Handle,
  type LineEnd,
  type Point,
} from "./canvas";
import { CanvasElementView, CanvasLineView } from "./canvas-elements";
import type { DashboardWidget } from "./spec";

type Definition = Record<string, unknown>;

/** What the studio lets the person do to a canvas. */
export interface CanvasEditing {
  layoutId: string;
  selection: readonly string[];
  onSelect: (ids: string[]) => void;
  /** A change made on the stage, as a change of the definition: one undo step. */
  onChange: (update: (definition: Definition) => Definition) => void;
  /** A key the stage does not handle itself (copy, paste, duplicate, undo, redo). */
  onShortcut?: (event: KeyboardEvent) => void;
  /** The editor's zoom; `null` shows the canvas as its display mode says. */
  zoom: number | null;
}

/** How each element is called, for its accessible name. @i18n-keys */
const ELEMENT_LABELS = {
  rectangle: "Rectangle",
  ellipse: "Ellipse",
  line: "Line",
  icon: "Icon",
  image: "Image",
  markdown: "Markdown",
} as const satisfies Record<ElementKind, string>;

/** How near a port a dragged line end snaps to it, in screen pixels. */
const SNAP_REACH = 16;
/** A pointer that moved less than this (screen pixels) clicked. */
const CLICK_SLOP = 3;

type Gesture =
  | { kind: "move"; ids: Set<string>; start: Point; dx: number; dy: number }
  | { kind: "resize"; id: string; handle: Handle; start: Point; box: Box; current: Box }
  | { kind: "end"; id: string; which: "from" | "to"; current: Point; snap: { item: string; port: "n" | "e" | "s" | "w" } | null }
  | { kind: "marquee"; origin: Point; current: Point; additive: boolean; startClient: Point };

const HANDLE_PLACE: Record<Handle, string> = {
  n: "left-1/2 top-0 -translate-x-1/2 -translate-y-1/2 cursor-ns-resize",
  ne: "right-0 top-0 translate-x-1/2 -translate-y-1/2 cursor-nesw-resize",
  e: "right-0 top-1/2 translate-x-1/2 -translate-y-1/2 cursor-ew-resize",
  se: "right-0 bottom-0 translate-x-1/2 translate-y-1/2 cursor-nwse-resize",
  s: "left-1/2 bottom-0 -translate-x-1/2 translate-y-1/2 cursor-ns-resize",
  sw: "left-0 bottom-0 -translate-x-1/2 translate-y-1/2 cursor-nesw-resize",
  w: "left-0 top-1/2 -translate-x-1/2 -translate-y-1/2 cursor-ew-resize",
  nw: "left-0 top-0 -translate-x-1/2 -translate-y-1/2 cursor-nwse-resize",
};

/** The items as a gesture under way would leave them. */
function previewItems(items: CanvasItem[], gesture: Gesture | null): CanvasItem[] {
  if (gesture?.kind === "move") return shiftItems(items, gesture.ids, gesture.dx, gesture.dy);
  if (gesture?.kind === "resize") return withBox(items, gesture.id, gesture.current);
  if (gesture?.kind === "end") return withLineEnd(items, gesture.id, gesture.which, gesture.snap ?? gesture.current);
  return items;
}

/** The width of the container, and the height left in the window below its top. */
function useViewport(ref: React.RefObject<HTMLElement | null>) {
  const [size, setSize] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      const height = Math.max(200, window.innerHeight - Math.max(0, rect.top) - 16);
      setSize((current) => (current.width === rect.width && current.height === height ? current : { width: rect.width, height }));
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(element);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [ref]);
  return size;
}

function CanvasBackground({ fileId, sizeType, opacity, dashboardId }: { fileId: string; sizeType: "auto" | "contain" | "cover"; opacity: number; dashboardId?: string }) {
  const access = useDashboardAccess();
  const src = useAuthenticatedSrc(dashboardId ? dashboardAssetUrl(access, dashboardId, fileId) : null);
  if (!src) return null;
  return (
    <img
      src={src}
      alt=""
      aria-hidden="true"
      draggable={false}
      className={cn(
        "pointer-events-none absolute left-0 top-0",
        sizeType === "auto" ? "max-w-none" : "h-full w-full",
        sizeType === "contain" && "object-contain",
        sizeType === "cover" && "object-cover",
      )}
      style={{ opacity }}
    />
  );
}

export function DashboardCanvas({
  canvas,
  widgets,
  renderWidget,
  editing,
  dashboardId,
  label,
}: {
  canvas: CanvasModel;
  /** The tab's widgets by id: a block that is no element draws one. */
  widgets: ReadonlyMap<string, DashboardWidget>;
  renderWidget: (widget: DashboardWidget) => ReactNode;
  editing?: CanvasEditing | null;
  dashboardId?: string;
  /** The canvas's accessible name (the tab). */
  label: string;
}) {
  const { t_i18n } = useFormatter();
  const outer = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const viewport = useViewport(outer);
  const { options } = canvas;
  const scale = canvasScale(options, viewport, editing?.zoom ?? null);
  const [gesture, setGesture] = useState<Gesture | null>(null);
  const selected = useMemo(() => new Set(editing?.selection ?? []), [editing?.selection]);

  // A new canvas (another tab, a save) ends whatever gesture was under way.
  useEffect(() => setGesture(null), [canvas]);

  const items = previewItems(canvas.items, gesture);
  const blocks = blockMap(items);

  /** A pointer's place on the canvas, in canvas pixels. */
  const toCanvas = useCallback(
    (clientX: number, clientY: number): Point => {
      const rect = inner.current?.getBoundingClientRect();
      if (!rect) return { x: 0, y: 0 };
      return { x: (clientX - rect.left) / scale, y: (clientY - rect.top) / scale };
    },
    [scale],
  );

  const capture = (event: ReactPointerEvent) => {
    outer.current?.focus({ preventScroll: true });
    try {
      outer.current?.setPointerCapture(event.pointerId);
    } catch {
      // A synthetic pointer (a test) has no capture to take.
    }
  };

  const startMove = (event: ReactPointerEvent, id: string) => {
    if (!editing || event.button !== 0) return;
    event.stopPropagation();
    const additive = event.shiftKey || event.metaKey || event.ctrlKey;
    let ids: string[];
    if (additive) {
      ids = selected.has(id) ? [...selected].filter((other) => other !== id) : [...selected, id];
      editing.onSelect(ids);
      if (!ids.includes(id)) return;
    } else {
      ids = selected.has(id) ? [...selected] : [id];
      if (!selected.has(id)) editing.onSelect(ids);
    }
    capture(event);
    setGesture({ kind: "move", ids: new Set(ids), start: { x: event.clientX, y: event.clientY }, dx: 0, dy: 0 });
  };

  const startResize = (event: ReactPointerEvent, item: CanvasItem, handle: Handle) => {
    if (!editing || item.type !== "block" || event.button !== 0) return;
    event.stopPropagation();
    capture(event);
    const box = { x: item.x, y: item.y, w: item.w, h: item.h };
    setGesture({ kind: "resize", id: item.id, handle, start: { x: event.clientX, y: event.clientY }, box, current: box });
  };

  const startEnd = (event: ReactPointerEvent, id: string, which: "from" | "to", at: Point) => {
    if (!editing || event.button !== 0) return;
    event.stopPropagation();
    capture(event);
    setGesture({ kind: "end", id, which, current: at, snap: null });
  };

  const startMarquee = (event: ReactPointerEvent) => {
    if (!editing || event.button !== 0) return;
    capture(event);
    const at = toCanvas(event.clientX, event.clientY);
    setGesture({
      kind: "marquee",
      origin: at,
      current: at,
      additive: event.shiftKey || event.metaKey || event.ctrlKey,
      startClient: { x: event.clientX, y: event.clientY },
    });
  };

  const onPointerMove = (event: ReactPointerEvent) => {
    if (!gesture) return;
    if (gesture.kind === "move") {
      setGesture({ ...gesture, dx: Math.round((event.clientX - gesture.start.x) / scale), dy: Math.round((event.clientY - gesture.start.y) / scale) });
    } else if (gesture.kind === "resize") {
      const dx = (event.clientX - gesture.start.x) / scale;
      const dy = (event.clientY - gesture.start.y) / scale;
      setGesture({ ...gesture, current: resizeBox(gesture.box, gesture.handle, dx, dy) });
    } else if (gesture.kind === "end") {
      const at = toCanvas(event.clientX, event.clientY);
      const current = { x: Math.round(at.x), y: Math.round(at.y) };
      setGesture({ ...gesture, current, snap: nearestPort(canvas.items, current, SNAP_REACH / scale) });
    } else {
      setGesture({ ...gesture, current: toCanvas(event.clientX, event.clientY) });
    }
  };

  const onPointerUp = (event: ReactPointerEvent) => {
    if (!gesture || !editing) return;
    setGesture(null);
    const { layoutId } = editing;
    if (gesture.kind === "move") {
      const { dx, dy, ids } = gesture;
      if (dx !== 0 || dy !== 0) editing.onChange((definition) => moveItems(definition, layoutId, [...ids], dx, dy));
    } else if (gesture.kind === "resize") {
      if (JSON.stringify(gesture.current) !== JSON.stringify(gesture.box)) {
        editing.onChange((definition) => setBox(definition, layoutId, gesture.id, gesture.current));
      }
    } else if (gesture.kind === "end") {
      const end: LineEnd = gesture.snap ?? gesture.current;
      editing.onChange((definition) => setLineEnd(definition, layoutId, gesture.id, gesture.which, end));
    } else {
      const moved = Math.hypot(event.clientX - gesture.startClient.x, event.clientY - gesture.startClient.y) > CLICK_SLOP;
      if (!moved) {
        if (!gesture.additive) editing.onSelect([]);
        return;
      }
      const area = {
        x: Math.min(gesture.origin.x, gesture.current.x),
        y: Math.min(gesture.origin.y, gesture.current.y),
        w: Math.abs(gesture.origin.x - gesture.current.x),
        h: Math.abs(gesture.origin.y - gesture.current.y),
      };
      const picked = itemsIn(canvas.items, area);
      editing.onSelect(gesture.additive ? [...new Set([...selected, ...picked])] : picked);
    }
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (!editing) return;
    const ids = [...selected];
    const { layoutId } = editing;
    const arrow = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (arrow && ids.length > 0) {
      event.preventDefault();
      const step = event.shiftKey ? FINE_NUDGE : NUDGE;
      const [x, y] = arrow;
      editing.onChange((definition) =>
        event.altKey ? resizeItems(definition, layoutId, ids, x * step, y * step) : moveItems(definition, layoutId, ids, x * step, y * step),
      );
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && ids.length > 0) {
      event.preventDefault();
      editing.onChange((definition) => removeItems(definition, ids));
      editing.onSelect([]);
      return;
    }
    if (event.key === "Escape" && ids.length > 0) {
      event.preventDefault();
      event.stopPropagation();
      editing.onSelect([]);
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
      event.preventDefault();
      editing.onSelect(canvas.items.map((item) => item.id));
      return;
    }
    editing.onShortcut?.(event);
  };

  const labelOf = (item: CanvasItem): string => {
    if (item.type === "line") return t_i18n(ELEMENT_LABELS.line);
    if (item.element) return t_i18n(ELEMENT_LABELS[item.element.kind]);
    return widgets.get(item.id)?.title ?? item.id;
  };

  const single = editing && selected.size === 1 ? [...selected][0] : null;
  const outline = `${2 / scale}px solid var(--color-accent)`;
  const handleSize = 10 / scale;

  return (
    <div
      ref={outer}
      className={cn(
        "relative w-full",
        options.display === "actual-size" && !editing?.zoom ? "overflow-auto" : "overflow-hidden",
        editing && "overflow-auto rounded-sm outline-offset-2 focus-visible:outline-2 focus-visible:outline-focus",
      )}
      role={editing ? "application" : "region"}
      aria-label={label}
      aria-roledescription={editing ? t_i18n("Absolute layout") : undefined}
      tabIndex={editing ? 0 : undefined}
      onKeyDown={editing ? onKeyDown : undefined}
      onPointerMove={editing ? onPointerMove : undefined}
      onPointerUp={editing ? onPointerUp : undefined}
      onPointerCancel={() => setGesture(null)}
      data-testid="dashboard.canvas"
    >
      <div
        // An outline, not a border: it takes no room, so a fitted layout never scrolls.
        className={cn("relative", editing && "outline-1 -outline-offset-1 outline-dashed outline-[var(--border-elevation-subtle)]")}
        style={{ width: options.width * scale, height: options.height * scale }}
      >
        <div
          ref={inner}
          className="absolute left-0 top-0 origin-top-left overflow-hidden"
          style={{
            width: options.width,
            height: options.height,
            transform: `scale(${scale})`,
            ...(options.backgroundColor ? { backgroundColor: options.backgroundColor } : {}),
          }}
          onPointerDown={editing ? startMarquee : undefined}
          data-canvas-surface=""
        >
          {options.backgroundImage && <CanvasBackground {...options.backgroundImage} dashboardId={dashboardId} />}
          {items.map((item) => {
            if (item.type === "line") {
              const a = endPoint(item.from, blocks);
              const b = endPoint(item.to, blocks);
              const isSelected = selected.has(item.id);
              return (
                <div key={item.id} className="contents" data-canvas-item={item.id}>
                  <CanvasLineView
                    id={item.id}
                    element={item.element}
                    a={a}
                    b={b}
                    hit={editing ? { selected: isSelected, onPointerDown: (event) => startMove(event, item.id) } : undefined}
                  />
                  {editing && (
                    <button
                      type="button"
                      className="sr-only"
                      aria-label={labelOf(item)}
                      aria-pressed={isSelected}
                      onClick={(event) => editing.onSelect(event.shiftKey ? [...selected, item.id] : [item.id])}
                    />
                  )}
                  {single === item.id &&
                    (["from", "to"] as const).map((which) => {
                      const at = which === "from" ? a : b;
                      return (
                        <span
                          key={which}
                          className="absolute z-10 -translate-x-1/2 -translate-y-1/2 cursor-crosshair rounded-full border border-accent bg-elevation-default"
                          style={{ left: at.x, top: at.y, width: handleSize * 1.4, height: handleSize * 1.4 }}
                          onPointerDown={(event) => startEnd(event, item.id, which, at)}
                          data-canvas-end={which}
                          aria-hidden="true"
                        />
                      );
                    })}
                </div>
              );
            }
            const widget = item.element ? undefined : widgets.get(item.id);
            const isSelected = selected.has(item.id);
            return (
              <div
                key={item.id}
                className="absolute"
                style={{ left: item.x, top: item.y, width: item.w, height: item.h }}
                data-canvas-item={item.id}
              >
                <div className={cn("h-full w-full", editing && "pointer-events-none select-none")}>
                  {item.element ? (
                    <CanvasElementView element={item.element} w={item.w} h={item.h} />
                  ) : widget ? (
                    // The widget sizes itself to its box, as it does to a grid cell.
                    <WidgetSizeContext.Provider value={cellOfBox(item)}>{renderWidget(widget)}</WidgetSizeContext.Provider>
                  ) : null}
                </div>
                {editing && (
                  <div
                    role="button"
                    tabIndex={0}
                    aria-label={labelOf(item)}
                    aria-pressed={isSelected}
                    className="absolute inset-0 cursor-move focus-visible:outline-none"
                    style={isSelected ? { outline } : undefined}
                    onPointerDown={(event) => startMove(event, item.id)}
                    onKeyDown={(event) => {
                      if ((event.key === "Enter" || event.key === " ") && event.target === event.currentTarget) {
                        event.preventDefault();
                        editing.onSelect(event.shiftKey ? [...selected, item.id] : [item.id]);
                      }
                    }}
                  />
                )}
                {single === item.id &&
                  HANDLES.map((handle) => (
                    <span
                      key={handle}
                      className={cn("absolute z-10 border border-accent bg-elevation-default", HANDLE_PLACE[handle])}
                      style={{ width: handleSize, height: handleSize }}
                      onPointerDown={(event) => startResize(event, item, handle)}
                      data-canvas-handle={handle}
                      aria-hidden="true"
                    />
                  ))}
              </div>
            );
          })}
          {gesture?.kind === "marquee" && (
            <div
              className="pointer-events-none absolute border border-accent bg-accent/10"
              style={{
                left: Math.min(gesture.origin.x, gesture.current.x),
                top: Math.min(gesture.origin.y, gesture.current.y),
                width: Math.abs(gesture.origin.x - gesture.current.x),
                height: Math.abs(gesture.origin.y - gesture.current.y),
              }}
              data-testid="dashboard.canvas.marquee"
            />
          )}
        </div>
      </div>
    </div>
  );
}
