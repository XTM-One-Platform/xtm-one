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
 * The `quote` block: four looks over one structure - an optional
 * oversized quote mark, the text, the credit row (a portrait, "Author, Role,
 * Organization", a company logo) and up to four unboxed figures.
 *
 * `card` is the historical rendering and keeps its DOM attribute for attribute
 * (`quoteLookAttributes` writes nothing at the card's values, and the legacy
 * one-line attribution renders into a bare `figcaption` as it always did);
 * `statement`, `pull` and `testimonial` are overrides of it in the stylesheet,
 * keyed by `data-*`. Every mark is a real glyph in the text flow, never a CSS
 * pseudo-element: the editable PPTX export reads text nodes, so the mark
 * travels as a text run in the accent colour. The decorative glyph is
 * `aria-hidden` (the blockquote already says it is a quotation).
 */
import { Fragment, type RefObject, useLayoutEffect, useRef } from "react";

import { useFormatter } from "@/i18n";
import type { DeckQuoteBlock } from "@/types";

import { useDeckPictureLabel } from "../asset-context";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { MetricTile } from "./metrics";
import {
  type QuoteCredit,
  type QuoteCreditField,
  centredPullRule,
  fitHeroQuote,
  quoteCredit,
  quoteLookAttributes,
  quotePictureAlt,
  resolveQuoteLook,
} from "./quote-style";
import { SlideImage } from "./shared";

const OPEN_MARK = "\u201C";
const CLOSE_MARK = "\u201D";

const CREDIT_CLASS: Record<QuoteCreditField, string> = {
  author: "deck-quote-author",
  role: "deck-quote-role",
  organization: "deck-quote-org",
};

const lineHeightOf = (text: object) => parseFloat(getComputedStyle(text as Element).lineHeight);

/**
 * Re-measure a hero quote after every layout and once the deck fonts land
 * (`fitHeroQuote`). A resize is answered in an animation frame, like `fit.ts`
 * and `spread.ts`: a step written inside the observer's own delivery resizes
 * the box it watches and raises "loop completed with undelivered
 * notifications"; a pass that changes nothing produces no notification.
 */
function useFitHeroQuote(ref: RefObject<HTMLElement | null>, active: boolean, deps: readonly unknown[]) {
  useLayoutEffect(() => {
    const figure = ref.current;
    if (!figure) return;
    const fit = () => fitHeroQuote(figure, lineHeightOf);
    fit();
    if (!active) return;
    let cancelled = false;
    // A glyph-width change from the web font moves no box the observer below
    // watches until it wraps, so the pass runs again once the fonts are ready -
    // the export page and the layout audit then count lines in the drawn font.
    const fonts = typeof document !== "undefined" ? document.fonts : undefined;
    if (fonts?.ready) {
      void fonts.ready.then(() => {
        if (!cancelled) fit();
      });
    }
    if (typeof ResizeObserver === "undefined") {
      return () => {
        cancelled = true;
      };
    }
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        fit();
      });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(figure);
    return () => {
      cancelled = true;
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the text and the look are the dependency
  }, [active, ...deps]);
}

export function QuoteBlockView({ block }: { block: DeckQuoteBlock }) {
  const look = resolveQuoteLook(block);
  const figureRef = useRef<HTMLElement>(null);
  useFitHeroQuote(figureRef, look.size === "hero", [block.content, look.variant, look.align, look.quote_marks]);
  const inline = look.quote_marks === "inline";
  return (
    <figure ref={figureRef} className="deck-quote" {...quoteLookAttributes(look)} {...useBlockEditAttrs()}>
      {centredPullRule(look) ? <span className="deck-quote-rule" aria-hidden="true" /> : null}
      {look.quote_marks === "decorative" ? (
        <span className="deck-quote-glyph" aria-hidden="true">
          {OPEN_MARK}
        </span>
      ) : null}
      <blockquote>
        {inline ? <span className="deck-quote-open">{OPEN_MARK}</span> : null}
        <InlineMarkup value={block.content} field="content" />
        {inline ? <span className="deck-quote-close">{CLOSE_MARK}</span> : null}
      </blockquote>
      <QuoteCaption block={block} credit={quoteCredit(block)} />
      {block.figures?.length ? (
        <div className="deck-quote-figures">
          {block.figures.map((figure, index) => (
            <MetricTile key={index} metric={figure} field={`figures.${index}`} variant="plain" align={look.align} />
          ))}
        </div>
      ) : null}
    </figure>
  );
}

/** The credit's words: the legacy line as it always rendered, or "Author, Role, Organization". */
function CreditWords({ credit }: { credit: QuoteCredit }) {
  if (credit.kind === "attribution") return <InlineMarkup value={credit.value} field="attribution" />;
  return (
    <>
      {credit.parts.map((part, index) => (
        <Fragment key={part.field}>
          {index > 0 ? ", " : null}
          <InlineMarkup as="span" className={CREDIT_CLASS[part.field]} value={part.value} field={part.field} />
        </Fragment>
      ))}
    </>
  );
}

/**
 * The credit row. Without a picture it is the bare `figcaption` of the card;
 * with one it is a row - the portrait before the words, the company logo
 * after them - each picture its own box so the export places it as an image
 * and frames the words on their own. A picture the visible credit names
 * takes an empty `alt` (a name read twice is noise to a screen reader); one
 * it does not name is announced by the file's description, its filename, or
 * a generic label, never as nothing (`quotePictureAlt`). The static export
 * page renders this same DOM, so the PDF and the PPTX carry the same alt.
 */
function QuoteCaption({ block, credit }: { block: DeckQuoteBlock; credit: QuoteCredit | null }) {
  const { t_i18n } = useFormatter();
  const photo = block.photo_file_id;
  const logo = block.logo_file_id;
  const photoAlt = quotePictureAlt(block, "photo_file_id", useDeckPictureLabel(photo), t_i18n("Portrait"));
  const logoAlt = quotePictureAlt(block, "logo_file_id", useDeckPictureLabel(logo), t_i18n("Logo"));
  if (!credit && !photo && !logo) return null;
  if (!photo && !logo) {
    return (
      <figcaption>
        <CreditWords credit={credit as QuoteCredit} />
      </figcaption>
    );
  }
  return (
    <figcaption data-pictured="true">
      {photo ? (
        <span className="deck-quote-photo">
          <SlideImage fileId={photo} alt={photoAlt} className="deck-quote-portrait" fit="cover" />
        </span>
      ) : null}
      {credit ? (
        <span className="deck-quote-credit">
          <CreditWords credit={credit} />
        </span>
      ) : null}
      {logo ? (
        <span className="deck-quote-logo">
          <SlideImage fileId={logo} alt={logoAlt} className="deck-quote-logo-image" fit="contain" />
        </span>
      ) : null}
    </figcaption>
  );
}
