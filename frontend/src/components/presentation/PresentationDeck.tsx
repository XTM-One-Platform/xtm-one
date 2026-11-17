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
 * `@revealjs/react` renderer for an agent-generated deck.
 *
 * Takes a validated `PresentationSpec` and renders one `<Slide>` per entry, each
 * body produced by the controlled layout component for its `layout`. Navigation
 * (keyboard, touch, overview), transitions and PDF export come from reveal.js
 * itself. Speaker notes do NOT: reveal's notes plugin loads the current page
 * into iframes (which showed the studio rail as the "next slide"), so the
 * speaker view is the platform's own window (`speaker-sync.ts`) and no
 * reveal plugin is loaded here.
 *
 * Geometry: every deck is authored on a FIXED reference canvas — 1920x1080 for
 * 16:9, 1440x1080 for 4:3 — and reveal scales that canvas to whatever box it is
 * given (a chat card, a fullscreen element, a projector, a headless export
 * page). Every size in `presentation.css` is therefore an absolute pixel value
 * on that canvas, which is what makes the on-screen deck, the PDF and the PPTX
 * pixel-identical: they all rasterize the same 1920x1080 layout.
 *
 * Editing behaviour: when the agent adds, removes or reorders
 * slides, the backend rewrites the SAME deck row and bumps `revision`. This
 * component must therefore NOT be keyed on `revision` — React reconciles the
 * `<Slide>` children in place, slides are keyed by their stable `id`, and the
 * effect below calls `Reveal.sync()` + `layout()` on every revision so the deck
 * updates without remounting (and without resetting the viewer to slide 1). Key
 * on the deck `id` only.
 *
 * Controls: reveal's own on-slide chrome (corner arrows, progress bar, help
 * overlay, jump box, its `F` fullscreen of the bare element) is switched OFF.
 * The slide surface carries the slide and nothing else; every operation lives
 * in `DeckToolbar` (driven through `useDeckController`, attached via `onDeck`)
 * and the keys reveal keeps are gated by `deckMayHandleKey` so a menu, a dialog
 * or a form next to the deck owns its own arrows. App-level shortcuts (`F`,
 * `G`, `S`, `?`, `F1`, `Esc`) are bound through reveal's `keyboard` map so they go
 * through the same gate and the same input/modifier checks as every other key,
 * and are reported to the owner through `onShortcut`.
 *
 * Only reveal's structural CSS is imported; the visual identity comes from
 * `presentation.css` (see the note there about reveal's own themes).
 */
import { useCallback, useEffect, useMemo, useRef } from "react";
import { Deck, Slide } from "@revealjs/react";
import type { RevealApi } from "reveal.js";
import "reveal.js/reveal.css";

import { useDeckPolls } from "@/hooks/api/usePresentationApi";
import { cn } from "@/lib/utils";
import type { DeckChrome as DeckChromeSpec, Presentation, PresentationSlide, PresentationTransition } from "@/types";

import { DeckAssetMetaProvider, DeckPictureLabelProvider, NO_PICTURE_LABELS, useDeckAssetSrc } from "./asset-context";
import { DeckCanvasContext, DeckShowsLogoContext } from "./canvas-context";
import { DeckChrome } from "./chrome";
import { chromeEmblem, chromeFooterRight, chromeText, chromeWatermark, type ChromeText } from "./chrome-pattern";
import { DeckDesignContext } from "./design-context";
import { designRootAttributes } from "./design-tokens";
import { slidePaletteAttributes, slidePaletteStyle } from "./slide-palette";
import { DECK_KEY_CODES, deckMayHandleKey, type DeckShortcutAction } from "./deck-keys";
import { EditModeContext, SlideIndexContext, type SlideEditSession, useEditMode } from "./edit-context";
import { deckFormat, deckSize } from "./geometry";
import { heroHostsBackgroundImage } from "./hero";
import { SlideBody } from "./layouts";
import { DeckPollsContext, specHasPoll } from "./poll-context";
import { SlideLinkContext, useSlideLinksValue } from "./slide-link-context";
import { resolveSlideScheme, SlideSchemeContext, slidePaper, slideRootAttributes, themeConfigFor, themeStyle } from "./theme";
import { DeckBrandContext, deckBrandAssets } from "./brand-picture";
import "./presentation.css";

export type DeckVariant =
  /** Inside the app shell (chat card, dialog): reveal must not own the page. */
  | "embedded"
  /** The standalone `/presentations/:id` viewer — keyboard always active. */
  | "standalone"
  /** Print layout for PDF export (reveal lays every slide out as a page). */
  | "print";

/**
 * How the papers under the slides change with them. Each slide's
 * paper is its reveal background, with nothing under it on a stage: the
 * papers slide in step with a sliding deck, cross-fade in place under a fade,
 * a zoom or a turn (the stage holds the outgoing paper under the incoming
 * one, see `presentation.css`), and switch at once when the deck has no
 * transition. Sliding papers under a fading slide swept a hard seam across
 * it.
 */
function paperTransition(transition: PresentationTransition): "none" | "slide" | "fade" {
  switch (transition) {
    case "none":
      return "none";
    case "fade":
    case "zoom":
    case "convex":
    case "concave":
      return "fade";
    default:
      return "slide";
  }
}

/**
 * One slide. Its own component so the background image (a platform file id) can
 * be resolved with a hook per slide — `<Slide backgroundImage>` needs a URL, and
 * platform downloads require the JWT in a header (`asset-context.tsx`).
 *
 * The body is wrapped in `.deck-slide`, the flex frame that fills the whole
 * canvas and carries the slide padding. reveal toggles `display` on the
 * `<section>` itself to show/hide slides, so the frame cannot be the section.
 * The chrome (logo, footer, number) is a sibling drawn over the frame so it
 * never takes part in the body's flex layout.
 *
 * The section is the SLIDE ROOT: it carries the scheme this slide is
 * read in and the template palette for it (`resolveSlideScheme`, the same
 * resolver `StaticSlide` uses), so a slide with a theme of its own draws its
 * own paper, ink, tones and logo whatever the deck theme is. A slide that
 * INHERITS keeps reveal's background layer for its image and a transparent
 * section. A slide that OVERRIDES paints its own paper and image on the
 * section itself (`data-scheme-override`), so a light cover on a dark deck
 * slides in with its paper and never flashes the deck's.
 *
 * Either way the paper UNDER the slide is the slide's own: reveal
 * paints `slidePaper` as the slide's background, inside the deck's clip, and
 * on a stage the boxes that clip paint none. A deck-wide paper under every
 * slide left the section - scaled to a fractional size by reveal's transform
 * - a sub-pixel short of the snapped paper, and the deck's white showed as a
 * hairline along one edge of a dark cover, which edge depending on the size
 * of the stage.
 */
function DeckSlide({
  slide,
  index,
  total,
  chrome,
  chromeTextValue,
  presentation,
}: {
  slide: PresentationSlide;
  index: number;
  total: number;
  chrome: DeckChromeSpec | undefined;
  chromeTextValue: ChromeText;
  presentation: Presentation;
}) {
  // A `split` hero hosts the background image in its own panel.
  const background = useDeckAssetSrc(heroHostsBackgroundImage(slide) ? undefined : slide.background_image_file_id);
  const resolution = resolveSlideScheme(presentation, slide);
  const root = slideRootAttributes(resolution);
  // The template's design tokens, on the same root as the palette.
  const { style: designStyle, ...design } = designRootAttributes(presentation.template_design);
  // The slide's own palette after the template's, so its tokens win.
  const rootStyle = { ...root.style, ...slidePaletteStyle(slide, resolution.scheme), ...designStyle };
  // The frame's index and id are what the in-place editor resolves a
  // click to; published in edit mode only, so a presented deck, the chat card
  // and the speaker view render the DOM they always did.
  const editing = !!useEditMode();
  // `notes` is deliberately NOT handed to reveal: the speaker view reads them
  // from the spec, and an `<aside class="notes">` in the slide DOM would be
  // one more thing for the export's primitive extraction to skip.
  const ownPaper = resolution.overridden;
  const paper = slidePaper(resolution);
  return (
    <Slide
      backgroundColor={paper.color}
      // reveal's per-slide sync writes a gradient but never clears one: a slide
      // whose palette names none says so, or a theme or template picked in the
      // editor would leave the old gradient over the new paper.
      backgroundGradient={paper.gradient ?? "none"}
      backgroundImage={!ownPaper && background ? background : undefined}
      backgroundSize={!ownPaper && background ? "cover" : undefined}
      data-scheme={root["data-scheme"]}
      data-scheme-override={root["data-scheme-override"]}
      data-has-background={ownPaper && background ? "true" : undefined}
      {...design}
      {...slidePaletteAttributes(slide)}
      style={ownPaper && background ? { ...rootStyle, backgroundImage: `url("${background}")` } : rootStyle}
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
                <DeckCanvasContext.Provider value={deckSize(presentation.spec.aspect_ratio)}>
                  <DeckShowsLogoContext.Provider value={chrome?.show_logo ?? true}>
                    <DeckDesignContext.Provider value={presentation.template_design ?? null}>
                      <SlideBody slide={slide} watermark={chromeWatermark(presentation.spec, presentation.template_chrome)} />
                    </DeckDesignContext.Provider>
                  </DeckShowsLogoContext.Provider>
                </DeckCanvasContext.Provider>
              </DeckPictureLabelProvider>
            </DeckAssetMetaProvider>
          </SlideIndexContext.Provider>
        </div>
        <DeckChrome
          chrome={chrome}
          text={chromeTextValue}
          emblem={chromeEmblem(presentation.spec, presentation.template_chrome)}
          footerRight={chromeFooterRight(presentation.spec, presentation.template_chrome, index)}
          emblemSize={presentation.template_chrome?.emblem_size}
          emblemAlign={presentation.template_chrome?.emblem_align}
          emblemInk={presentation.template_chrome?.emblem_ink}
          sectionIsBody={presentation.template_design?.section === "outline"}
          coverHostsLogo={presentation.template_design?.cover === "brand"}
          slide={slide}
          index={index}
          total={total}
          theme={resolution.scheme}
          themeConfig={resolution.tokens}
        />
        </DeckBrandContext.Provider>
      </SlideSchemeContext.Provider>
    </Slide>
  );
}

export function PresentationDeck({
  presentation,
  variant = "embedded",
  keyboardActive = false,
  className,
  onReady,
  onDeck,
  onShortcut,
  edit = null,
}: {
  presentation: Presentation;
  variant?: DeckVariant;
  /** The in-place editing session, or `null` for a plain render. */
  edit?: SlideEditSession | null;
  /** Whether an EMBEDDED deck owns the keyboard. Inside the chat the arrows
   *  belong to the page until the reader engages the deck (a click on it or on
   *  its toolbar — tracked by `DeckStage`, since reveal's own focus tracking
   *  drops the deck the moment a toolbar button outside `.reveal` is clicked);
   *  the standalone viewer and a fullscreen stage own the keys outright. */
  keyboardActive?: boolean;
  className?: string;
  /** Called with the live Reveal instance once the deck is ready. The PDF
   *  export path needs it: reveal only lays the deck out as printable pages
   *  when print view is applied to an already-measured deck (see the note on
   *  the `print` variant in `config` below). */
  onReady?: (deck: RevealApi) => void;
  /** Receives the Reveal instance when ready and `null` when it is torn down —
   *  `useDeckController().attach` goes here. */
  onDeck?: (deck: RevealApi | null) => void;
  /** App-level shortcuts pressed while the deck owns the keyboard (see
   *  `DECK_KEY_CODES`). The owner acts: fullscreen belongs to the stage, the
   *  jump menu and the shortcut sheet to the toolbar. */
  onShortcut?: (action: DeckShortcutAction) => void;
}) {
  const { spec } = presentation;
  const size = deckSize(spec.aspect_ratio);
  // The palette of the theme ON SCREEN — the editor previews a draft whose
  // theme may differ from the saved one (`themeConfigFor`).
  const themeConfig = themeConfigFor(presentation, spec.theme);
  // What the footer / header read: one deck setting, computed once for every
  // slide — the template's pattern filled from the deck, or the v1
  // event label + footer text.
  const chromeTextValue = useMemo(
    () => chromeText(spec, presentation.template_chrome),
    [spec, presentation.template_chrome],
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const deckRef = useRef<RevealApi | null>(null);

  // Read through refs by the stable keyboard bindings below: `@revealjs/react`
  // reconfigures reveal whenever a config VALUE changes identity, so the
  // bindings must not be rebuilt on every render.
  const shortcutRef = useRef(onShortcut);
  shortcutRef.current = onShortcut;
  const keyboardActiveRef = useRef(keyboardActive);
  keyboardActiveRef.current = keyboardActive;
  const variantRef = useRef(variant);
  variantRef.current = variant;

  const keyboardCondition = useCallback((event: KeyboardEvent) => {
    if (!deckMayHandleKey(event, rootRef.current)) return false;
    return variantRef.current === "standalone" || keyboardActiveRef.current;
  }, []);

  const keyboard = useMemo(
    () => ({
      [DECK_KEY_CODES.F]: () => shortcutRef.current?.("fullscreen"),
      [DECK_KEY_CODES.G]: () => shortcutRef.current?.("jump"),
      // `S` opens the platform's speaker window rather than reveal's notes plugin
      // (displacing the plugin's own binding).
      [DECK_KEY_CODES.S]: () => shortcutRef.current?.("speaker"),
      [DECK_KEY_CODES.A]: () => shortcutRef.current?.("audience"),
      [DECK_KEY_CODES.F1]: () => shortcutRef.current?.("help"),
      [DECK_KEY_CODES.ESC]: () => shortcutRef.current?.("escape"),
      // `?` is Shift + `/` on the same key code; plain `/` keeps reveal's
      // built-in meaning (black screen), which the custom binding displaces.
      [DECK_KEY_CODES.SLASH]: (event: KeyboardEvent) => {
        if (event.shiftKey) shortcutRef.current?.("help");
        else deckRef.current?.togglePause();
      },
    }),
    [],
  );

  const config = useMemo(
    () => ({
      ...size,
      // The canvas is padded by the slide frame itself; reveal's own margin
      // would shrink the 1920x1080 layout inside the box for no reason.
      margin: 0,
      minScale: 0.05,
      // Default 2.0 would cap a 16:9 deck at 3840px — a 4K wall is exactly the
      // case a presenter cares about.
      maxScale: 4,
      // Layouts own their vertical alignment (a title slide centres, a content
      // slide anchors its header at the top); reveal must not re-centre them.
      center: false,
      transition: spec.transition,
      backgroundTransition: paperTransition(spec.transition),
      // reveal marks every non-current slide `hidden` (for assistive tech) and
      // shows the neighbours it has loaded through an INLINE `display: block`
      // (the outgoing slide of a transition, every slide of the overview).
      // Tailwind's preflight turns `[hidden]` into `display: none !important`,
      // which beats a plain inline style: the overview then draws one slide and
      // N-1 empty frames, and a `slide` transition pushes nothing out. reveal
      // has a first-class knob for this — an `!important` display value is set
      // through `style.setProperty(..., "important")`, and an important inline
      // declaration outranks every stylesheet one, layered or not.
      display: "block !important",
      // A deck lives inside the app: reveal must never take over the page or the
      // URL. `hash`/`respondToHashChanges` would rewrite the chat route's
      // fragment and scroll-jack the conversation on load.
      embedded: variant !== "print",
      hash: false,
      respondToHashChanges: false,
      history: false,
      // Nothing of reveal's is drawn over the slide: navigation, progress, the
      // slide counter, help and the jump box all live in the app toolbar.
      controls: false,
      progress: false,
      slideNumber: false,
      help: false,
      jumpToSlide: false,
      keyboard,
      keyboardCondition,
      touch: true,
      overview: variant !== "print",
      hideInactiveCursor: variant === "standalone" || keyboardActive,
      // reveal's own PDF layout: every slide becomes a printable page. This can
      // ONLY be set in the initial config — reveal activates print view during
      // initialize() and exposes no toggle afterwards.
      view: variant === "print" ? ("print" as const) : null,
      pdfSeparateFragments: false,
    }),
    [size, spec.transition, variant, keyboardActive, keyboard, keyboardCondition],
  );

  const attachDeck = useCallback(
    (deck: RevealApi | null) => {
      deckRef.current = deck;
      onDeck?.(deck);
    },
    [onDeck],
  );

  // A slide link (`[text](#/<slide id>)`) moves THIS deck: the stage around it
  // (the viewer, the chat card, the studio) follows reveal's `slidechanged`
  // like any other move. The print layout lays every slide out at once and
  // has nowhere to go.
  const followSlideLink = useCallback((index: number) => {
    const deck = deckRef.current;
    if (deck?.isReady()) deck.slide(index);
  }, []);
  const slideLinks = useSlideLinksValue(spec.slides, variant === "print" ? undefined : followSlideLink);

  // reveal only re-measures on `window.resize`. An embedded deck changes size
  // without one all the time — the chat pane is resized, the card goes
  // fullscreen, a dialog opens — and would otherwise keep its stale scale until
  // the next window resize. Observe the root box instead. Coalesced per frame:
  // fullscreen transitions fire a burst of observations.
  useEffect(() => {
    const root = rootRef.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        const deck = deckRef.current;
        if (deck?.isReady()) deck.layout();
      });
    });
    observer.observe(root);
    return () => {
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  // In-place edit: same id, new `revision`. `@revealjs/react` only syncs when
  // the SET of sections changes, so a revision that rewrites slide content (or
  // the theme) without adding or removing a slide would leave reveal's cached
  // slide state — indices, notes, backgrounds — one revision behind. The
  // editor's live preview is the same situation without a revision: every
  // draft edit is a new `slides` array (a reorder keeps the set and the count
  // but moves the sections), so the sync keys on the array's identity too.
  // TanStack's structural sharing keeps that identity stable while the server
  // reports no change, so a poll never triggers a sync.
  useEffect(() => {
    const deck = deckRef.current;
    if (!deck?.isReady()) return;
    deck.sync();
    deck.layout();
  }, [presentation.revision, spec.slides]);

  // Live polls (#10157): read only for a signed-in reader of a deck that holds a
  // poll block; a public link and the export page draw link + QR without results.
  const pollsLive = variant !== "print" && (presentation.user_id !== null || presentation.can_manage) && specHasPoll(spec.slides);
  const polls = useDeckPolls(presentation.id, pollsLive);
  const pollsValue = useMemo(
    () => (pollsLive ? { presentationId: presentation.id, data: polls.data } : null),
    [pollsLive, presentation.id, polls.data],
  );

  return (
    <div
      ref={rootRef}
      className={cn("deck-root", className)}
      data-deck-theme={spec.theme}
      // Slide or portrait page: the stylesheet keys its type scale on it.
      data-deck-format={deckFormat(spec.aspect_ratio)}
      // Lets the stylesheet drop the clipped-viewport box for PDF export, where
      // the document must be as tall as all its pages.
      data-deck-print={variant === "print" ? "true" : undefined}
      // The stylesheet's hover / selection outlines are gated on this.
      data-deck-editing={edit?.enabled ? "true" : undefined}
      style={themeStyle(themeConfig)}
    >
      <EditModeContext.Provider value={edit}>
      <DeckPollsContext.Provider value={pollsValue}>
      <SlideLinkContext.Provider value={slideLinks}>
      <Deck
        // Keyed on the deck id ONLY — see the note at the top of this file.
        key={presentation.id}
        // `.deck-canvas` is the typography root shared with the static render
        // page (export): the stylesheet types slide content under it, not
        // under `.reveal`, so a slide reads the same with or without reveal.
        className="deck-canvas"
        config={config}
        // No reveal plugins: the speaker view is the platform's own window
        // (`speaker-sync.ts`), so nothing here needs to load the page again.
        plugins={[]}
        deckRef={attachDeck}
        onReady={onReady}
      >
        {spec.slides.map((slide, index) => (
          <DeckSlide
            key={slide.id ?? `slide-${index}`}
            slide={slide}
            index={index}
            total={spec.slides.length}
            chrome={spec.chrome}
            chromeTextValue={chromeTextValue}
            presentation={presentation}
          />
        ))}
      </Deck>
      </SlideLinkContext.Provider>
      </DeckPollsContext.Provider>
      </EditModeContext.Provider>
    </div>
  );
}
