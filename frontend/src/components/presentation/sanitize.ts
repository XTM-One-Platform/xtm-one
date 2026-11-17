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
 * DOMPurify strict profile for `rich_text` blocks.
 *
 * `rich_text` is the ONLY place an agent can emit markup inside a generated
 * artifact, so this is the enforcing boundary: everything that reaches
 * `dangerouslySetInnerHTML` in a report block goes through `sanitizeRichText`
 * first. Decks no longer carry the field at all: a slide's text is
 * flat inline markup rendered through a tokenizer, and a stored v1 slide is
 * upgraded to typed blocks when it is read, so no slide component renders HTML.
 *
 * Defence in depth, not the only layer — the backend's
 * `app/services/rich_text_guard.py` already *rejects* a fragment carrying
 * scripts, iframes, forms or `on*` handlers at generation time, so the agent
 * gets a clear error instead of silently losing content. This layer assumes that
 * check could be bypassed (an older spec in the database, a hand-crafted row)
 * and still guarantees nothing executable renders.
 *
 * The allow-list is deliberately paragraph-level: block elements that lay out a
 * page (`div`, `section`, `style`), anything that loads a resource (`img`,
 * `iframe`, `object`, `link`) and anything interactive (`form`, `input`,
 * `button`) are all absent. Images go through controlled file-id fields
 * instead, which resolve to an authenticated platform download — so a
 * rendered artifact can never trigger an outbound request.
 */
import DOMPurify from "dompurify";

/** Inline + list + table markup only — no layout, no resource loading. */
const ALLOWED_TAGS = [
  "p",
  "br",
  "span",
  "strong",
  "b",
  "em",
  "i",
  "u",
  "s",
  "small",
  "mark",
  "code",
  "pre",
  "kbd",
  "blockquote",
  "ul",
  "ol",
  "li",
  "dl",
  "dt",
  "dd",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "a",
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "th",
  "td",
  "caption",
];

/** No `style` (CSS can reposition content over the whole viewport), no
 *  `id` (would collide with reveal.js's own DOM), no data attributes. */
const ALLOWED_ATTR = ["href", "title", "colspan", "rowspan", "lang", "dir"];

/** http(s) and mailto only: `javascript:` and `data:` URLs are the two classic
 *  ways a link executes or smuggles a document. */
const ALLOWED_URI_REGEXP = /^(?:https?:|mailto:)/i;

let hookInstalled = false;

/** Force every surviving link to open safely in a new tab.
 *
 *  Installed once, lazily: a deck renders inside the app shell, so a link that
 *  navigated the current tab (or kept a `window.opener` handle) would take the
 *  user out of their conversation. `rel` is set on the sanitized output rather
 *  than allow-listed as an attribute, so an agent cannot opt out of it. */
function installLinkHook() {
  if (hookInstalled) return;
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A" && node.getAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  hookInstalled = true;
}

/**
 * Sanitize one `rich_text` fragment.
 *
 * Returns a string safe to pass to `dangerouslySetInnerHTML`. An empty or
 * missing fragment returns `""` so callers can render nothing without a
 * null-check dance.
 */
export function sanitizeRichText(html: string | undefined | null): string {
  if (!html) return "";
  installLinkHook();
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOWED_URI_REGEXP,
    // Belt-and-braces: these are already excluded by the allow-list, but naming
    // them documents the threat model the profile was written against and keeps
    // the intent obvious if the allow-list is ever extended.
    FORBID_TAGS: [
      "script",
      "iframe",
      "object",
      "embed",
      "form",
      "input",
      "button",
      "textarea",
      "select",
      "style",
      "link",
      "meta",
      "base",
      "svg",
      "math",
    ],
    FORBID_ATTR: ["srcdoc", "style", "onerror", "onclick", "onload"],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    // Keep the fragment a fragment: no <html>/<body> wrapper is introduced.
    RETURN_DOM: false,
    RETURN_DOM_FRAGMENT: false,
  });
}
