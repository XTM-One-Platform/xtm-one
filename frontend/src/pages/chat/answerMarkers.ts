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
 * The markers an agent embeds in an answer (`[[KIND:args]]`), read exactly as
 * the chat panel reads them (`@filigran/chatbot/markdown`), for what shows an
 * answer outside the panel: a run's result (`InlineFileContent`), its previews
 * (run cards, notifications, the flow map), and the investigation report a
 * stored reply holds (`XtmChatPanel`).
 *
 * Imports only the package's React-free entry, never its root: an eagerly
 * loaded page may use it without pulling the panel into its chunk.
 */
import {
  inlineContainerMarkers,
  markerKinds,
  splitAnswerMarkers,
  stripAnswerMarkers,
  type AnswerPart,
} from "@filigran/chatbot/markdown";
import { showSecretMarkers } from "@/pages/chat/secretMarkers";

/**
 * XTM One's own marker kinds, beside the panel's built-in `FILE`,
 * `PRESENTATION`, `REPORT` and `VIDEO`: the cards `markers.tsx` draws (a
 * streamed `CHART` marker draws nothing until the stored answer replaces it
 * with its block).
 */
export const XTM_HOST_MARKER_KINDS = markerKinds(["CHART", "INVESTIGATION", "WORK_ITEM", "DASHBOARD"]);

/** The ids every marker names: the backend writes canonical UUIDs. */
const MARKER_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The id a marker names, lowercase as the backend writes it; `null` for
 * anything else an agent wrote there.
 */
export function markerId(args: string): string | null {
  return MARKER_ID.test(args) ? args.toLowerCase() : null;
}

/** *content* cut into prose and markers, in source order, with XTM One's own kinds. */
export function answerParts(content: string): AnswerPart[] {
  return splitAnswerMarkers(content, false, XTM_HOST_MARKER_KINDS);
}

/**
 * *content* with every marker that sits in a table row, a list item or a quote
 * kept in place as a `#inline-marker/<KIND>/<args>` link (`ChatMarkdown` draws
 * it through `InlineMarkerContext`), as the panel renders it (#11697): split on
 * its markers, such a table would lose its later rows. A marker on its own
 * line stays a marker, with its card.
 */
export function answerWithInlineMarkers(content: string): string {
  return inlineContainerMarkers(content, XTM_HOST_MARKER_KINDS);
}

/**
 * *content* as a person reads it: without its markers (each one has a card of
 * its own, or none), and a stored secret shown as its display text
 * (`secretMarkers.ts`, #5136).
 */
export function answerProse(content: string): string {
  return showSecretMarkers(stripAnswerMarkers(content, XTM_HOST_MARKER_KINDS));
}

/** One thing a marker names, and the slide a deck's marker points at. */
export interface MarkerRef {
  /** Canonical lowercase id. */
  id: string;
  /** The slide a deck's marker wrote (`#/<slide id>`), `null` for any other. */
  slideRef: string | null;
}

/**
 * What the markers of *kind* in *parts* name, in order and once each: an id
 * named twice keeps its first place, and a deck its first slide, so a plain
 * marker never hides a focused one. A marker that names no id is skipped.
 */
export function markerRefs(parts: readonly AnswerPart[], kind: string): MarkerRef[] {
  const ordered: MarkerRef[] = [];
  const byId = new Map<string, MarkerRef>();
  for (const part of parts) {
    let args: string;
    let slideRef: string | null = null;
    if (part.type === "file" && kind === "FILE") args = part.fileId;
    else if (part.type === "artifact" && part.kind.toUpperCase() === kind) {
      args = part.id;
      slideRef = part.slideRef ?? null;
    } else if (part.type === "marker" && part.kind === kind) args = part.args;
    else continue;
    const id = markerId(args);
    if (!id) continue;
    const known = byId.get(id);
    if (!known) {
      const ref = { id, slideRef };
      byId.set(id, ref);
      ordered.push(ref);
    } else if (known.slideRef === null) {
      known.slideRef = slideRef;
    }
  }
  return ordered;
}

/**
 * A marker-free, single-line preview of a run's result, cut at *maxChars*:
 * every place a `result_summary` shows as a snippet (run cards, sub-run rows,
 * notifications, the flow map), so no marker ever leaks into one.
 */
export function summaryPreview(text: string, maxChars: number): string {
  const clean = answerProse(text).replace(/\s+/g, " ").trim();
  return clean.length > maxChars ? `${clean.slice(0, maxChars)}…` : clean;
}
