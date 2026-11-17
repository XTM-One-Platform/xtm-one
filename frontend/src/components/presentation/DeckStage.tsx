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
 * Toolbar + deck as one unit: the thing that goes fullscreen.
 *
 * The chat card and the standalone viewer both render a `DeckStage`; what
 * differs between them (title, actions, sizing) is passed in. The stage owns:
 *
 *   - Fullscreen. Requested on the STAGE, not the bare deck, so the toolbar
 *     comes along; there it is a transient overlay along the top edge
 *     — the deck is sized against the WHOLE screen, so a 16:9 deck fills a
 *     16:9 screen edge to edge, and the stage letterboxes it in black on any
 *     other screen (the deck never letterboxes itself in its own paper) — and
 *     the bar slides away after a moment of stillness, returning on pointer
 *     activity (not on keys — a presenter on the arrows must not have it
 *     flash), so the controls are one nudge away and the slide never moves.
 *     The stage forces the dark token set while presenting (`data-mode`), so
 *     the bar and its menus read light on the black screen in the light app
 *     mode too. `Esc` leaves, as does the toolbar button. Where the browser
 *     refuses its fullscreen the stage takes the app's viewport instead
 *     (`fullscreen.ts`) — same toolbar, same keys, same `Esc`; only the OS
 *     chrome stays.
 *   - Keyboard ownership. An embedded deck takes the arrows once the reader has
 *     engaged it — a click anywhere on the stage, toolbar included — and hands
 *     them back on a click elsewhere; the standalone viewer and a fullscreen
 *     stage own them outright. Tracked here rather than by reveal, whose own
 *     focus tracking drops the deck the moment a toolbar button is clicked.
 *   - The app shortcuts reveal reports (`F`, `G`, `S`, `?`, `F1`, `Esc`), the
 *     jump menu and the shortcut sheet, portaled into the stage while
 *     fullscreen (nothing outside the fullscreen element paints).
 *   - The speaker bridge. Every stage publishes its position over the
 *     deck's `BroadcastChannel` and obeys the speaker window's commands, so
 *     `S` works from the chat card as well as the viewer, and the speaker
 *     window never has to load this page to know where the deck is.
 */
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";

import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Presentation } from "@/types";

import type { DeckController } from "./deck-controller";
import { IDLE_HIDE_MS } from "./deck-idle";
import { clickAdvanceStep, type DeckShortcutAction } from "./deck-keys";
import { DeckShortcutsDialog } from "./DeckShortcutsDialog";
import { DeckToolbar, type DeckToolbarProps } from "./DeckToolbar";
import { type ElementFullscreen, presentedElement, useElementFullscreen } from "./fullscreen";
import { deckAspectRatio, isDocumentFormat } from "./geometry";
import { PollControls, PollPresentBar } from "./PollControls";
import { PresentationDeck } from "./PresentationDeck";
import type { DeckEditor } from "./presentation-editor";
import { useSpeakerStage } from "./speaker-sync";
import { openAudienceWindow, openNotesHandout, openSpeakerView } from "./speaker-url";

// The editor's layer (and the schema catalogs, the runs model and the path
// algebra it pulls in) loads when a deck is first EDITED, never for a chat
// card, a presented deck or the export page, which only ever read.
const SlideEditLayer = lazy(() => import("./SlideEditLayer").then((m) => ({ default: m.SlideEditLayer })));

/** What the stage tells its consumer's slots about itself. */
export interface DeckStageHandle {
  fullscreen: ElementFullscreen;
  /** Portal target for anything the consumer opens from the toolbar (a share
   *  dialog) so it is visible while fullscreen. */
  portalContainer: HTMLElement | null;
}

type Slot<T> = T | ((stage: DeckStageHandle) => T);

function resolve<T>(slot: Slot<T> | undefined, stage: DeckStageHandle): T | undefined {
  return typeof slot === "function" ? (slot as (stage: DeckStageHandle) => T)(stage) : slot;
}

/** The toolbar's collaboration slots, passed straight through by the stage. */
type CollaborationProps = Pick<
  DeckToolbarProps,
  | "onToggleComments"
  | "commentsOpen"
  | "openCommentCount"
  | "onToggleHistory"
  | "historyOpen"
  | "presence"
  | "onOpenEditor"
>;

export function DeckStage({
  presentation,
  variant,
  controller,
  leading,
  actions,
  menuItems,
  compact = false,
  autoEngage = false,
  viewerUrl,
  className,
  surfaceClassName,
  editor = null,
  presenterTools = true,
  ...collaboration
}: CollaborationProps & {
  presentation: Presentation;
  variant: "embedded" | "standalone";
  controller: DeckController;
  leading?: Slot<ReactNode>;
  actions?: Slot<ReactNode>;
  menuItems?: Slot<OverflowMenuItem[]>;
  compact?: boolean;
  /** The manual editing session: the stage becomes a slide editor
   *  while it is enabled - selection, in-place text, the floating bars. */
  editor?: DeckEditor | null;
  /** Own the keys from the first render instead of waiting for a click: for
   *  an embedded deck that is the only thing on screen (a preview inside a
   *  modal dialog), where nothing else competes for the arrows. */
  autoEngage?: boolean;
  /** The speaker view, the notes handout and the `S` key. Off on a
   *  public link: its deck carries no notes, and both windows are
   *  pages of a signed-in session. */
  presenterTools?: boolean;
  viewerUrl?: string;
  className?: string;
  /** Sizing of the deck box itself (the chat card's aspect box). The surface
   *  letterboxes the deck at its own ratio inside that box, in the app
   *  background; ignored while fullscreen, where the surface is the
   *  whole screen and the letterbox is black. */
  surfaceClassName?: string;
}) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  // State, not a ref: it is handed to portals as a `container`, which must be
  // a rendered value.
  const [stageEl, setStageEl] = useState<HTMLDivElement | null>(null);
  const setStage = useCallback((el: HTMLDivElement | null) => {
    stageRef.current = el;
    setStageEl(el);
  }, []);

  const fullscreen = useElementFullscreen(stageRef);
  const [engaged, setEngaged] = useState(variant === "standalone" || autoEngage);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [idle, setIdle] = useState(false);
  const { t_i18n } = useFormatter();
  const { toast } = useToast();

  // The speaker bridge: this stage publishes where the deck is and
  // obeys the speaker window. A page is read, not presented — no bridge, no
  // speaker view, no `S`.
  const speakerTools = !isDocumentFormat(presentation.spec.aspect_ratio) && presenterTools;
  useSpeakerStage({
    presentationId: presentation.id,
    controller,
    revision: presentation.revision,
    enabled: speakerTools,
  });
  // Opening the speaker view never ends presenting: the browser drops its
  // fullscreen when another window opens, and the stage then keeps the deck
  // presented in the viewport mode (`fullscreen.keepPresenting`).
  const { keepPresenting } = fullscreen;
  const openSpeaker = useCallback(() => {
    keepPresenting(() => {
      if (!openSpeakerView(presentation.id)) {
        toast({
          title: t_i18n("Speaker view blocked"),
          description: t_i18n("The browser blocked the speaker window. Allow pop-ups for this site and try again."),
          variant: "error",
        });
      }
    });
  }, [keepPresenting, presentation.id, t_i18n, toast]);
  const openHandout = useCallback(() => openNotesHandout(presentation.id), [presentation.id]);
  // "Present to a meeting" (xtm-one #10552, slice A2): the audience window to
  // share, slides only, and the speaker view to drive it from.
  const presentToMeeting = useCallback(() => {
    keepPresenting(() => {
      if (!openAudienceWindow(presentation.id)) {
        toast({
          title: t_i18n("Audience window blocked"),
          description: t_i18n("The browser blocked the audience window. Allow pop-ups for this site and try again."),
          variant: "error",
        });
        return;
      }
      openSpeakerView(presentation.id);
    });
  }, [keepPresenting, presentation.id, t_i18n, toast]);

  // Click to advance while presenting: a left click on the slide is the next
  // slide, Shift + click the previous one; links, buttons, poll controls and
  // media keep their click (`clickAdvanceStep`). Never in edit mode.
  const editing = !!editor?.enabled;
  const onSurfaceClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!fullscreen.isFullscreen || editing) return;
      const step = clickAdvanceStep(event.nativeEvent);
      if (step === "next") controller.next();
      else if (step === "prev") controller.prev();
    },
    [controller, editing, fullscreen.isFullscreen],
  );

  // Engagement: a pointer down inside the stage claims the keyboard for an
  // embedded deck, one outside it gives the keys back to the page.
  useEffect(() => {
    if (variant === "standalone") return;
    const onPointerDown = (event: PointerEvent) => {
      const inside = !!stageRef.current?.contains(event.target as Node);
      setEngaged(inside);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [variant]);

  // Fullscreen auto-hide: pointer activity shows the toolbar and restarts the
  // timer; the CSS keeps it up while hovered, focused or holding an open menu
  // (`aria-expanded`), so the timer firing under a menu being read never
  // takes the bar away. Keys deliberately do NOT bring it back — a presenter
  // stepping through slides with the arrows or a clicker must not have the
  // bar flash on every press.
  useEffect(() => {
    if (!fullscreen.isFullscreen) {
      setIdle(false);
      return;
    }
    let timer = 0;
    const arm = () => {
      setIdle(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setIdle(true), IDLE_HIDE_MS);
    };
    arm();
    const stage = stageRef.current;
    stage?.addEventListener("pointermove", arm);
    stage?.addEventListener("pointerdown", arm);
    stage?.addEventListener("touchstart", arm, { passive: true });
    return () => {
      window.clearTimeout(timer);
      stage?.removeEventListener("pointermove", arm);
      stage?.removeEventListener("pointerdown", arm);
      stage?.removeEventListener("touchstart", arm);
    };
  }, [fullscreen.isFullscreen]);

  // A menu or the sheet open in fullscreen must not slide away under the user;
  // once it closes, the focus it hands back to its toolbar trigger would keep
  // the bar on screen (`:has(:focus-visible)`) for as long as the presenter
  // only uses the keys — so the deck gets the focus back instead.
  useEffect(() => {
    if (jumpOpen || shortcutsOpen) {
      setIdle(false);
      return;
    }
    if (!fullscreen.isFullscreen) return;
    const frame = requestAnimationFrame(() => {
      const active = document.activeElement;
      const toolbar = stageRef.current?.querySelector(".deck-toolbar");
      if (active instanceof HTMLElement && toolbar?.contains(active)) active.blur();
    });
    return () => cancelAnimationFrame(frame);
  }, [jumpOpen, shortcutsOpen, fullscreen.isFullscreen]);

  const onShortcut = useCallback(
    (action: DeckShortcutAction) => {
      switch (action) {
        case "fullscreen":
          fullscreen.toggle();
          break;
        case "jump":
          setJumpOpen(true);
          break;
        case "help":
          setShortcutsOpen((open) => !open);
          break;
        case "speaker":
          if (speakerTools) openSpeaker();
          break;
        case "audience":
          if (speakerTools) presentToMeeting();
          break;
        case "escape": {
          // Live reads, not the render's snapshot: `O` then `Esc` in quick
          // succession arrives before the `overviewshown` re-render, and a
          // snapshot-driven check would leave the deck in the overview it is
          // already in.
          const deck = controller.api;
          if (presentedElement() === stageRef.current) {
            fullscreen.exit();
          } else if (deck?.isReady() && deck.isOverview()) {
            deck.toggleOverview();
          }
          break;
        }
        default:
          break;
      }
    },
    [fullscreen, controller, speakerTools, openSpeaker, presentToMeeting],
  );

  const handle: DeckStageHandle = {
    fullscreen,
    portalContainer: fullscreen.isFullscreen ? stageEl : null,
  };

  // The comments / history panels and the editor live OUTSIDE the stage, so a
  // fullscreen stage is left before one opens — otherwise it would open behind
  // the fullscreen element, invisible.
  const { onToggleComments, onToggleHistory, onOpenEditor, ...toolbarCollaboration } = collaboration;
  const outsideStage = (fn: (() => void) | undefined) =>
    fn &&
    (() => {
      if (fullscreen.isFullscreen) fullscreen.exit();
      fn();
    });

  return (
    <div
      ref={setStage}
      // `dark` is the class the design system declares its dark token set on,
      // so the forced-dark subtree carries it next to the attribute the
      // per-mode variants read.
      className={cn("deck-stage", fullscreen.isFullscreen && "dark", className)}
      data-deck-fullscreen={fullscreen.mode ?? undefined}
      data-deck-idle={idle ? "true" : undefined}
      // The overview as the app's controller holds it, not reveal's own class:
      // it is set once the `overviewshown` event has reached the controller,
      // the state the hosting dialog's Escape guard falls back to.
      data-deck-overview={controller.overview ? "true" : undefined}
      // A presented deck is on a black screen: the bar over it and the menus
      // portaled into the stage read from the dark token set whatever mode
      // the app is in. The attribute is what the `light:` / `dark:` variants
      // key on (`index.css`); the `dark` class above carries the tokens.
      data-mode={fullscreen.isFullscreen ? "dark" : undefined}
    >
      <DeckToolbar
        controller={controller}
        slides={presentation.spec.slides}
        fullscreen={fullscreen}
        jumpOpen={jumpOpen}
        onJumpOpenChange={setJumpOpen}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        leading={resolve(leading, handle)}
        actions={resolve(actions, handle)}
        menuItems={resolve(menuItems, handle)}
        compact={compact && !fullscreen.isFullscreen}
        portalContainer={handle.portalContainer}
        viewerUrl={viewerUrl}
        onOpenSpeakerView={speakerTools ? openSpeaker : undefined}
        onPresentToMeeting={speakerTools ? presentToMeeting : undefined}
        onOpenNotesHandout={speakerTools ? openHandout : undefined}
        onToggleComments={outsideStage(onToggleComments)}
        onToggleHistory={outsideStage(onToggleHistory)}
        onOpenEditor={outsideStage(onOpenEditor)}
        {...toolbarCollaboration}
      />
      <div
        className={cn("deck-stage-surface", surfaceClassName)}
        data-aspect={deckAspectRatio(presentation.spec.aspect_ratio)}
        onClick={onSurfaceClick}
      >
        <PresentationDeck
          presentation={presentation}
          variant={variant}
          keyboardActive={engaged || fullscreen.isFullscreen}
          onDeck={controller.attach}
          onShortcut={onShortcut}
          edit={editor?.enabled ? editor.stageSession : null}
        />
        {/* Never while presenting: a fullscreen stage is the audience's. */}
        {editor?.enabled && !fullscreen.isFullscreen && (
          <Suspense fallback={null}>
            <SlideEditLayer
              editor={editor}
              activeIndex={controller.index}
              followsActiveSlide
              presentation={presentation}
              collaborators={collaboration.presence}
            />
          </Suspense>
        )}
      </div>
      {/* Poll presenter controls (#10157): app chrome under the deck, never on
          the slide and never while presenting fullscreen (the room's screen). */}
      {presenterTools && !fullscreen.isFullscreen && (
        <PollControls presentationId={presentation.id} slide={presentation.spec.slides[controller.index]} />
      )}
      {/* Presenting fullscreen on one screen (#10235): a bar along the bottom edge that
          hides with the toolbar when the stage goes idle, and the L / V / Shift+R / N / P keys. */}
      {presenterTools && fullscreen.isFullscreen && (
        <PollPresentBar
          presentationId={presentation.id}
          slide={presentation.spec.slides[controller.index]}
          stage={stageEl}
        />
      )}
      <DeckShortcutsDialog
        open={shortcutsOpen}
        onOpenChange={setShortcutsOpen}
        container={handle.portalContainer}
        showSpeaker={speakerTools}
        editing={!!editor?.enabled}
      />
    </div>
  );
}
