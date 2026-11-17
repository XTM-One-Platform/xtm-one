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
 * What the renderer knows about the in-place editor.
 *
 * Three contexts, all inert outside edit mode so a chat card, the export page
 * and the speaker view render byte-identical DOM:
 *
 *   - `EditModeContext` - the session: whether editing is on, which text is
 *     being edited in place and where a commit goes. Provided ONCE around the
 *     deck by `PresentationDeck` / `PageDocument` when the viewer hands them
 *     an `edit` prop.
 *   - `SlideIndexContext` - the slide a subtree belongs to (`DeckSlide`,
 *     `StaticSlide`), so a text target knows whether IT is the one under edit.
 *   - `BlockPathContext` - the spec path of the enclosing block (`Block`, from
 *     the `path` its list hands it), so a block root can publish itself as
 *     `data-edit-block="blocks.2"` and a text inside it as
 *     `data-edit-text="blocks.2.items.0.text"`.
 *
 * The attributes are the whole contract between the renderer and the editor:
 * the layer over the stage resolves a click to the nearest `[data-edit-text]`
 * / `[data-edit-block]`, and writes to the draft at that path
 * (`edit-paths.ts`). No block component knows the editor exists beyond
 * spreading `useBlockEditAttrs()` on its root and naming its text fields.
 */
import { createContext, useContext } from "react";

import { childPath } from "./edit-paths";

/** The text under edit in place: the slide it is on and its slide-relative path. */
export interface EditingText {
  slide: number;
  path: string;
  /** The caret offset (plain-text characters) to open at, from the click. */
  caret?: number;
  /** Which opening of a text this is: its writes are one history step. */
  session?: number;
}

export interface SlideEditSession {
  enabled: boolean;
  editingText: EditingText | null;
  /** The text as it is being typed (valid markup only), to the draft while the field stays open. */
  onLiveText: (slide: number, path: string, value: string) => void;
  /** The edited text, as markup (or plain text for a plain field), goes to the draft. */
  onCommitText: (slide: number, path: string, value: string) => void;
  onCancelText: () => void;
}

/**
 * Marks the editor PANEL for the stage's key handler: `Ctrl+Z` /
 * `Ctrl+Y` pressed in one of its fields undo / redo the deck - a field there
 * writes to the draft on every keystroke, one coalesced step per field, so
 * the deck's undo is the field's undo and the browser's own is suppressed.
 * A field anywhere else (the studio composer, a comment, a dialog) keeps its
 * own. The selector is what `closest()` reads.
 */
export const EDITOR_PANEL_ATTR = { "data-presentation-editor-panel": "true" } as const;
export const EDITOR_PANEL_SELECTOR = "[data-presentation-editor-panel]";
/**
 * A field INSIDE the editor panel that writes nothing to the draft
 * (the picture picker's search box): its undo is the browser's own, and a
 * `Ctrl+Z` there must never walk the deck back.
 */
export const OWN_UNDO_ATTR = { "data-own-undo": "true" } as const;
export const OWN_UNDO_SELECTOR = "[data-own-undo]";

/** Around the whole deck: the session, or `null` for a plain render. */
export const EditModeContext = createContext<SlideEditSession | null>(null);
/** Around one slide's subtree. */
export const SlideIndexContext = createContext<number>(-1);
/** Around one block's subtree - the block's spec path. */
export const BlockPathContext = createContext<string | null>(null);

/** Whether the subtree renders under an active edit session. */
export function useEditMode(): SlideEditSession | null {
  const session = useContext(EditModeContext);
  return session?.enabled ? session : null;
}

/** The enclosing block's path, or `null` at slide level. */
export function useBlockPath(): string | null {
  return useContext(BlockPathContext);
}

export function useSlideIndex(): number {
  return useContext(SlideIndexContext);
}

type BlockEditAttrs = { "data-edit-block"?: string };

/**
 * Spread on a block component's root element: publishes the block's path in
 * edit mode, nothing otherwise.
 */
export function useBlockEditAttrs(): BlockEditAttrs {
  const session = useEditMode();
  const path = useBlockPath();
  if (!session || path === null) return {};
  return { "data-edit-block": path };
}

/**
 * The full slide-relative path of a text field named relative to the
 * enclosing block (`"content"`, `"items.0.text"`) - or to the slide when
 * rendered outside any block (`"title"`). `null` outside edit mode.
 */
export function useEditTextPath(field: string | undefined): string | null {
  const session = useEditMode();
  const blockPath = useBlockPath();
  if (!session || field === undefined) return null;
  return childPath(blockPath ?? "", field);
}
