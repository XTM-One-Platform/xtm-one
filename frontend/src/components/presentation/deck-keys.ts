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
 * Who owns a key press while a deck is on screen.
 *
 * reveal.js listens for `keydown` on the DOCUMENT, so with the deck's controls
 * living in an app toolbar rather than on the slide, an arrow pressed inside an
 * open menu, a dialog or a form would ALSO turn the page. `deckMayHandleKey` is
 * the single answer to "may reveal act on this event?" — installed as reveal's
 * `keyboardCondition` by `PresentationDeck`, so every custom binding and every
 * built-in one goes through it.
 *
 * The rules, in order:
 *   1. Inside a menu, a dialog, a listbox / combobox or anything marked
 *      `data-deck-keys="off"` (the editor panel), NO key belongs to the deck.
 *      Those surfaces own the arrows, Escape, Home / End and Enter themselves.
 *      The trigger of an OPEN popup counts as inside it: a menu opened with the
 *      mouse leaves the focus on its button, and the arrows pressed there are
 *      meant for the menu. A surface that CONTAINS the deck is exempt — a
 *      template preview lives inside a dialog, and that dialog is the deck's
 *      host, not a popup over it.
 *   2. Space and Enter on a focused control (a toolbar button, a link) activate
 *      THAT control — the browser fires its click — and must not also advance
 *      the deck, or the "Next" button would move two slides per press.
 *   3. Everything else is reveal's: arrows on a focused toolbar button still
 *      turn the page, like a media player whose transport keys work wherever
 *      the focus is.
 *
 * reveal already ignores `input` / `textarea` / contenteditable targets on its
 * own, so typing a slide number never navigates.
 */

import type { DeckController } from "./deck-controller";
import { presentedElement } from "./fullscreen";

/** Mark a region whose keys the deck must leave alone (`data-deck-keys="off"`). */
export const DECK_KEYS_OFF_ATTR = { "data-deck-keys": "off" } as const;

/**
 * The surfaces that own every key pressed inside them (rule 1). Shared with
 * the in-place editor's stage shortcuts (`SlideEditLayer`), which
 * answer the same question for the selection's keys: one list, so a region
 * the deck leaves alone is a region the editor leaves alone.
 */
export const OWNED_REGIONS =
  '[role="menu"],[role="dialog"],[role="alertdialog"],[role="listbox"],' +
  '[role="combobox"],[aria-haspopup][aria-expanded="true"],[data-deck-keys="off"]';

/** The controls Space and Enter activate (rule 2) - never also a deck key. */
export const ACTIVATION_TARGETS =
  'button,a[href],summary,select,[role="button"],[role="menuitem"],[role="option"],' +
  '[role="tab"],[role="switch"],[role="checkbox"]';

/**
 * @param deckRoot the deck's own root element, so a dialog that hosts the deck
 *   is told apart from one opened over it. Without it every owned region wins.
 */
export function deckMayHandleKey(event: KeyboardEvent, deckRoot?: Element | null): boolean {
  const target = event.target;
  if (!(target instanceof Element)) return true;
  // The NEAREST owned ancestor decides: a menu opened inside the hosting dialog
  // is nearer than the dialog and does not contain the deck, so it still wins.
  const owner = target.closest(OWNED_REGIONS);
  if (owner && !(deckRoot && owner.contains(deckRoot))) return false;
  if ((event.key === " " || event.key === "Enter") && target.closest(ACTIVATION_TARGETS)) {
    return false;
  }
  return true;
}

/**
 * The deck shortcuts the app itself binds (on top of reveal's built-ins), by
 * keyCode — reveal's `keyboard` config is keyed that way. `F` replaces reveal's
 * own fullscreen (which would fullscreen the bare `.reveal` element and leave
 * the toolbar behind); `G` opens the toolbar's "go to slide" menu instead of
 * reveal's on-slide input; `S` opens the platform's speaker window
 * where reveal's notes plugin used to open the page in iframes; `?` / `F1`
 * open the app's shortcut sheet instead of reveal's unstyled help overlay;
 * `Esc` leaves fullscreen or the overview instead of reveal's default, which
 * also OPENS the overview.
 */
export const DECK_KEY_CODES = {
  F: 70,
  G: 71,
  S: 83,
  /** The audience window ("Present to a meeting", xtm-one #10552). */
  A: 65,
  /** `/` and, with Shift, `?` — reveal whitelists Shift for this code. */
  SLASH: 191,
  F1: 112,
  ESC: 27,
} as const;

/**
 * Click to advance in a presenting window (xtm-one #10552, slice A2): a left
 * click on the slide moves to the next slide, a Shift + left click to the
 * previous one. Never on interactive content - a link, a button (a chart
 * legend, a poll control), a form field, embedded media, an owned region (a
 * menu, a dialog, the editor) - and never with a modifier other than Shift,
 * so Ctrl / Cmd + click keeps its browser meaning.
 */
const CLICK_IGNORED_TARGETS =
  `${ACTIVATION_TARGETS},${OWNED_REGIONS},input,textarea,label,video,audio,iframe,object,embed,[contenteditable]`;

export type ClickStep = "next" | "prev" | null;

export function clickAdvanceStep(
  event: Pick<MouseEvent, "button" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey" | "target" | "defaultPrevented">,
): ClickStep {
  if (event.defaultPrevented || event.button !== 0) return null;
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  const target = event.target;
  if (target instanceof Element && target.closest(CLICK_IGNORED_TARGETS)) return null;
  return event.shiftKey ? "prev" : "next";
}

export type DeckShortcutAction = "fullscreen" | "jump" | "speaker" | "audience" | "help" | "escape";

/**
 * `onEscapeKeyDown` for a dialog that HOSTS a deck (the template preview, the
 * generator's candidate). Radix dismisses on Escape in the CAPTURE phase on the
 * document, before reveal ever sees the key — so while the deck has a state
 * Escape should leave first (the slide overview, fullscreen), the dialog must
 * stay open and let the deck have it.
 */
export function previewEscapeGuard(controller: DeckController) {
  return (event: KeyboardEvent) => {
    // Ask the deck itself, not the controller's snapshot: the snapshot is React
    // state re-derived from reveal's events a render later, and `O` then `Esc`
    // in quick succession lands the Escape in that gap — the dialog would then
    // close on a deck that was, live, already in the overview.
    const deck = controller.api;
    const overview = deck?.isReady() ? deck.isOverview() : controller.overview;
    // Either presentation mode, read live: the viewport fallback is not the
    // browser's fullscreen and would not show in `document.fullscreenElement`.
    if (overview || presentedElement()) event.preventDefault();
  };
}
