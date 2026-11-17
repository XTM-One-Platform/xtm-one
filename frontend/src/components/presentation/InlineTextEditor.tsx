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
 * A deck string edited IN ITS PLACE on the slide.
 *
 * `InlineMarkup` swaps itself for this component while its text is the one
 * under edit: the same element (`as`), the same class, so the same stylesheet
 * rules - the text is edited at the size, font, colour and wrapping the
 * audience sees, and the slide reflows around it as it grows, the way a text
 * box does in PowerPoint. What differs is `contentEditable`.
 *
 * The element is UNCONTROLLED on purpose. React renders its initial children
 * once (from the runs of the initial value) and never again: the browser owns
 * the DOM while the user types, and a React re-render of this subtree would
 * diff against a tree the browser has restructured. The rule that makes it
 * safe: the children vdom never changes during a session (memoised on mount),
 * so React never touches the DOM under the editable; a formatting action
 * rebuilds the children by hand (`runsToNodes`) and the whole element is
 * unmounted when the session ends. `InlineMarkup` keys the editor on the
 * target, so a different text under edit is a new mount.
 *
 * The words reach the draft WHILE they are typed: a pause of
 * `LIVE_TEXT_DELAY_MS` hands the text as it stands to the session
 * (`onChange`), so the autosave writes it without the field being closed -
 * only markup the save would accept, the rest waits for the person to fix it.
 * `flush()` hands it over at once (a save, a collaborator's revision to merge).
 *
 * Commit = blur, `Ctrl` / `Cmd` + `Enter`, `Tab`. Cancel = `Escape` (the text
 * as it was when the field opened goes back to the draft). `Enter`
 * inserts a line break (the grammar's `\n`) - a plain field commits instead,
 * and so does a `singleLine` one (markup allowed, drawn on ONE row: a profile
 * fact - the validator refuses a line break there, so the field never
 * offers one: a pasted break becomes a space).
 * `Ctrl` / `Cmd` + `B` / `I` / `E` toggle bold / emphasis / code over the
 * selection (or the word at the caret); the floating bar (`SlideEditLayer`)
 * drives the same `applyFormat` through the handle it registers. Pasting and
 * dropping insert plain text: the grammar has no room for a pasted `<font>`
 * (a drag of the field's own text is a move, done in the runs model).
 *
 * The grammar has no escape, so a stray `` ` ``, `**`, `[[` or `<` is markup
 * the save would refuse. An explicit commit (the keys above, the
 * bar's Done, the commit a save asks for) of such a text is refused: the
 * field stays open with the text as typed and a toast says why, in the
 * inspector's words. A blur keeps the words and says the same (`inlineCommit`).
 *
 * After every formatting action the runs are CANONICALISED
 * (`canonicalRuns`): a decoration the grammar cannot carry at that position -
 * emphasis glued to a letter - disappears on screen at once rather than at the
 * save, so what is shown is what is kept.
 *
 * `Ctrl` / `Cmd` + `Z` / `Y` (and `Shift+Z`) walk the SESSION's own history
 * (`text-history.ts`): typing coalesced into bursts, every formatting,
 * line break, paste and drop a step of its own, each undone back to the
 * selection the change began at. The browser's own undo is suppressed - the
 * keys, and the `historyUndo` / `historyRedo` a context menu or the Edit menu
 * dispatch through `beforeinput` - because it knows nothing of the rebuilds a
 * formatting does by hand, and reset itself on each. The stage's key handler
 * leaves `Ctrl+Z` to this field while it is open; committing or cancelling
 * hands back to the deck's history, where the whole edit is one step.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import { Fragment } from "react";

import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";

import { publishInlineEditor, retireInlineEditor, type InlineEditorHandle } from "./inline-editor-registry";
import {
  applyLink,
  applyStyle,
  canonicalRuns,
  inlineCommit,
  linkAround,
  moveRange,
  rangeHasStyle,
  rangeLinkTarget,
  replaceRange,
  runsFromMarkup,
  runsToMarkup,
  runsToPlainText,
  styleAt,
  type TextRun,
} from "./inline-runs";
import {
  caretOffsetAtPoint,
  domToRuns,
  runsToNodes,
  selectionOffsets,
  setSelectionOffsets,
  wordRangeAt,
} from "./inline-runs-dom";
import { useMarkupProblemText } from "./markup-problems";
import {
  recordSnapshot,
  redoSnapshot,
  textHistoryFrom,
  undoSnapshot,
  type TextHistory,
  type TextSelection,
} from "./text-history";

/** How long the typing pauses before the text is handed to the draft. */
const LIVE_TEXT_DELAY_MS = 300;

function renderInitial(runs: readonly TextRun[]): ReactNode[] {
  const nodes: ReactNode[] = [];
  runs.forEach((run, index) => {
    if (run.style === "plain") {
      const lines = run.text.split("\n");
      lines.forEach((line, lineIndex) => {
        if (lineIndex > 0) nodes.push(<br key={`${index}-br-${lineIndex}`} />);
        if (line) nodes.push(<Fragment key={`${index}-${lineIndex}`}>{line}</Fragment>);
      });
      return;
    }
    switch (run.style) {
      case "bold":
        nodes.push(<strong key={index}>{run.text}</strong>);
        break;
      case "em":
        nodes.push(<em key={index}>{run.text}</em>);
        break;
      case "code":
        nodes.push(<code key={index}>{run.text}</code>);
        break;
      default:
        nodes.push(
          <span key={index} className="deck-span" data-tone={run.tone}>
            {run.text}
          </span>,
        );
    }
  });
  return nodes;
}

export function InlineTextEditor({
  as: Tag,
  className,
  target,
  initialValue,
  plain = false,
  singleLine = false,
  caret,
  onChange,
  onCommit,
  onCancel,
}: {
  as: keyof React.JSX.IntrinsicElements;
  className?: string;
  /** The slide-relative text path, kept on the element for the layer's geometry. */
  target: string;
  initialValue: string;
  /** A plain-text field (a metric value): no markup, no line breaks. */
  plain?: boolean;
  /** A one-row markup field (a profile fact): markup, no line breaks - Enter commits. */
  singleLine?: boolean;
  /** Where to open the caret (plain-text offset); the end when omitted. */
  caret?: number;
  /** The text as it is being typed, valid markup only. */
  onChange?: (value: string) => void;
  onCommit: (value: string) => void;
  onCancel: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const problemText = useMarkupProblemText();
  const ref = useRef<HTMLElement | null>(null);
  // Read once: the session is uncontrolled from here on (see the module note).
  const initialRuns = useMemo(
    () => (plain ? [{ text: initialValue, style: "plain" as const }] : runsFromMarkup(initialValue)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const children = useMemo(() => renderInitial(initialRuns), [initialRuns]);
  const settled = useRef(false);
  const commitRef = useRef(onCommit);
  commitRef.current = onCommit;
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  // What the draft holds for this field: the opening value, then each text
  // handed over. A timer pending a hand-over, cleared when the session ends.
  const handed = useRef(initialValue);
  const liveTimer = useRef<number | null>(null);

  const stopLiveTimer = () => {
    if (liveTimer.current !== null) window.clearTimeout(liveTimer.current);
    liveTimer.current = null;
  };

  const readValue = (): string => {
    const element = ref.current;
    if (!element) return initialValue;
    const runs = domToRuns(element);
    if (plain) return runsToPlainText(runs).replace(/\s*\n\s*/g, " ").trim();
    const markup = runsToMarkup(runs);
    // A break the browser slipped in anyway (a drag, an input method) is a
    // space: the validator refuses a line break in a one-row field.
    return singleLine ? markup.replace(/\s*\n\s*/g, " ").trim() : markup;
  };

  // The text as it stands to the draft, the field left open (`flush`).
  const handOver = (): "clean" | "pushed" | "refused" => {
    stopLiveTimer();
    if (settled.current) return "clean";
    const value = readValue();
    if (value === handed.current) return "clean";
    if (!inlineCommit(value, "done", plain).commit) return "refused";
    handed.current = value;
    changeRef.current?.(value);
    return "pushed";
  };

  // `done` is an explicit commit, `commit` a blur (`inlineCommit`). Whether
  // the session ended: `false` when the field refused and stays open.
  const finish = (how: "done" | "commit" | "cancel"): boolean => {
    if (settled.current) return true;
    if (how === "cancel") {
      stopLiveTimer();
      settled.current = true;
      // Escape takes back what the field already handed to the draft: the
      // value it opened with is the session's last write.
      if (handed.current !== initialValue) commitRef.current(initialValue);
      else cancelRef.current();
      return true;
    }
    const value = readValue();
    const { commit, problem } = inlineCommit(value, how, plain);
    const reason = problemText(problem);
    if (reason) toast({ title: reason, variant: "error" });
    if (!commit) return false;
    stopLiveTimer();
    settled.current = true;
    commitRef.current(value);
    return true;
  };

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.focus({ preventScroll: true });
    const length = runsToPlainText(initialRuns).length;
    const at = caret === undefined ? length : Math.min(Math.max(caret, 0), length);
    setSelectionOffsets(element, at, at);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    // The session's history: snapshots of the runs and the selection,
    // seeded with the field as it opened. `input` events and the editor's own
    // rebuilds record; Ctrl+Z / Ctrl+Y restore by rebuilding. Every record
    // carries the selection as it was right BEFORE the change (`before`): it
    // is where an undo of that change puts the caret back, and a change that
    // does not begin where the last one left the caret is a step of its own.
    const caretAt = (): TextSelection => {
      const offsets = selectionOffsets(element);
      if (offsets) return offsets;
      const length = runsToPlainText(domToRuns(element)).length;
      return { start: length, end: length };
    };
    let history: TextHistory = textHistoryFrom({ runs: initialRuns, ...caretAt() });
    // The selection `beforeinput` saw, for the `input` event that follows it;
    // `null` once consumed, so a change the browser made without announcing
    // it first is recorded without a "before" rather than with a stale one.
    let pendingBefore: TextSelection | null = null;
    // Every change is also on its way to the draft, once the typing pauses.
    const scheduleHandOver = () => {
      stopLiveTimer();
      liveTimer.current = window.setTimeout(() => {
        liveTimer.current = null;
        handOver();
      }, LIVE_TEXT_DELAY_MS);
    };
    const record = (kind: string, before?: TextSelection) => {
      history = recordSnapshot(history, { runs: domToRuns(element), ...caretAt() }, kind, Date.now(), before);
      scheduleHandOver();
    };

    const restore = (runs: readonly TextRun[], selection: TextSelection) => {
      element.replaceChildren(...runsToNodes([...runs], element.ownerDocument));
      setSelectionOffsets(element, selection.start, selection.end);
      scheduleHandOver();
    };
    const rebuild = (runs: TextRun[], selection: TextSelection, kind: string, before: TextSelection) => {
      restore(runs, selection);
      record(kind, before);
    };
    const walk = (direction: "undo" | "redo") => {
      const next = direction === "undo" ? undoSnapshot(history) : redoSnapshot(history);
      if (!next) return;
      history = next;
      restore(next.present.runs, next.present);
    };

    const currentRange = (): { start: number; end: number } => {
      const offsets = selectionOffsets(element);
      if (!offsets) {
        const length = runsToPlainText(domToRuns(element)).length;
        return { start: length, end: length };
      }
      if (offsets.start === offsets.end) {
        return wordRangeAt(runsToPlainText(domToRuns(element)), offsets.start);
      }
      return offsets;
    };

    const handle: InlineEditorHandle = {
      element,
      plain,
      singleLine,
      activeStyles: () => {
        const runs = domToRuns(element);
        const range = currentRange();
        // The tone pressed is the tone AT the selection (the run under the
        // caret, or the one the selection starts in), and only when the whole
        // selection carries that same tone - `[[info:a]] [[error:b]]` with
        // the caret on `b` reads `error`, never the first span of the field.
        const at = styleAt(runs, range.end > range.start ? range.start + 1 : range.start);
        const tone = at?.style === "span" ? (at.tone ?? null) : null;
        const offsets = selectionOffsets(element);
        const link =
          offsets && offsets.start === offsets.end
            ? (linkAround(runs, offsets.start)?.target ?? null)
            : rangeLinkTarget(runs, range.start, range.end);
        return {
          bold: rangeHasStyle(runs, range.start, range.end, "bold"),
          em: rangeHasStyle(runs, range.start, range.end, "em"),
          code: rangeHasStyle(runs, range.start, range.end, "code"),
          tone: tone && rangeHasStyle(runs, range.start, range.end, "span", tone) ? tone : null,
          link,
        };
      },
      applyLink: (target) => {
        if (plain) return;
        const runs = domToRuns(element);
        const offsets = selectionOffsets(element);
        // "Remove link" with the caret in a label takes off the whole link,
        // not the word under the caret.
        const around = !target && offsets && offsets.start === offsets.end ? linkAround(runs, offsets.start) : null;
        const range = around ?? currentRange();
        if (range.end <= range.start) return;
        rebuild(canonicalRuns(applyLink(runs, range.start, range.end, target)), range, "format", range);
      },
      applyFormat: (style, tone) => {
        if (plain) return;
        const runs = domToRuns(element);
        const range = currentRange();
        if (range.end <= range.start) return;
        const already =
          style === "plain" ? false : rangeHasStyle(runs, range.start, range.end, style, tone);
        const next = canonicalRuns(
          already ? applyStyle(runs, range.start, range.end, "plain") : applyStyle(runs, range.start, range.end, style, tone),
        );
        // Undone, the formatting leaves the range it styled selected (the word
        // a collapsed caret widened to included).
        rebuild(next, range, "format", range);
      },
      insertLineBreak: () => {
        if (plain || singleLine) return;
        const runs = domToRuns(element);
        const offsets = selectionOffsets(element);
        const length = runsToPlainText(runs).length;
        const at = offsets ? offsets.start : length;
        const end = offsets ? offsets.end : length;
        // The selection (or the caret) becomes a line break.
        rebuild(canonicalRuns(replaceRange(runs, at, end, "\n")), { start: at + 1, end: at + 1 }, "line-break", { start: at, end });
      },
      commit: () => finish("done"),
      flush: handOver,
      cancel: () => finish("cancel"),
    };
    publishInlineEditor(handle);

    const onKeyDown = (event: KeyboardEvent) => {
      // A key pressed WHILE an input method composes belongs to the
      // composition: a Japanese conversion is confirmed with Enter and dropped
      // with Escape, and reading those as "line break" / "cancel the edit"
      // would break the word being typed. Keep the key in the field and let
      // the browser finish the composition; the committed text is read from
      // the DOM afterwards like any other.
      if (event.isComposing) {
        event.stopPropagation();
        return;
      }
      const mod = event.ctrlKey || event.metaKey;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        finish("cancel");
        return;
      }
      // A commit key held down auto-repeats: one attempt per press, or a text
      // the field refuses would raise a toast per repeat.
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        if (mod || plain || singleLine) {
          if (!event.repeat) finish("done");
        } else handle.insertLineBreak();
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        if (!event.repeat) finish("done");
        return;
      }
      if (mod) {
        const key = event.key.toLowerCase();
        // The session's own undo / redo: the browser's would not know
        // the rebuilds a formatting does, and reset itself on each of them.
        if (key === "z" || key === "y") {
          event.preventDefault();
          event.stopPropagation();
          walk(key === "y" || event.shiftKey ? "redo" : "undo");
          return;
        }
        if (!plain && (key === "b" || key === "i" || key === "e")) {
          event.preventDefault();
          event.stopPropagation();
          handle.applyFormat(key === "b" ? "bold" : key === "i" ? "em" : "code");
        }
      }
      // Every other key (arrows, letters, Delete) stays inside the field:
      // reveal ignores a contentEditable target, the layer's shortcuts do too.
      event.stopPropagation();
    };
    // The kind the `input` event of an insertion made here is recorded as.
    let inserting: "insertFromPaste" | "insertFromDrop" | null = null;
    const insertPlain = (text: string, kind: "insertFromPaste" | "insertFromDrop") => {
      const flat = plain || singleLine ? text.replace(/\s*\n\s*/g, " ") : text;
      // `insertText` goes through the browser's editing machinery (the caret,
      // the selection replaced) and fires the `input` event the history reads
      // - as a paste or a drop, its own step, not a burst of typing.
      const before = caretAt();
      pendingBefore = before;
      inserting = kind;
      try {
        element.ownerDocument.execCommand("insertText", false, flat);
      } finally {
        inserting = null;
      }
      // An insertion the browser applied without an `input` event is recorded
      // here (a no-op when the event fired: the runs are already the present's).
      record(kind, pendingBefore ?? undefined);
      pendingBefore = null;
    };
    const onPaste = (event: ClipboardEvent) => {
      event.preventDefault();
      insertPlain(event.clipboardData?.getData("text/plain") ?? "", "insertFromPaste");
    };
    // The browser's own drop inserts what the source put in the drag - the
    // styled HTML of a page, a Word fragment - and moves the field's own
    // text as HTML too. A drop is a paste at the drop point; a drag
    // that began in this field (`dragged`, the stretch it carries) is a move.
    let dragged: TextSelection | null = null;
    const onDragStart = () => {
      const offsets = selectionOffsets(element);
      dragged = offsets && offsets.end > offsets.start ? offsets : null;
    };
    const onDragEnd = () => {
      dragged = null;
    };
    const onDrop = (event: DragEvent) => {
      event.preventDefault();
      const at = caretOffsetAtPoint(element, event.clientX, event.clientY);
      const source = dragged;
      dragged = null;
      if (source) {
        // Reported as a copy: no engine also removes the source behind this move.
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
        const moved = at === null ? null : moveRange(domToRuns(element), source.start, source.end, at);
        if (moved) rebuild(canonicalRuns(moved.runs), moved, "insertFromDrop", source);
        return;
      }
      element.focus({ preventScroll: true });
      if (at !== null) setSelectionOffsets(element, at, at);
      insertPlain(event.dataTransfer?.getData("text/plain") ?? "", "insertFromDrop");
    };
    const onBlur = () => finish("commit");
    // Before the browser changes the field: the selection the change begins
    // at, for the snapshot the `input` event records. A composition is left
    // alone HERE (its updates replace the text being composed; each would
    // read as beginning somewhere else) - where it began is read once, on
    // `compositionstart`, below. The browser's OWN undo / redo - reached from
    // the context menu or the Edit menu, since the keys are taken below - is
    // cancelled and walks the session's history instead: applied, it would
    // rewrite the field from a stack that never saw the editor's rebuilds,
    // and the `input` event it fires would record that as one more change.
    const onBeforeInput = (event: Event) => {
      const { inputType, isComposing } = event as InputEvent;
      if (inputType === "historyUndo" || inputType === "historyRedo") {
        event.preventDefault();
        walk(inputType === "historyUndo" ? "undo" : "redo");
        return;
      }
      pendingBefore = isComposing || inputType === "insertCompositionText" ? null : caretAt();
    };
    // Where a composition BEGAN: the selection as the input method
    // opened, handed to the first `insertCompositionText` snapshot - so an
    // undo of the composed word puts the caret where it was typed, not where
    // the field had opened or the previous change had ended - and cleared at
    // once, so the updates that follow coalesce into that step (they begin
    // where the last left the caret) rather than each break the burst.
    let compositionBefore: TextSelection | null = null;
    const onCompositionStart = () => {
      compositionBefore = caretAt();
    };
    const onCompositionEnd = () => {
      compositionBefore = null;
    };
    // Every change the browser makes to the field (typing, deleting, a paste,
    // a composition) is a snapshot, keyed on its kind so a burst of typing is
    // one step.
    const onInput = (event: Event) => {
      const inputType = (event as InputEvent).inputType || "input";
      let before = pendingBefore ?? undefined;
      pendingBefore = null;
      if (inputType === "insertCompositionText" && compositionBefore) {
        before = compositionBefore;
        compositionBefore = null;
      }
      record(inserting ?? inputType, before);
    };
    // A click inside the field must not bubble to the layer as a selection.
    const stop = (event: Event) => event.stopPropagation();

    element.addEventListener("keydown", onKeyDown);
    element.addEventListener("beforeinput", onBeforeInput);
    element.addEventListener("compositionstart", onCompositionStart);
    element.addEventListener("compositionend", onCompositionEnd);
    element.addEventListener("input", onInput);
    element.addEventListener("paste", onPaste);
    element.addEventListener("dragstart", onDragStart);
    element.addEventListener("dragend", onDragEnd);
    element.addEventListener("drop", onDrop);
    element.addEventListener("blur", onBlur);
    element.addEventListener("pointerdown", stop);
    element.addEventListener("click", stop);
    element.addEventListener("dblclick", stop);
    return () => {
      element.removeEventListener("keydown", onKeyDown);
      element.removeEventListener("beforeinput", onBeforeInput);
      element.removeEventListener("compositionstart", onCompositionStart);
      element.removeEventListener("compositionend", onCompositionEnd);
      element.removeEventListener("input", onInput);
      element.removeEventListener("paste", onPaste);
      element.removeEventListener("dragstart", onDragStart);
      element.removeEventListener("dragend", onDragEnd);
      element.removeEventListener("drop", onDrop);
      element.removeEventListener("blur", onBlur);
      element.removeEventListener("pointerdown", stop);
      element.removeEventListener("click", stop);
      element.removeEventListener("dblclick", stop);
      stopLiveTimer();
      retireInlineEditor(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const Element = Tag as "span";
  return (
    <Element
      ref={ref as React.Ref<HTMLSpanElement>}
      className={className}
      contentEditable
      suppressContentEditableWarning
      spellCheck
      role="textbox"
      aria-label={t_i18n("Edit text")}
      aria-multiline={!plain && !singleLine}
      data-edit-text={target}
      data-edit-editing="true"
    >
      {children}
    </Element>
  );
}
