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
 * The backdrop of a `title` / `closing` slide, drawn UNDER its content.
 *
 * Rendered by the opening layouts as the first child of `.deck-title` /
 * `.deck-closing`. It is positioned against the slide root (the frame is not
 * positioned, so `inset: 0` escapes its padding and its clip) and painted in
 * the negative z-order of the frame's stacking context (`.deck-slide` isolates,
 * `presentation.css`), so it sits above the canvas paper and below every block
 * of the slide whatever the block's own stacking. Every colour is a deck token
 * read on THIS slide's root, so a dark cover on a light deck paints dark.
 *
 * Three variants, none of them a raster:
 *   - `gradient`  the brand gradient washed over the paper, the template's
 *                 mark (its `logo_mark` asset, `mark_file_id` on the slide's
 *                 palette) as a faint watermark in the corner - the
 *                 Filigran PowerPoint cover;
 *   - `split`     a right-hand panel (45% of the slide) carrying the gradient
 *                 and the slide's background image as a cover, or a soft
 *                 radial glow when there is none; the text keeps the left;
 *   - `spotlight` one large radial accent glow behind the headline and two
 *                 thin concentric arcs bottom right (inline SVG);
 *   - `mosaic`    pixel blocks clustered in the corners the text leaves free,
 *                 under a film grain (inline SVG, composed by `mosaic.ts` on
 *                 the canvas grid, seeded by the slide so it never moves);
 *   - `cascade` / `horizon` / `pillar` the same tiles in another shape
 *                 (`MOSAIC_HEROES`): stepping down from the top-right corner,
 *                 a skyline along the bottom, a column down the right edge.
 *
 * The COLOUR of a backdrop is the slide's `hero_tone` (`data-hero-tone` on the
 * root element here): the stylesheet maps it onto the `--deck-hero-from` /
 * `--deck-hero-to` pair every variant reads - the template's gradient stops
 * for `brand`, one palette token for both otherwise - so a brand whose
 * gradient runs into a secondary colour still opens on its primary alone.
 *
 * Export: the element painting a `background-image` is what the PPTX
 * extractor screenshots (`data-export-backdrop` asks the capture loop to hide
 * the slide's content first, so the picture never carries the headline the
 * writer sets as editable runs). `aria-hidden` keeps the whole backdrop out
 * of the layout audit's measurements, like the ghost section number.
 */
import { useId, useMemo } from "react";

import type { DeckHero, DeckHeroTone, DeckOverlay } from "@/types";

import { SlideImage } from "./blocks/shared";
import { useDeckCanvas } from "./canvas-context";
import { MOSAIC_HEROES } from "./hero";
import { mosaicTiles, type MosaicComposition, type MosaicPattern } from "./mosaic";
import { useSlideScheme } from "./theme";

/** The watermark mark of the `gradient` cover, from the slide's own palette. */
function HeroMark() {
  const resolution = useSlideScheme();
  const fileId = resolution?.tokens.mark_file_id;
  if (!fileId) return null;
  return <SlideImage fileId={fileId} alt="" className="deck-hero-mark" fit="contain" />;
}

/**
 * A tiled drawing. A tile carries its colour SLOT and tint, never a colour:
 * the stylesheet fills `rect[data-slot]` from the hero tone's six mosaic
 * colours. The grain is the film over the tiles - noise blended in overlay,
 * half strength, clipped to the tiles' own alpha so the paper stays clean.
 * The filter id is per instance: a page renders every slide at once.
 */
function HeroMosaic({
  seed,
  pattern,
  composition,
}: {
  seed: string;
  pattern: MosaicPattern;
  composition: MosaicComposition;
}) {
  const canvas = useDeckCanvas();
  const grid = useMemo(
    () => mosaicTiles({ seed, pattern, composition, width: canvas.width, height: canvas.height }),
    [seed, pattern, composition, canvas.width, canvas.height],
  );
  const grainId = `deck-mosaic-grain-${useId().replace(/[^A-Za-z0-9_-]/g, "")}`;
  const cell = grid.tile;
  const width = grid.cols * cell;
  const height = grid.rows * cell;
  return (
    <svg
      className="deck-hero-mosaic"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid slice"
      focusable="false"
    >
      <defs>
        <filter
          id={grainId}
          filterUnits="userSpaceOnUse"
          x="0"
          y="0"
          width={width}
          height={height}
          colorInterpolationFilters="sRGB"
        >
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves={2} stitchTiles="stitch" result="noise" />
          <feColorMatrix in="noise" type="matrix" values="1 0 0 0 0  1 0 0 0 0  1 0 0 0 0  0 0 0 0 1" result="grey" />
          <feBlend in="grey" in2="SourceGraphic" mode="overlay" result="grained" />
          <feComposite in="grained" in2="SourceGraphic" operator="arithmetic" k1={0} k2={0.45} k3={0.55} k4={0} result="mixed" />
          <feComposite in="mixed" in2="SourceGraphic" operator="in" />
        </filter>
      </defs>
      <g filter={`url(#${grainId})`}>
        {grid.tiles.map((tile) => (
          <rect
            key={`${tile.col}-${tile.row}`}
            x={tile.col * cell}
            y={tile.row * cell}
            width={tile.w * cell}
            height={tile.h * cell}
            data-slot={tile.slot}
            data-tint={tile.tint ? "true" : undefined}
          />
        ))}
      </g>
    </svg>
  );
}

export function HeroBackdrop({
  hero,
  tone = "brand",
  imageFileId,
  overlay,
  seed = "",
  composition = "start",
}: {
  hero: DeckHero;
  /** The palette colour(s) the backdrop is drawn in (`slideHeroTone`). */
  tone?: DeckHeroTone;
  /** The slide's background image - hosted by the `split` panel. */
  imageFileId?: string;
  overlay: DeckOverlay;
  /** What seeds a generated backdrop (the `mosaic`): the slide's id. */
  seed?: string;
  /** Where the cover's content sits, for a backdrop that keeps it clear. */
  composition?: MosaicComposition;
}) {
  switch (hero) {
    case "gradient":
      return (
        <div
          className="deck-hero-backdrop"
          data-hero="gradient"
          data-hero-tone={tone}
          data-export-backdrop="true"
          aria-hidden="true"
        >
          <HeroMark />
        </div>
      );
    case "split":
      return (
        <div className="deck-hero-backdrop" data-hero="split" data-hero-tone={tone} aria-hidden="true">
          {/* `data-export-backdrop` sits on the PANEL, not on the wrapper: the
              wrapper paints nothing of its own here, so the PPTX captures the
              right-hand gradient / image panel alone as a picture and the left
              text panel stays native, editable runs - unlike `gradient` and
              `spotlight`, whose wash covers the whole slide. */}
          <div
            className="deck-hero-panel"
            data-export-backdrop="true"
            data-has-image={imageFileId ? "true" : undefined}
            data-overlay={imageFileId && overlay !== "none" ? overlay : undefined}
          >
            {imageFileId ? (
              <SlideImage fileId={imageFileId} alt="" className="deck-hero-panel-image" fit="cover" />
            ) : (
              <div className="deck-hero-glow" />
            )}
          </div>
        </div>
      );
    case "spotlight":
      return (
        <div
          className="deck-hero-backdrop"
          data-hero="spotlight"
          data-hero-tone={tone}
          data-export-backdrop="true"
          aria-hidden="true"
        >
          <svg className="deck-hero-arcs" viewBox="0 0 800 800" fill="none" focusable="false">
            <circle cx="800" cy="800" r="520" />
            <circle cx="800" cy="800" r="680" />
          </svg>
        </div>
      );
    case "mosaic":
    case "cascade":
    case "horizon":
    case "pillar":
      return (
        <div
          className="deck-hero-backdrop"
          data-hero={hero}
          data-hero-tone={tone}
          data-export-backdrop="true"
          aria-hidden="true"
        >
          <HeroMosaic seed={seed} pattern={MOSAIC_HEROES[hero] ?? "scatter"} composition={composition} />
        </div>
      );
    default:
      return null;
  }
}
