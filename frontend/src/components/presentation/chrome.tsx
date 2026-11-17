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
 * Deck chrome: the furniture every slide carries — brand logo, footer
 * bar, slide number, and the header bar a template may define.
 *
 * Drawn INSIDE the 1920x1080 canvas rather than by reveal's own `slideNumber` /
 * overlay controls, which live outside the scaled slide and vanish from a PDF or
 * a captured PNG. Anything inside the canvas exports exactly as it displays.
 *
 * What shows where is decided by the layout, not the agent: opening, section
 * and closing slides carry the logo only; body slides also carry the footer bar
 * and the header (`slideShowsFooter`), which a slide can opt out of with
 * `hide_footer`. The deck-level `chrome` toggles switch each piece off globally.
 *
 * WHERE the logo sits is the one placement the deck decides
 * (`chrome.logo_position`), and only on the opening and closing slides — the two
 * with no footer bar in their bottom row (the title slide has no number either,
 * the closing slide keeps its number in the corner), so the corner is a choice
 * there rather than a necessity. `auto` follows the slide's own composition (a left-aligned
 * cover puts it bottom-left on the edge the headline starts on, a centred cover
 * and the closing slide put it on the slide axis); `start` / `center` / `end`
 * pin it. Body and section slides keep the corner whatever the deck says: the
 * footer bar and the slide number share their row, and the mark must not move
 * between two body slides (`resolveLogoPosition` in `logo-position.ts`). The
 * position lands on the bar as `data-logo-position`; `start` is DOM order
 * alone (the logo is the row's first child), `center` is the stylesheet's
 * three-column grid.
 *
 * WHAT the bars say is not decided here either: `text` is computed once per
 * deck by `chromeText()` (`chrome-pattern.ts`) — the template's footer /
 * header pattern filled from the deck settings when the deck wears a template
 * that defines one, the v1 `event_label` / `footer_text` segments otherwise.
 * This component never reads a deck title or a slide's text: the footer is a
 * deck setting, so it is the same on every slide by construction.
 *
 * The footer bar (`.deck-chrome`) stays the slide frame's NEXT sibling: the
 * stylesheet's `.deck-slide:has(+ .deck-chrome)` reserves the body room above
 * it. The template's corner emblem (its `chrome.emblem`, `chromeEmblem()`)
 * comes next - the mark, small, in the top margin's right corner of every
 * slide but the opening and closing ones, whose backdrop carries the brand -
 * then the header, which its sibling rule keeps clear of the emblem.
 *
 * A template may also declare `footer_right`: the footer's right corner
 * then reads that pattern (`chromeFooterRight()`, the page in two digits and
 * the year filled per slide) instead of the `n / total` number, and nothing on
 * the opening and closing slides. Its `emblem_size` / `emblem_align` place the
 * emblem (`data-align="title"`: on the right margin, its top on the title's).
 * `emblem_ink: "accent"` inks a one-colour mark with the slide's accent
 * on a light slide and its foreground on a dark one: the mark is drawn as a CSS
 * mask over that colour (`EmblemInk`), not as an image under a filter.
 */
import type { CSSProperties } from "react";

import type { DeckChrome as DeckChromeSpec, PresentationSlide, PresentationThemeConfig } from "@/types";

import { useDeckAssetSrc } from "./asset-context";
import { SlideImage } from "./blocks/shared";
import type { ChromeText } from "./chrome-pattern";
import { heroHostsLogo } from "./hero";
import { slideShowsFooter } from "./layouts/footer";
import { resolveLogoPosition } from "./logo-position";

/** The slides that carry no corner emblem: the covers, which wear the brand in their backdrop. */
const EMBLEM_FREE_LAYOUTS: ReadonlySet<string> = new Set(["title", "closing"]);

/**
 * The emblem inked in ONE colour (`emblem_ink: "accent"`): the mark's
 * shape is the mask of a box painted with the scheme's emblem ink
 * (`--deck-emblem-ink`). One picture for the editable PPTX
 * (`data-export-picture`), like a logo mark.
 */
function EmblemInk({ fileId }: { fileId: string }) {
  const src = useDeckAssetSrc(fileId);
  if (!src) return null;
  return (
    <span
      className="deck-chrome-emblem-ink"
      data-export-picture="true"
      style={{ "--deck-emblem-mask": `url("${src}")` } as CSSProperties}
    />
  );
}

export function DeckChrome({
  chrome,
  text,
  emblem = false,
  footerRight = null,
  emblemSize,
  emblemAlign,
  emblemInk,
  sectionIsBody = false,
  coverHostsLogo = false,
  slide,
  index,
  total,
  theme,
  themeConfig,
}: {
  chrome: DeckChromeSpec | undefined;
  /** What the footer and header read on this deck — `chromeText()`. */
  text: ChromeText;
  /** Whether the template's corner emblem is drawn on this deck — `chromeEmblem()`. */
  emblem?: boolean;
  /** The template's right-corner footer on this slide — `chromeFooterRight()`;
   *  `null` when the template declares none (the number is drawn). */
  footerRight?: string | null;
  /** The emblem's height in canvas px (`TemplateChrome.emblem_size`). */
  emblemSize?: number;
  /** Where the emblem sits (`TemplateChrome.emblem_align`). */
  emblemAlign?: "margin" | "title";
  /** How the emblem is inked (`TemplateChrome.emblem_ink`); `mark` by default. */
  emblemInk?: "mark" | "accent";
  /** The template draws its section slide as a body slide, footer included
   *  (`design.section: "outline"`). */
  sectionIsBody?: boolean;
  /** The template's cover and closing draw the logo themselves
   *  (`design.cover: "brand"`): the chrome leaves its own out there. */
  coverHostsLogo?: boolean;
  slide: PresentationSlide;
  index: number;
  total: number;
  theme: string;
  themeConfig: PresentationThemeConfig | undefined;
}) {
  // A cover that draws the logo itself, large in its top corner (`cascade`,
  // `horizon`), keeps the bottom row free of a second one.
  const showLogo =
    (chrome?.show_logo ?? true) &&
    !heroHostsLogo(slide) &&
    !(coverHostsLogo && EMBLEM_FREE_LAYOUTS.has(slide.layout));
  const showFooter = (chrome?.show_footer ?? true) && slideShowsFooter(slide, sectionIsBody);
  // A template's right footer replaces the number, and the covers carry none.
  const rightText = footerRight !== null && !EMBLEM_FREE_LAYOUTS.has(slide.layout) ? footerRight : "";
  const showNumbers = footerRight === null && (chrome?.show_slide_numbers ?? true) && slide.layout !== "title";
  // The header is the template's: it shows on the slides the footer shows on,
  // under the same `show_footer` switch (the deck editor labels the switch
  // "footer and header" when the template defines one), as long as the
  // pattern reads something on this deck.
  const header = showFooter && text.header ? text.header : null;
  // A light theme wants the dark-on-light logo variant when the brand ships one.
  const logoFileId =
    theme === "light"
      ? themeConfig?.logo_light_file_id ?? themeConfig?.logo_file_id
      : themeConfig?.logo_file_id;
  const footerParts = text.footer;
  const logoPosition = resolveLogoPosition(chrome?.logo_position, slide);
  const logo =
    showLogo && logoFileId ? (
      <SlideImage fileId={logoFileId} alt="" className="deck-chrome-logo" fit="contain" />
    ) : null;
  // The right-hand group exists for what it holds: the number, and the logo
  // when it keeps the corner. An empty group would still be a grid column.
  const showRight = showNumbers || rightText !== "" || (logo !== null && logoPosition === "end");
  // The emblem is the slide's own palette mark, so a light slide draws the
  // mark its scheme resolves to; `emblem` already folds in `show_logo`.
  const emblemFileId = emblem && !EMBLEM_FREE_LAYOUTS.has(slide.layout) ? themeConfig?.mark_file_id : undefined;

  const showBar = showLogo || showFooter || showNumbers || rightText !== "";
  if (!showBar && !header && !emblemFileId) return null;
  return (
    <>
      {showBar ? (
        <div className="deck-chrome" aria-hidden="true" data-logo-position={logoPosition}>
          {logoPosition === "start" ? logo : null}
          {showFooter && footerParts.length ? (
            <div className="deck-chrome-footer">
              {footerParts.map((part, partIndex) => (
                <span key={partIndex}>{part}</span>
              ))}
            </div>
          ) : null}
          {logoPosition === "center" ? logo : null}
          {showRight ? (
            <div className="deck-chrome-right">
              {showNumbers ? (
                <span className="deck-chrome-number">
                  {index + 1}
                  <span className="deck-chrome-total"> / {total}</span>
                </span>
              ) : null}
              {rightText ? <span className="deck-chrome-legal">{rightText}</span> : null}
              {logoPosition === "end" ? logo : null}
            </div>
          ) : null}
        </div>
      ) : null}
      {emblemFileId ? (
        <span
          className="deck-chrome-emblem"
          aria-hidden="true"
          data-align={emblemAlign === "title" ? "title" : undefined}
          data-ink={emblemInk === "accent" ? "accent" : undefined}
          style={emblemSize ? ({ "--deck-emblem-size": `${emblemSize}px` } as CSSProperties) : undefined}
        >
          {emblemInk === "accent" ? (
            <EmblemInk fileId={emblemFileId} />
          ) : (
            <SlideImage fileId={emblemFileId} alt="" className="deck-chrome-emblem-mark" fit="contain" />
          )}
        </span>
      ) : null}
      {header ? (
        <div className="deck-chrome-header" aria-hidden="true">
          {header}
        </div>
      ) : null}
    </>
  );
}
