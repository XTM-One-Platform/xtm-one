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
 * The native `positioning_map` block: a competitive landscape drawn by the
 * deck - two real axes with arrowheads, their titles and end labels, four
 * named quadrants over a faint tint, and every item placed at its x / y
 * (0-100) with its mark at ONE size (a square box, the mark at one optical
 * weight) and its name beside it, never over another name.
 *
 * Sizing contract: the plot's box comes from the stylesheet alone (the
 * column's width, a 4:3 ratio, capped by the bucket's height), never from
 * what is drawn in it, so the map renders once and stays still - two maps in
 * a two-column slide settle in one pass. The box is read back (canvas px) only
 * to place the labels (`layoutPositioningMap`).
 *
 * Export: the plot is plain HTML - the tints and the axes are boxes, the
 * labels text, each mark ONE picture (`LogoMark`'s `data-export-picture`), so
 * the PPTX carries the map as editable shapes; the arrowheads are small SVGs.
 * A label that found no free side is counted on `data-map-overlaps` for the
 * layout check.
 *
 * Differentiation: an item's `note` is its hover title (never drawn, so the
 * plot stays still); the block's `callouts` (1-4) are drawn under the map or
 * beside it, saying why the highlight sits where it does.
 */
import { useMemo, useRef, type CSSProperties } from "react";

import type {
  DeckMapAxis,
  DeckMapCallout,
  DeckMapItem,
  DeckMapQuadrant,
  DeckPositioningMapBlock,
} from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { markupToPlainText } from "../markup";
import { useSettledBox } from "../measured-box";
import { LogoMark } from "./logos";
import { MAP_HIGHLIGHT_MARK_PX, MAP_MARK_PX, layoutPositioningMap } from "./positioning-map-layout";

type Corner = "top_left" | "top_right" | "bottom_left" | "bottom_right";
const CORNERS: Corner[] = ["top_left", "top_right", "bottom_left", "bottom_right"];

/**
 * A quadrant's tint and its label. Once the plot is measured the label sits
 * where the layout put it (its outer corner unless a mark or an end label
 * covers it), in px of the quadrant's own box.
 */
function Quadrant({
  corner,
  quadrant,
  placed,
}: {
  corner: Corner;
  quadrant: DeckMapQuadrant | undefined;
  placed: { left: number; top: number; overlap: boolean } | undefined;
}) {
  if (!quadrant) return null;
  return (
    <div className="deck-map-quadrant" data-corner={corner} data-tone={quadrant.tone} data-color={quadrant.color}>
      <span
        className="deck-map-quadrant-label"
        data-placed={placed ? "true" : undefined}
        data-overlap={placed?.overlap ? "true" : undefined}
        style={placed ? { left: `${placed.left}px`, top: `${placed.top}px` } : undefined}
      >
        <InlineMarkup value={quadrant.label} />
      </span>
    </div>
  );
}

function Arrow({ direction }: { direction: "right" | "up" }) {
  return (
    <svg
      className="deck-map-arrow"
      data-direction={direction}
      data-export-alone="true"
      viewBox="0 0 12 12"
      aria-hidden="true"
    >
      <path d={direction === "right" ? "M1 1 L11 6 L1 11 Z" : "M1 11 L6 1 L11 11 Z"} />
    </svg>
  );
}

function AxisEnds({ axis, which }: { axis: DeckMapAxis; which: "x" | "y" }) {
  return (
    <>
      {axis.low_label ? (
        <span className="deck-map-end" data-axis={which} data-end="low">
          <InlineMarkup value={axis.low_label} />
        </span>
      ) : null}
      {axis.high_label ? (
        <span className="deck-map-end" data-axis={which} data-end="high">
          <InlineMarkup value={axis.high_label} />
        </span>
      ) : null}
    </>
  );
}

function ItemMark({ item, size }: { item: DeckMapItem; size: number }) {
  if (item.logo_file_id) {
    return (
      <LogoMark
        item={{ file_id: item.logo_file_id, label: item.label }}
        look={{ heightPx: Math.round(size * 0.72), balance: true, weight: true }}
      />
    );
  }
  if (item.icon) return <DeckIcon name={item.icon} size={Math.round(size * 0.6)} className="deck-map-icon" />;
  return <span className="deck-map-dot" aria-hidden="true" />;
}

function Callouts({ callouts }: { callouts: DeckMapCallout[] }) {
  return (
    <ul className="deck-map-callouts">
      {callouts.map((callout, index) => (
        <li key={index} className="deck-map-callout">
          <span className="deck-map-callout-title">
            <InlineMarkup value={callout.title} />
          </span>
          {callout.text ? (
            <span className="deck-map-callout-text">
              <InlineMarkup value={callout.text} />
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function PositioningMapBlockView({ block }: { block: DeckPositioningMapBlock }) {
  const plotRef = useRef<HTMLDivElement>(null);
  // The plot's room is CSS alone; a room that keeps moving is frozen and
  // named (`data-layout-unsettled`), never chased.
  const { box, unsettled } = useSettledBox(plotRef, block.items.length);
  const inputs = useMemo(
    () =>
      block.items.map((item) => ({
        x: item.x,
        y: item.y,
        label: markupToPlainText(item.label),
        sublabel: item.sublabel ? markupToPlainText(item.sublabel) : undefined,
        highlight: item.emphasis === "highlight",
      })),
    [block.items],
  );
  const axes = block.axes ?? "center";
  const frame = useMemo(() => {
    const plain = (value: DeckPositioningMapBlock["title"]) => (value ? markupToPlainText(value) : undefined);
    const quadrants: Partial<Record<Corner, string>> = {};
    for (const corner of CORNERS) {
      const label = plain(block.quadrants?.[corner]?.label);
      if (label) quadrants[corner] = label;
    }
    return {
      axes,
      quadrants,
      ends: {
        xLow: plain(block.x_axis.low_label),
        xHigh: plain(block.x_axis.high_label),
        yLow: plain(block.y_axis.low_label),
        yHigh: plain(block.y_axis.high_label),
      },
    };
  }, [axes, block.quadrants, block.x_axis, block.y_axis]);
  const layout = useMemo(
    () => (box ? layoutPositioningMap(inputs, box.width, box.height, frame) : null),
    [inputs, box, frame],
  );
  const quadrantPlace = (corner: Corner) => {
    const placed = layout?.quadrants[corner];
    if (!placed || !box) return undefined;
    // The quadrant's own box starts at the plot's centre on its right / lower side.
    const left = placed.rect.x - (corner.endsWith("right") ? box.width / 2 : 0);
    const top = placed.rect.y - (corner.startsWith("bottom") ? box.height / 2 : 0);
    return { left, top, overlap: placed.overlap };
  };
  const description = `${markupToPlainText(block.x_axis.title)} / ${markupToPlainText(block.y_axis.title)}`;
  const callouts = block.callouts ?? [];
  return (
    <figure
      className="deck-positioning-map"
      {...useBlockEditAttrs()}
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-axes={axes}
      data-map-overlaps={layout ? String(layout.overlaps) : undefined}
      data-layout-unsettled={unsettled ? "true" : undefined}
      data-callouts={callouts.length ? (block.callouts_position ?? "below") : undefined}
      aria-label={description}
    >
      {block.title ? (
        <figcaption className="deck-map-title">
          <InlineMarkup value={block.title} />
        </figcaption>
      ) : null}
      <div className="deck-map-body">
      <div className="deck-map-frame">
        <div className="deck-map-y-title">
          <span>
            <InlineMarkup value={block.y_axis.title} />
          </span>
          {/* Edges: the y end labels sit outside the plot, at the two ends of
              the y title's column, so no mark in a corner can cover them. */}
          {axes === "edges" ? <AxisEnds axis={block.y_axis} which="y" /> : null}
        </div>
        <div className="deck-map-plot" ref={plotRef}>
          {CORNERS.map((corner) => (
            <Quadrant key={corner} corner={corner} quadrant={block.quadrants?.[corner]} placed={quadrantPlace(corner)} />
          ))}
          <div className="deck-map-axis" data-axis="x">
            <Arrow direction="right" />
          </div>
          <div className="deck-map-axis" data-axis="y">
            <Arrow direction="up" />
          </div>
          <AxisEnds axis={block.x_axis} which="x" />
          {axes === "edges" ? null : <AxisEnds axis={block.y_axis} which="y" />}
          {block.items.map((item, index) => {
            const placed = layout?.items[index];
            const size = item.emphasis === "highlight" ? MAP_HIGHLIGHT_MARK_PX : MAP_MARK_PX;
            const markStyle = {
              "--map-x": `${Math.min(100, Math.max(0, item.x))}%`,
              "--map-y": `${100 - Math.min(100, Math.max(0, item.y))}%`,
              "--map-mark": `${size}px`,
              ...(placed ? { left: `${placed.cx}px`, top: `${placed.cy}px` } : {}),
            } as CSSProperties;
            return (
              <div
                key={index}
                className="deck-map-item"
                data-emphasis={item.emphasis ?? "normal"}
                title={item.note || undefined}
              >
                <div className="deck-map-mark" style={markStyle}>
                  <ItemMark item={item} size={size} />
                </div>
                {placed ? (
                  <div
                    className="deck-map-label"
                    data-side={placed.side}
                    data-overlap={placed.overlap ? "true" : undefined}
                    style={{ left: `${placed.label.x}px`, top: `${placed.label.y}px`, width: `${placed.label.w}px` }}
                  >
                    <span className="deck-map-name">
                      <InlineMarkup value={item.label} />
                    </span>
                    {item.sublabel ? (
                      <span className="deck-map-sub">
                        <InlineMarkup value={item.sublabel} />
                      </span>
                    ) : null}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
        <div className="deck-map-x-title">
          <InlineMarkup value={block.x_axis.title} />
        </div>
      </div>
      {callouts.length ? <Callouts callouts={callouts} /> : null}
      </div>
      {block.caption ? (
        <p className="deck-map-caption">
          <InlineMarkup value={block.caption} />
        </p>
      ) : null}
    </figure>
  );
}
