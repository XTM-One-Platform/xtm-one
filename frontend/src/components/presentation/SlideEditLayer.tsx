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
 * The stage in edit mode: select, hover, arrange and edit in place.
 *
 * Mounted INSIDE the stage surface next to the deck (the reveal canvas or the
 * page document), covering it without taking its pointer events. It listens
 * on the surface for the pointer, resolves what is under it through the
 * attributes the renderer publishes (`data-slide-index`, `data-edit-block`,
 * `data-edit-text` - see `edit-context.tsx`) and keeps the editor session
 * (`presentation-editor.ts`) told. What it DRAWS lives in two places:
 *
 *   - the outlines, on the slide itself: `data-edit-hover` and
 *     `data-edit-selected` set imperatively on the block element, styled by
 *     `presentation.css` in canvas pixels so they scale with the deck and
 *     cost no React re-render of the slides;
 *   - the floating bars, here, in screen pixels over the stage: the BLOCK
 *     bar (type, move, duplicate, insert, delete) above the selected block,
 *     the TEXT bar (bold, emphasis, code, tone, line break) above the text
 *     under edit. Positioned from `getBoundingClientRect`, re-measured when
 *     anything can have moved (a draft change, a resize, a slide transition,
 *     a scroll of the page document);
 *   - the COLLABORATORS: the block each other editor has selected,
 *     outlined in their colour with their name, on the slides on screen -
 *     read off presence, so a person sees where the others work before
 *     changing the same block.
 *
 * Interaction, the way a slide editor is expected to behave:
 *   click a text       - edit it, caret where the pointer is (and select its block)
 *   click a block      - select it (a click on empty slide selects the slide)
 *   click it again     - edit the one text its row / tile / card holds
 *   double-click       - the same, at once
 *   Enter              - edit the selected block's main text
 *   Tab / Shift+Tab    - next / previous block on the slide
 *   Delete / Backspace - delete the selected block
 *   Ctrl+D             - duplicate it;  Alt+Up / Alt+Down - move it
 *   Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y - undo / redo, from the panel's fields too
 *                        (a text under edit in place walks its own history)
 *   Ctrl+S             - save (commits the text under edit first)
 *   Escape             - stop editing, then deselect, then the deck's own
 * Keys are read on the document in the CAPTURE phase so the layer can keep
 * one from reveal (Escape, Alt+arrows) and let every other through; nothing
 * is read while a field has the focus (the inspector, the text under edit).
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Bold,
  ChevronDown,
  ChevronUp,
  Code,
  CopyPlus,
  CornerDownLeft,
  Italic,
  Link2,
  Link2Off,
  PencilLine,
  Plus,
  RemoveFormatting,
  Trash2,
  Check,
  type LucideIcon,
} from "lucide-react";

import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Presentation, PresentationPresenceEntry, PresentationSlide } from "@/types";

import { BLOCK_ICONS } from "./block-icons";
import { blockPaletteItems } from "./block-palette";
import { BLOCK_SCHEMA } from "./block-schema";
import { ACTIVATION_TARGETS, DECK_KEYS_OFF_ATTR, OWNED_REGIONS } from "./deck-keys";
import { EDITOR_PANEL_SELECTOR, OWN_UNDO_SELECTOR } from "./edit-context";
import { blockSlot, childPath, getAt, joinPath, parsePath } from "./edit-paths";
import { caretOffsetAtPoint } from "./inline-runs-dom";
import { type InlineEditorHandle, subscribeInlineEditor } from "./inline-editor-registry";
import { MARKUP_SPAN_KINDS, type MarkupSpanKind } from "./markup";
import { slideHeading } from "./slide-label";
import { presenceColors } from "./presence-colors";
import {
  addableRuns,
  blockAt,
  blockPathsOf,
  canGrowRun,
  canRemoveBlock,
  primaryTextField,
  type BlockRun,
  type DeckEditor,
} from "./presentation-editor";
import { resolveSlideScheme, themeStyle } from "./theme";

/** A spec path as the editor writes one (`blocks.2.items.0`): what a collaborator's entry may name. */
const BLOCK_PATH = /^[a-z_]+(?:\.[a-z0-9_]+)*$/;

/** A collaborator's selected block, measured on the stage. */
interface OtherSelection {
  userId: string;
  name: string;
  box: Box;
}

const TYPING_TARGETS = "input,textarea,select,[contenteditable]:not([contenteditable='false'])";

/** The re-measure schedule after a change: reveal's slide transition and the
 *  fit / spread passes settle within this. */
const REMEASURE_DELAYS_MS = [0, 120, 400, 900];

/** How far (in stage px) below its block's top a text may sit for the
 *  formatting bar to hang above the block rather than above the text. */
const TEXT_BAR_BLOCK_REACH = 160;

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

function boxOf(element: Element, host: Element): Box {
  const rect = element.getBoundingClientRect();
  const origin = host.getBoundingClientRect();
  return {
    top: rect.top - origin.top,
    left: rect.left - origin.left,
    width: rect.width,
    height: rect.height,
  };
}

/** The slide element (`.deck-slide`) for `index` inside the host, if drawn. */
function slideElement(host: Element, index: number): HTMLElement | null {
  return host.querySelector<HTMLElement>(`.deck-slide[data-slide-index="${index}"]`);
}

function blockElement(host: Element, index: number, path: string): HTMLElement | null {
  return slideElement(host, index)?.querySelector<HTMLElement>(`[data-edit-block="${path}"]`) ?? null;
}

/** The size of a run's "+" (stage px). */
const RUN_ADD_SIZE = 24;

/** Where a run's "+" sits, relative to the layer: centred under its last block. */
interface RunAnchor extends BlockRun {
  left: number;
  top: number;
}

/**
 * The "+" of one run: centred on the run, just under its last block
 * (`data-edit-list` is the run the renderer publishes in edit mode). A run
 * with no block draws no element: an empty slide body puts it at the top of
 * the body, an empty container at the bottom of its block. Kept inside the
 * slide, so the last run of a full slide still shows it.
 */
function runAnchor(slideEl: HTMLElement, run: BlockRun, layer: Element): RunAnchor | null {
  const origin = layer.getBoundingClientRect();
  const frame = slideEl.getBoundingClientRect();
  const list = slideEl.querySelector<HTMLElement>(`[data-edit-list="${run.list}"]`);
  const last = list?.lastElementChild;
  let x: number;
  let y: number;
  if (list && last) {
    const box = list.getBoundingClientRect();
    x = box.left + box.width / 2;
    y = last.getBoundingClientRect().bottom + 6;
  } else {
    const holder = run.container
      ? slideEl.querySelector(`[data-edit-block="${run.container}"]`)
      : slideEl.querySelector(".deck-fit");
    if (!holder) return null;
    const box = holder.getBoundingClientRect();
    x = box.left + box.width / 2;
    y = run.container ? box.bottom - RUN_ADD_SIZE - 8 : box.top + 8;
  }
  const top = Math.min(y, frame.bottom - RUN_ADD_SIZE - 4) - origin.top;
  return { ...run, left: x - RUN_ADD_SIZE / 2 - origin.left, top };
}

/** A human label for a block path, outermost first: `Callout > Text`. */
function describePath(editor: DeckEditor, slide: number, path: string, t: (id: string) => string): string {
  const parts = parsePath(path);
  const names: string[] = [];
  // Every prefix that resolves to a block is a level of nesting (`blocks.2`,
  // `blocks.2.children.1`); the lists and sides between them are not.
  for (let end = 1; end <= parts.length; end += 1) {
    const block = blockAt(editor.draft.slides[slide], joinPath(parts.slice(0, end)));
    if (block) names.push(t(BLOCK_SCHEMA[block.type]?.label ?? block.type));
  }
  return names.join(" \u203a ");
}

export function SlideEditLayer({
  editor,
  activeIndex,
  followsActiveSlide,
  presentation,
  collaborators = [],
}: {
  editor: DeckEditor;
  /** The others with the deck open (presence): the blocks they have selected are outlined. */
  collaborators?: PresentationPresenceEntry[];
  /** The slide on the stage (a deck) or under the reading line (a page document). */
  activeIndex: number;
  /** A deck shows one slide: a selection on another one is dropped when the
   *  deck moves. A page document shows them all and keeps it. */
  followsActiveSlide: boolean;
  /** The deck AS THE STAGE RENDERS IT (the draft overlay): its colour scheme
   *  and its template's palette dress the layer like the canvas, because the
   *  layer sits beside `.deck-root` and draws the tone swatches of its text
   *  bar in the tones the slide draws (`presentation.css`). */
  presentation: Presentation;
}) {
  const { t_i18n } = useFormatter();
  const layerRef = useRef<HTMLDivElement | null>(null);
  const hostRef = useRef<HTMLElement | null>(null);
  const [blockBox, setBlockBox] = useState<Box | null>(null);
  const [textBox, setTextBox] = useState<Box | null>(null);
  const [runAnchors, setRunAnchors] = useState<RunAnchor[]>([]);
  const [others, setOthers] = useState<OtherSelection[]>([]);
  const collaboratorsRef = useRef(collaborators);
  collaboratorsRef.current = collaborators;
  const [handle, setHandle] = useState<InlineEditorHandle | null>(null);
  const [styles, setStyles] = useState({
    bold: false,
    em: false,
    code: false,
    tone: null as MarkupSpanKind | null,
    link: null as string | null,
  });
  // The text bar's slide picker (`Link to a slide`): a list INSIDE the bar,
  // never a menu - a menu takes the focus, and a blur commits the text.
  const [linkPickerOpen, setLinkPickerOpen] = useState(false);
  // The text under the pointer on the click that selected the block: a second
  // click on it opens the editor there.
  const lastText = useRef<{ slide: number; path: string } | null>(null);
  const editorRef = useRef(editor);
  editorRef.current = editor;
  const activeRef = useRef(activeIndex);
  activeRef.current = activeIndex;

  const { selection, editingText, enabled } = editor;

  useEffect(() => subscribeInlineEditor(setHandle), []);

  // The stage surface the layer covers: read BEFORE the passive effects below
  // (a layout effect), so the listeners and the outlines find it on the first
  // pass rather than one render late.
  useLayoutEffect(() => {
    hostRef.current = layerRef.current?.parentElement ?? null;
  });

  // A deck that moved on leaves a selection on a slide no longer on screen.
  useEffect(() => {
    if (!followsActiveSlide || !selection || selection.slide === activeIndex) return;
    editor.select(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIndex, followsActiveSlide]);

  // ── Outlines: the selected / hovered element carries an attribute ─────────
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host || !enabled || !selection) return;
    const slide = slideElement(host, selection.slide);
    const outlined = selection.block === null ? slide : slide?.querySelector(`[data-edit-block="${selection.block}"]`);
    if (!outlined) return;
    outlined.setAttribute("data-edit-selected", selection.block === null ? "slide" : "true");
    // The slide outlives the layer: a saved deck keeps its elements, and the
    // DOM outside the editor carries no edit attribute.
    return () => outlined.removeAttribute("data-edit-selected");
  }, [enabled, selection, editor.draft]);

  // ── Geometry of the floating bars ─────────────────────────────────────────
  const measure = useCallback(() => {
    const host = hostRef.current;
    const layer = layerRef.current;
    const current = editorRef.current;
    if (!host || !layer || !current.enabled) {
      setBlockBox(null);
      setTextBox(null);
      setRunAnchors([]);
      setOthers([]);
      return;
    }
    // The blocks the others have selected, on the slides drawn: found by the
    // slide's stable id in THIS draft (their positions may differ) and drawn
    // only where the slide is on screen.
    setOthers(
      collaboratorsRef.current.flatMap((viewer) => {
        if (!viewer.slide_id || !viewer.block || !BLOCK_PATH.test(viewer.block)) return [];
        const index = current.draft.slides.findIndex((slide) => slide.id === viewer.slide_id);
        if (index < 0 || (followsActiveSlide && index !== activeRef.current)) return [];
        const element = blockElement(host, index, viewer.block);
        if (!element) return [];
        return [{ userId: viewer.user_id, name: viewer.name?.trim() || "", box: boxOf(element, layer) }];
      }),
    );
    // Boxes are relative to the LAYER, which scrolls with a page document's
    // sheets (an absolute child of the scroll surface), so a scrolled page
    // reads the same as an unscrolled deck.
    const sel = current.selection;
    const block = sel?.block !== null && sel ? blockElement(host, sel.slide, sel.block) : null;
    setBlockBox(block ? boxOf(block, layer) : null);
    const editing = host.querySelector("[data-edit-editing]");
    setTextBox(editing ? boxOf(editing, layer) : null);
    // The runs of the slide in focus take a "+" each, while no text is under edit.
    const focus = sel?.slide ?? activeRef.current;
    const slideEl = editing ? null : slideElement(host, focus);
    setRunAnchors(
      slideEl
        ? addableRuns(current.draft.slides[focus], sel?.block ?? null).flatMap((run) => {
            const anchor = runAnchor(slideEl, run, layer);
            return anchor ? [anchor] : [];
          })
        : [],
    );
  }, [followsActiveSlide]);

  const scheduleMeasure = useCallback(() => {
    const timers = REMEASURE_DELAYS_MS.map((delay) => window.setTimeout(measure, delay));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [measure]);

  useEffect(
    () => scheduleMeasure(),
    [scheduleMeasure, selection, editingText, editor.draft, activeIndex, handle, collaborators],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => measure()) : null;
    observer?.observe(host);
    host.addEventListener("transitionend", measure);
    host.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      host.removeEventListener("transitionend", measure);
      host.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  // The text bar's pressed states follow the caret.
  useEffect(() => {
    if (!handle) return;
    const refresh = () => setStyles(handle.activeStyles());
    refresh();
    setLinkPickerOpen(false);
    document.addEventListener("selectionchange", refresh);
    return () => document.removeEventListener("selectionchange", refresh);
  }, [handle]);

  // ── Pointer: resolve what was clicked ─────────────────────────────────────
  useEffect(() => {
    const layer = layerRef.current;
    const host = hostRef.current;
    if (!host || !enabled) return;

    /**
     * The text a click MEANS when it lands next to one: the row, the tile,
     * the card the pointer is on holds exactly one text (a bullet's line, a
     * badge, a KPI's label...), so that text is what opens - a click has to
     * hit the glyphs themselves otherwise, which no slide editor asks for.
     * Walks up to the block (or the slide) and stops at the first ancestor
     * holding one text; an ancestor holding several is ambiguous and yields
     * nothing.
     */
    const impliedText = (from: Element, within: Element): HTMLElement | null => {
      let node: Element | null = from;
      while (node && node !== within.parentElement) {
        const texts = node.querySelectorAll<HTMLElement>("[data-edit-text]");
        if (texts.length === 1) return texts[0];
        if (texts.length > 1) return null;
        node = node.parentElement;
      }
      return null;
    };

    const resolve = (target: EventTarget | null) => {
      if (!(target instanceof Element)) return null;
      if (layer?.contains(target)) return "layer" as const;
      if (target.closest("[data-edit-editing]")) return "editing" as const;
      const slideEl = target.closest<HTMLElement>(".deck-slide[data-slide-index]");
      if (!slideEl) return null;
      const slide = Number(slideEl.dataset.slideIndex);
      const blockEl = target.closest<HTMLElement>("[data-edit-block]");
      const block = blockEl && slideEl.contains(blockEl) ? blockEl : null;
      const direct = target.closest<HTMLElement>("[data-edit-text]");
      const onGlyphs = !!direct && slideEl.contains(direct);
      return {
        slide,
        block: block ? (block.dataset.editBlock ?? null) : null,
        text: onGlyphs ? direct : impliedText(target, block ?? slideEl),
        /** The pointer is ON the text, not merely on its row. */
        onGlyphs,
      };
    };

    let hovered: Element | null = null;
    const setHover = (next: Element | null) => {
      if (hovered === next) return;
      hovered?.removeAttribute("data-edit-hover");
      hovered = next;
      hovered?.setAttribute("data-edit-hover", "true");
    };

    const onPointerOver = (event: PointerEvent) => {
      const hit = resolve(event.target);
      if (!hit || hit === "layer" || hit === "editing") {
        setHover(null);
        return;
      }
      const slideEl = slideElement(host, hit.slide);
      const el = hit.block !== null ? slideEl?.querySelector(`[data-edit-block="${hit.block}"]`) : null;
      setHover(el ?? null);
    };
    const onPointerLeave = () => setHover(null);

    // Whether the block under the pointer was ALREADY selected when the press
    // began - read by the click that follows, after the press has selected it.
    let pressedSelected = false;

    const onPointerDown = (event: PointerEvent) => {
      const hit = resolve(event.target);
      if (hit === "layer" || hit === "editing") return;
      const current = editorRef.current;
      if (!hit) {
        current.select(null);
        return;
      }
      pressedSelected = current.selection?.slide === hit.slide && current.selection?.block === hit.block;
      if (!pressedSelected) current.select({ slide: hit.slide, block: hit.block });
      lastText.current = hit.text?.dataset.editText ? { slide: hit.slide, path: hit.text.dataset.editText } : null;
    };

    const openText = (hit: Exclude<ReturnType<typeof resolve>, null | "layer" | "editing">, event: MouseEvent) => {
      const path = hit.text?.dataset.editText;
      if (!path || !hit.text) return false;
      const caret = caretOffsetAtPoint(hit.text, event.clientX, event.clientY);
      editorRef.current.startTextEdit(hit.slide, path, caret ?? undefined);
      return true;
    };

    const onClick = (event: MouseEvent) => {
      const hit = resolve(event.target);
      if (!hit || hit === "layer" || hit === "editing") return;
      // A link on a banner must not leave the editor.
      if ((event.target as Element).closest("a[href]")) event.preventDefault();
      // A click ON a text opens it at once, the way a slide editor places the
      // caret where the pointer is; a click on the rest of a block selects the
      // block, and the next click on it opens the text the row implies. (The
      // second click of a double-click arrives on the editor it opened and is
      // ignored above.)
      if (hit.text && (hit.onGlyphs || pressedSelected)) openText(hit, event);
    };

    const onDoubleClick = (event: MouseEvent) => {
      const hit = resolve(event.target);
      if (!hit || hit === "layer" || hit === "editing") return;
      if (openText(hit, event)) event.preventDefault();
    };

    host.addEventListener("pointerover", onPointerOver);
    host.addEventListener("pointerleave", onPointerLeave);
    host.addEventListener("pointerdown", onPointerDown, true);
    host.addEventListener("click", onClick, true);
    host.addEventListener("dblclick", onDoubleClick, true);
    return () => {
      setHover(null);
      host.removeEventListener("pointerover", onPointerOver);
      host.removeEventListener("pointerleave", onPointerLeave);
      host.removeEventListener("pointerdown", onPointerDown, true);
      host.removeEventListener("click", onClick, true);
      host.removeEventListener("dblclick", onDoubleClick, true);
    };
  }, [enabled]);

  // ── Keys ──────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!enabled) return;
    /**
     * Whether an undo / redo asked from `target` is the SESSION's:
     * from anywhere in the viewer but a field - and from a field INSIDE the
     * editor panel, which writes to the draft on every keystroke, one
     * coalesced step per field, so the deck's undo is the field's undo and
     * the browser's per-input history (which the session never sees) is
     * suppressed. Never from a text under edit in place, which walks its own
     * session history (`InlineTextEditor`), nor from a field elsewhere (the
     * studio composer, a comment), which keeps the browser's - nor from a
     * panel field that writes nothing to the draft (`OWN_UNDO_ATTR`).
     */
    const routesUndo = (target: Element | null): boolean => {
      const typing = !!target?.closest(TYPING_TARGETS);
      const inPlace = !!target?.closest("[data-edit-editing]");
      const panelField = !!target?.closest(EDITOR_PANEL_SELECTOR) && !target?.closest(OWN_UNDO_SELECTOR);
      return !inPlace && (!typing || panelField);
    };
    // The browser's own undo / redo asked WITHOUT a key - the Edit menu, the
    // context menu of a panel field - arrives as a `beforeinput` on that
    // field; routed like Ctrl+Z, or the input's native history would
    // run and its result reach the draft as one more change.
    const onBeforeInput = (event: InputEvent) => {
      if (event.inputType !== "historyUndo" && event.inputType !== "historyRedo") return;
      const target = event.target instanceof Element ? event.target : null;
      if (!routesUndo(target)) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.inputType === "historyRedo") editorRef.current.redo();
      else editorRef.current.undo();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      // A key pressed WHILE an input method composes belongs to the
      // composition (Enter confirms it, Escape drops it): none of the
      // shortcuts below may read it - the in-place editor's rule.
      if (event.isComposing) return;
      const current = editorRef.current;
      const target = event.target instanceof Element ? event.target : null;
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      const typing = !!target?.closest(TYPING_TARGETS);
      // Save works from anywhere in the viewer. Undo / redo too, the
      // editor panel's fields included - see `routesUndo`.
      if (mod && key === "s") {
        event.preventDefault();
        event.stopPropagation();
        // A text under edit is committed first - and nothing is saved when
        // the field refuses that commit (`requestSave`). A held key
        // asks once, not once per auto-repeat.
        if (!event.repeat) current.requestSave();
        return;
      }
      if (mod && (key === "z" || key === "y") && routesUndo(target)) {
        event.preventDefault();
        event.stopPropagation();
        if (key === "y" || event.shiftKey) current.redo();
        else current.undo();
        return;
      }
      // The rest is about the selection on the stage: not from the panel, not
      // from a field, not from a menu (the deck's own list of owned regions),
      // and only while the focus IS on the stage - the body after a click on
      // a slide, the stage surface, the layer's own bars (the one keys-off
      // region that still counts as the stage). A toolbar button, the viewer
      // header, the studio rail keep their keys: Tab moves the focus on, Enter
      // and Space activate the control (`deckMayHandleKey`'s rule 2), Delete
      // deletes nothing.
      const owned = target?.closest(OWNED_REGIONS);
      if (typing || (owned && owned !== layerRef.current)) return;
      const onStage =
        !target ||
        target === document.body ||
        !!hostRef.current?.contains(target) ||
        !!layerRef.current?.contains(target);
      if (!onStage) return;
      if ((event.key === "Enter" || event.key === " ") && target?.closest(ACTIVATION_TARGETS)) return;
      // Inside the layer's own bars the Tab key moves the focus from button
      // to button, as anywhere else; it walks the blocks from the stage only.
      if (event.key === "Tab" && target && layerRef.current?.contains(target)) return;
      const sel = current.selection;
      if (event.key === "Escape") {
        if (current.editingText) return; // the editor owns it (and stops it)
        if (sel) {
          event.preventDefault();
          event.stopPropagation();
          current.select(null);
        }
        return;
      }
      if (!sel) return;
      const slide = current.draft.slides[sel.slide];
      if (!slide) return;
      if (event.key === "Tab") {
        const paths = blockPathsOf(slide);
        if (paths.length === 0) return;
        event.preventDefault();
        event.stopPropagation();
        const at = sel.block ? paths.indexOf(sel.block) : -1;
        const next = event.shiftKey ? paths[(at <= 0 ? paths.length : at) - 1] : paths[(at + 1) % paths.length];
        current.select({ slide: sel.slide, block: next });
        return;
      }
      if (event.key === "Enter") {
        const block = sel.block ? blockAt(slide, sel.block) : undefined;
        const field = block ? primaryTextField(block) : null;
        // Without a main text, the text last clicked - on THIS slide: a path
        // remembered from another slide names nothing here (or a same-shaped
        // field by accident).
        const remembered = lastText.current?.slide === sel.slide ? lastText.current.path : null;
        const path = field && sel.block ? childPath(sel.block, field) : remembered;
        if (path) {
          event.preventDefault();
          event.stopPropagation();
          current.startTextEdit(sel.slide, path);
        }
        return;
      }
      if (!sel.block) return;
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        event.stopPropagation();
        if (canRemoveBlock(slide, sel.block)) current.removeBlock(sel.slide, sel.block);
        return;
      }
      if (mod && key === "d") {
        event.preventDefault();
        event.stopPropagation();
        current.duplicateBlock(sel.slide, sel.block);
        return;
      }
      if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        event.preventDefault();
        event.stopPropagation();
        current.moveBlock(sel.slide, sel.block, event.key === "ArrowUp" ? -1 : 1);
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("beforeinput", onBeforeInput, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("beforeinput", onBeforeInput, true);
    };
  }, [enabled]);

  // ── The block bar ─────────────────────────────────────────────────────────
  const slide = selection ? editor.draft.slides[selection.slide] : undefined;
  const block = selection?.block && slide ? blockAt(slide, selection.block) : undefined;
  const slot = selection?.block ? blockSlot(selection.block) : null;
  const siblings = slot && slide ? getAt(slide, slot.list) : undefined;
  const siblingCount = Array.isArray(siblings) ? siblings.length : 0;
  const listPath = slot ? joinPath(slot.list) : null;
  // The run's cap (`MAX_BLOCKS_PER_*`): Duplicate and Insert stop here, not at a 422.
  const runFull = !!slide && !!listPath && !canGrowRun(slide, listPath);

  const paletteItems = useMemo<OverflowMenuItem[]>(() => {
    if (!selection?.block || !listPath || !slot) return [];
    return blockPaletteItems(editor, selection.slide, listPath, slot.index + 1, t_i18n);
  }, [editor, listPath, selection, slot, t_i18n]);
  const focusSlide = selection?.slide ?? activeIndex;
  /** What a run's "+" says: where the block it adds lands. */
  const runLabel = (run: BlockRun) =>
    run.kind === "column"
      ? t_i18n("Add a block to column {n}", { n: run.column ?? 1 })
      : run.kind === "inside"
        ? t_i18n("Add a block inside {block}", {
            block: describePath(editor, focusSlide, run.container ?? "", t_i18n),
          })
        : t_i18n("Add a block");

  if (!enabled) return null;

  // Geometry, plus the template's palette variables the canvas also carries
  // inline (the one styling exception the deck makes: a palette is deck data),
  // resolved for the scheme the SELECTED slide renders in (a slide may
  // carry a theme of its own), so the text bar's tone swatches are the tones
  // that slide draws - as every deck host resolves them.
  const focusedSlide = selection ? editor.draft.slides[selection.slide] : undefined;
  const { scheme: theme, tokens } = resolveSlideScheme(presentation, focusedSlide);
  const layerStyle: CSSProperties = {
    ...themeStyle(tokens),
    position: "absolute",
    inset: 0,
    pointerEvents: "none",
    zIndex: 20,
  };

  const placeAbove = (box: Box, barHeight: number): CSSProperties => {
    const layer = layerRef.current;
    const width = layer?.clientWidth ?? 0;
    const top = box.top - barHeight - 8;
    return {
      position: "absolute",
      // Above the box when there is room, below it otherwise; clamped inside the stage.
      top: top >= 4 ? top : box.top + box.height + 8,
      left: Math.max(4, Math.min(box.left, Math.max(4, width - 360))),
      pointerEvents: "auto",
    };
  };

  const textField = block ? primaryTextField(block) : null;
  const BlockIcon = block ? BLOCK_ICONS[block.type] : null;

  /**
   * Where the formatting bar hangs while a text is edited: above the BLOCK
   * when the text sits in its upper part, so the bar covers none of the
   * block's own lines (the bullet above the one being typed, a card's title
   * over its description); above the text itself when the text is deep inside
   * a tall block, where a bar at the block's edge would float far from the
   * caret. Aligned on the text's left edge either way.
   */
  const textBarAnchor = (text: Box, container: Box | null): Box => {
    if (!container || text.top - container.top > TEXT_BAR_BLOCK_REACH) return text;
    return { ...container, left: text.left };
  };
  const canMoveUp = !!slot && slot.index > 0;
  const canMoveDown = !!slot && slot.index < siblingCount - 1;
  const canDelete = !!slide && !!selection?.block && canRemoveBlock(slide, selection.block);

  return (
    <div
      ref={layerRef}
      className="deck-edit-layer"
      style={layerStyle}
      data-deck-theme={theme}
      data-testid="presentation.editLayer"
      {...DECK_KEYS_OFF_ATTR}
    >
      {/* Where the others work: their selected block, in their colour,
          under the person's own bars and never in the way of the pointer. */}
      {others.map((other) => {
        const colors = presenceColors(other.userId);
        return (
          <div
            key={other.userId}
            className={cn("pointer-events-none absolute rounded-sm border-2 border-dashed", colors.outline)}
            style={{ top: other.box.top - 3, left: other.box.left - 3, width: other.box.width + 6, height: other.box.height + 6 }}
            data-testid="presentation.collaboratorSelection"
            aria-hidden="true"
          >
            {other.name && (
              <span
                className={cn(
                  "absolute -top-5 left-0 flex max-w-[180px] items-center gap-1 truncate rounded-sm border px-1.5 py-px content-caption font-medium",
                  "layer-2 bg-elevation-default text-default-primary",
                  colors.outline,
                )}
              >
                <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", colors.dot)} />
                {other.name}
              </span>
            )}
          </div>
        );
      })}
      {/* A "+" at the end of every run of the slide in focus: the body
          or each column, and the runs inside the selected container - a block
          added there lands after the run's last one. */}
      {!editingText &&
        runAnchors.map((run) => (
          <div
            key={run.list}
            className="absolute"
            style={{ top: run.top, left: run.left, pointerEvents: "auto" }}
            data-testid="presentation.runAdd"
            data-run={run.list}
          >
            <OverflowMenu
              items={blockPaletteItems(editor, focusSlide, run.list, run.length, t_i18n)}
              label={runLabel(run)}
              trigger={<Plus className="h-3.5 w-3.5" aria-hidden="true" />}
              side={run.top > (layerRef.current?.clientHeight ?? 0) / 2 ? "top" : "bottom"}
              scroll
              className="h-6 w-6 rounded-full border border-accent/50 layer-1 bg-elevation-default px-0 text-accent shadow-overlay backdrop-blur-sm hover:bg-accent/15"
              header={runLabel(run)}
            />
          </div>
        ))}
      {blockBox && selection?.block && block && !editingText && (
        <div
          role="toolbar"
          aria-label={t_i18n("Block")}
          data-testid="presentation.blockBar"
          className="flex items-center gap-0.5 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-1 shadow-overlay backdrop-blur-sm"
          style={placeAbove(blockBox, 36)}
        >
          <span className="flex items-center gap-1.5 px-1.5 text-[11px] font-medium text-accent">
            {BlockIcon && <BlockIcon className="h-3.5 w-3.5" aria-hidden="true" />}
            <span className="max-w-[180px] truncate">
              {describePath(editor, selection.slide, selection.block, t_i18n)}
            </span>
          </span>
          <span className="mx-0.5 h-4 w-px bg-[var(--border-elevation-subtle)]" aria-hidden="true" />
          {textField && (
            <BarButton
              icon={PencilLine}
              label={t_i18n("Edit text (Enter)")}
              onClick={() => editor.startTextEdit(selection.slide, childPath(selection.block!, textField))}
            />
          )}
          <BarButton
            icon={ChevronUp}
            label={t_i18n("Move up (Alt+Up)")}
            disabled={!canMoveUp}
            onClick={() => editor.moveBlock(selection.slide, selection.block!, -1)}
          />
          <BarButton
            icon={ChevronDown}
            label={t_i18n("Move down (Alt+Down)")}
            disabled={!canMoveDown}
            onClick={() => editor.moveBlock(selection.slide, selection.block!, 1)}
          />
          <BarButton
            icon={CopyPlus}
            label={t_i18n("Duplicate (Ctrl+D)")}
            disabled={!slot || runFull}
            onClick={() => editor.duplicateBlock(selection.slide, selection.block!)}
          />
          {paletteItems.length > 0 && (
            <OverflowMenu
              items={paletteItems}
              label={t_i18n("Insert a block after this one")}
              trigger={<Plus className="h-3.5 w-3.5" aria-hidden="true" />}
              scroll
              className="h-7 w-7 rounded-sm text-default-secondary hover:bg-elevation-hover hover:text-accent"
              header={t_i18n("Insert after")}
            />
          )}
          <BarButton
            icon={Trash2}
            label={t_i18n("Delete (Delete)")}
            disabled={!canDelete}
            destructive
            onClick={() => editor.removeBlock(selection.slide, selection.block!)}
          />
        </div>
      )}

      {textBox && handle && editingText && (
        <div
          role="toolbar"
          aria-label={t_i18n("Text formatting")}
          data-testid="presentation.textBar"
          className="flex items-center gap-0.5 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-1 shadow-overlay backdrop-blur-sm"
          style={placeAbove(textBarAnchor(textBox, blockBox), 36)}
          // The bar must not take the focus from the field: a blur commits.
          onMouseDown={(event) => event.preventDefault()}
        >
          {linkPickerOpen && !handle.plain && (
            <SlideLinkPicker
              slides={editor.draft.slides}
              current={editingText.slide}
              linked={styles.link}
              onPick={(target) => {
                handle.applyLink(target);
                setLinkPickerOpen(false);
              }}
            />
          )}
          {!handle.plain && (
            <>
              <BarButton
                icon={Bold}
                label={t_i18n("Bold (Ctrl+B)")}
                pressed={styles.bold}
                onClick={() => handle.applyFormat("bold")}
              />
              <BarButton
                icon={Italic}
                label={t_i18n("Emphasis (Ctrl+I)")}
                pressed={styles.em}
                onClick={() => handle.applyFormat("em")}
              />
              <BarButton
                icon={Code}
                label={t_i18n("Code (Ctrl+E)")}
                pressed={styles.code}
                onClick={() => handle.applyFormat("code")}
              />
              <span className="mx-0.5 h-4 w-px bg-[var(--border-elevation-subtle)]" aria-hidden="true" />
              {MARKUP_SPAN_KINDS.map((tone) => (
                <Tip key={tone} content={t_i18n("Tone: {tone}", { tone })}>
                  <button
                    type="button"
                    aria-label={t_i18n("Tone: {tone}", { tone })}
                    aria-pressed={styles.tone === tone}
                    onClick={() => handle.applyFormat("span", tone)}
                    className={cn(
                      "flex h-7 w-6 items-center justify-center rounded-sm hover:bg-elevation-hover",
                      styles.tone === tone && "bg-accent/15",
                    )}
                  >
                    <span className="deck-edit-tone-swatch" data-tone={tone} aria-hidden="true" />
                  </button>
                </Tip>
              ))}
              <BarButton
                icon={Link2}
                label={t_i18n("Link to a slide")}
                pressed={styles.link !== null || linkPickerOpen}
                onClick={() => setLinkPickerOpen((open) => !open)}
              />
              <BarButton
                icon={RemoveFormatting}
                label={t_i18n("Clear formatting")}
                onClick={() => handle.applyFormat("plain")}
              />
              {!handle.singleLine && (
                <>
                  <span className="mx-0.5 h-4 w-px bg-[var(--border-elevation-subtle)]" aria-hidden="true" />
                  <BarButton
                    icon={CornerDownLeft}
                    label={t_i18n("Line break (Enter)")}
                    onClick={() => handle.insertLineBreak()}
                  />
                </>
              )}
            </>
          )}
          <BarButton icon={Check} label={t_i18n("Done (Ctrl+Enter)")} accent onClick={() => handle.commit()} />
        </div>
      )}
    </div>
  );
}

/**
 * The slides a text under edit can link to (`[text](#/<slide id>)`), by
 * number and heading, hung under the text bar. Every slide but the one being
 * edited and one with no id (a deck stored before slides had ids: a link
 * names its target by id). `Remove link` first when the selection is a link.
 */
function SlideLinkPicker({
  slides,
  current,
  linked,
  onPick,
}: {
  slides: PresentationSlide[];
  current: number;
  linked: string | null;
  onPick: (target: string | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const targets = slides.flatMap((slide, index) => (slide.id && index !== current ? [{ slide, index }] : []));
  const item =
    "flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-xs text-default-primary hover:bg-elevation-hover";
  return (
    <div
      role="group"
      aria-label={t_i18n("Link to a slide")}
      data-testid="presentation.slideLinkPicker"
      className="absolute left-0 top-full mt-1 flex max-h-64 w-80 flex-col gap-0.5 overflow-y-auto rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-1 shadow-overlay"
    >
      {linked !== null && (
        <button type="button" className={cn(item, "text-feedback-error-primary")} onClick={() => onPick(null)}>
          <Link2Off className="h-3.5 w-3.5 flex-none" aria-hidden="true" />
          {t_i18n("Remove link")}
        </button>
      )}
      {targets.length === 0 ? (
        <p className="px-2 py-1 text-xs text-default-secondary">{t_i18n("No other slide to link to.")}</p>
      ) : (
        targets.map(({ slide, index }) => (
          <button
            key={slide.id}
            type="button"
            aria-pressed={linked === slide.id}
            className={cn(item, linked === slide.id && "bg-accent/15 text-accent")}
            onClick={() => onPick(slide.id ?? null)}
          >
            <span className="w-6 flex-none text-right tabular-nums text-default-secondary">{index + 1}</span>
            <span className="truncate">{slideHeading(slide) || t_i18n("Untitled slide")}</span>
          </button>
        ))
      )}
    </div>
  );
}

function BarButton({
  icon: Icon,
  label,
  onClick,
  disabled,
  pressed,
  destructive,
  accent,
}: {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  pressed?: boolean;
  destructive?: boolean;
  accent?: boolean;
}) {
  return (
    <Tip content={label} wrap={disabled}>
      <button
        type="button"
        aria-label={label}
        aria-pressed={pressed}
        disabled={disabled}
        onClick={onClick}
        className={cn(
          "flex h-7 w-7 items-center justify-center rounded-sm text-default-secondary transition-colors",
          "hover:bg-elevation-hover hover:text-accent disabled:pointer-events-none disabled:opacity-40",
          pressed && "bg-accent/15 text-accent",
          destructive && "hover:text-feedback-error-primary",
          accent && "text-accent",
        )}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </Tip>
  );
}
