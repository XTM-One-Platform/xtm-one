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
 * Inline markup renderer for deck text. The grammar and tokenizer live
 * in `markup.ts`; this turns the segments into React nodes.
 *
 * In-place editing: a call site that names its FIELD (`field="content"`,
 * relative to the enclosing block; `field="title"` at slide level) publishes
 * the text's spec path as `data-edit-text` while the deck is in edit mode, and
 * swaps itself for `InlineTextEditor` while that text is the one under edit.
 * Outside edit mode `field` changes nothing - not an attribute, not a wrapper.
 */
import type { MouseEvent, ReactNode } from "react";
import { Fragment, lazy, Suspense } from "react";

import { useFormatter } from "@/i18n";

import { splitAccentLine } from "./accent-line";
import { useEditMode, useEditTextPath, useSlideIndex } from "./edit-context";
import { isLinkUrl, type MarkupSegment, parseInlineMarkup } from "./markup";
import { useSlideLinks } from "./slide-link-context";

// The editable field (and the runs model behind it) is loaded the first time
// a text is opened in place - never by a chat card, the speaker view or the
// export page, which render text and only text. Until it arrives the
// text stays drawn as it was (the Suspense fallback is the static rendering).
const InlineTextEditor = lazy(() => import("./InlineTextEditor").then((m) => ({ default: m.InlineTextEditor })));

/**
 * Render inline markup to React nodes.
 *
 * Wraps nothing by default (so it can sit inside an `<h1>` or a `<td>`); pass
 * `as` to get an element around it. Segments are keyed by position — the list
 * is derived from a single string, so a change re-renders the whole run.
 *
 * With `field`, in edit mode, a bare run is wrapped in a `<span>` carrying the
 * target (an attribute needs an element) - inline, so the parent's layout is
 * unchanged, and the same wrapper the editor uses.
 */
export function InlineMarkup({
  value,
  as: Tag,
  className,
  field,
  singleLine = false,
  accentLastLine = false,
}: {
  value: string | undefined | null;
  as?: keyof React.JSX.IntrinsicElements;
  className?: string;
  /** The spec field this text renders, for in-place editing. */
  field?: string;
  /** Drawn on ONE row (a profile fact): the editor commits on Enter instead of breaking the line. */
  singleLine?: boolean;
  /**
   * Draw the LAST LINE (after the last line break; the last word of a
   * one-line text) in the accent tone without markup in the value - the
   * title slide's `hero_accent_word`. A rendering choice only: the
   * value is untouched, so the in-place editor still opens the plain title.
   */
  accentLastLine?: boolean;
}) {
  const session = useEditMode();
  const target = useEditTextPath(field);
  const slide = useSlideIndex();
  const nodes = value ? renderSegments(parseInlineMarkup(value), accentLastLine) : null;
  // An emptied field keeps its target in edit mode (an element to click back
  // into - a required field cannot be saved empty); it draws nothing otherwise.
  const Empty = (Tag ?? "span") as "span";
  const drawn = !nodes ? (
    target ? (
      <Empty className={className} data-edit-text={target} data-edit-empty="true" />
    ) : null
  ) : !Tag ? (
    target ? (
      <span data-edit-text={target}>{nodes}</span>
    ) : (
      <>{nodes}</>
    )
  ) : (
    <Tag className={className} data-edit-text={target ?? undefined}>
      {nodes}
    </Tag>
  );
  if (
    session &&
    target &&
    session.editingText &&
    session.editingText.slide === slide &&
    session.editingText.path === target
  ) {
    return (
      <Suspense fallback={drawn}>
        <InlineTextEditor
          key={target}
          as={Tag ?? "span"}
          className={className}
          target={target}
          initialValue={value ?? ""}
          singleLine={singleLine}
          caret={session.editingText.caret}
          onChange={(next) => session.onLiveText(slide, target, next)}
          onCommit={(next) => session.onCommitText(slide, target, next)}
          onCancel={session.onCancelText}
        />
      </Suspense>
    );
  }
  return drawn;
}

/**
 * A deck string that is NOT markup (a metric value, a price, a timeline
 * marker): rendered verbatim, editable in place as plain text.
 *
 * Like `InlineMarkup`, wraps nothing by default: a call site that had a bare
 * text node keeps it outside edit mode (the export extractor reads a text
 * node and a `<span>` through different paths), and gets the inline `<span>`
 * carrying the target only while the deck is being edited. Pass `as` for an
 * element that was always there.
 */
export function PlainText({
  value,
  as: Tag,
  className,
  field,
}: {
  value: string | number | undefined | null;
  as?: keyof React.JSX.IntrinsicElements;
  className?: string;
  field?: string;
}) {
  const session = useEditMode();
  const target = useEditTextPath(field);
  const slide = useSlideIndex();
  const Element = (Tag ?? "span") as "span";
  const drawn =
    value === undefined || value === null || value === "" ? (
      // An emptied field keeps its target in edit mode; nothing otherwise.
      target ? (
        <Element className={className} data-edit-text={target} data-edit-plain="true" data-edit-empty="true" />
      ) : null
    ) : !Tag && !target ? (
      <>{value}</>
    ) : (
      <Element
        className={className}
        data-edit-text={target ?? undefined}
        data-edit-plain={target ? "true" : undefined}
      >
        {value}
      </Element>
    );
  if (
    session &&
    target &&
    session.editingText &&
    session.editingText.slide === slide &&
    session.editingText.path === target
  ) {
    return (
      <Suspense fallback={drawn}>
        <InlineTextEditor
          key={target}
          as={Tag ?? "span"}
          className={className}
          target={target}
          initialValue={value === undefined || value === null ? "" : String(value)}
          plain
          caret={session.editingText.caret}
          onChange={(next) => session.onLiveText(slide, target, next)}
          onCommit={(next) => session.onCommitText(slide, target, next)}
          onCancel={session.onCancelText}
        />
      </Suspense>
    );
  }
  return drawn;
}

/**
 * The segments as nodes; with `accentLastLine`, the last line - or the last
 * WORD of a one-line text, found across the runs and cut out of the run that
 * holds it (`splitAccentLine`, `accent-line.ts`) - wrapped in
 * `.deck-hero-accent`, a class the stylesheet colours with the accent token.
 * Not a tone span (`.deck-span`): the accent is a hero's rendering, not markup
 * the runs model must read back.
 */
function renderSegments(segments: MarkupSegment[], accentLastLine: boolean): ReactNode[] {
  if (!accentLastLine || segments.length === 0) {
    return segments.map((segment, index) => renderSegment(segment, index));
  }
  const { head, accent, trailing } = splitAccentLine(segments);
  if (accent.length === 0) return segments.map((segment, index) => renderSegment(segment, index));
  // Keys stay positional per list: a split run yields one node in `head` and
  // one inside the accent span, each keyed in its own list.
  const offset = head.length + accent.length;
  return [
    ...head.map((segment, index) => renderSegment(segment, index)),
    <span key="accent" className="deck-hero-accent">
      {accent.map((segment, index) => renderSegment(segment, index))}
    </span>,
    ...trailing.map((segment, index) => renderSegment(segment, offset + index)),
  ];
}

function renderSegment(segment: MarkupSegment, key: number): ReactNode {
  switch (segment.kind) {
    case "bold":
      return <strong key={key}>{segment.text}</strong>;
    case "em":
      return <em key={key}>{segment.text}</em>;
    case "code":
      return <code key={key}>{segment.text}</code>;
    case "span":
      return (
        <span key={key} className="deck-span" data-tone={segment.tone}>
          {segment.text}
        </span>
      );
    case "link":
      return <SlideLink key={key} text={segment.text} target={segment.target} />;
    case "br":
      return <br key={key} />;
    default:
      return <Fragment key={key}>{segment.text}</Fragment>;
  }
}

/**
 * `[text](#/<slide id>)`: a link to another slide of the deck, resolved by
 * the deck host (`slide-link-context.ts`). `[text](https://...)` /
 * `mailto:`: a link out, opened in a new tab with no opener and no referrer
 * (the public link's token never rides it), measured by the PPTX exporter
 * through `data-link-href` and kept by the PDF as a URI link.
 *
 * - A target that is no slide of the deck draws the label as plain text.
 * - With no host (a thumbnail) the link is drawn, inert.
 * - On a host that moves the deck, a click moves it - and is kept from the
 *   address bar, which belongs to the page around the deck (the chat).
 * - While the deck is EDITED a click selects the text like any other, so the
 *   link never navigates; the runs model reads the element back by
 *   `data-slide-link` (`inline-runs-dom.ts`).
 * - On the export page it is a native anchor to the slide's canvas
 *   (`#deck-slide-<n>`), which the PDF keeps as an internal link, and
 *   `data-slide-link-index` is what the PPTX exporter measures.
 */
function SlideLink({ text, target }: { text: string; target: string }) {
  const links = useSlideLinks();
  const editing = !!useEditMode();
  const { t_i18n } = useFormatter();
  const url = isLinkUrl(target);
  if (!links) {
    return (
      <span className="deck-slide-link" data-slide-link={url ? undefined : target} data-link-href={url ? target : undefined}>
        {text}
      </span>
    );
  }
  if (url) {
    return (
      <a
        className="deck-slide-link"
        href={target}
        target="_blank"
        rel="noopener noreferrer"
        data-link-href={target}
        title={target}
        // While the deck is edited a click places the caret, never leaves.
        onClick={editing ? (event) => event.preventDefault() : undefined}
      >
        {text}
      </a>
    );
  }
  const index = links.indexOf(target);
  if (index === null) return <>{text}</>;
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (editing) {
      event.preventDefault();
      return;
    }
    if (!links.follow) return;
    event.preventDefault();
    links.follow(index);
  };
  return (
    <a
      className="deck-slide-link"
      href={links.href?.(index) ?? `#/${target}`}
      data-slide-link={target}
      data-slide-link-index={index}
      title={t_i18n("Go to slide {number}", { number: index + 1 })}
      onClick={onClick}
    >
      {text}
    </a>
  );
}
