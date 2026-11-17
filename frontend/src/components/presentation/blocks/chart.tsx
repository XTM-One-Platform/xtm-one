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
 * The native `chart` block: a figure that owns the room its drawing
 * takes, the legend, and the hidden-series state. The drawing itself
 * (`ChartCanvas.tsx`, Recharts) is loaded lazily, like the Mermaid diagram -
 * a deck with no chart never downloads it.
 *
 * Sizing contract: the drawing is handed an EXPLICIT width and height read off
 * `.deck-chart-canvas` with a ResizeObserver (never Recharts' own responsive
 * container - the deck canvas is scaled with a CSS transform and the static
 * export must render deterministically). A canvas that measures 0 is a slide
 * reveal keeps out of the DOM's flow (`display: none`); it is not drawn, and
 * is drawn the moment it gets room. The export page hides no slide, so a
 * canvas that measures 0 there has no room for good: it stands empty at once
 * rather than holding the ready signal until the exporter times out, and the
 * layout check reports it as a chart drawn under its readable height
 * (`CHART_MIN_HEIGHT_PX`). Until the figure is complete - painted,
 * and grown in when it animates, since the values are written only then - the
 * canvas carries `data-deck-pending`, the attribute `deckSettled()` waits on
 * before the export page declares itself ready (and the one a test waits on
 * before it reads the figure).
 *
 * No build holds it for good: one nothing draws - a canvas with no
 * room for `STALL_MS`, a drawing whose chunk has not landed within
 * `FIGURE_WAIT_MS`, both in visible time and never on the export page - gives
 * up as it stands, and a drawing that cannot load, or throws, shows the
 * block's error state. Either way the canvas drops `data-deck-pending` and
 * carries `data-deck-unfinished`, which the browser print reads to say so. A
 * canvas that gets room later is a new build, pending again.
 *
 * Surface contract: the figure stands on a card unless the block says
 * `framed: false`, and nothing about its surface follows the build - the
 * stylesheet paints the waiting placeholder only on a canvas that holds no
 * drawing, never under one that is growing in.
 *
 * Title contract: a `title` puts a head row first in the figure -
 * `.deck-chart-head`, the title and the legend side by side as a dashboard
 * panel sets them, title left and legend right - over the canvas, so a `fill`
 * chart's plot takes every pixel the frame leaves under its head. Without a
 * title the figure's DOM is the one it always had (the legend a direct child).
 * A panel titles its chart itself: the validator refuses a chart title there.
 *
 * Legend contract: `legend_position` says where the legend sits -
 * `top` (unset: the historical DOM, the legend before the canvas, or in the
 * head beside the title), `bottom` or `right` (after the canvas, with
 * `data-legend-position` on the figure for the stylesheet's grids; the head
 * then holds the title alone). The entries, their toggles, swatches and font
 * are the same wherever it goes; a block that draws no legend carries no
 * position.
 *
 * A butterfly that names no `legend_position` lays its legend out
 * over its two sides - the first series at the left end, the second at the
 * right end - on a row of its own above the canvas (`data-split` on the
 * list, `data-legend-split` on the figure); `show_totals` writes each side's
 * total under its entry, wherever the legend sits.
 *
 * Motion contract: the figure builds when its SLIDE ENTERS, not when it is
 * drawn. reveal lays the next few slides out ahead of time, so a chart is
 * usually drawn while its slide is still off screen - drawn SETTLED, the
 * complete figure a transition or the overview shows - and the entry
 * (reveal's `present` class on the slide's `<section>`, watched by
 * `useSlideEntry`) remounts the drawing so the series grow in, the target line
 * draws and the gauge sweeps, every time the slide is entered. The canvas
 * carries `data-deck-animate` while it does, which is what the stylesheet's
 * enter animations key on. Nothing moves on the static export page, in a
 * preview, or for a person who asked for reduced motion.
 *
 * Export contract: the figure carries the chart AS DATA on `data-deck-chart`
 * (`chartExportPayload`) and the drawing publishes what it settled on in a
 * hidden `.deck-chart-facts` element, so the editable PPTX export rebuilds a
 * native PowerPoint chart from the numbers the viewer plotted
 * (`presentation_export.py` / `pptx_writer.py`) instead of pasting a picture.
 *
 * Map contract: a `map` is drawn by `MapCanvas.tsx`, a lazy chunk of
 * its own that carries the atlas, under the same box, motion and settle
 * rules. Its legend is not a list of toggles but its ramp - the series'
 * name, the lowest value, the colours between, the highest - in
 * `.deck-chart-legend` like any legend, so it sits where `legend_position`
 * says and the PPTX writes its text beside the picture of the map. A classed
 * map (`legend_style: "classes"`) lists its classes instead - a dot in
 * each class's colour and its range - beside the plot unless `legend_position`
 * puts them elsewhere.
 */
import {
  lazy,
  memo,
  Suspense,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";

import { useFormatter } from "@/i18n";
import type { DeckChartBlock } from "@/types";

import { useBlockEditAttrs } from "../edit-context";
import { FigureBoundary } from "../FigureBoundary";
import { useWaitBound } from "../figure-wait";
import { InlineMarkup } from "../inline-markup";
import { useMeasuredBox } from "../measured-box";
import { FIGURE_WAIT_MS } from "../ready";
import {
  butterflyTotals,
  chartExportPayload,
  chartValueWriter,
  isMirroredKind,
  isSingleSeriesKind,
  labelSurface,
  legendIdentities,
  legendPosition,
  legendShown,
  mapClasses,
  mapLegendStyle,
  mapRampLegend,
  paletteColor,
  resolveHidden,
  secondaryTargetLegendName,
  seriesColors,
  targetLegendName,
  toggleHidden,
  waterfallColor,
  writtenValues,
} from "./chart-format";
import { useChartLocale } from "./chart-locale";
import { STALL_MS } from "./chart-settle";

/**
 * Memoized: a render of the figure that changes nothing the drawing reads -
 * the settle dropping `data-deck-pending`, a parent re-rendering the slide -
 * must not reach it. Every render of the drawing hands Recharts fresh
 * formatters, which it takes for a new chart: it grows the series in again,
 * replacing the bars on every frame and hiding the value labels meanwhile.
 */
const ChartCanvas = memo(lazy(() => import("./ChartCanvas")));
/** A map's drawing and its atlas, downloaded only by a deck that draws one. */
const MapCanvas = memo(lazy(() => import("./MapCanvas")));

/**
 * Whether the drawing may animate at all: never on the static export page
 * (the capture must be the settled figure), never in a preview (the speaker
 * view's panes and the handout's thumbnails are stills that remount on every
 * move), and never for a person who asked for reduced motion. Read once per
 * mount, before the first drawing - all three are properties of the page, not
 * of the slide.
 */
function useChartMotion(ref: React.RefObject<HTMLElement | null>): boolean {
  const [motion, setMotion] = useState(false);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    if (host.closest('[data-deck-static="true"], [data-deck-preview="true"]')) return;
    if (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    setMotion(true);
  }, [ref]);
  return motion;
}

/** Whether the chart is on the export page (`PresentationStaticRender`), read once per mount like the motion. */
function useExportPage(ref: React.RefObject<HTMLElement | null>): boolean {
  const [exportPage, setExportPage] = useState(false);
  useLayoutEffect(() => {
    if (ref.current?.closest('[data-deck-static="true"]')) setExportPage(true);
  }, [ref]);
  return exportPage;
}

/** The slide that holds a chart in a live deck: reveal's own `<section>`, never a dashboard panel's. */
const REVEAL_SLIDE_SELECTOR = ".reveal .slides > section";
/** reveal marks the slide on screen with this class (and the `future` / `past` ones by name too). */
const PRESENT_CLASS = "present";

/**
 * How many times the chart's slide has been entered: `0` while it waits off
 * screen in a live deck, and one more on every entry. reveal shows the current
 * slide by toggling `present` on its `<section>`, so that class is watched;
 * outside a live deck (the static export page, a page document, a preview) the
 * chart is on screen from its first frame and counts as entered once.
 *
 * Read in a layout effect, like the box, so the first drawing already knows
 * whether it is the one on screen: the slide a deck opens on animates on its
 * first frame instead of flashing settled and remounting.
 */
function useSlideEntry(ref: React.RefObject<HTMLElement | null>): number {
  const [entries, setEntries] = useState(0);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    const section = host.closest(REVEAL_SLIDE_SELECTOR);
    if (!section) {
      setEntries(1);
      return;
    }
    let present = section.classList.contains(PRESENT_CLASS);
    if (present) setEntries((count) => Math.max(count, 1));
    if (typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(() => {
      const now = section.classList.contains(PRESENT_CLASS);
      if (now === present) return;
      present = now;
      if (now) setEntries((count) => count + 1);
    });
    observer.observe(section, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, [ref]);
  return entries;
}

/**
 * Mounted while nothing draws the build `build` - the canvas has no room, or
 * the drawing's chunk is on its way - and gives it up once `ms` of visible
 * time has passed. Draws nothing itself.
 */
function UndrawnBuild({
  host,
  build,
  ms,
  onGiveUp,
}: {
  host: RefObject<HTMLElement | null>;
  build: string;
  ms: number;
  onGiveUp: (build: string) => void;
}) {
  useWaitBound(host, build, ms, onGiveUp);
  return null;
}

interface LegendEntry {
  index: number;
  name: string;
  color: string;
}

/** What the legend lists: the series of an axis chart, the slices of a pie / donut; a map's is its ramp. */
function legendEntries(block: DeckChartBlock): LegendEntry[] {
  if (block.kind === "map") return [];
  if (isSingleSeriesKind(block.kind)) {
    return block.categories.map((name, index) => ({ index, name, color: paletteColor(index) }));
  }
  // A waterfall's one series is drawn in its roles' tones; its swatch is the totals'.
  const colors =
    block.kind === "waterfall" ? [waterfallColor("total", block.series[0]?.tone)] : seriesColors(block.series);
  return block.series.map((entry, index) => ({ index, name: entry.name, color: colors[index] }));
}

/** The names of the entries the legend can toggle: the slices of a pie / donut, the series otherwise. */
function toggleNames(block: DeckChartBlock): string[] {
  return isSingleSeriesKind(block.kind) ? block.categories : block.series.map((entry) => entry.name);
}

export function ChartBlockView({ block }: { block: DeckChartBlock }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const box = useMeasuredBox(hostRef);
  const motion = useChartMotion(hostRef);
  const exportPage = useExportPage(hostRef);
  const entries = useSlideEntry(hostRef);
  // A chart drawn ahead of its slide is drawn settled; the drawing is keyed on
  // the entry count so each entry mounts a fresh one that builds. The key is
  // pinned where nothing may move, so the export page never sees a redraw.
  const animate = motion && entries > 0;
  const drawingKey = motion ? entries : 0;
  // The toggles are kept by entry identity, never by position: an edit that
  // removes or reorders a series must not hide another one in its place.
  const identities = useMemo(() => legendIdentities(toggleNames(block)), [block]);
  const [hiddenState, setHiddenState] = useState<ReadonlySet<string>>(() => new Set());
  const hidden = useMemo(() => resolveHidden(hiddenState, identities), [hiddenState, identities]);
  // Toggles that hide nothing in this block (their series are gone, a repeated
  // name's list of entries changed, or an edit left none visible) are dropped,
  // or a later edit restoring those entries would hide them again.
  if (hidden.size !== hiddenState.size) setHiddenState(new Set([...hidden].map((index) => identities[index])));
  const locale = useChartLocale();
  // A drawing is rebuilt in place when what it draws changes - an edit (the
  // editor's live draft, an agent's save), a legend click, a resize, a new
  // locale: Recharts grows the series again, so that is a new build, pending
  // until it settles. A canvas that lost its room keeps its state; it is
  // rebuilt when it gets room again.
  const [drawn, setDrawn] = useState({ block, hidden, box, locale, build: 0 });
  if (
    box &&
    (drawn.block !== block || drawn.hidden !== hidden || drawn.box !== box || drawn.locale !== locale)
  ) {
    setDrawn({ block, hidden, box, locale, build: drawn.build + 1 });
  }
  // Settled is a property of ONE build: a fresh drawing on a slide entry, or a
  // drawing rebuilt in place, is pending again until it has grown in, so the
  // marker never says "complete" over a figure whose values are not written yet.
  const buildKey = `${drawingKey}:${drawn.build}`;
  const [settledKey, setSettledKey] = useState<string | null>(null);
  const onSettled = useCallback(() => setSettledKey(buildKey), [buildKey]);
  // A build nothing draws gives up as it stands (`UndrawnBuild`), and a
  // drawing that cannot load or throws shows the error state until the block
  // changes: neither holds the ready signal, both are unfinished.
  const [gaveUpKey, setGaveUpKey] = useState<string | null>(null);
  const [failedBlock, setFailedBlock] = useState<DeckChartBlock | null>(null);
  const failed = failedBlock === block;
  const onDrawingFailed = useCallback(() => setFailedBlock(block), [block]);
  const complete = settledKey === buildKey;
  // Neither pending nor unfinished: an empty canvas on the export page is the
  // layout's doing, which the layout check reports - not a drawing to wait for.
  const roomless = exportPage && box === null;
  const settled = complete || failed || gaveUpKey === buildKey || roomless;
  const unfinished = failed || (gaveUpKey === buildKey && !complete);
  const { t_i18n } = useFormatter();
  const showLegend = legendShown(block);
  const legend = useMemo(() => (showLegend ? legendEntries(block) : []), [block, showLegend]);
  const classed = mapLegendStyle(block) === "classes";
  const ramp = useMemo(
    () => (showLegend && block.kind === "map" && !classed ? mapRampLegend(block, locale) : null),
    [block, showLegend, classed, locale],
  );
  const classes = useMemo(
    () => (showLegend && classed ? mapClasses(block, locale) : []),
    [block, showLegend, classed, locale],
  );
  const targetName = showLegend ? targetLegendName(block, locale) : null;
  const gapLabel = t_i18n("Unexplained");
  const runningLabel = t_i18n("Running total");
  const noDataLabel = t_i18n("No data");
  const secondaryTargetName = showLegend ? secondaryTargetLegendName(block, locale) : null;
  const legendDrawn =
    Boolean(ramp) || classes.length > 0 || legend.length > 0 || Boolean(targetName) || Boolean(secondaryTargetName);
  // A butterfly's legend over its two sides, unless the block places it.
  const split = legendDrawn && isMirroredKind(block.kind) && !block.legend_position;
  const totals = useMemo(() => {
    const sides = showLegend ? butterflyTotals(block) : null;
    if (!sides) return null;
    const write = chartValueWriter(block, writtenValues(block), locale, labelSurface(block));
    return sides.map((total) => `${t_i18n("Total")} ${write(total)}`);
  }, [block, showLegend, locale, t_i18n]);
  // Where the legend sits: before the canvas in the column for `top`
  // (the historical DOM, so a block that says nothing renders as it always
  // did), after it for `bottom` and `right`, where the stylesheet keys the
  // figure's and the panel's grid on `data-legend-position`. The attribute
  // is the block's own word - or a classed map's own default, `right` - and
  // is carried only while a legend is drawn: a placement without a legend
  // changes nothing about the figure.
  const position = legendPosition(block);
  const placed = legendDrawn && (block.legend_position || position !== "top") ? position : undefined;
  // The chart as data, for the PPTX export (see `chartExportPayload`).
  const exportPayload = useMemo(() => chartExportPayload(block, locale, gapLabel), [block, locale, gapLabel]);
  const editAttrs = useBlockEditAttrs();

  const toggle = (index: number) => {
    setHiddenState((current) => toggleHidden(current, index, identities));
  };

  const legendItems = classes.length ? (
    // A classed map's legend: a dot in each class's colour and its range, lowest first - nothing to toggle.
    <ul className="deck-chart-legend deck-chart-classes">
      {classes.map((entry) => (
        <li key={entry.min}>
          <span className="deck-chart-legend-item">
            <span
              className="deck-chart-swatch"
              style={{ "--deck-class-swatch": entry.color } as CSSProperties}
              aria-hidden="true"
            />
            <span className="deck-chart-legend-name">{entry.label}</span>
          </span>
        </li>
      ))}
    </ul>
  ) : ramp ? (
    // A map's legend is its ramp: nothing to toggle, one series coloured from its lowest value to its highest.
    <div className="deck-chart-legend deck-chart-ramp">
      <span className="deck-chart-legend-name">{ramp.name}</span>
      <span className="deck-chart-ramp-value" data-end="low">
        {ramp.low}
      </span>
      <span
        className="deck-chart-ramp-bar"
        style={{ "--deck-ramp-from": ramp.from, "--deck-ramp-to": ramp.to } as CSSProperties}
        aria-hidden="true"
      />
      <span className="deck-chart-ramp-value" data-end="high">
        {ramp.high}
      </span>
    </div>
  ) : legendDrawn ? (
    <ul className="deck-chart-legend" data-split={split ? "true" : undefined}>
      {legend.map((entry) => (
        <li key={entry.index} data-total={totals?.[entry.index] ? "true" : undefined}>
          <button
            type="button"
            className="deck-chart-legend-item"
            data-hidden={hidden.has(entry.index) ? "true" : undefined}
            aria-pressed={!hidden.has(entry.index)}
            onClick={() => toggle(entry.index)}
          >
            <span className="deck-chart-swatch" style={{ background: entry.color }} aria-hidden="true" />
            <span className="deck-chart-legend-name">{entry.name}</span>
          </button>
          {totals?.[entry.index] ? <span className="deck-chart-legend-total">{totals[entry.index]}</span> : null}
        </li>
      ))}
      {targetName ? (
        // The target line is named here, not toggled: it is a reading aid, not a series.
        <li>
          <span className="deck-chart-legend-item deck-chart-legend-target">
            <span className="deck-chart-swatch deck-chart-swatch-line" aria-hidden="true" />
            <span className="deck-chart-legend-name">{targetName}</span>
          </span>
        </li>
      ) : null}
      {secondaryTargetName ? (
        // The right-hand axis' target line, dotted like the line itself.
        <li>
          <span className="deck-chart-legend-item deck-chart-legend-target">
            <span className="deck-chart-swatch deck-chart-swatch-line" data-axis="secondary" aria-hidden="true" />
            <span className="deck-chart-legend-name">{secondaryTargetName}</span>
          </span>
        </li>
      ) : null}
    </ul>
  ) : null;
  // At the top the list stands before the canvas - in the head beside the
  // title, or a direct child of the figure; elsewhere it follows the canvas.
  // A butterfly's split legend is its own row between the head and the canvas.
  const legendList = position === "top" && !split ? legendItems : null;
  const legendSplit = split ? legendItems : null;
  const legendAfter = position === "top" ? null : legendItems;

  return (
    <figure
      className="deck-chart"
      data-kind={block.kind}
      data-size={block.size ?? "md"}
      data-label-size={block.label_size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-framed={block.framed === false ? undefined : "true"}
      data-legend-position={placed}
      data-legend-split={split ? "true" : undefined}
      data-deck-chart={exportPayload}
      {...editAttrs}
    >
      {block.title ? (
        <div className="deck-chart-head">
          <InlineMarkup as="h3" className="deck-chart-title" value={block.title} field="title" />
          {legendList}
        </div>
      ) : (
        legendList
      )}
      {legendSplit}
      <div
        className="deck-chart-canvas"
        ref={hostRef}
        data-deck-pending={settled ? undefined : "true"}
        data-deck-unfinished={unfinished ? "true" : undefined}
        data-deck-animate={animate ? "true" : undefined}
      >
        {failed ? (
          <p className="deck-chart-error">{t_i18n("This chart could not be drawn.")}</p>
        ) : box ? (
          <FigureBoundary onError={onDrawingFailed}>
            <Suspense
              fallback={
                settled ? null : (
                  <UndrawnBuild host={hostRef} build={buildKey} ms={FIGURE_WAIT_MS} onGiveUp={setGaveUpKey} />
                )
              }
            >
              {block.kind === "map" ? (
                <MapCanvas
                  key={drawingKey}
                  block={block}
                  width={box.width}
                  height={box.height}
                  locale={locale}
                  onSettled={onSettled}
                  noDataLabel={noDataLabel}
                />
              ) : (
                <ChartCanvas
                  key={drawingKey}
                  block={block}
                  width={box.width}
                  height={box.height}
                  hidden={hidden}
                  animate={animate}
                  locale={locale}
                  onSettled={onSettled}
                  gapLabel={gapLabel}
                  runningLabel={runningLabel}
                />
              )}
            </Suspense>
          </FigureBoundary>
        ) : settled ? null : (
          <UndrawnBuild host={hostRef} build={buildKey} ms={STALL_MS} onGiveUp={setGaveUpKey} />
        )}
      </div>
      {legendAfter}
      {block.caption ? <InlineMarkup as="figcaption" value={block.caption} field="caption" /> : null}
    </figure>
  );
}
