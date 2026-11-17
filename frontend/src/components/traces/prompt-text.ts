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
 * Faithful rendering of prompt text pulled out of trace payloads.
 *
 * The trace inspector exists to show *exactly* what was sent to the model, so
 * the normalisation it applies on the way to `ChatMarkdown` must never silently
 * delete a character the model actually received. It used to, twice over, and
 * both were in the catch-all `<[^>]+>` strip that closed the HTML-to-Markdown
 * conversion:
 *
 * * it deleted every angle-bracket token whether or not it was HTML — the
 *   `wait` primitive's system-prompt block carried `until="<task_id>,<task_id>"`
 *   and the inspector rendered it as `until=","`, a correct prompt reading as a
 *   platform bug; and
 * * the rules feeding it were prefix matches (`<\/?p[^>]*>`), so `<placeholder>`
 *   was read as a `<p>` tag and replaced with a *newline* rather than merely
 *   stripped.
 *
 * Prompts are full of angle brackets — XML-style sections, placeholders,
 * generics, comparisons — so this is the common case, not an edge one. Every
 * rule below is therefore anchored on a known tag name followed by a delimiter.
 *
 * `ChatMarkdown` is deliberately NOT involved in the fix. It is shared with the
 * chat UI, and `react-markdown` (v10, configured here without `rehype-raw`)
 * turns out to render raw HTML as literal visible text rather than dropping it:
 * `<instructions>` survives it untouched. Escaping angle brackets on the way in
 * would therefore buy nothing and would break the `<https://example.com>`
 * autolink form, so this module hands the renderer plain text and stops there.
 *
 * These helpers live outside `span-inspector.tsx` because they are pure string
 * transforms with no React dependency, which is what makes them unit-testable
 * (see `prompt-text.test.ts`).
 */

/** Tag names the HTML-to-Markdown conversion recognises. */
const HTML_TAG_NAMES =
  "br|p|div|span|strong|b|em|i|u|s|code|pre|a|ul|ol|li|h[1-6]|" +
  "blockquote|table|thead|tbody|tfoot|tr|td|th|hr|img|figure|figcaption";

/**
 * A *known* HTML tag, and only that: the name must be followed by `>`, `/>` or
 * whitespace, so `<pattern>` is never mistaken for a `<p>` tag.
 */
const KNOWN_HTML_TAG = new RegExp(`</?(?:${HTML_TAG_NAMES})(?:\\s[^>]*)?/?>`, "gi");

/**
 * Undo the backslash escaping of a prompt embedded in a serialised payload.
 *
 * One pass, not a chain of `.replace()`. Collapsing `\\` last let an earlier
 * rule read the second backslash as the start of its own escape: the literal
 * path `C:\newfolder` arrives serialised as `C:\\newfolder`, and the `\n` rule
 * turned its `\` + `n` into a newline. Matching an escaped backslash together
 * with its partner is what stops the partner being re-read — the same class of
 * bug as the tag strip this module exists to fix, one function earlier.
 */
export function unescapeSerializedText(value: string): string {
  // The two-escape alternative spells out the surrogate ranges (high D800-DBFF,
  // low DC00-DFFF) rather than accepting any second `\uXXXX`, so only a genuine
  // pair is ever consumed as one token. A permissive partner swallowed an
  // escape the pair branch could not use and the fallback then dropped —
  // `\ud83d\u0041` lost its "A" — which is the deletion this module exists to
  // prevent. An invalid partner is now simply matched on the next iteration.
  const ESCAPE =
    /\\(r\\n|u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|[nrtbf"/\\])/g;
  return value.replace(ESCAPE, (_match, escape: string) => {
    // `\uXXXX` covers every non-ASCII character a serialised payload carries;
    // leaving it unhandled showed `\u00e9` as seven literal characters, which
    // is the same "shown wrong" failure this module is named after.
    //
    // A surrogate pair is matched as ONE escape and combined, rather than
    // decoded a code unit at a time: this pane renders bounded previews, so a
    // payload clipped between the halves leaves a lone `\ud83d`, and emitting
    // that as a bare code unit yields a string that is not well-formed UTF-16 —
    // `encodeURIComponent` throws on it and `JSON.stringify` (the Copy button's
    // path) re-emits it escaped. An unpaired surrogate stays literal instead.
    if (escape[0] === "u") {
      const units = Array.from(escape.matchAll(/u([0-9a-fA-F]{4})/g), (m) => parseInt(m[1], 16));
      const [high, low] = units;
      // Two units can only be a valid pair — the pattern admits nothing else.
      if (units.length === 2) {
        return String.fromCodePoint((high - 0xd800) * 0x400 + (low - 0xdc00) + 0x10000);
      }
      if (high >= 0xd800 && high <= 0xdfff) return `\\u${escape.slice(1, 5)}`;
      return String.fromCharCode(high);
    }
    switch (escape) {
      case "r\\n":
      case "n":
        return "\n";
      case "r":
        return "\r";
      case "t":
        return "\t";
      case "b":
        return "\b";
      case "f":
        return "\f";
      default:
        // `"`, `/` and `\` stand for themselves.
        return escape;
    }
  });
}

/**
 * One HTML attribute, however it was quoted.
 *
 * Reading only `name="…"` is the tag strip's bug in miniature: a
 * single-quoted or unquoted attribute simply missed, the value came back
 * empty, and the element it belonged to was dropped with everything in it.
 * Single quotes are routine in prompts precisely because these strings arrive
 * inside a JSON payload, where a double quote has to be escaped.
 */
function htmlAttr(tag: string, name: string): string {
  const match = new RegExp(`\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'>]+))`, "i").exec(tag);
  return match ? (match[1] ?? match[2] ?? match[3] ?? "") : "";
}

/**
 * Wrap *body* in a backtick span long enough to contain its own backticks.
 *
 * A fixed single backtick breaks the span at the first inner one, dropping the
 * rest of the content out of the code context.
 */
function codeSpan(body: string): string {
  const longest = Math.max(0, ...Array.from(body.matchAll(/`+/g), (m) => m[0].length));
  const fence = "`".repeat(longest + 1);
  const pad = body.startsWith("`") || body.endsWith("`") ? " " : "";
  return `${fence}${pad}${body}${pad}${fence}`;
}

/**
 * Convert one non-code stretch of a payload.
 *
 * Anything that is not a known tag is left exactly as it was found — including
 * the leftovers of a tag-shaped token this list does not cover, which
 * `react-markdown` will display verbatim.
 *
 * Container tags survive the strip harmlessly: their text content is what the
 * reader wanted and it stays behind. Void elements do not — `<hr>` and `<img>`
 * hold everything they mean in the tag itself, so they are converted before the
 * strip rather than erased by it.
 */
function convertHtmlSegment(value: string): string {
  return value
    .replace(/<br(?:\s[^>]*)?\/?>/gi, "\n")
    .replace(/<\/?p(?:\s[^>]*)?>/gi, "\n")
    .replace(/<strong(?:\s[^>]*)?>([\s\S]*?)<\/strong>/gi, "**$1**")
    .replace(/<b(?:\s[^>]*)?>([\s\S]*?)<\/b>/gi, "**$1**")
    .replace(/<em(?:\s[^>]*)?>([\s\S]*?)<\/em>/gi, "*$1*")
    .replace(/<i(?:\s[^>]*)?>([\s\S]*?)<\/i>/gi, "*$1*")
    .replace(/<code(?:\s[^>]*)?>([\s\S]*?)<\/code>/gi, (_tag, body: string) => codeSpan(body))
    // The attributes are captured separately because a `replace` callback is
    // handed the WHOLE match: passing that to `htmlAttr` let it read `href` out
    // of the link *body*, so prose merely mentioning `href=` acquired a
    // destination — and `[text](url)` reaches `ChatMarkdown` as a live link,
    // the same confused-deputy shape as the `<img>` rule below, one click away.
    // The old `[^>]*href="…"` form confined the search to the tag by accident
    // of `[^>]`; this does it by construction.
    .replace(/<a(\s[^>]*)?>([\s\S]*?)<\/a>/gi, (_match, attrs: string | undefined, body: string) => {
      const href = htmlAttr(attrs ?? "", "href");
      return href ? `[${body}](${href})` : body;
    })
    // Void elements carry their whole payload in their attributes, so the
    // generic strip below would erase them outright rather than merely drop
    // their markup — the one way this conversion can still lose content.
    //
    // The form is deliberately INERT. `![alt](src)` reaches `ChatMarkdown`,
    // whose `img` renders a live element: an off-origin `src` planted in a
    // prompt would fire from the engineer's browser on opening the trace, and
    // an `/api/v1/chat/files/…` one routes through `AuthenticatedImage`, which
    // replays the viewer's own JWT at an attacker-chosen path. Prompt bodies
    // carry end-user text, so that is stored untrusted content reaching a
    // privileged viewer. A textual form keeps every character — and shows the
    // `src` a rendered image would hide — without issuing any request.
    .replace(/<img(?:\s[^>]*)?\/?>/gi, (tag) => {
      const src = htmlAttr(tag, "src");
      const alt = htmlAttr(tag, "alt");
      if (src && alt) return `[image: ${alt} — ${src}]`;
      if (src) return `[image: ${src}]`;
      return alt ? `[image: ${alt}]` : "[image]";
    })
    .replace(/<hr(?:\s[^>]*)?\/?>/gi, "\n\n---\n\n")
    .replace(KNOWN_HTML_TAG, "")
    .replace(/\n{3,}/g, "\n\n");
}

/**
 * Fenced blocks and inline spans, which are shown verbatim rather than
 * converted. The capture group makes `split` interleave them into the result,
 * so the odd indices are exactly the code.
 *
 * An unterminated fence runs to end of input. This pane renders *bounded*
 * previews by design — `PayloadNotice` exists to say so — and a payload clipped
 * mid-block leaves exactly one dangling fence. Requiring a closer would treat
 * that whole tail as prose and convert the HTML inside it, reopening the bug on
 * precisely the inputs the pane is built to show.
 */
const CODE_SPAN =
  /(```[\s\S]*?```|```[\s\S]*$|~~~[\s\S]*?~~~|~~~[\s\S]*$|`[^`\n]*`)/g;

/**
 * Convert the handful of HTML tags a payload may carry into Markdown, leaving
 * code alone.
 *
 * A prompt that fences HTML is *deliberately* showing markup, so it is where
 * fidelity is most expected and where a conversion is most obviously wrong:
 * without the split, a ```html block demonstrating `<p>hello</p>` rendered as
 * `hello` and the sample the prompt was teaching from was gone. Skipping code
 * also keeps the blank-line collapse from reflowing a fenced block.
 */
export function htmlToMarkdownLite(value: string): string {
  return value
    .split(CODE_SPAN)
    .map((part, index) => (index % 2 === 1 ? part : convertHtmlSegment(part)))
    .join("");
}

/**
 * Prepare one prompt or message body for `ChatMarkdown`.
 *
 * `unescape` says which side of a JSON decode the text is on. Text scraped out
 * of a serialised payload still carries its backslash escapes and must be
 * unescaped; text `JSON.parse` already produced carries the backslashes the
 * model actually received, and unescaping it again turns `C:\newfolder` into a
 * line break. The default is the raw side, so a caller that forgets the flag
 * fails in the direction that deletes nothing.
 */
export function normalizeRichText(
  value: string,
  { unescape = true }: { unescape?: boolean } = {},
): string {
  return htmlToMarkdownLite(unescape ? unescapeSerializedText(value) : value).trim();
}

export interface PromptRoleBlock {
  role: string;
  content: string;
}

/** Parse a payload string that is a whole JSON object or array; anything else comes back as it was. */
export function maybeParseJsonString(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) return value;
  const looksJson =
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"));
  if (!looksJson) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

/** The raw, still-escaped body of a `"system_prompt": "..."` field in serialised text. */
function extractSystemPromptFromSerializedText(value: string): string | null {
  const marker = '"system_prompt":';
  const start = value.indexOf(marker);
  if (start === -1) return null;

  const afterMarker = value.slice(start + marker.length);
  const messagesMarker = ', "messages":';
  const messagesMarkerNoSpace = ',"messages":';
  const end = afterMarker.indexOf(messagesMarker);
  const endNoSpace = afterMarker.indexOf(messagesMarkerNoSpace);

  const firstEnd =
    end >= 0 && endNoSpace >= 0
      ? Math.min(end, endNoSpace)
      : end >= 0
        ? end
        : endNoSpace;

  const rawChunk = firstEnd >= 0 ? afterMarker.slice(0, firstEnd) : afterMarker;
  const trimmed = rawChunk.trim();
  if (!trimmed) return null;

  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }

  return trimmed;
}

/**
 * Role blocks scraped by regex out of serialised text that did not parse. Every
 * capture is the body of a JSON string, escapes included, so it is unescaped.
 */
export function extractRoleBlocksFromSerializedText(value: string): PromptRoleBlock[] {
  const blocks: PromptRoleBlock[] = [];

  const systemMatch = value.match(/"system_prompt"\s*:\s*"((?:\\.|[^"\\])*)"/);
  if (systemMatch?.[1]) {
    const text = normalizeRichText(systemMatch[1]);
    if (text) {
      blocks.push({ role: "system", content: text });
    }
  }

  const messageRegex = /"role"\s*:\s*"([^"\\]+)"[\s\S]*?"content"\s*:\s*"((?:\\.|[^"\\])*)"/g;
  let match: RegExpExecArray | null;
  while ((match = messageRegex.exec(value)) !== null) {
    const role = (match[1] ?? "").trim().toLowerCase();
    const text = normalizeRichText(match[2] ?? "");
    if (!role || !text) continue;
    blocks.push({ role, content: text });
  }

  return blocks;
}

/**
 * Prepare a payload string for `ChatMarkdown`.
 *
 * `decoded` says where the string came from, never what it looks like: a
 * model's answer can open like JSON and be cut by the display preview, so no
 * guess from its content is safe. Decoded text is rendered as received. Only a
 * known serialisation (an offload preview, `decoded: false`) has its
 * `system_prompt` body scraped out and its escapes undone.
 */
export function normalizeMarkdownLikeString(
  value: string,
  { decoded }: { decoded: boolean },
): string {
  if (decoded) return normalizeRichText(value, { unescape: false });
  return normalizeRichText(extractSystemPromptFromSerializedText(value) ?? value);
}
