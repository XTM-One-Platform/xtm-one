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
 * The DOM half of the runs model: runs to nodes, nodes to runs, and
 * the selection as plain-text offsets - what `InlineTextEditor` needs to edit
 * inline markup WYSIWYG inside a `contentEditable` element.
 *
 * Nodes are BUILT (`createElement`, `createTextNode`), never parsed from a
 * string: this module has no HTML sink, like every other renderer of deck
 * text (`inline-markup.tsx`), and the tripwire in
 * `tests/test_presentation_spec_v2.py` keeps it that way. The elements mirror
 * `renderSegment` one to one - `<strong>`, `<em>`, `<code>`,
 * `<span class="deck-span" data-tone>`, `<a class="deck-slide-link"
 * data-slide-link>` (no `href`: under edit a link goes nowhere), `<br>` - so
 * the text under edit is styled by the same stylesheet rules as the text at
 * rest.
 */
import { isLinkUrl, MARKUP_SPAN_KINDS, type MarkupSpanKind } from "./markup";
import { type TextRun } from "./inline-runs";

const SPAN_KIND_SET: ReadonlySet<string> = new Set(MARKUP_SPAN_KINDS);

/** DOM nodes for `runs` - what goes into the editable element. */
export function runsToNodes(runs: readonly TextRun[], doc: Document = document): Node[] {
  const nodes: Node[] = [];
  for (const run of runs) {
    if (run.style === "plain") {
      const lines = run.text.split("\n");
      lines.forEach((line, index) => {
        if (index > 0) nodes.push(doc.createElement("br"));
        if (line) nodes.push(doc.createTextNode(line));
      });
      continue;
    }
    let element: HTMLElement;
    switch (run.style) {
      case "bold":
        element = doc.createElement("strong");
        break;
      case "em":
        element = doc.createElement("em");
        break;
      case "code":
        element = doc.createElement("code");
        break;
      case "link":
        element = doc.createElement("a");
        element.className = "deck-slide-link";
        if (run.target && isLinkUrl(run.target)) element.dataset.linkHref = run.target;
        else if (run.target) element.dataset.slideLink = run.target;
        break;
      default:
        element = doc.createElement("span");
        element.className = "deck-span";
        if (run.tone) element.dataset.tone = run.tone;
    }
    element.textContent = run.text;
    nodes.push(element);
  }
  return nodes;
}

/** The style an element imposes on the text inside it, if any. */
function styleOf(element: Element): Pick<TextRun, "style" | "tone" | "target"> | null {
  const tag = element.tagName;
  if (tag === "STRONG" || tag === "B") return { style: "bold" };
  if (tag === "EM" || tag === "I") return { style: "em" };
  if (tag === "CODE") return { style: "code" };
  // Only a link the renderer drew: a pasted `<a href>` contributes its text.
  const slideLink = (element as HTMLElement).dataset?.slideLink;
  if ((tag === "A" || tag === "SPAN") && slideLink) return { style: "link", target: slideLink };
  const linkHref = (element as HTMLElement).dataset?.linkHref;
  if ((tag === "A" || tag === "SPAN") && linkHref && isLinkUrl(linkHref)) return { style: "link", target: linkHref };
  const tone = (element as HTMLElement).dataset?.tone;
  if (tag === "SPAN" && tone && SPAN_KIND_SET.has(tone)) return { style: "span", tone: tone as MarkupSpanKind };
  return null;
}

const BLOCK_TAGS = new Set(["DIV", "P", "LI"]);

/**
 * The runs the editable element holds after the browser (and the user) had
 * their way with it. Text nodes take the style of their INNERMOST styling
 * ancestor below `root`; a `<br>` is a line break; a block element the
 * browser inserted (a pasted paragraph, a `<div>` from an unguarded Enter)
 * starts a new line. Anything else - a `<font>`, a styled `<span>` from a
 * paste - contributes its text and nothing else.
 */
export function domToRuns(root: Element): TextRun[] {
  const runs: TextRun[] = [];
  const push = (text: string, style: Pick<TextRun, "style" | "tone" | "target"> | null) => {
    if (!text) return;
    runs.push(style ? { text, ...style } : { text, style: "plain" });
  };
  const walk = (node: Node, inherited: Pick<TextRun, "style" | "tone" | "target"> | null, first: boolean) => {
    if (node.nodeType === Node.TEXT_NODE) {
      // The editable is `white-space: pre-wrap`-agnostic: a newline typed into
      // a text node (some browsers do on Shift+Enter) is a line break too.
      push((node.textContent ?? "").replace(/\u00a0/g, " "), inherited);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const element = node as Element;
    if (element.tagName === "BR") {
      push("\n", null);
      return;
    }
    if (BLOCK_TAGS.has(element.tagName) && !first) push("\n", null);
    const style = styleOf(element) ?? inherited;
    let firstChild = true;
    for (const child of Array.from(element.childNodes)) {
      walk(child, style, firstChild);
      firstChild = false;
    }
  };
  let first = true;
  for (const child of Array.from(root.childNodes)) {
    walk(child, null, first);
    first = false;
  }
  return runs;
}

/** Plain-text length of a node the way `domToRuns` counts it. */
function textLength(node: Node): number {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent ?? "").length;
  if (node.nodeType !== Node.ELEMENT_NODE) return 0;
  const element = node as Element;
  if (element.tagName === "BR") return 1;
  let total = 0;
  for (const child of Array.from(element.childNodes)) total += textLength(child);
  return total;
}

/** The plain-text offset of a DOM position (`node`, `offset`) inside `root`. */
function offsetOf(root: Element, node: Node, offset: number): number {
  // Position inside a text node: characters before it in that node.
  let total = 0;
  const target =
    node.nodeType === Node.TEXT_NODE
      ? { node, before: offset }
      : // An element position: `offset` child nodes precede it.
        { node, before: -1 };
  const walk = (current: Node): boolean => {
    if (current === target.node) {
      if (target.before >= 0) {
        total += Math.min(target.before, (current.textContent ?? "").length);
      } else {
        const children = Array.from(current.childNodes).slice(0, offset);
        for (const child of children) total += textLength(child);
      }
      return true;
    }
    if (current.nodeType === Node.TEXT_NODE) {
      total += (current.textContent ?? "").length;
      return false;
    }
    if (current.nodeType === Node.ELEMENT_NODE && (current as Element).tagName === "BR") {
      total += 1;
      return false;
    }
    for (const child of Array.from(current.childNodes)) {
      if (walk(child)) return true;
    }
    return false;
  };
  if (!root.contains(node)) return textLength(root);
  walk(root);
  return total;
}

/** The current selection as `[start, end]` plain-text offsets inside `root`, or `null` when it lies elsewhere. */
export function selectionOffsets(root: Element): { start: number; end: number } | null {
  const selection = root.ownerDocument.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const a = offsetOf(root, range.startContainer, range.startOffset);
  const b = offsetOf(root, range.endContainer, range.endOffset);
  return { start: Math.min(a, b), end: Math.max(a, b) };
}

/** The DOM position `offset` plain-text characters into `root`. */
function positionAt(root: Element, offset: number): { node: Node; offset: number } {
  let remaining = Math.max(0, offset);
  let last: { node: Node; offset: number } = { node: root, offset: root.childNodes.length };
  const walk = (node: Node): { node: Node; offset: number } | null => {
    if (node.nodeType === Node.TEXT_NODE) {
      const length = (node.textContent ?? "").length;
      if (remaining <= length) return { node, offset: remaining };
      remaining -= length;
      last = { node, offset: length };
      return null;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const element = node as Element;
    if (element.tagName === "BR") {
      if (remaining === 0) return { node: element.parentNode as Node, offset: indexIn(element) };
      remaining -= 1;
      last = { node: element.parentNode as Node, offset: indexIn(element) + 1 };
      return null;
    }
    for (const child of Array.from(element.childNodes)) {
      const found = walk(child);
      if (found) return found;
    }
    return null;
  };
  return walk(root) ?? last;
}

function indexIn(element: Element): number {
  return Array.from(element.parentNode?.childNodes ?? []).indexOf(element);
}

/** Select `[start, end]` (a caret when equal) inside `root`. */
export function setSelectionOffsets(root: Element, start: number, end: number = start): void {
  const doc = root.ownerDocument;
  const selection = doc.getSelection();
  if (!selection) return;
  const from = positionAt(root, start);
  const to = positionAt(root, end);
  const range = doc.createRange();
  range.setStart(from.node, from.offset);
  range.setEnd(to.node, to.offset);
  selection.removeAllRanges();
  selection.addRange(range);
}

/**
 * The plain-text offset under a point (a double-click), so the editor opens
 * with the caret where the pointer was instead of at the end.
 */
export function caretOffsetAtPoint(root: Element, x: number, y: number): number | null {
  const doc = root.ownerDocument as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
  };
  let node: Node | null = null;
  let offset = 0;
  if (typeof doc.caretPositionFromPoint === "function") {
    const position = doc.caretPositionFromPoint(x, y);
    if (position) {
      node = position.offsetNode;
      offset = position.offset;
    }
  } else if (typeof doc.caretRangeFromPoint === "function") {
    const range = doc.caretRangeFromPoint(x, y);
    if (range) {
      node = range.startContainer;
      offset = range.startOffset;
    }
  }
  if (!node || !root.contains(node)) return null;
  return offsetOf(root, node, offset);
}

const WORD_RE = /[\p{L}\p{N}_'-]/u;

/** The word around `offset` in `text` - what a collapsed caret formats. */
export function wordRangeAt(text: string, offset: number): { start: number; end: number } {
  let start = Math.min(Math.max(offset, 0), text.length);
  let end = start;
  while (start > 0 && WORD_RE.test(text[start - 1])) start -= 1;
  while (end < text.length && WORD_RE.test(text[end])) end += 1;
  return { start, end };
}
