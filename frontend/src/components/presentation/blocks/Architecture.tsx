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
 * The native `architecture` diagram block.
 *
 * The agent hands over DATA (nodes on a grid, labeled edges, zones, a legend);
 * `architecture-layout.ts` turns it into geometry and this component draws that
 * geometry as inline SVG THROUGH REACT — every glyph is a `<text>` in the deck's
 * own fonts, every colour a deck token, every shape a `<rect>` / `<circle>` /
 * `<path>`. There is no markup sink anywhere: no raw-HTML prop, no embedded
 * HTML island inside the SVG, nothing the spec could smuggle a tag through.
 *
 * Vector all the way down: the same drawing scales with the canvas on screen,
 * prints as vectors in the PDF and is measured glyph by glyph by the layout
 * check (SVG `<text>` is what `LAYOUT_AUDIT_JS` reads). Product logos come from
 * the template's predefined assets (`logo_file_id`, resolved like every other
 * deck image); icons from the shared Lucide catalog.
 *
 * Interactive in the live deck only: hovering or focusing a node lights up its
 * edges and neighbours and dims the rest, a click pins that state, Escape or a
 * click on the background clears it, and a node with a `description` shows it
 * in a small card. None of this exists for the export (no pointer, no focus).
 *
 * Sizing contract: the stage is the drawing's box and the `<svg>` is
 * absolutely positioned inside it, so the drawing's own size never feeds back
 * into the box it is measured from. The stage publishes the drawing's canvas
 * width (`--deck-architecture-w`) and its natural ratio
 * (`--deck-architecture-ratio`, width over the UNCOMPACTED height) and the
 * stylesheet sizes the box from them: as wide as the column up to the canvas
 * width, as tall as the bucket allows, the drawing never magnified - a row is
 * as tall as its content, whatever the column count. In `fill` / `full` the
 * flex column hands the stage its height, as it does a chart's canvas. The box
 * is read back with a ResizeObserver and handed to the layout as its room,
 * which compacts the rows before the slide's shrink-to-fit sees anything.
 */
import {
  createElement,
  useCallback,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

import type { DeckArchitectureBlock, DeckAssetMeta } from "@/types";

import { useDeckAsset, useDeckAssetMeta, useDeckAssetMetaMap } from "../asset-context";
import { useBrandPicture } from "../brand-picture";
import { resolveDeckIcon } from "../deck-icons";
import { useBlockEditAttrs } from "../edit-context";
import { useGiveUp } from "../figure-wait";
import { InlineMarkup } from "../inline-markup";
import { LOGO_HEIGHT_PX, trimBox } from "../logo-sizing";
import { markupToPlainText } from "../markup";
import { useSettledBox } from "../measured-box";
import { useDeckIconCatalog } from "../useDeckIconCatalog";
import {
  ARCH,
  layoutArchitecture,
  type ArchitectureLayout,
  type LayoutEdge,
  type LayoutNode,
  type LayoutZone,
  type LegendItemLayout,
  type Rect,
  type TextBlock,
} from "./architecture-layout";

type NodeState = "focus" | "linked" | "dim" | undefined;
type EdgeState = "linked" | "dim" | undefined;

// ── Primitives ────────────────────────────────────────────────────────────────

/** A measured text block as SVG `<text>` lines; runs keep their inline markup. */
function SvgText({
  block,
  x,
  y,
  anchor = "middle",
  className,
}: {
  block: TextBlock;
  x: number;
  /** Top of the block; each line is centred in its line box. */
  y: number;
  anchor?: "start" | "middle";
  className: string;
}) {
  return (
    <>
      {block.lines.map((line, index) => (
        <text
          key={index}
          className={className}
          x={x}
          y={y + index * block.lineHeight + block.lineHeight / 2}
          textAnchor={anchor}
          dominantBaseline="central"
          fontSize={block.fontSize}
          xmlSpace="preserve"
        >
          {line.runs.map((run, runIndex) => (
            <tspan
              key={runIndex}
              className={run.code ? "deck-arch-code" : undefined}
              data-tone={run.tone}
              fontWeight={run.bold ? 700 : undefined}
              fontStyle={run.em ? "italic" : undefined}
            >
              {run.text}
            </tspan>
          ))}
        </text>
      ))}
    </>
  );
}

/** A Lucide icon drawn inside the SVG, with a placeholder box until the catalog chunk lands (`useDeckIconCatalog`). Shared with the org chart. */
export function SvgIcon({ name, x, y, size }: { name: string; x: number; y: number; size: number }) {
  const placeholder = useRef<SVGRectElement>(null);
  const { catalog, waiting } = useDeckIconCatalog(placeholder, true);
  if (!catalog) {
    return (
      <rect
        ref={placeholder}
        className="deck-arch-icon-pending"
        x={x}
        y={y}
        width={size}
        height={size}
        rx={6}
        data-deck-pending={waiting ? "true" : undefined}
        data-deck-unfinished={waiting ? undefined : "true"}
        aria-hidden="true"
      />
    );
  }
  const icon = resolveDeckIcon(catalog, name);
  if (!icon) return null;
  return createElement(icon, {
    className: "deck-arch-icon",
    x,
    y,
    size,
    strokeWidth: 1.75,
    "aria-hidden": "true",
  });
}

/** The file's own box, in the units of its stored size, with the viewBox that shows only its mark (its trim box). */
interface LogoCrop {
  width: number;
  height: number;
  viewBox: string;
}

/** How to crop a logo file to its mark: from its stored geometry, or `undefined` when its size is not known. */
function logoCrop(meta: DeckAssetMeta | undefined): LogoCrop | undefined {
  if (!meta?.w || !meta?.h) return undefined;
  const [x0, y0, x1, y1] = trimBox(meta);
  const { w, h } = meta;
  const f = (n: number) => Number(n.toFixed(3));
  return { width: w, height: h, viewBox: `${f(x0 * w)} ${f(y0 * h)} ${f((x1 - x0) * w)} ${f((y1 - y0) * h)}` };
}

/**
 * A template asset (a product logo) drawn as an SVG `<image>`. With a `crop`
 * the file is laid out at its stored size inside a nested `<svg>` whose
 * viewBox is the mark, so the transparent margin of the file is cut and the
 * mark itself meets the box.
 *
 * The template's own mark or logo reads like the chrome reads it
 * (`brand-picture.ts`): the scheme's variant, and on a template that inks its
 * mark, the mark as the ALPHA MASK of a rect filled with `--deck-emblem-ink`.
 * The image itself stays, transparent, as what the export waits for.
 */
function SvgLogo({
  fileId: placedFileId,
  x,
  y,
  width,
  height,
  align,
  revision,
  crop,
}: {
  fileId: string;
  x: number;
  y: number;
  width: number;
  height: number;
  align: "start" | "middle";
  revision?: string;
  crop?: LogoCrop;
}) {
  const { fileId, inked } = useBrandPicture(placedFileId);
  const maskId = `deck-arch-logo-mask-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const { src, loading } = useDeckAsset(fileId, revision);
  const placeholder = useRef<SVGRectElement>(null);
  const image = useRef<SVGImageElement>(null);
  // What the image reported, for the source it reported about: a new source
  // is waited for again.
  const [settledSrc, setSettledSrc] = useState<string | null>(null);
  const settled = src !== null && settledSrc === src;
  const onSettled = useCallback(() => setSettledSrc(src), [src]);
  // Neither the on-screen fetch of the file nor the image's bytes report
  // anything while they run: each stands as it is after `FIGURE_WAIT_MS`.
  // A fetch that failed is not loading, so a deleted file holds
  // nothing.
  const fetchGaveUp = useGiveUp(placeholder, loading ? "fetch" : null);
  const bytesGaveUp = useGiveUp(image, src && !settled ? src : null);
  if (loading) {
    return (
      <rect
        ref={placeholder}
        className="deck-arch-logo-pending"
        x={x}
        y={y}
        width={width}
        height={height}
        data-deck-pending={fetchGaveUp ? undefined : "true"}
        data-deck-unfinished={fetchGaveUp ? "true" : undefined}
        aria-hidden="true"
      />
    );
  }
  if (!src) return null;
  const aspect = align === "start" ? "xMinYMid meet" : "xMidYMid meet";
  // The export capture waits for `<img>` on its own but not for an SVG
  // image; the marker keeps it waiting until the bytes are painted.
  const ready = {
    "data-deck-pending": settled || bytesGaveUp ? undefined : "true",
    "data-deck-unfinished": bytesGaveUp ? "true" : undefined,
    onLoad: onSettled,
    onError: onSettled,
  };
  // The inked mark: the same image is the mask, the rect under it the ink.
  // The ink and the mask region are both bounded to `paint` (the shown box:
  // the crop's viewBox, else the logo box): a mask region left to its default
  // (120% of the viewport) and an ink rect wider than the crop let the PDF
  // rasteriser paint a hairline of ink at the crop's edge.
  const ink = (
    box: { x?: number; y?: number; width: number; height: number },
    ratio: string,
    paint: { x: number; y: number; width: number; height: number },
  ) =>
    inked ? (
      <>
        <mask id={maskId} style={{ maskType: "alpha" }} maskUnits="userSpaceOnUse" {...paint}>
          <image href={src} {...box} preserveAspectRatio={ratio} />
        </mask>
        <rect className="deck-arch-logo-ink" {...paint} mask={`url(#${maskId})`} data-brand-ink="true" />
      </>
    ) : null;
  if (crop) {
    const box = { width: crop.width, height: crop.height };
    const [cx, cy, cw, ch] = crop.viewBox.split(" ").map(Number);
    return (
      <svg x={x} y={y} width={width} height={height} viewBox={crop.viewBox} preserveAspectRatio={aspect}>
        <image
          ref={image}
          className="deck-arch-logo"
          href={src}
          {...box}
          preserveAspectRatio="none"
          opacity={inked ? 0 : undefined}
          {...ready}
        />
        {ink(box, "none", { x: cx, y: cy, width: cw, height: ch })}
      </svg>
    );
  }
  const box = { x, y, width, height };
  return (
    <>
      <image
        ref={image}
        className="deck-arch-logo"
        href={src}
        {...box}
        preserveAspectRatio={aspect}
        opacity={inked ? 0 : undefined}
        {...ready}
      />
      {ink(box, aspect, box)}
    </>
  );
}

// ── Pieces ────────────────────────────────────────────────────────────────────

/** A zone's logo: the platform's mark, cropped to its ink, at the start of the title row. */
function ZoneLogo({ fileId, box }: { fileId: string; box: Rect }) {
  // The geometry of the file drawn: a template mark may be swapped for the scheme's.
  const meta = useDeckAssetMeta(useBrandPicture(fileId).fileId);
  return (
    <SvgLogo
      fileId={fileId}
      revision={meta?.rev}
      crop={logoCrop(meta)}
      x={box.x}
      y={box.y}
      width={box.w}
      height={box.h}
      align="start"
    />
  );
}

function ZoneView({ zone }: { zone: LayoutZone }) {
  const { color, tone, logo_file_id: logoFileId } = zone.zone;
  return (
    <g
      className="deck-arch-zone"
      data-tone={color ? undefined : tone}
      data-color={color}
      data-chips-overflow={zone.chipsOverflow > 0 ? Math.ceil(zone.chipsOverflow) : undefined}
    >
      <rect className="deck-arch-zone-panel" x={zone.x} y={zone.y} width={zone.w} height={zone.h} rx={22} />
      {zone.logo && logoFileId ? (
        // Keyed on the file id, as a node's logo: a swapped logo waits for its own bytes.
        <ZoneLogo key={logoFileId} fileId={logoFileId} box={zone.logo} />
      ) : zone.zone.icon ? (
        <SvgIcon name={zone.zone.icon} x={zone.iconX} y={zone.titleY - ARCH.ZONE_ICON / 2} size={ARCH.ZONE_ICON} />
      ) : null}
      {zone.title ? (
        <SvgText
          block={zone.title}
          x={zone.titleX}
          y={zone.titleY - zone.title.lineHeight / 2}
          anchor="start"
          className="deck-arch-zone-title"
        />
      ) : null}
      {zone.chips.map((chip, index) => (
        <g key={index} className="deck-arch-chip deck-arch-zone-chip">
          <rect x={chip.x} y={chip.y} width={chip.w} height={chip.h} rx={chip.h / 2} />
          <SvgText block={chip.text} x={chip.x + chip.w / 2} y={chip.y} className="deck-arch-chip-text" />
        </g>
      ))}
    </g>
  );
}

function EdgeView({ edge, state, markerId }: { edge: LayoutEdge; state: EdgeState; markerId: (tone: string) => string }) {
  const marker = `url(#${markerId(edge.tone)})`;
  return (
    <g className="deck-arch-edge" data-tone={edge.tone} data-dashed={edge.dashed ? "true" : undefined} data-state={state}>
      <path
        className="deck-arch-edge-line"
        d={edge.d}
        markerEnd={edge.arrows === "none" ? undefined : marker}
        markerStart={edge.arrows === "both" ? marker : undefined}
      />
      {edge.label ? (
        <g className="deck-arch-edge-label">
          <rect x={edge.label.x} y={edge.label.y} width={edge.label.w} height={edge.label.h} rx={edge.label.h / 2} />
          <SvgText
            block={edge.label.text}
            x={edge.label.x + edge.label.w / 2}
            y={edge.label.y}
            className="deck-arch-edge-text"
          />
        </g>
      ) : null}
    </g>
  );
}

/** A node logo sized by `logo_height`: cropped to its ink like a zone's mark, so the mark itself meets the box. */
function SizedNodeLogo({ fileId, box, align }: { fileId: string; box: Rect; align: "start" | "middle" }) {
  const meta = useDeckAssetMeta(useBrandPicture(fileId).fileId);
  return (
    <SvgLogo
      fileId={fileId}
      x={box.x}
      y={box.y}
      width={box.w}
      height={box.h}
      align={align}
      revision={meta?.rev}
      crop={logoCrop(meta)}
    />
  );
}

function NodeMedia({ node }: { node: LayoutNode }) {
  const box = node.mediaBox;
  const drawn = node.mediaRect;
  if (!box || !drawn || !node.media) return null;
  if (node.media === "logo" && node.node.logo_file_id && node.node.logo_height && node.kind !== "pill" && node.kind !== "circle") {
    // Drawn at its own rect: a row's sized cards share one media box, each mark keeps its balanced height in it.
    // The step asked (slide px) and the height drawn (canvas px) are published for the layout check, which
    // names a mark its column could not draw at its step once the drawing is fitted to the slide.
    return (
      <g className="deck-arch-logo-sized" data-logo-step={LOGO_HEIGHT_PX[node.node.logo_height]} data-logo-drawn={drawn.h.toFixed(1)}>
        <SizedNodeLogo
          key={node.node.logo_file_id}
          fileId={node.node.logo_file_id}
          box={drawn}
          align={node.kind === "band" ? "start" : "middle"}
        />
      </g>
    );
  }
  if (node.media === "logo" && node.node.logo_file_id) {
    // Keyed on the file id like `SlideImage`: a Studio edit that swaps the
    // logo on the same node mounts a fresh, pending `<image>` instead of
    // inheriting the settled state of the one it replaces.
    return (
      <SvgLogo
        key={node.node.logo_file_id}
        fileId={node.node.logo_file_id}
        x={box.x}
        y={box.y}
        width={box.w}
        height={box.h}
        align={node.kind === "band" ? "start" : "middle"}
      />
    );
  }
  if (node.media === "icon" && node.node.icon) {
    if (node.mediaTile) {
      // The icon keeps its share of the tile when a mixed logo / icon row grows the tile.
      const size = (drawn.w * ARCH.ICON) / ARCH.ICON_TILE;
      const inset = (drawn.w - size) / 2;
      return (
        <>
          <rect className="deck-arch-node-tile" x={drawn.x} y={drawn.y} width={drawn.w} height={drawn.h} rx={14} />
          <SvgIcon name={node.node.icon} x={drawn.x + inset} y={drawn.y + inset} size={size} />
        </>
      );
    }
    // Bare: a pill's and a circle's icon, and a card's or band's with
    // `icon_tile: false`, drawn at the icon's own size where the layout put it.
    return <SvgIcon name={node.node.icon} x={drawn.x} y={drawn.y} size={drawn.w} />;
  }
  return null;
}

/** How far a frameless node's hit area reaches past its content: the pointer target, and the keyboard focus ring. */
const BARE_HIT_PAD = 10;

function NodeView({
  node,
  state,
  pinned,
  onHover,
  onToggle,
}: {
  node: LayoutNode;
  state: NodeState;
  pinned: boolean;
  onHover: (node: LayoutNode | null, element: SVGGElement | null) => void;
  onToggle: (node: LayoutNode, element: SVGGElement) => void;
}) {
  const { w, h } = node;
  const isCircle = node.kind === "circle";
  // The hover / focus detail card is `pointer-events: none` and transient, so
  // the description is also exposed as the node's SVG `<desc>`: a
  // screen reader gets it on focus, and the visual card stays a visual.
  const description = markupToPlainText(node.node.description);
  const uid = useId();
  const descId = `${uid.replace(/[^a-zA-Z0-9_-]/g, "")}-desc`;
  return (
    <g
      className="deck-arch-node"
      transform={`translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`}
      data-shape={node.kind}
      data-emphasis={node.emphasis}
      data-frame={node.frame ? undefined : "false"}
      data-tone={node.color ? undefined : (node.tone ?? (node.emphasis === "core" ? "accent" : undefined))}
      data-color={node.color}
      data-state={state}
      tabIndex={0}
      role="button"
      aria-label={node.ariaLabel}
      aria-describedby={description ? descId : undefined}
      aria-pressed={pinned}
      onPointerEnter={(event) => onHover(node, event.currentTarget)}
      onPointerLeave={() => onHover(null, null)}
      onFocus={(event) => onHover(node, event.currentTarget)}
      onBlur={() => onHover(null, null)}
      onClick={(event: MouseEvent<SVGGElement>) => {
        event.stopPropagation();
        onToggle(node, event.currentTarget);
      }}
      onKeyDown={(event: KeyboardEvent<SVGGElement>) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onToggle(node, event.currentTarget);
        }
      }}
    >
      {description ? <desc id={descId}>{description}</desc> : null}
      {!node.frame ? (
        // No shape, no glow: a transparent hit area around the content takes
        // the pointer and shows the keyboard focus, and paints nothing else.
        <rect
          className="deck-arch-node-hit"
          x={node.content.x - BARE_HIT_PAD}
          y={node.content.y - BARE_HIT_PAD}
          width={node.content.w + 2 * BARE_HIT_PAD}
          height={node.content.h + 2 * BARE_HIT_PAD}
          rx={12}
        />
      ) : isCircle ? (
        <>
          {node.emphasis === "core" ? (
            <circle className="deck-arch-node-glow" cx={w / 2} cy={h / 2} r={w / 2} aria-hidden="true" />
          ) : null}
          <circle className="deck-arch-node-shape" cx={w / 2} cy={h / 2} r={w / 2} />
        </>
      ) : (
        <rect className="deck-arch-node-shape" width={w} height={h} rx={node.radius} />
      )}
      <NodeMedia node={node} />
      <SvgText
        block={node.label}
        x={node.textAnchor === "start" ? node.labelBox.x : node.labelBox.x + node.labelBox.w / 2}
        y={node.kind === "pill" ? (h - node.label.lineHeight) / 2 : node.labelBox.y}
        anchor={node.textAnchor}
        className="deck-arch-node-label"
      />
      {node.sublabel && node.sublabelBox ? (
        <SvgText
          block={node.sublabel}
          x={node.textAnchor === "start" ? node.sublabelBox.x : node.sublabelBox.x + node.sublabelBox.w / 2}
          y={node.kind === "pill" ? (h - node.sublabel.lineHeight) / 2 : node.sublabelBox.y}
          anchor={node.textAnchor}
          className="deck-arch-node-sublabel"
        />
      ) : null}
      {node.chips.map((chip, index) => (
        <g key={index} className="deck-arch-chip">
          <rect x={chip.x} y={chip.y} width={chip.w} height={ARCH.CHIP_H} rx={ARCH.CHIP_H / 2} />
          <SvgText block={chip.text} x={chip.x + chip.w / 2} y={chip.y} className="deck-arch-chip-text" />
        </g>
      ))}
    </g>
  );
}

function LegendMarker({ item, markerId }: { item: LegendItemLayout; markerId: (tone: string) => string }) {
  const cy = item.y + ARCH.LEGEND_H / 2;
  const x1 = item.x;
  const x2 = item.x + ARCH.LEGEND_MARKER_W;
  switch (item.marker) {
    case "node":
      return <rect className="deck-arch-legend-node" x={x1 + 10} y={cy - 12} width={24} height={24} rx={7} />;
    case "dashed":
      return <line className="deck-arch-legend-line" x1={x1} y1={cy} x2={x2} y2={cy} strokeDasharray="10 8" />;
    case "line":
      return <line className="deck-arch-legend-line" x1={x1} y1={cy} x2={x2} y2={cy} />;
    default:
      return (
        <line
          className="deck-arch-legend-line"
          x1={x1}
          y1={cy}
          x2={x2 - 4}
          y2={cy}
          markerEnd={`url(#${markerId(item.tone)})`}
        />
      );
  }
}

// ── The block ─────────────────────────────────────────────────────────────────

function nodeState(active: string | null, id: string, layout: ArchitectureLayout): NodeState {
  if (!active) return undefined;
  if (active === id) return "focus";
  return layout.neighbours.get(active)?.has(id) ? "linked" : "dim";
}

function edgeState(active: string | null, edge: LayoutEdge): EdgeState {
  if (!active) return undefined;
  return edge.edge.source === active || edge.edge.target === active ? "linked" : "dim";
}

/** The active node plus where its detail card goes, in percent of the stage. */
interface Active {
  id: string;
  x: number;
  y: number;
  placement: "above" | "below";
}

/**
 * Widest the detail card gets, in canvas px, and the gutter it keeps from the
 * stage edge when the stage is narrower than that. Mirrors
 * `.deck-architecture-detail { max-width: min(380px, calc(100% - 2 * 16px)) }`.
 */
const DETAIL_MAX_WIDTH = 380;
const DETAIL_GUTTER = 16;

/**
 * Measured from the live boxes rather than from the layout: in `fill` mode the
 * SVG is letterboxed inside the stage, so viewBox coordinates alone would put
 * the card beside the node instead of under it. Ratios of client rects are
 * scale-free, which is what makes them right inside the deck's transform.
 */
function measureActive(node: LayoutNode, element: SVGGElement, stage: HTMLDivElement | null): Active {
  const fallback: Active = { id: node.id, x: 50, y: 50, placement: "below" };
  if (!stage) return fallback;
  const s = stage.getBoundingClientRect();
  const n = element.getBoundingClientRect();
  if (!s.width || !s.height) return fallback;
  const cx = (n.left + n.width / 2 - s.left) / s.width;
  const below = (n.top + n.height / 2 - s.top) / s.height < 0.5;
  const edge = below ? (n.bottom - s.top) / s.height : (n.top - s.top) / s.height;
  // Keep the card on the stage, a gutter from its edge; `offsetWidth` is in
  // canvas px, untouched by the deck's scale transform. The card is as wide
  // as the CSS lets it be: 380px, or the stage minus the gutters on a narrow
  // drawing (where the clamp collapses to the centre).
  const stageWidth = Math.max(1, stage.offsetWidth);
  const cardWidth = Math.max(0, Math.min(DETAIL_MAX_WIDTH, stageWidth - 2 * DETAIL_GUTTER));
  const half = Math.min(0.5, (DETAIL_GUTTER + cardWidth / 2) / stageWidth);
  const x = Math.max(half, Math.min(1 - half, cx));
  return { id: node.id, x: x * 100, y: edge * 100 + (below ? 1.5 : -1.5), placement: below ? "below" : "above" };
}

export function ArchitectureBlockView({ block }: { block: DeckArchitectureBlock }) {
  const stageRef = useRef<HTMLDivElement>(null);
  const assets = useDeckAssetMetaMap();
  // The stage's box is the drawing's room (see the sizing contract above). Its
  // height follows the drawing's ratio, and in a `columns` slide its width
  // follows the slide's fit, so the room can feed back into itself: the box is
  // frozen once it keeps moving (`useSettledBox`, #11580) and the figure says
  // so (`data-layout-unsettled`) for the layout check to name it.
  const settleKey = useMemo(() => ({ block, assets }), [block, assets]);
  const { box: room, unsettled } = useSettledBox(stageRef, settleKey);
  const layout = useMemo(() => layoutArchitecture(block, { room, assets }), [block, room, assets]);
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const markerId = useCallback((tone: string) => `deck-arch-${uid}-${tone}`, [uid]);
  const [hovered, setHovered] = useState<Active | null>(null);
  const [pinned, setPinned] = useState<Active | null>(null);

  const onHover = useCallback((node: LayoutNode | null, element: SVGGElement | null) => {
    setHovered(node && element ? measureActive(node, element, stageRef.current) : null);
  }, []);
  const onToggle = useCallback((node: LayoutNode, element: SVGGElement) => {
    setPinned((current) => (current?.id === node.id ? null : measureActive(node, element, stageRef.current)));
  }, []);

  // A pin or a hover outlives the node it names only until the next render:
  // an edit through the studio can remove the node under the pointer, and an
  // unmount emits no `pointerleave`, so derive, never store, the validity.
  const alive = (state: Active | null) => (state && layout.nodes.some((node) => node.id === state.id) ? state : null);
  const validPin = alive(pinned);
  const active = validPin ?? alive(hovered);
  const activeId = active?.id ?? null;
  // Release everything at once: with the pin gone, a lingering hover or focus
  // would make `active` fall straight back to the same node.
  const release = useCallback(() => {
    setPinned(null);
    setHovered(null);
  }, []);

  const tones = useMemo(() => {
    const set = new Set<string>(layout.edges.map((edge) => edge.tone));
    for (const item of layout.legend) if (item.marker === "arrow") set.add(item.tone);
    return Array.from(set);
  }, [layout]);

  const activeNode = activeId ? layout.nodes.find((node) => node.id === activeId) : undefined;
  const size = block.size ?? "md";
  const description = [markupToPlainText(block.caption), `${layout.nodes.length} nodes, ${layout.edges.length} links`]
    .filter(Boolean)
    .join(". ");

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !active) return;
    // Un-pin first; the deck's own Escape (overview, fullscreen) is the next
    // press. A hover-only highlight is released without claiming the key.
    if (validPin) event.stopPropagation();
    release();
  };

  return (
    <figure
      className="deck-architecture"
      {...useBlockEditAttrs()}
      data-size={size}
      data-fill={block.fill ? "true" : undefined}
      data-active={active ? "true" : undefined}
      data-density={layout.density < 1 ? layout.density.toFixed(2) : undefined}
      data-layout-unsettled={unsettled ? "true" : undefined}
      style={
        {
          "--deck-architecture-w": `${layout.width}px`,
          "--deck-architecture-ratio": String(layout.width / layout.naturalHeight),
          "--deck-architecture-drawn-h": `${layout.height}px`,
        } as CSSProperties
      }
      onKeyDown={onKeyDown}
    >
      <div className="deck-architecture-stage" ref={stageRef}>
        <svg
          className="deck-architecture-svg"
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          role="group"
          aria-label={description}
          onClick={release}
        >
          <defs>
            {tones.map((tone) => (
              <marker
                key={tone}
                id={markerId(tone)}
                className="deck-arch-marker"
                data-tone={tone}
                viewBox="0 0 12 12"
                refX="10"
                refY="6"
                markerWidth="16"
                markerHeight="16"
                markerUnits="userSpaceOnUse"
                orient="auto-start-reverse"
              >
                <path d="M 1 1 L 10 6 L 1 11 Z" />
              </marker>
            ))}
          </defs>
          <g className="deck-arch-zones">
            {layout.zones.map((zone, index) => (
              <ZoneView key={index} zone={zone} />
            ))}
          </g>
          <g className="deck-arch-edges">
            {layout.edges.map((edge) => (
              <EdgeView key={edge.key} edge={edge} state={edgeState(activeId, edge)} markerId={markerId} />
            ))}
          </g>
          <g className="deck-arch-nodes">
            {layout.nodes.map((node) => (
              <NodeView
                key={node.id}
                node={node}
                state={nodeState(activeId, node.id, layout)}
                pinned={validPin?.id === node.id}
                onHover={onHover}
                onToggle={onToggle}
              />
            ))}
          </g>
          {layout.legend.length ? (
            <g className="deck-arch-legend" role="list" aria-label="Legend">
              {layout.legend.map((item, index) => (
                <g
                  key={index}
                  className="deck-arch-legend-item"
                  data-tone={item.tone}
                  role="listitem"
                  aria-label={`${markupToPlainText(item.item.label)} (${item.marker})`}
                >
                  <LegendMarker item={item} markerId={markerId} />
                  <SvgText block={item.text} x={item.textX} y={item.y} anchor="start" className="deck-arch-legend-text" />
                </g>
              ))}
            </g>
          ) : null}
        </svg>
        {active && activeNode?.node.description ? (
          <div
            className="deck-architecture-detail"
            role="tooltip"
            data-placement={active.placement}
            style={{ "--deck-arch-x": `${active.x}%`, "--deck-arch-y": `${active.y}%` } as CSSProperties}
          >
            <InlineMarkup value={activeNode.node.label} as="strong" />
            <InlineMarkup value={activeNode.node.description} as="p" />
          </div>
        ) : null}
      </div>
      {block.caption ? <InlineMarkup value={block.caption} as="figcaption" field="caption" /> : null}
    </figure>
  );
}
