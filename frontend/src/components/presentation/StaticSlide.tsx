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
 * One slide (or portrait page) laid out as an UNSCALED canvas, with no reveal
 * runtime: the same `SlideBody` + `DeckChrome` the on-screen deck
 * renders, on a `.deck-canvas.deck-static-slide` box of the format's exact
 * pixel size (`DECK_SIZE`).
 *
 * Two consumers, one definition, so what they show cannot drift:
 *   - `PresentationStaticRender`, the page the headless browser prints,
 *     screenshots and measures for the PDF / PPTX export;
 *   - `PageDocument`, the on-screen viewer of a portrait page document, which
 *     scales each canvas to the reader's window.
 *
 * The per-slide background is deck data resolved at runtime (a platform file),
 * so it is an inline style — the same exception as the theme variables.
 *
 * The canvas is a SLIDE ROOT: it carries the scheme the slide is read
 * in (`data-scheme`, keying the stylesheet's token block) and the template
 * palette for that scheme inline, resolved through `resolveSlideScheme` - the
 * one resolver the reveal deck uses too - so a slide with a theme of its own
 * paints its own paper, ink and logo whatever the deck theme is, on screen,
 * in the PDF and in the DOM the PPTX writer measures.
 */
import { DeckAssetMetaProvider, DeckPictureLabelProvider, NO_PICTURE_LABELS, useDeckAssetSrc } from "./asset-context";
import { DeckBrandContext, deckBrandAssets } from "./brand-picture";
import { DeckCanvasContext, DeckShowsLogoContext } from "./canvas-context";
import { DeckChrome } from "./chrome";
import { chromeEmblem, chromeFooterRight, chromeText, chromeWatermark } from "./chrome-pattern";
import { DeckDesignContext } from "./design-context";
import { designRootAttributes } from "./design-tokens";
import { slidePaletteAttributes, slidePaletteStyle } from "./slide-palette";
import { SlideIndexContext, useEditMode } from "./edit-context";
import { deckAspectRatio, deckSize } from "./geometry";
import { heroHostsBackgroundImage } from "./hero";
import { SlideBody } from "./layouts";
import { slideAnchorId, useSlideLinks } from "./slide-link-context";
import { resolveSlideScheme, SlideSchemeContext, slideRootAttributes } from "./theme";
import type { Presentation, PresentationSlide } from "@/types";

export function StaticSlide({
  slide,
  index,
  presentation,
}: {
  slide: PresentationSlide;
  index: number;
  presentation: Presentation;
}) {
  const { spec } = presentation;
  // A `split` hero hosts the background image in its own panel; the canvas
  // must not paint it full-bleed as well.
  const background = useDeckAssetSrc(heroHostsBackgroundImage(slide) ? undefined : slide.background_image_file_id);
  const resolution = resolveSlideScheme(presentation, slide);
  const root = slideRootAttributes(resolution);
  // The template's design tokens: flat switches as attributes, radius
  // and spacing as variables; nothing at all for a template declaring none.
  const { style: designStyle, ...design } = designRootAttributes(presentation.template_design);
  // The slide's own palette after the template's, so its tokens win.
  const rootStyle = { ...root.style, ...slidePaletteStyle(slide, resolution.scheme), ...designStyle };
  // The frame's index and id are what the in-place editor resolves a
  // click to; published in edit mode only, so the export page and the
  // thumbnails render the DOM they always did (the outer canvas keeps its own
  // `data-slide-id`, which the layout audit reads).
  const editing = !!useEditMode();
  // The export page links slides natively (`#deck-slide-<n>`, kept by the PDF
  // as an internal link): its canvases are the anchors. Nowhere else - two
  // decks on one page (a transcript) would repeat the ids.
  const anchored = !!useSlideLinks()?.href;
  return (
    <div
      id={anchored ? slideAnchorId(index) : undefined}
      className="deck-canvas deck-static-slide"
      data-aspect={deckAspectRatio(spec.aspect_ratio)}
      data-slide-index={index}
      data-slide-id={slide.id}
      data-scheme={root["data-scheme"]}
      data-scheme-override={root["data-scheme-override"]}
      data-has-background={background ? "true" : undefined}
      {...design}
      {...slidePaletteAttributes(slide)}
      style={background ? { ...rootStyle, backgroundImage: `url("${background}")` } : rootStyle}
    >
      <SlideSchemeContext.Provider value={resolution}>
        <DeckBrandContext.Provider value={deckBrandAssets(presentation)}>
        <div
          className="deck-slide"
          data-layout={slide.layout}
          data-slide-index={editing ? index : undefined}
          data-slide-id={editing ? slide.id : undefined}
        >
          <SlideIndexContext.Provider value={index}>
            <DeckAssetMetaProvider value={presentation.asset_meta}>
              <DeckPictureLabelProvider value={presentation.picture_labels ?? NO_PICTURE_LABELS}>
                <DeckCanvasContext.Provider value={deckSize(spec.aspect_ratio)}>
                  <DeckShowsLogoContext.Provider value={spec.chrome?.show_logo ?? true}>
                    <DeckDesignContext.Provider value={presentation.template_design ?? null}>
                      <SlideBody slide={slide} watermark={chromeWatermark(spec, presentation.template_chrome)} />
                    </DeckDesignContext.Provider>
                  </DeckShowsLogoContext.Provider>
                </DeckCanvasContext.Provider>
              </DeckPictureLabelProvider>
            </DeckAssetMetaProvider>
          </SlideIndexContext.Provider>
        </div>
        <DeckChrome
          chrome={spec.chrome}
          // The same footer / header text the on-screen deck computes.
          text={chromeText(spec, presentation.template_chrome)}
          emblem={chromeEmblem(spec, presentation.template_chrome)}
          // The page and the year, filled at render (and so at export) time.
          footerRight={chromeFooterRight(spec, presentation.template_chrome, index)}
          emblemSize={presentation.template_chrome?.emblem_size}
          emblemAlign={presentation.template_chrome?.emblem_align}
          emblemInk={presentation.template_chrome?.emblem_ink}
          sectionIsBody={presentation.template_design?.section === "outline"}
          coverHostsLogo={presentation.template_design?.cover === "brand"}
          slide={slide}
          index={index}
          total={spec.slides.length}
          theme={resolution.scheme}
          themeConfig={resolution.tokens}
        />
        </DeckBrandContext.Provider>
      </SlideSchemeContext.Provider>
    </div>
  );
}
