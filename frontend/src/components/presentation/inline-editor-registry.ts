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
 * The one in-place text editor alive at a time, published for the
 * stage layer that draws the formatting bar over it and drives it.
 */
import type { MarkupSpanKind } from "./markup";
import type { RunStyle } from "./inline-runs";

/** What the formatting bar reads and drives. */
export interface InlineEditorHandle {
  element: HTMLElement;
  plain: boolean;
  /** Markup allowed, line breaks not (a profile fact, drawn on ONE row): the bar hides its line-break button. */
  singleLine: boolean;
  /** The styles active over the current selection (for pressed buttons). */
  activeStyles: () => {
    bold: boolean;
    em: boolean;
    code: boolean;
    tone: MarkupSpanKind | null;
    /** The slide id the selection (or the caret) links to, `null` for none. */
    link: string | null;
  };
  /** Toggle a style over the selection, or the word at the caret. */
  applyFormat: (style: Exclude<RunStyle, "link">, tone?: MarkupSpanKind) => void;
  /**
   * Link the selection (or the word at the caret) to the slide with id
   * `target`; `null` takes the link off - the whole link around the caret
   * when nothing is selected.
   */
  applyLink: (target: string | null) => void;
  insertLineBreak: () => void;
  /**
   * End the edit with the text as it stands; `false` when the field refused
   * (markup the validator would refuse) and stays open.
   */
  commit: () => boolean;
  /**
   * Hand the text as it stands to the draft NOW, the field left open:
   * `clean` when the draft already holds it, `pushed` when it was just handed
   * over (it lands on the next render), `refused` when the markup is one the
   * save would refuse - nothing is handed over, and nothing is said (the
   * field's explicit commit is what explains).
   */
  flush: () => "clean" | "pushed" | "refused";
  cancel: () => void;
}

const listeners = new Set<(handle: InlineEditorHandle | null) => void>();
let activeHandle: InlineEditorHandle | null = null;

export function publishInlineEditor(handle: InlineEditorHandle | null): void {
  activeHandle = handle;
  for (const listener of listeners) listener(handle);
}

/** The editor open right now, if any - what a save commits first. */
export function activeInlineEditor(): InlineEditorHandle | null {
  return activeHandle;
}

/** Only the editor that published `handle` may retire it (a later mount may have replaced it). */
export function retireInlineEditor(handle: InlineEditorHandle): void {
  if (activeHandle === handle) publishInlineEditor(null);
}

export function subscribeInlineEditor(listener: (handle: InlineEditorHandle | null) => void): () => void {
  listeners.add(listener);
  listener(activeHandle);
  return () => {
    listeners.delete(listener);
  };
}
