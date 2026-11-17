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
 * Logo walls: the `logo_grid` block and the
 * native `logo_wall` - logos grouped by sector, one card per group.
 *
 * A logo is sized by its MARK, not by its cell. `LogoMark` reads the stored
 * geometry of the file (`asset_meta`: the box of its non-transparent pixels
 * and its ink, read server-side when the file was stored), crops the image to
 * that box and draws it at the height `logo-sizing.ts` computes - the block's
 * `logo_height`, raised for a squarer, lighter mark by optical balance. The
 * box is a span with the mark's aspect ratio and that height; a row too
 * narrow for its logos shrinks all of them in proportion (the stylesheet), so
 * a row keeps one height. A file with no stored geometry is drawn from the
 * size it loads at, untrimmed. With `tile_height` every cell is that
 * tall in canvas px, whatever it holds, and the mark fits its inner box: a
 * wordmark takes the width, a seal grows to the inner height (the balance is
 * not capped in a fixed tile, `logoMaxBoost`).
 *
 * Rows are cut here (`logoRows`), never left to wrapping: `columns` / `rows`
 * (or `per_row` on a wall) are exact, and a short last row is centred. The
 * geometry and the ink mask are runtime values (a measured ratio, a resolved
 * URL), so they are the one inline style besides the theme variables, as
 * custom properties the stylesheet reads - same exception as `MeasuredImage`;
 * the count of an equal grid's columns and a tile's height are spec values
 * carried the same way.
 *
 * `tone: mono / muted` inks every logo in the text or the muted colour through
 * a CSS mask of its alpha (`.deck-logo-ink`); the image stays in the DOM under
 * it, which is what a hover - and the editor, which shows the original - reveal.
 * `tone: ink` recolours each logo through its own SVG filter (`LogoInkFilter`),
 * read against the logo's own colours: pixels darker than its lightest colour
 * take the text colour, the lightest turn transparent, so white letters on a
 * dark badge stay letters and a light-coloured wordmark is inked whole. The
 * colour is `--deck-fg`, so it follows the theme.
 * `optical_weight` sizes every logo to one visual weight (`logoWeightHeightPx`)
 * and a logo's own `scale` multiplies the height it gets.
 * The mark is ONE picture for the editable PPTX (`data-export-picture`): the
 * export screenshots the cropped, inked box at the position the render
 * measured, never the untrimmed image under it. A wall card is one group
 * (`data-export-group`), written as one PowerPoint group shape.
 */
import { useCallback, useId, useState, type CSSProperties, type SyntheticEvent } from "react";

import type { DeckLogoGridBlock, DeckLogoItem, DeckLogoTone, DeckLogoWallBlock } from "@/types";

import { useDeckAsset, useDeckAssetMeta } from "../asset-context";
import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs, useEditMode } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import {
  LOGO_HEIGHT_PX,
  currentLoad,
  fullRowLength,
  logoGridAlign,
  logoGridEqualCells,
  logoHeightPx,
  logoImageShown,
  logoInkAlphaRow,
  logoInkRange,
  logoMaxBoost,
  logoPending,
  logoRows,
  logoScale,
  logoShape,
  logoWeightHeightPx,
  logoWallAutoMaxPerRow,
  logoWallColumns,
  shortRows,
  trimBox,
  type LogoInkRange,
  type LogoLoad,
} from "../logo-sizing";
import { markupToPlainText } from "../markup";

/** Longest side the colours of an inked logo are sampled at. */
const INK_SAMPLE_PX = 64;

/**
 * The luminance range of a decoded logo (`logoInkRange`), sampled on a small
 * canvas; `undefined` when the pixels cannot be read, and the filter then
 * falls back to absolute luminance.
 */
function readInkRange(img: HTMLImageElement): LogoInkRange | undefined {
  const scale = Math.min(1, INK_SAMPLE_PX / Math.max(img.naturalWidth, img.naturalHeight));
  const width = Math.max(1, Math.round(img.naturalWidth * scale));
  const height = Math.max(1, Math.round(img.naturalHeight * scale));
  try {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return undefined;
    context.drawImage(img, 0, 0, width, height);
    return logoInkRange(context.getImageData(0, 0, width, height).data);
  } catch {
    // A tainted canvas (a cross-origin source) cannot be read.
    return undefined;
  }
}

/** A URL as a CSS `url()` value, quoted and escaped (it rides a custom property). */
function cssUrl(src: string): string {
  return `url("${src.replace(/["\\\n\r]/g, (ch) => `\\${ch.charCodeAt(0).toString(16)} `)}")`;
}

interface LogoLook {
  /** Canvas px of a wide logo (`LOGO_HEIGHT_PX[logo_height]`). */
  heightPx: number;
  balance: boolean;
  /** Every logo at one visual weight (`optical_weight`); wins over `balance`. */
  weight?: boolean;
  /** How far the balance may raise a squarer mark (`logoMaxBoost`); the boost's own cap when unset. */
  maxBoost?: number;
  /** `undefined` = the logos' own colours (also while editing). */
  tone?: Exclude<DeckLogoTone, "original">;
}

/**
 * One logo: a box with the mark's ratio and balanced height, the image
 * cropped to the mark inside it, and the ink layer of a toned wall. Also the
 * logo of a timeline step (`blocks/flow.tsx`).
 */
export function LogoMark({ item, look }: { item: DeckLogoItem; look: LogoLook }) {
  const meta = useDeckAssetMeta(item.file_id);
  // Versioned by the bytes the geometry was read from, so the image and its
  // trim box always come from the same file.
  const { src, loading } = useDeckAsset(item.file_id, meta?.rev);
  // What the image reported, for the source it reported about: a logo
  // replaced in place is shown from a new source, which the old load must
  // not settle. With no stored geometry the ratio is the size the image
  // loads at; until then the box has no size and holds the ready signal.
  const [load, setLoad] = useState<LogoLoad | null>(null);
  const current = currentLoad(load, src);
  const inked = look.tone === "ink";
  const measure = useCallback(
    (img: HTMLImageElement, forSrc: string) => {
      const natural = img.naturalWidth > 0 && img.naturalHeight > 0 ? { width: img.naturalWidth, height: img.naturalHeight } : null;
      setLoad({ src: forSrc, natural, decoded: false });
      // The ink mask paints from the image the browser has just decoded; the
      // export waits for it too (`deckSettled` waits out `data-deck-pending`).
      // `ink` reads the logo's own colours once decoded, before it settles.
      void img
        .decode()
        .catch(() => undefined)
        .then(() => {
          const inkRange = inked && natural ? readInkRange(img) : undefined;
          setLoad((previous) =>
            previous?.src === forSrc ? { ...previous, decoded: true, ...(inkRange ? { inkRange } : {}) } : previous,
          );
        });
    },
    [inked],
  );
  const attach = useCallback(
    (img: HTMLImageElement | null) => {
      if (src && img?.complete && img.naturalWidth > 0) measure(img, src);
    },
    [measure, src],
  );
  const onLoad = useCallback(
    (event: SyntheticEvent<HTMLImageElement>) => {
      if (src) measure(event.currentTarget, src);
    },
    [measure, src],
  );
  const onError = useCallback(() => {
    if (src) setLoad({ src, natural: null, decoded: true, failed: true });
  }, [src]);

  const shape = logoShape(meta, current?.natural);
  const alt = markupToPlainText(item.label);
  const [x0, y0, x1, y1] = trimBox(meta);
  const cropW = x1 - x0;
  const cropH = y1 - y0;
  // `ink` paints the image itself through its own filter: no mask layer.
  const masked = look.tone !== undefined && !inked;
  const inkId = `deck-logo-ink-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const heightPx = shape
    ? (look.weight
        ? logoWeightHeightPx(shape, look.heightPx, look.maxBoost)
        : logoHeightPx(shape, look.heightPx, look.balance, look.maxBoost)) * logoScale(item.scale)
    : 0;
  const style = (
    shape
      ? {
          "--logo-h": `${Math.round(heightPx * 10) / 10}px`,
          "--logo-ratio": String(shape.ratio),
          "--logo-crop-w": `${(100 / cropW).toFixed(3)}%`,
          "--logo-crop-h": `${(100 / cropH).toFixed(3)}%`,
          "--logo-crop-x": `${((-100 * x0) / cropW).toFixed(3)}%`,
          "--logo-crop-y": `${((-100 * y0) / cropH).toFixed(3)}%`,
          ...(masked && src ? { "--logo-mask": cssUrl(src) } : {}),
          ...(inked ? { "--logo-filter": `url(#${inkId})` } : {}),
        }
      : undefined
  ) as CSSProperties | undefined;
  // The ready signal waits for the logo's bytes (the on-screen fetch), then the
  // load and decode of the source now shown (`logoPending`).
  const pending = logoPending(loading, src, current, Boolean(look.tone));
  return (
    <span
      className="deck-logo-mark"
      data-sized={shape ? "true" : undefined}
      data-deck-pending={pending ? "true" : undefined}
      // One picture for the editable PPTX: the cropped (and inked) box.
      data-export-picture="true"
      style={style}
    >
      {inked ? <LogoInkFilter id={inkId} range={current?.inkRange} /> : null}
      {logoImageShown(src, current) ? (
        <img
          ref={attach}
          className="deck-logo"
          src={src}
          alt={alt}
          draggable={false}
          onLoad={onLoad}
          onError={onError}
        />
      ) : null}
      {masked && src ? <span className="deck-logo-ink" aria-hidden="true" /> : null}
    </span>
  );
}

/** One cell: the mark, and its label under it when the block shows labels. */
function LogoCell({
  item,
  look,
  showLabels,
  labelField,
}: {
  item: DeckLogoItem;
  look: LogoLook;
  showLabels: boolean;
  labelField: string;
}) {
  return (
    <div className="deck-logo-cell" role="listitem">
      <LogoMark item={item} look={look} />
      {showLabels && item.label ? (
        <InlineMarkup as="span" className="deck-logo-label" value={item.label} field={labelField} />
      ) : null}
    </div>
  );
}

/**
 * The logos, cut into rows of the given lengths: a column of rows, each one
 * flex line of cells at their logos' widths. A row shorter than a full row -
 * the configured `capacity` (`columns` / `per_row`), else the longest row -
 * or holding a single logo is `data-short` (`shortRows`): centred rather than
 * spread, so a row of two never pins its logos to the edges.
 */
function LogoField({
  items,
  rows,
  capacity,
  look,
  showLabels,
  labelField,
}: {
  items: DeckLogoItem[];
  rows: number[];
  capacity: number | undefined;
  look: LogoLook;
  showLabels: boolean;
  labelField: (index: number) => string;
}) {
  const short = shortRows(rows, capacity);
  let start = 0;
  return (
    <div className="deck-logo-field" role="list" data-rows={rows.length}>
      {rows.map((length, row) => {
        const first = start;
        start += length;
        return (
          <div className="deck-logo-row" key={row} data-short={short[row] ? "true" : undefined}>
            {items.slice(first, first + length).map((item, offset) => (
              <LogoCell
                key={`${item.file_id}-${first + offset}`}
                item={item}
                look={look}
                showLabels={showLabels}
                labelField={labelField(first + offset)}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function useLogoLook(
  block: Pick<DeckLogoGridBlock, "optical_balance" | "optical_weight" | "tone" | "tile_height">,
  heightPx: number,
): LogoLook {
  const editing = useEditMode() !== null;
  const tone = block.tone && block.tone !== "original" && !editing ? block.tone : undefined;
  return {
    heightPx,
    balance: block.optical_balance !== false,
    ...(block.optical_weight ? { weight: true } : {}),
    maxBoost: logoMaxBoost(block),
    tone,
  };
}

/**
 * The filter of `tone: ink` for ONE logo: alpha becomes the pixel's darkness
 * against the logo's own colours (`logoInkAlphaRow`), times the source alpha,
 * then the text colour floods what is left. White letters on a badge stay
 * letters; an orange or yellow wordmark on transparent is inked whole rather
 * than read as pale. `flood-color` reads `--deck-fg` from the stylesheet, so
 * the ink follows the theme. Only rendered for an inked block, so a deck that
 * never sets `ink` renders the same DOM as before.
 */
function LogoInkFilter({ id, range }: { id: string; range: LogoInkRange | undefined }) {
  const values = `0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  ${logoInkAlphaRow(range).join(" ")}`;
  return (
    <svg className="deck-logo-ink-defs" aria-hidden="true" focusable="false" width="0" height="0">
      <filter id={id} colorInterpolationFilters="sRGB" x="0" y="0" width="100%" height="100%">
        <feColorMatrix type="matrix" values={values} result="darkness" />
        <feComposite in="darkness" in2="SourceAlpha" operator="in" result="shape" />
        <feFlood className="deck-logo-ink-flood" result="ink" />
        <feComposite in="ink" in2="shape" operator="in" />
      </filter>
    </svg>
  );
}

/**
 * The attributes a fixed tile height puts on a logo block: the cell
 * height as a custom property the stylesheet reads, the flag its rules key
 * on, and whether a label line is to be left under the mark.
 */
function tileAttributes(block: Pick<DeckLogoGridBlock, "tile_height" | "show_labels">): {
  "data-tile"?: "true";
  "data-labels"?: "true";
  style?: CSSProperties;
} {
  if (!block.tile_height) return {};
  return {
    "data-tile": "true",
    "data-labels": block.show_labels === true ? "true" : undefined,
    style: { "--logo-tile-h": `${block.tile_height}px` } as CSSProperties,
  };
}

/**
 * A wall of logos (`logo_grid`): rows of equal cells, each logo centred in its
 * own (`align: even`, the default with `columns`; `start` under `columns` is
 * the same cells packed from the start), rows spread edge to edge
 * (`stretch`, the default without) or packed (`start` / `center`), `fill`
 * spreading the rows over the height the container leaves, each row centred
 * in its band; `framed` puts each logo on a tile - the whole cell when the
 * cells are equal, a tile hugging the logo otherwise - and the tiles of a row
 * share its tallest logo's height, or the block's `tile_height`. An equal
 * grid's cell count is a runtime value (`--logo-columns`), like the logo
 * geometry.
 */
export function LogoGridBlockView({ block }: { block: DeckLogoGridBlock }) {
  const look = useLogoLook(block, LOGO_HEIGHT_PX[block.logo_height ?? block.size ?? "md"]);
  const rows = logoRows(block.items.length, { perRow: block.columns, rows: block.rows });
  const align = logoGridAlign(block);
  const equalCells = logoGridEqualCells(block);
  const tile = tileAttributes(block);
  const style = {
    ...(equalCells ? { "--logo-columns": String(fullRowLength(rows, block.columns)) } : {}),
    ...tile.style,
  } as CSSProperties;
  return (
    <figure
      className="deck-logo-grid"
      data-framed={block.framed === false ? "false" : "true"}
      data-fill={block.fill ? "true" : undefined}
      data-gap={block.gap ?? "md"}
      data-align={align}
      data-equal-cells={equalCells ? "true" : undefined}
      data-tile={tile["data-tile"]}
      data-labels={tile["data-labels"]}
      data-logo-height={block.logo_height ?? block.size ?? "md"}
      data-logo-tone={look.tone}
      style={Object.keys(style).length ? style : undefined}
      {...useBlockEditAttrs()}
    >
      <LogoField
        items={block.items}
        rows={rows}
        capacity={block.columns}
        look={look}
        showLabels={block.show_labels === true}
        labelField={(index) => `items.${index}.label`}
      />
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}

/**
 * Logos grouped by sector / category (`logo_wall`): one card per group - its
 * icon and title on one line in the group's tone, its logos under them over
 * the card's full width, the rows spread over the card's height. The cards of
 * a row share one height (a CSS grid row), and with `fill` (the default) the
 * rows of cards share the height the slide leaves.
 */
export function LogoWallBlockView({ block }: { block: DeckLogoWallBlock }) {
  const look = useLogoLook(block, LOGO_HEIGHT_PX[block.logo_height ?? "md"]);
  const columns = block.columns ?? logoWallColumns(block.groups.length);
  const autoMaxPerRow = logoWallAutoMaxPerRow(columns);
  const tile = tileAttributes(block);
  const wallStyle = { ...tile.style } as CSSProperties;
  return (
    <figure
      className="deck-logo-wall"
      data-columns={columns}
      data-fill={block.fill === false ? undefined : "true"}
      data-framed={block.framed ? "true" : "false"}
      data-tile={tile["data-tile"]}
      data-labels={tile["data-labels"]}
      data-logo-height={block.logo_height ?? "md"}
      data-logo-tone={look.tone}
      style={Object.keys(wallStyle).length ? wallStyle : undefined}
      {...useBlockEditAttrs()}
    >
      <div className="deck-logo-wall-groups">
        {block.groups.map((group, groupIndex) => (
          // A div, never a <section>: reveal reads a nested section as a vertical slide.
          <div
            className="deck-logo-group"
            key={groupIndex}
            role="group"
            aria-label={markupToPlainText(group.title)}
            data-tone={group.tone ?? "accent"}
            data-export-group="true"
          >
            <div className="deck-logo-group-head">
              {group.icon ? (
                <span className="deck-logo-group-icon">
                  <DeckIcon name={group.icon} />
                </span>
              ) : null}
              <InlineMarkup
                as="h4"
                className="deck-logo-group-title"
                value={group.title}
                field={`groups.${groupIndex}.title`}
              />
            </div>
            <LogoField
              items={group.logos}
              rows={logoRows(group.logos.length, { perRow: block.per_row, autoMaxPerRow })}
              capacity={block.per_row}
              look={look}
              showLabels={block.show_labels === true}
              labelField={(index) => `groups.${groupIndex}.logos.${index}.label`}
            />
          </div>
        ))}
      </div>
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}
