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

// One popover for the handover type of an agent graph edge: the type
// picker of a new edge and the editor of an existing one share its header,
// option rows and footer, so a width, string or contrast fix lands once.

import { useCallback, useEffect, useRef } from "react";

import { useFormatter } from "@/i18n";
import { handoverTypeConfig } from "@/lib/constants";

// The design-system keyboard ring: the focus now enters the popover from the keyboard.
const FOCUS_RING = "outline-none focus-visible:ring-2 focus-visible:ring-focus";

/** A type the popover offers: its key, and the English label and description read through `t_i18n`. */
export interface HandoverTypeOption {
  key: string;
  label: string;
  desc: string;
}

/** The box, in the graph container's coordinates, of what opened the popover from the keyboard. */
export interface PopoverAvoidBox {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

/** Inputs of {@link placePopover}, all in the graph container's coordinates. */
export interface PopoverPlacementInput {
  /** The anchor: the pointer, or the middle of the box opened from the keyboard. */
  x: number;
  y: number;
  width: number;
  height: number;
  containerWidth: number;
  containerHeight: number;
  /** Kept clear when set: the popover never intersects this box when the canvas has room for it. */
  avoid?: PopoverAvoidBox;
  /**
   * Kept clear as well when a placement allows it: the end nodes of an
   * edge opened from the keyboard. `avoid` stays the box that must never be covered.
   */
  alsoAvoid?: PopoverAvoidBox[];
  pad?: number;
}

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, Math.max(min, max)));

const intersects = (left: number, top: number, width: number, height: number, b: PopoverAvoidBox) =>
  left < b.right && left + width > b.left && top < b.bottom && top + height > b.top;

const union = (boxes: PopoverAvoidBox[]): PopoverAvoidBox => ({
  top: Math.min(...boxes.map((b) => b.top)),
  bottom: Math.max(...boxes.map((b) => b.bottom)),
  left: Math.min(...boxes.map((b) => b.left)),
  right: Math.max(...boxes.map((b) => b.right)),
});

/**
 * Where the popover goes. Without a box to keep
 * clear it hangs under the anchor, centred and clamped inside the container. With
 * one it goes below the box, else above it, else beside it (right, then left),
 * vertically centred on the box and clamped: on a short canvas the popover fits
 * neither below nor above, and a bottom clamp alone would move it back over the box.
 * With `alsoAvoid` (the end nodes of an edge) the first of those places,
 * taken around all the boxes and then around `avoid`, that fits the container and
 * covers none of them wins. A popover taller than the container then tries the
 * places beside those boxes with its top at `pad`; only when none
 * clears them all is `avoid` alone kept clear.
 */
export function placePopover({
  x, y, width, height, containerWidth, containerHeight, avoid, alsoAvoid = [], pad = 8,
}: PopoverPlacementInput): { left: number; top: number } {
  const maxLeft = containerWidth - width - pad;
  const maxTop = containerHeight - height - pad;
  const centredLeft = clamp(x - width / 2, pad, maxLeft);
  if (!avoid) return { left: centredLeft, top: clamp(y + pad, pad, maxTop) };
  if (alsoAvoid.length > 0) {
    const boxes = [avoid, ...alsoAvoid];
    const fits = (left: number, top: number) =>
      left >= pad && top >= pad && left + width <= containerWidth - pad && top + height <= containerHeight - pad
      && boxes.every((b) => !intersects(left, top, width, height, b));
    for (const ref of [union(boxes), avoid]) {
      const lefts = [centredLeft, clamp(ref.left, pad, maxLeft), clamp(ref.right - width, pad, maxLeft)];
      const midTop = clamp((ref.top + ref.bottom) / 2 - height / 2, pad, maxTop);
      const candidates: Array<[number, number]> = [
        ...lefts.map((l): [number, number] => [l, ref.bottom + pad]),
        ...lefts.map((l): [number, number] => [l, ref.top - pad - height]),
        [ref.right + pad, midTop],
        [ref.left - pad - width, midTop],
      ];
      const found = candidates.find(([l, t]) => fits(l, t));
      if (found) return { left: found[0], top: found[1] };
    }
    // Taller than the canvas (1024): no place fits vertically,
    // so the beside places are tried with the top at `pad` and only the width contained.
    if (height > containerHeight - 2 * pad) {
      const fitsBeside = (left: number) =>
        left >= pad && left + width <= containerWidth - pad
        && boxes.every((b) => !intersects(left, pad, width, height, b));
      for (const ref of [union(boxes), avoid]) {
        const beside = [ref.right + pad, ref.left - pad - width].find(fitsBeside);
        if (beside !== undefined) return { left: beside, top: pad };
      }
    }
  }
  const below = avoid.bottom + pad;
  if (below + height <= containerHeight - pad) return { left: centredLeft, top: below };
  const above = avoid.top - pad - height;
  if (above >= pad) return { left: centredLeft, top: above };
  const top = clamp((avoid.top + avoid.bottom) / 2 - height / 2, pad, maxTop);
  const right = avoid.right + pad;
  const left = avoid.left - pad - width;
  if (right + width <= containerWidth - pad) return { left: right, top };
  if (left >= pad) return { left, top };
  // Room on neither side: the wider side, clamped, so the least of the box is covered.
  const roomRight = containerWidth - avoid.right;
  return { left: clamp(roomRight >= avoid.left ? right : left, pad, maxLeft), top };
}

export interface HandoverTypePopoverProps {
  /** The data attribute the graph and its tests tell the two uses apart by. */
  kind: "type-picker" | "edge-editor";
  /** The header, already translated. */
  title: string;
  options: HandoverTypeOption[];
  selectedType: string;
  /** Anchor in the graph container's coordinates; the place callback centres and clamps it. */
  x: number;
  y: number;
  /** Kept clear when set: the popover goes below this box, or above it, never over it. */
  avoid?: PopoverAvoidBox;
  /** Kept clear as well when the canvas allows it: the edge's end nodes. */
  alsoAvoid?: PopoverAvoidBox[];
  /** Width class, wide enough for the longest translated footer. */
  widthClass: string;
  /** Dot colour of an option with no type colour of its own (all types). */
  fallbackColor: string;
  /** Opened from the keyboard: the focus moves to the first option. */
  autoFocus?: boolean;
  /** Places the popover inside the graph once it is drawn (reads the `data-avoid-*` box; see {@link placePopover}). */
  place: (el: HTMLDivElement | null) => void;
  onSelect: (key: string) => void;
  onCancel: () => void;
  onSave: () => void;
  /** Shown as a Delete button at the start of the footer when set (the edge editor). */
  onDelete?: () => void;
}

export function HandoverTypePopover({
  kind, title, options, selectedType, x, y, avoid, alsoAvoid, widthClass, fallbackColor, autoFocus,
  place, onSelect, onCancel, onSave, onDelete,
}: HandoverTypePopoverProps) {
  const { t_i18n } = useFormatter();
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Stable, so the placement runs once per opening and never shifts a placed popover again.
  const attach = useCallback((el: HTMLDivElement | null) => {
    rootRef.current = el;
    place(el);
  }, [place]);

  useEffect(() => {
    if (!autoFocus) return;
    rootRef.current?.querySelector<HTMLButtonElement>("[data-handover-option]")?.focus();
  }, [autoFocus]);

  const stop = (e: React.SyntheticEvent, then: () => void) => {
    e.stopPropagation();
    then();
  };

  // Tab and Shift+Tab stay inside the popover while it is open:
  // the focus wraps from the last button to the first and back; Escape closes it.
  const keepFocusInside = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Tab") return;
    const buttons = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("button:not([disabled])")];
    if (buttons.length === 0) return;
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !e.currentTarget.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !e.currentTarget.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      {...{ [`data-${kind}`]: "true" }}
      ref={attach}
      role="dialog"
      aria-label={title}
      aria-modal="true"
      data-avoid-top={avoid?.top}
      data-avoid-bottom={avoid?.bottom}
      data-avoid-left={avoid?.left}
      data-avoid-right={avoid?.right}
      data-avoid-also={avoid && alsoAvoid?.length ? JSON.stringify(alsoAvoid) : undefined}
      onKeyDown={keepFocusInside}
      className={`absolute z-50 ${widthClass} rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay animate-in fade-in-0 zoom-in-95`}
      style={{ left: x, top: y }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="px-3 py-2 border-b border-elevation-subtle-soft">
        <p className="content-compact font-medium text-default-secondary">{title}</p>
      </div>
      <div className="py-1">
        {options.map((opt) => {
          const colour = handoverTypeConfig(opt.key)?.edgeColor ?? fallbackColor;
          const isSelected = selectedType === opt.key;
          return (
            <button
              key={opt.key}
              type="button"
              data-handover-option={opt.key}
              aria-pressed={isSelected}
              className={`w-full px-3 py-2 text-left transition-colors flex items-center gap-2 ${FOCUS_RING} focus-visible:ring-inset ${
                isSelected ? "bg-elevation-hover" : "hover:bg-elevation-hover"
              }`}
              onClick={(e) => stop(e, () => onSelect(opt.key))}
            >
              <span
                className="h-2 w-2 rounded-full flex-shrink-0"
                style={{ background: colour, boxShadow: isSelected ? `0 0 6px ${colour}` : undefined }}
              />
              <div className="flex-1">
                <p className="content-compact font-medium text-default-secondary">{t_i18n(opt.label)}</p>
                <p className="content-caption text-default-secondary">{t_i18n(opt.desc)}</p>
              </div>
              {isSelected && <div className="h-1.5 w-1.5 rounded-full bg-accent" />}
            </button>
          );
        })}
      </div>
      <div
        className={`border-t border-elevation-subtle-soft px-3 py-2 flex items-center gap-2 ${
          onDelete ? "justify-between" : "justify-end"
        }`}
      >
        {onDelete && (
          // Error text at 4.5:1 on the popover in both themes:
          // error-primary painted 4.26:1 on bg-elevation-default in dark; error-tertiary
          // is the light red in dark and the deep red in light.
          <button
            type="button"
            data-tone="error"
            className={`rounded-sm px-3 py-1.5 content-compact text-feedback-error-tertiary hover:text-feedback-error-tertiary hover:bg-feedback-error-primary/10 transition-colors ${FOCUS_RING}`}
            onClick={(e) => stop(e, onDelete)}
          >
            {t_i18n("Delete")}
          </button>
        )}
        <div className="flex items-center gap-2">
          <button
            type="button"
            className={`rounded-sm px-3 py-1.5 content-compact text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors ${FOCUS_RING}`}
            onClick={(e) => stop(e, onCancel)}
          >
            {t_i18n("Cancel")}
          </button>
          <button
            type="button"
            className={`rounded-sm px-3 py-1.5 content-compact font-medium text-negative-primary bg-accent hover:bg-accent/90 transition-colors ${FOCUS_RING}`}
            onClick={(e) => stop(e, onSave)}
          >
            {t_i18n("Save")}
          </button>
        </div>
      </div>
    </div>
  );
}
