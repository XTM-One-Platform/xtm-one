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
 * Toolbar + scrolling pages as one unit: the on-screen form of an
 * A4 / letter presentation — the counterpart of `DeckStage` for the `page` kind.
 *
 * A product brief, a one-pager or a datasheet is READ, not presented, so this
 * is a document viewer: the pages are stacked in a scrolling surface, each one
 * a sheet scaled to the surface width (times the reader's zoom), with a page
 * indicator, page navigation and zoom in the bar — and none of the deck's
 * presenting apparatus (no reveal runtime, no fullscreen, no overview, no
 * black screen, no speaker notes).
 *
 * Every page is rendered by `StaticSlide`, the very component the headless
 * browser prints for the PDF export, laid out at its full canvas size and
 * transformed onto the sheet — so the screen shows exactly what the export
 * prints, down to the line breaks.
 *
 * Keyboard: `←` `→` / `PgUp` `PgDn` / `N` `P` step pages, `Home` `End` jump to
 * the ends, `+` `-` `0` zoom, `G` opens the page menu, `?` / `F1` the shortcut
 * sheet; `↑` `↓` and `Space` are left to the browser's own scrolling of the
 * surface. The standalone viewer owns the keys outright (`keyScope="document"`);
 * an embedded document (the chat card) takes them once the reader has engaged
 * it — a click anywhere on it — and hands them back on a click elsewhere.
 * `Ctrl` + wheel zooms the document instead of the page around it.
 */
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Presentation } from "@/types";

import { deckMayHandleKey } from "./deck-keys";
import { DeckShortcutsDialog } from "./DeckShortcutsDialog";
import { EditModeContext } from "./edit-context";
import { deckAspectRatio, deckFormat, deckSize } from "./geometry";
import type { PageController } from "./page-controller";
import { PageToolbar, type PageToolbarProps } from "./PageToolbar";
import type { DeckEditor } from "./presentation-editor";
import { SlideLinkContext, useSlideLinksValue } from "./slide-link-context";
import { StaticSlide } from "./StaticSlide";
import { themeConfigFor, themeStyle } from "./theme";

// Loaded when a page is first EDITED, never for a chat card or the export
// page, which only read - the same split as `DeckStage`.
const SlideEditLayer = lazy(() => import("./SlideEditLayer").then((m) => ({ default: m.SlideEditLayer })));

/** Gutter around the pages, in CSS pixels (`--page-gap` in presentation.css). */
const GAP_PX = { regular: 24, compact: 16 } as const;
/** A sheet never gets narrower than this, whatever the surface. */
const MIN_SHEET_PX = 240;

const TYPING_TARGETS = "input,textarea,select,[contenteditable]:not([contenteditable='false'])";

/** The toolbar's collaboration slots, passed straight through. */
type CollaborationProps = Pick<
  PageToolbarProps,
  | "onToggleComments"
  | "commentsOpen"
  | "openCommentCount"
  | "onToggleHistory"
  | "historyOpen"
  | "presence"
  | "onOpenEditor"
>;

export interface PageDocumentProps extends CollaborationProps {
  presentation: Presentation;
  controller: PageController;
  /** Toolbar's left region: title, "all presentations". */
  leading?: ReactNode;
  /** Toolbar's right region, before the `More` menu. */
  actions?: ReactNode;
  /** Appended to the `More` menu. */
  menuItems?: OverflowMenuItem[];
  /** Tighter bar and gutters for the chat card. */
  compact?: boolean;
  /** Who gets the keys: the whole document (the standalone viewer, where the
   *  document is the page) or only an engaged surface (an embedded card). */
  keyScope?: "document" | "focus";
  /** The standalone viewer, offered as "open in a new tab" by an embedded card. */
  viewerUrl?: string;
  className?: string;
  /** Sizing of the scroll surface (the chat card's height cap). */
  surfaceClassName?: string;
  /** The manual editing session: the pages become an editor while it
   *  is enabled - selection, in-place text, the floating bars. */
  editor?: DeckEditor | null;
}

export function PageDocument({
  presentation,
  controller,
  leading,
  actions,
  menuItems,
  compact = false,
  keyScope = "focus",
  viewerUrl,
  className,
  surfaceClassName,
  editor = null,
  ...collaboration
}: PageDocumentProps) {
  const { t_i18n } = useFormatter();
  const stageRef = useRef<HTMLDivElement | null>(null);
  const surfaceRef = useRef<HTMLElement | null>(null);
  const [surfaceWidth, setSurfaceWidth] = useState(0);
  const [engaged, setEngaged] = useState(keyScope === "document");
  const [jumpOpen, setJumpOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  const { spec } = presentation;
  const aspect = deckAspectRatio(spec.aspect_ratio);
  const canvas = deckSize(aspect);
  const gap = compact ? GAP_PX.compact : GAP_PX.regular;

  // The controller is a fresh snapshot every render; its actions are stable,
  // so the effects below depend on the pieces they call — never on the object,
  // or the ref callback would re-attach (and re-read) the surface every render.
  const { attach, next, prev, first, last, zoomIn, zoomOut, fitWidth: zoomToFit, zoom } = controller;
  // A slide link (`[text](#/<slide id>)`) scrolls the document to its page.
  const slideLinks = useSlideLinksValue(spec.slides, controller.goTo);

  // The surface is handed to the controller (which reads the scroll position
  // from it) and measured here (which sizes the sheets to it).
  const attachSurface = useCallback(
    (el: HTMLDivElement | null) => {
      surfaceRef.current = el;
      attach(el);
    },
    [attach],
  );

  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;
    const measure = () => setSurfaceWidth(surface.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(surface);
    return () => observer.disconnect();
  }, []);

  // Fit-width is the zoom's unit: a sheet at zoom 1 fills the surface between
  // the gutters. The transform scale follows from the sheet's width over the
  // canvas's — both set as variables the stylesheet sizes the sheets from.
  const fitWidth = Math.max(MIN_SHEET_PX, surfaceWidth - 2 * gap);
  const sheetWidth = Math.round(fitWidth * zoom);
  const documentStyle = {
    // The palette of the theme ON SCREEN (a draft's, in the editor).
    ...themeStyle(themeConfigFor(presentation, spec.theme)),
    "--page-sheet-width": `${sheetWidth}px`,
    "--page-scale": sheetWidth / canvas.width,
  } as CSSProperties;

  // Engagement: a pointer down inside the stage claims the keyboard for an
  // embedded document, one outside it gives the keys back to the page.
  useEffect(() => {
    if (keyScope === "document") return;
    const onPointerDown = (event: PointerEvent) => {
      setEngaged(!!stageRef.current?.contains(event.target as Node));
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [keyScope]);

  // The keys, on the document: an owned region (a menu, a dialog, the editor
  // panel) keeps its own, a control keeps its activation keys, and a text
  // field keeps everything (`deckMayHandleKey`, shared with the deck).
  useEffect(() => {
    if (!engaged) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      if (target instanceof Element && target.closest(TYPING_TARGETS)) return;
      if (!deckMayHandleKey(event, stageRef.current)) return;
      switch (event.key) {
        case "ArrowRight":
        case "PageDown":
        case "n":
        case "N":
          next();
          break;
        case "ArrowLeft":
        case "PageUp":
        case "p":
        case "P":
          prev();
          break;
        case "Home":
          first();
          break;
        case "End":
          last();
          break;
        case "+":
        case "=":
          zoomIn();
          break;
        case "-":
          zoomOut();
          break;
        case "0":
          zoomToFit();
          break;
        case "g":
        case "G":
          setJumpOpen(true);
          break;
        case "?":
        case "F1":
          setShortcutsOpen((open) => !open);
          break;
        default:
          return;
      }
      event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [engaged, next, prev, first, last, zoomIn, zoomOut, zoomToFit]);

  // `Ctrl` + wheel over the pages zooms the document, not the browser. A native
  // listener: React registers `wheel` as passive, so its handler could not
  // cancel the browser's own zoom.
  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      if (event.deltaY < 0) zoomIn();
      else if (event.deltaY > 0) zoomOut();
    };
    surface.addEventListener("wheel", onWheel, { passive: false });
    return () => surface.removeEventListener("wheel", onWheel);
  }, [zoomIn, zoomOut]);

  return (
    <div ref={stageRef} className={cn("page-stage", className)}>
      <PageToolbar
        controller={controller}
        pages={spec.slides}
        jumpOpen={jumpOpen}
        onJumpOpenChange={setJumpOpen}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        leading={leading}
        actions={actions}
        menuItems={menuItems}
        compact={compact}
        viewerUrl={viewerUrl}
        {...collaboration}
      />
      <div
        ref={attachSurface}
        role="document"
        aria-label={presentation.title}
        tabIndex={0}
        data-compact={compact ? "true" : undefined}
        className={cn(
          "page-surface bg-elevation-default focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus",
          surfaceClassName,
        )}
      >
        <div
          className="deck-root"
          data-deck-theme={spec.theme}
          data-deck-format={deckFormat(aspect)}
          data-deck-document="true"
          data-deck-editing={editor?.enabled ? "true" : undefined}
          style={documentStyle}
        >
          <EditModeContext.Provider value={editor?.enabled ? editor.stageSession : null}>
            <SlideLinkContext.Provider value={slideLinks}>
            {spec.slides.map((slide, index) => (
              <div
                key={slide.id ?? `page-${index}`}
                className="page-sheet deck-aspect-box"
                data-aspect={aspect}
                data-page-index={index}
                aria-label={t_i18n("Page {current} of {total}", {
                  current: index + 1,
                  total: spec.slides.length,
                })}
              >
                <StaticSlide slide={slide} index={index} presentation={presentation} />
              </div>
            ))}
            </SlideLinkContext.Provider>
          </EditModeContext.Provider>
        </div>
        {/* Every page is on screen: a selection survives the scroll. */}
        {editor?.enabled && (
          <Suspense fallback={null}>
            <SlideEditLayer
              editor={editor}
              activeIndex={controller.index}
              followsActiveSlide={false}
              presentation={presentation}
              collaborators={collaboration.presence}
            />
          </Suspense>
        )}
      </div>
      <DeckShortcutsDialog
        open={shortcutsOpen}
        onOpenChange={setShortcutsOpen}
        kind="page"
        editing={!!editor?.enabled}
      />
    </div>
  );
}
