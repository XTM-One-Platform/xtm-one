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
 * The native `org_chart` block: reporting lines, teams and headcount as a
 * tree of seats.
 *
 * The agent hands over the TREE (each seat names its manager);
 * `org-chart-layout.ts` - the one engine the viewer, the exports, the layout
 * check and the video figure share - turns it into geometry, and this
 * component draws that geometry as inline SVG THROUGH REACT: every word a
 * `<text>` in the deck's fonts, every colour a deck token, every shape a
 * `<rect>` / `<circle>` / `<path>`. No markup sink, nothing the spec could
 * smuggle a tag through.
 *
 * What the other readers of the drawing find on it:
 * - the PPTX export (`EXTRACT_PRIMITIVES_JS`): `data-deck-org-chart` on the
 *   figure, `data-org-node` on every seat, `data-points` on every line - the
 *   engine's geometry, written as native shapes, ovals and connectors;
 * - the layout check (`LAYOUT_AUDIT_JS`): the engine's own account of the
 *   picture (`data-org-facts`) and, on every line of text, the width it was
 *   fitted to (`data-max-w`) - the painted glyphs are measured against it;
 * - the video figure (`figure-build.ts`): `data-level` on every seat and line,
 *   `data-count-to` on every number that counts up.
 *
 * Interactive in the live deck only: hovering or focusing a seat shows its
 * whole roll-up, its chips, its note and its dotted managers in a card; a
 * folded seat opens in place on click or Enter (the viewer's state - the spec
 * is unchanged); the arrow keys walk the tree (up to the manager, down to the
 * first report, left and right along the level), Home to the root.
 *
 * Sizing contract (the architecture's): the stage is the drawing's
 * box - as wide as the column up to the drawing's own width, as tall as its
 * ratio makes it within the size bucket, handed its height in `fill` / `full`
 * - and the `<svg>` meets it absolutely positioned, so the drawing never
 * feeds back into the box it is measured from. The engine is handed the
 * column's width and the stage's height as its room and decides the spine
 * and the refusal from them.
 */
import {
  useCallback,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

import { useFormatter } from "@/i18n";
import type { DeckOrgChartBlock, DeckOrgNodeKind } from "@/types";

import { useDeckAsset } from "../asset-context";
import { useBlockEditAttrs } from "../edit-context";
import { useGiveUp } from "../figure-wait";
import { InlineMarkup } from "../inline-markup";
import { markupToPlainText } from "../markup";
import { useMeasuredBox } from "../measured-box";
import { SvgIcon } from "./Architecture";
import { useChartLocale } from "./chart-locale";
import {
  layoutOrgChart,
  orgNeighbours,
  rollupText,
  type OrgChartLayout,
  type OrgLink,
  type OrgMedia,
  type OrgNodeLayout,
  type OrgPill,
  type OrgText,
  type OrgWords,
} from "./org-chart-layout";

/** The height each size bucket gives the stage (`--deck-org-h`, mirrored in `presentation.css`). */
const BUCKET_HEIGHT: Record<string, number> = { sm: 380, md: 600, lg: 800, full: 800 };

/** The engine's words, in the viewer's language. */
function useOrgWords(): OrgWords {
  const { t_i18n } = useFormatter();
  return useMemo(
    () => ({
      open: t_i18n("Open role"),
      planned: t_i18n("Planned"),
      people: (count: number) => t_i18n("{count, plural, one {# person} other {# people}}", { count }),
      openCount: (count: number) => t_i18n("+{count} open", { count }),
      plannedCount: (count: number) => t_i18n("+{count} planned", { count }),
      below: (count: number) => t_i18n("+{count} below", { count }),
      by: (period: string) => t_i18n("by {period}", { period }),
      dotted: t_i18n("Dotted line"),
      kinds: {
        person: t_i18n("Person"),
        vacant: t_i18n("Open seat"),
        planned: t_i18n("Planned hire"),
        group: t_i18n("Team"),
        bubble: t_i18n("Team, by size"),
        external: t_i18n("External"),
      },
    }),
    [t_i18n],
  );
}

// ── Pieces ────────────────────────────────────────────────────────────────────

function SvgLine({ text, className }: { text: OrgText; className: string }) {
  return (
    <text
      className={className}
      x={text.x}
      y={text.y}
      textAnchor={text.anchor}
      dominantBaseline="central"
      fontSize={text.fontSize}
      fontWeight={text.weight}
      data-max-w={Math.round(text.maxWidth)}
      data-broken={text.broken ? "true" : undefined}
      data-count-to={text.count ? text.count.value : undefined}
      data-count-suffix={text.count?.suffix || undefined}
      xmlSpace="preserve"
    >
      {text.text}
    </text>
  );
}

function Pill({ pill, className }: { pill: OrgPill; className: string }) {
  return (
    <g className={className} data-role={pill.role}>
      <rect x={pill.x} y={pill.y} width={pill.w} height={pill.h} rx={pill.h / 2} />
      <SvgLine text={pill.text} className={`${className}-text`} />
    </g>
  );
}

/** A person's photo, a template asset, cut to a circle; it holds the export's ready signal until painted. */
function SvgPhoto({ media, clip }: { media: OrgMedia; clip: string }) {
  const { src, loading } = useDeckAsset(media.fileId);
  const placeholder = useRef<SVGCircleElement>(null);
  const image = useRef<SVGImageElement>(null);
  const [settledSrc, setSettledSrc] = useState<string | null>(null);
  const settled = src !== null && settledSrc === src;
  const onSettled = useCallback(() => setSettledSrc(src), [src]);
  const fetchGaveUp = useGiveUp(placeholder, loading ? "fetch" : null);
  const bytesGaveUp = useGiveUp(image, src && !settled ? src : null);
  const r = media.size / 2;
  const cx = media.x + r;
  const cy = media.y + r;
  return (
    <g className="deck-org-photo">
      <clipPath id={clip}>
        <circle cx={cx} cy={cy} r={r} />
      </clipPath>
      <circle
        ref={placeholder}
        className="deck-org-media-tile"
        cx={cx}
        cy={cy}
        r={r}
        data-deck-pending={loading && !fetchGaveUp ? "true" : undefined}
        data-deck-unfinished={loading && fetchGaveUp ? "true" : undefined}
      />
      {src ? (
        <image
          ref={image}
          href={src}
          x={media.x}
          y={media.y}
          width={media.size}
          height={media.size}
          preserveAspectRatio="xMidYMid slice"
          clipPath={`url(#${clip})`}
          data-org-media="photo"
          data-deck-pending={settled || bytesGaveUp ? undefined : "true"}
          data-deck-unfinished={bytesGaveUp ? "true" : undefined}
          onLoad={onSettled}
          onError={onSettled}
        />
      ) : null}
    </g>
  );
}

function NodeMedia({ node, clip }: { node: OrgNodeLayout; clip: string }) {
  const media = node.media;
  if (!media) return null;
  const r = media.size / 2;
  const cx = media.x + r;
  const cy = media.y + r;
  switch (media.kind) {
    case "photo":
      return media.fileId ? <SvgPhoto key={media.fileId} media={media} clip={clip} /> : null;
    case "initials":
      return (
        <g className="deck-org-initials">
          <circle className="deck-org-media-tile" cx={cx} cy={cy} r={r} />
          <text x={cx} y={cy} textAnchor="middle" dominantBaseline="central" fontSize={Math.round(media.size * 0.38)} fontWeight={600}>
            {media.initials}
          </text>
        </g>
      );
    case "empty":
      return (
        <g className="deck-org-empty">
          <circle className="deck-org-empty-ring" cx={cx} cy={cy} r={r - 1} />
          <SvgIcon name="user-plus" x={cx - media.size * 0.25} y={cy - media.size * 0.25} size={media.size * 0.5} />
        </g>
      );
    case "icon": {
      const icon = media.icon ?? "users";
      if (node.shape === "circle") return <SvgIcon name={icon} x={media.x} y={media.y} size={media.size} />;
      const inset = media.size * 0.22;
      return (
        <g className="deck-org-icon">
          <rect className="deck-org-media-tile" x={media.x} y={media.y} width={media.size} height={media.size} rx={media.size * 0.28} />
          <SvgIcon name={icon} x={media.x + inset} y={media.y + inset} size={media.size - inset * 2} />
        </g>
      );
    }
  }
}

function LinkView({ link, dimmed }: { link: OrgLink; dimmed: boolean }) {
  return (
    <path
      className="deck-org-link"
      d={link.d}
      data-kind={link.kind}
      data-level={link.level < 0 ? undefined : link.level}
      data-dotted={link.kind === "dotted" ? "true" : undefined}
      data-from={link.from}
      data-to={link.to.join(" ")}
      data-points={link.points.map((line) => line.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")).join(";")}
      data-state={dimmed ? "dim" : undefined}
    />
  );
}

type NodeState = "focus" | "linked" | "dim" | undefined;

function NodeView({
  node,
  state,
  tabbable,
  onFocusNode,
  onLeave,
  onToggle,
  onKeyNav,
}: {
  node: OrgNodeLayout;
  state: NodeState;
  tabbable: boolean;
  onFocusNode: (node: OrgNodeLayout, element: SVGGElement) => void;
  onLeave: () => void;
  onToggle: (node: OrgNodeLayout) => void;
  onKeyNav: (node: OrgNodeLayout, event: KeyboardEvent<SVGGElement>) => void;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const circle = node.shape === "circle";
  const core = node.emphasis === "core";
  // A folded seat is closed; one the viewer opened, or a manager whose reports are drawn, is open.
  const expanded = node.foldable ? node.kind !== "fold" && node.childIds.length > 0 : undefined;
  return (
    <g
      className="deck-org-node"
      data-org-node={node.id}
      data-kind={node.kind}
      data-shape={node.shape}
      data-level={node.level}
      data-parent={node.parentId ?? undefined}
      data-emphasis={node.emphasis}
      data-tone={node.color ? undefined : node.tone}
      data-color={node.color}
      data-foldable={node.foldable ? "true" : undefined}
      data-open={expanded === undefined ? undefined : String(expanded)}
      data-spine={node.spine ? "true" : undefined}
      data-state={state}
      tabIndex={tabbable ? 0 : -1}
      role="treeitem"
      aria-label={node.ariaLabel}
      aria-level={node.level + 1}
      aria-expanded={expanded}
      onPointerEnter={(event) => onFocusNode(node, event.currentTarget)}
      onPointerLeave={onLeave}
      onFocus={(event) => onFocusNode(node, event.currentTarget)}
      onBlur={onLeave}
      onClick={(event: MouseEvent<SVGGElement>) => {
        event.stopPropagation();
        if (node.foldable) onToggle(node);
      }}
      onKeyDown={(event) => onKeyNav(node, event)}
    >
      {core ? (
        circle ? (
          <circle className="deck-org-glow" cx={node.x + node.w / 2} cy={node.y + node.h / 2} r={node.w / 2} aria-hidden="true" />
        ) : (
          <rect className="deck-org-glow" x={node.x} y={node.y} width={node.w} height={node.h} rx={node.radius} aria-hidden="true" />
        )
      ) : null}
      {circle ? (
        <circle className="deck-org-shape" cx={node.x + node.w / 2} cy={node.y + node.h / 2} r={node.w / 2} />
      ) : (
        <rect className="deck-org-shape" x={node.x} y={node.y} width={node.w} height={node.h} rx={node.radius} />
      )}
      <NodeMedia node={node} clip={`${uid}-photo`} />
      {node.number ? <SvgLine text={node.number} className="deck-org-number" /> : null}
      {node.main ? <SvgLine text={node.main} className="deck-org-name" /> : null}
      {node.sub ? <SvgLine text={node.sub} className="deck-org-role" /> : null}
      {node.chips.map((chip, index) => (
        <Pill key={index} pill={chip} className="deck-org-chip" />
      ))}
      {node.badge ? <Pill pill={node.badge} className="deck-org-badge" /> : null}
      {node.rollupPill ? <Pill pill={node.rollupPill} className="deck-org-rollup" /> : null}
    </g>
  );
}

function LegendSwatch({ kind, dotted, x, y }: { kind?: DeckOrgNodeKind; dotted?: boolean; x: number; y: number }) {
  const size = 22;
  const cy = y + 17;
  if (dotted) return <line className="deck-org-legend-line" x1={x} y1={cy} x2={x + 26} y2={cy} />;
  if (kind === "bubble") return <circle className="deck-org-legend-swatch" cx={x + 13} cy={cy} r={size / 2} />;
  return <rect className="deck-org-legend-swatch" x={x + 2} y={cy - size / 2} width={size} height={size} rx={6} />;
}

// ── The detail card ──────────────────────────────────────────────────────────

interface Active {
  id: string;
  x: number;
  y: number;
  placement: "above" | "below";
}

const DETAIL_MAX_WIDTH = 380;
const DETAIL_GUTTER = 16;

/** Where the card goes, in percent of the stage, measured from the live boxes (correct at any scale and letterboxed). */
function measureActive(id: string, element: SVGGElement, stage: HTMLDivElement | null): Active {
  const fallback: Active = { id, x: 50, y: 50, placement: "below" };
  if (!stage) return fallback;
  const s = stage.getBoundingClientRect();
  const n = element.getBoundingClientRect();
  if (!s.width || !s.height) return fallback;
  const cx = (n.left + n.width / 2 - s.left) / s.width;
  const below = (n.top + n.height / 2 - s.top) / s.height < 0.5;
  const edge = below ? (n.bottom - s.top) / s.height : (n.top - s.top) / s.height;
  const stageWidth = Math.max(1, stage.offsetWidth);
  const cardWidth = Math.max(0, Math.min(DETAIL_MAX_WIDTH, stageWidth - 2 * DETAIL_GUTTER));
  const half = Math.min(0.5, (DETAIL_GUTTER + cardWidth / 2) / stageWidth);
  return { id, x: Math.max(half, Math.min(1 - half, cx)) * 100, y: edge * 100 + (below ? 1.5 : -1.5), placement: below ? "below" : "above" };
}

function Detail({ node, layout, block, words, active, format }: { node: OrgNodeLayout; layout: OrgChartLayout; block: DeckOrgChartBlock; words: OrgWords; active: Active; format: (n: number) => string }) {
  const { t_i18n } = useFormatter();
  const spec = node.spec;
  const byId = new Map(layout.nodes.map((n) => [n.id, n]));
  const dotted = node.dotted.map((id) => byId.get(id)?.main?.full ?? id);
  const chips = (spec?.chips ?? []).map((chip) => markupToPlainText(chip)).filter(Boolean);
  const hasReports = node.childIds.length > 0 || node.hidden !== null;
  return (
    <div
      className="deck-org-detail"
      role="tooltip"
      data-placement={active.placement}
      style={{ "--deck-org-x": `${active.x}%`, "--deck-org-y": `${active.y}%` } as CSSProperties}
    >
      {node.main ? <strong>{node.main.full}</strong> : null}
      {node.sub ? <p>{node.sub.full}</p> : null}
      {node.kind !== "person" && node.kind !== "fold" ? <p className="deck-org-detail-kind">{words.kinds[node.kind]}</p> : null}
      {spec?.location || spec?.period ? <p>{[spec.location, spec.period].filter(Boolean).join(" · ")}</p> : null}
      {chips.length ? (
        <ul className="deck-org-detail-chips">
          {chips.map((chip, index) => (
            <li key={index}>{chip}</li>
          ))}
        </ul>
      ) : null}
      {spec?.note ? <p>{spec.note}</p> : null}
      {dotted.length ? <p>{t_i18n("Dotted line to {names}", { names: dotted.join(", ") })}</p> : null}
      {hasReports ? (
        <p className="deck-org-detail-rollup">
          {t_i18n("Headcount: {value}", {
            value:
              rollupText(node.rollup, "current", words, format) +
              ((block.count_mode ?? "current") !== "current" && node.rollup.target !== node.rollup.current ? ` → ${format(node.rollup.target)}` : ""),
          })}
        </p>
      ) : null}
      {node.foldable ? <p className="deck-org-detail-hint">{node.childIds.length && node.kind !== "fold" ? t_i18n("Click to fold") : t_i18n("Click to expand")}</p> : null}
    </div>
  );
}

// ── The block ─────────────────────────────────────────────────────────────────

function stateOf(active: string | null, node: OrgNodeLayout, layout: OrgChartLayout): NodeState {
  if (!active) return undefined;
  if (active === node.id) return "focus";
  const focused = layout.nodes.find((n) => n.id === active);
  if (!focused) return undefined;
  // The active seat's line: its managers up to the root, its reports, its dotted managers.
  const line = new Set<string>([...focused.childIds, ...focused.dotted]);
  for (let id = focused.parentId; id; id = layout.nodes.find((n) => n.id === id)?.parentId ?? null) line.add(id);
  return line.has(node.id) ? "linked" : "dim";
}

export function OrgChartBlockView({ block }: { block: DeckOrgChartBlock }) {
  const { t_i18n } = useFormatter();
  const figureRef = useRef<HTMLElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const figureBox = useMeasuredBox(figureRef);
  const stageBox = useMeasuredBox(stageRef);
  const words = useOrgWords();
  const locale = useChartLocale();
  const size = block.size ?? "md";
  // A dashboard panel hands the stage its room the way `fill` does.
  const [inPanel, setInPanel] = useState(false);
  useLayoutEffect(() => setInPanel(!!figureRef.current?.closest(".deck-panel-body")), []);
  const stretches = block.fill === true || size === "full" || inPanel;
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set());
  const room = useMemo(() => {
    if (!figureBox) return null;
    const height = stretches ? (stageBox?.height ?? BUCKET_HEIGHT.md) : BUCKET_HEIGHT[size];
    return { width: figureBox.width, height };
  }, [figureBox, stageBox, stretches, size]);
  const layout = useMemo(() => layoutOrgChart(block, { room, opened, words, locale }), [block, room, opened, words, locale]);
  const format = useMemo(() => {
    const formatter = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
    return (n: number) => formatter.format(n);
  }, [locale]);
  const [hovered, setHovered] = useState<Active | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  const alive = (state: Active | null) => (state && layout.nodes.some((node) => node.id === state.id) ? state : null);
  const active = alive(hovered);
  const activeNode = active ? layout.nodes.find((node) => node.id === active.id) : undefined;
  const tabbableId = focusId && layout.nodes.some((node) => node.id === focusId) ? focusId : (layout.nodes[0]?.id ?? null);

  const onFocusNode = useCallback((node: OrgNodeLayout, element: SVGGElement) => {
    setHovered(measureActive(node.id, element, stageRef.current));
  }, []);
  const onLeave = useCallback(() => setHovered(null), []);
  const onToggle = useCallback((node: OrgNodeLayout) => {
    setOpened((current) => {
      const next = new Set(current);
      if (next.has(node.sourceId)) next.delete(node.sourceId);
      else next.add(node.sourceId);
      return next;
    });
    setFocusId(node.sourceId);
  }, []);
  const moveTo = useCallback((id: string | null) => {
    if (!id) return;
    setFocusId(id);
    // Seat ids are `[A-Za-z][A-Za-z0-9_-]*` (a fold card adds `~fold`): safe inside the quotes.
    const target = stageRef.current?.querySelector<SVGGElement>(`[data-org-node="${id}"]`);
    target?.focus();
  }, []);
  const onKeyNav = useCallback(
    (node: OrgNodeLayout, event: KeyboardEvent<SVGGElement>) => {
      const around = orgNeighbours(layout, node.id);
      const lr = layout.orientation === "left_right";
      const keys: Record<string, string | null> = {
        [lr ? "ArrowLeft" : "ArrowUp"]: around.parent,
        [lr ? "ArrowRight" : "ArrowDown"]: around.firstChild,
        [lr ? "ArrowUp" : "ArrowLeft"]: around.previous,
        [lr ? "ArrowDown" : "ArrowRight"]: around.next,
        Home: layout.nodes[0]?.id ?? null,
      };
      if (event.key in keys) {
        event.preventDefault();
        event.stopPropagation();
        moveTo(keys[event.key]);
        return;
      }
      if ((event.key === "Enter" || event.key === " ") && node.foldable) {
        // Space and Enter on a seat never advance the deck.
        event.preventDefault();
        event.stopPropagation();
        onToggle(node);
        return;
      }
      if (event.key === "Escape" && hovered) {
        setHovered(null);
      }
    },
    [layout, moveTo, onToggle, hovered],
  );

  const caption = markupToPlainText(block.caption);
  const description = [caption, t_i18n("{count, plural, one {Org chart, # seat} other {Org chart, # seats}}", { count: layout.facts.seats })]
    .filter(Boolean)
    .join(". ");
  const facts = JSON.stringify(layout.facts);

  return (
    <figure
      ref={figureRef}
      className="deck-org-chart"
      {...useBlockEditAttrs()}
      data-size={size}
      data-fill={block.fill ? "true" : undefined}
      data-orientation={layout.orientation}
      data-style={block.style ?? "cards"}
      data-active={active ? "true" : undefined}
      data-refused={layout.refused ? "true" : undefined}
      data-deck-org-chart={layout.refused ? undefined : "true"}
      data-org-facts={facts}
      style={
        {
          "--deck-org-w": `${layout.width}px`,
          "--deck-org-ratio": String(layout.width / layout.height),
        } as CSSProperties
      }
    >
      <div className="deck-org-stage" ref={stageRef}>
        {layout.refused ? (
          <div className="deck-org-refused" role="note">
            {t_i18n("{count, plural, one {This org chart is too large to read here (# seat).} other {This org chart is too large to read here (# seats).}}", {
              count: layout.refused.seats,
            })}
          </div>
        ) : (
          <svg className="deck-org-svg" viewBox={`0 0 ${layout.width} ${layout.height}`} role="tree" aria-label={description} onClick={() => setHovered(null)}>
            <g className="deck-org-zones">
              {layout.zones.map((zone) => (
                <rect key={zone.key} className="deck-org-zone" data-color={zone.color} data-branch={zone.root} x={zone.x} y={zone.y} width={zone.w} height={zone.h} rx={28} />
              ))}
            </g>
            <g className="deck-org-links">
              {layout.links
                .filter((link) => link.kind === "line")
                .map((link) => (
                  <LinkView key={link.key} link={link} dimmed={!!active && link.from !== active.id && !link.to.includes(active.id)} />
                ))}
            </g>
            <g className="deck-org-nodes">
              {layout.nodes.map((node) => (
                <NodeView
                  key={node.id}
                  node={node}
                  state={stateOf(active?.id ?? null, node, layout)}
                  tabbable={node.id === tabbableId}
                  onFocusNode={onFocusNode}
                  onLeave={onLeave}
                  onToggle={onToggle}
                  onKeyNav={onKeyNav}
                />
              ))}
            </g>
            <g className="deck-org-links deck-org-links-dotted">
              {layout.links
                .filter((link) => link.kind === "dotted")
                .map((link) => (
                  <LinkView key={link.key} link={link} dimmed={!!active && link.from !== active.id && !link.to.includes(active.id)} />
                ))}
            </g>
            {layout.legend.length ? (
              <g className="deck-org-legend" role="list" aria-label={t_i18n("Legend")}>
                {layout.legend.map((entry) => (
                  <g
                    key={entry.key}
                    className="deck-org-legend-item"
                    role="listitem"
                    data-kind={entry.kind}
                    data-tone={entry.tone}
                    data-dotted={entry.dotted ? "true" : undefined}
                  >
                    <LegendSwatch kind={entry.kind} dotted={entry.dotted} x={entry.x} y={entry.y} />
                    <SvgLine text={entry.text} className="deck-org-legend-text" />
                  </g>
                ))}
              </g>
            ) : null}
          </svg>
        )}
        {active && activeNode ? <Detail node={activeNode} layout={layout} block={block} words={words} active={active} format={format} /> : null}
      </div>
      {block.caption ? <InlineMarkup value={block.caption} as="figcaption" field="caption" /> : null}
    </figure>
  );
}
