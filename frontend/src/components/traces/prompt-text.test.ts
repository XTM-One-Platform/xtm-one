/**
 * Unit tests for the trace inspector's prompt normalisation.
 *
 * Run with `yarn test:unit` — Node's built-in runner, stripping the types
 * natively, so this needs no test framework in `package.json`.
 *
 * The property under test is fidelity: the trace inspector's whole job is to
 * show what was actually sent to the model, so nothing here may delete or alter
 * a character the model received unless it is real HTML being folded into
 * Markdown.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  extractRoleBlocksFromSerializedText,
  htmlToMarkdownLite,
  normalizeMarkdownLikeString,
  normalizeRichText,
  unescapeSerializedText,
} from "./prompt-text.ts";

test("keeps angle-bracket placeholders instead of eating them", () => {
  // The reported bug: the `wait` primitive's prompt block rendered as
  // `until=","`, which made a correct prompt look like a platform bug.
  const prompt = 'wait(duration="5m", until="<task_id>,<task_id>")';
  assert.equal(normalizeRichText(prompt), prompt);
});

test("does not read a token starting with p as a <p> tag", () => {
  // The old `<\/?p[^>]*>` rule matched any token starting with `p`, so a
  // placeholder was replaced with a newline rather than merely left alone.
  assert.equal(normalizeRichText("a <placeholder> b"), "a <placeholder> b");
  assert.equal(normalizeRichText("<pattern> and <price>"), "<pattern> and <price>");
});

test("keeps XML-style prompt sections", () => {
  const prompt = "<instructions>do the thing</instructions>";
  assert.equal(normalizeRichText(prompt), prompt);
});

test("keeps comparison operators and generics", () => {
  assert.equal(normalizeRichText("retry while attempts < 3"), "retry while attempts < 3");
  assert.equal(normalizeRichText("returns Awaitable<tuple<bool>>"), "returns Awaitable<tuple<bool>>");
});

test("keeps a tag-shaped token whose name merely starts like a known one", () => {
  // `b` → `<brand>`, `i` → `<item>`, `a` → `<agent>`, `s` → `<step>`: every rule
  // has to require a delimiter after the tag name, not just a prefix match.
  const prompt = "<brand> <item> <agent> <step> <table_name> <href>";
  assert.equal(normalizeRichText(prompt), prompt);
});

test("still folds real HTML into markdown", () => {
  assert.equal(normalizeRichText("<p>hello <strong>world</strong></p>"), "hello **world**");
  assert.equal(normalizeRichText("one<br/>two"), "one\ntwo");
  assert.equal(normalizeRichText("one<br>two"), "one\ntwo");
  assert.equal(normalizeRichText('<a href="https://example.com">link</a>'), "[link](https://example.com)");
  assert.equal(normalizeRichText("<div>boxed</div>"), "boxed");
  assert.equal(normalizeRichText('<span class="x">tagged</span>'), "tagged");
  assert.equal(normalizeRichText("<em>soft</em> and <i>slanted</i>"), "*soft* and *slanted*");
  assert.equal(normalizeRichText("<code>x = 1</code>"), "`x = 1`");
});

test("converts void elements rather than erasing them", () => {
  // A container tag leaves its text behind; `<hr>` and `<img>` carry everything
  // they mean in the tag, so stripping them deletes content outright.
  assert.equal(normalizeRichText('<img alt="a diagram" src="/x.png"/>'), "[image: a diagram — /x.png]");
  assert.equal(normalizeRichText('<img src="/x.png">'), "[image: /x.png]");
  assert.equal(normalizeRichText('<img alt="no source">'), "[image: no source]");
  assert.equal(normalizeRichText("<img>"), "[image]");
  assert.equal(normalizeRichText("above<hr/>below"), "above\n\n---\n\nbelow");
  assert.equal(normalizeRichText("above<hr>below"), "above\n\n---\n\nbelow");
});

test("renders an image inertly, never as live markup", () => {
  // `![alt](src)` reaches ChatMarkdown, whose `img` renders a live element: an
  // off-origin src fires from the engineer's browser, and an /api/v1/chat/files
  // one replays the viewer's JWT through AuthenticatedImage. Prompt bodies
  // carry end-user text, so that is untrusted content driving the request.
  const tracker = normalizeRichText('<img src="https://evil.example/p?t=1">');
  assert.equal(tracker, "[image: https://evil.example/p?t=1]");
  assert.ok(!tracker.includes("!["), "must not emit active image markup");

  const jwtReplay = normalizeRichText('<img src="/api/v1/chat/files/abc/download">');
  assert.ok(!jwtReplay.includes("!["), "must not emit active image markup");
  assert.ok(jwtReplay.includes("/api/v1/chat/files/abc/download"), "but must still show the URL");
});

test("reads attributes however they are quoted", () => {
  // Matching only `name="…"` missed single-quoted and unquoted forms, so the
  // lookup came back empty and the whole element was dropped — the erasure bug
  // this module exists to fix, reintroduced one layer down.
  assert.equal(normalizeRichText("<img src='/x.png'>"), "[image: /x.png]");
  assert.equal(normalizeRichText("<img src=/x.png>"), "[image: /x.png]");
  assert.equal(normalizeRichText("<img  src = '/x.png'  alt = 'shot' >"), "[image: shot — /x.png]");
  assert.equal(normalizeRichText("<a href='https://example.com'>link</a>"), "[link](https://example.com)");
  assert.equal(normalizeRichText("<a href=https://example.com>link</a>"), "[link](https://example.com)");
});

test("reads href from the tag, never from the link body", () => {
  // A `replace` callback receives the whole match, so handing that to htmlAttr
  // let prose that merely mentions `href=` supply a destination — and
  // `[text](url)` renders a live link from untrusted prompt content.
  assert.equal(
    normalizeRichText("<a name='x'>click href=\"https://evil.example\"</a>"),
    'click href="https://evil.example"',
  );
  assert.equal(normalizeRichText("<a name='top'>use href= here</a>"), "use href= here");
  // A real href in the tag still wins, body mention or not.
  assert.equal(
    normalizeRichText("<a href='https://example.com'>see href= docs</a>"),
    "[see href= docs](https://example.com)",
  );
});

test("handles a bare anchor with no attributes", () => {
  assert.equal(normalizeRichText("<a>text</a>"), "text");
});

test("keeps the link text when an anchor carries no href", () => {
  assert.equal(normalizeRichText("<a name='anchor'>text</a>"), "text");
});

test("does not read a token starting with img or hr as a void element", () => {
  const prompt = "<image_id> <hreflang> <imgur>";
  assert.equal(normalizeRichText(prompt), prompt);
});

test("leaves an unknown tag-shaped token for the renderer to display verbatim", () => {
  // react-markdown (no rehype-raw) renders raw HTML as literal text, so passing
  // it through is what puts it on screen. Dropping it is what hid it.
  assert.equal(htmlToMarkdownLite("<Component />"), "<Component />");
  assert.equal(htmlToMarkdownLite("<task_id>"), "<task_id>");
});

test("does not break the markdown autolink form", () => {
  // `<https://example.com>` is a link in CommonMark; escaping or stripping the
  // brackets would turn it into plain text.
  assert.equal(normalizeRichText("see <https://example.com>"), "see <https://example.com>");
});

test("unescapes a prompt embedded in a serialised payload", () => {
  assert.equal(unescapeSerializedText('line\\none\\ttab \\"quoted\\"'), 'line\none\ttab "quoted"');
  assert.equal(unescapeSerializedText("a\\r\\nb"), "a\nb");
});

test("does not re-read an escaped backslash as an escape sequence", () => {
  // The chained form collapsed `\\` last, so the `\n` rule got to the second
  // backslash first and turned a Windows path into a line break.
  assert.equal(unescapeSerializedText("C:\\\\newfolder"), "C:\\newfolder");
  assert.equal(unescapeSerializedText("a\\\\tb"), "a\\tb");
  assert.equal(unescapeSerializedText("match \\\\d+ digits"), "match \\d+ digits");
  assert.equal(unescapeSerializedText("\\\\\\\\n"), "\\\\n");
});

test("does not convert html inside a fenced code block", () => {
  // A prompt that fences HTML is deliberately showing markup; converting it
  // deletes the sample the prompt was teaching from.
  const fenced = ["```html", "<p>hello <strong>world</strong></p>", "```"].join("\n");
  assert.equal(normalizeRichText(fenced), fenced);

  const tilde = ["~~~html", "<hr/>", "~~~"].join("\n");
  assert.equal(normalizeRichText(tilde), tilde);
});

test("does not convert html inside an inline code span", () => {
  assert.equal(normalizeRichText("use `<br/>` for a line break"), "use `<br/>` for a line break");
  assert.equal(normalizeRichText("`<img src=\"/x.png\">` renders an image"), "`<img src=\"/x.png\">` renders an image");
});

test("still converts html outside a fenced block in the same prompt", () => {
  const prompt = ["<p>before</p>", "```html", "<p>inside</p>", "```", "<p>after</p>"].join("\n");
  // `<p>` yields a newline on each side, so the paragraphs are blank-line
  // separated — but the fence keeps its own spacing, since the blank-line
  // collapse now runs per segment and cannot reach across it.
  assert.equal(
    normalizeRichText(prompt),
    ["before", "", "```html", "<p>inside</p>", "```", "", "after"].join("\n"),
  );
});

test("treats an unterminated fence as running to the end", () => {
  // A payload clipped mid-block leaves one dangling fence; requiring a closer
  // would convert the HTML in the tail, which is the bug this PR is about.
  const clipped = ["intro", "```html", "<p>hello <strong>world</strong></p>"].join("\n");
  assert.equal(normalizeRichText(clipped), clipped);

  const tilde = ["intro", "~~~html", "<hr/>"].join("\n");
  assert.equal(normalizeRichText(tilde), tilde);
});

test("decodes unicode and the remaining json escapes", () => {
  assert.equal(unescapeSerializedText("caf\\u00e9 \\u2014 ok"), "café — ok");
  assert.equal(unescapeSerializedText("\\ud83d\\ude00"), "\u{1f600}");
  // A payload clipped between the halves must not yield a lone code unit:
  // `encodeURIComponent` throws on one and `JSON.stringify` re-escapes it.
  const clipped = unescapeSerializedText("emoji \\ud83d");
  assert.equal(clipped, "emoji \\ud83d");
  // `isWellFormed()` would say this directly but needs the ES2024 lib; the
  // project targets ES2020, and this input has no valid pair, so any surrogate
  // code unit at all is an unpaired one.
  assert.ok(!/[\uD800-\uDFFF]/.test(clipped), "must not emit an unpaired surrogate");
  assert.doesNotThrow(() => encodeURIComponent(clipped));
  // Two escapes that are not a valid pair stay as characters, not a code point.
  assert.equal(unescapeSerializedText("\\u0041\\u0042"), "AB");
});

test("never drops the partner of an invalid surrogate pair", () => {
  // A permissive second-escape match consumed a partner the pair branch could
  // not use, and the fallback re-emitted only the first — a silent deletion in
  // the module whose whole contract is not deleting.
  assert.equal(unescapeSerializedText("\\ud83d\\u0041"), "\\ud83dA");
  assert.equal(unescapeSerializedText("\\ude00\\ude00"), "\\ude00\\ude00");
  assert.equal(unescapeSerializedText("\\ud83d\\ud83d\\ude00"), "\\ud83d\u{1f600}");
  // Upper-case spelling is a valid pair too, and keeps its own spelling when it
  // is not.
  assert.equal(unescapeSerializedText("\\uD83D\\uDE00"), "\u{1f600}");
  assert.equal(unescapeSerializedText("\\uD83D\\u0041"), "\\uD83DA");
  assert.equal(unescapeSerializedText("a\\/b"), "a/b");
  assert.equal(unescapeSerializedText("a\\bb\\fc"), "a\bb\fc");
  // An escaped backslash still consumes its partner, so `\\u` stays literal.
  assert.equal(unescapeSerializedText("C:\\\\u00e9"), "C:\\u00e9");
});

test("wraps a code span in enough backticks to hold its own", () => {
  assert.equal(normalizeRichText("<code>a ` b</code>"), "``a ` b``");
  assert.equal(normalizeRichText("<code>plain</code>"), "`plain`");
  // A body that already reads as a code span is claimed by the CODE_SPAN split
  // before the `<code>` rule sees it. The wrapper is dropped but every
  // character survives, and the result is still a valid span.
  assert.equal(normalizeRichText("<code>``x``</code>"), "``x``");
});

test("preserves the line structure of a multi-line prompt", () => {
  const prompt = "[Waiting] Pause instead of polling.\n\nLimits: 5m per wait.";
  assert.equal(normalizeRichText(prompt), prompt);
});

// What the model received, real backslashes included.
const SENT_WITH_BACKSLASHES = [
  "C:\\newfolder",
  "C:\\table",
  "use \\n for a line break",
  "match \\d+ digits",
  "caf\\u00e9 is literal here",
];

test("leaves a real backslash alone on already-parsed input", () => {
  // JSON.parse already decoded these; unescaping again turned `C:\newfolder`
  // into a line break and `caf\u00e9` into an accented letter.
  const parsed = JSON.parse(JSON.stringify({ messages: SENT_WITH_BACKSLASHES.map((content) => ({ content })) }));
  for (const { content } of parsed.messages as { content: string }[]) {
    assert.equal(normalizeRichText(content, { unescape: false }), content);
  }
});

test("still unescapes the same prompt when it arrives serialised", () => {
  // The same literal renders differently depending on the side of the decode
  // it arrived on, and that difference is the point.
  for (const sent of SENT_WITH_BACKSLASHES) {
    const blob = `{"system_prompt": ${JSON.stringify(sent)}, "messages": [ ...truncated`;
    const [block] = extractRoleBlocksFromSerializedText(blob);
    assert.equal(block?.content, sent);
    assert.equal(normalizeRichText(JSON.stringify(sent).slice(1, -1)), sent);
  }
  assert.equal(normalizeRichText("use \\n here", { unescape: false }), "use \\n here");
  assert.equal(normalizeRichText("use \\n here"), "use \n here");
});

test("keeps decoded payload text as it was received", () => {
  for (const sent of SENT_WITH_BACKSLASHES) {
    assert.equal(normalizeMarkdownLikeString(sent, { decoded: true }), sent);
  }
});

test("never infers a serialisation from what decoded text looks like", () => {
  // A model's answer can open like JSON and be cut by the display preview; it is
  // still the model's text, backslashes and all.
  const answer = '{"answer": "C:\\newfolder", "reasoning": "cut her';
  assert.equal(normalizeMarkdownLikeString(answer, { decoded: true }), answer);
  // Nor is a `"system_prompt":` fragment inside decoded text scraped out.
  const quoting = 'The payload was {"system_prompt": "a\\nb", "messages": [';
  assert.equal(normalizeMarkdownLikeString(quoting, { decoded: true }), quoting);
});

test("unescapes a known serialisation, and scrapes its system_prompt body", () => {
  // An offload preview is a truncated `json.dumps` of the payload.
  const preview = '{"answer": "C:\\\\newfolder\\nnext line", "reasoning": "cut her';
  assert.equal(
    normalizeMarkdownLikeString(preview, { decoded: false }),
    '{"answer": "C:\\newfolder\nnext line", "reasoning": "cut her',
  );
  const withSystem = '{"system_prompt": "line one\\nline two", "messages": [';
  assert.equal(normalizeMarkdownLikeString(withSystem, { decoded: false }), "line one\nline two");
});

test("preserves a fenced code block containing angle brackets", () => {
  const prompt = ["```python", "if a < b:", "    tag = '<task_id>'", "```"].join("\n");
  assert.equal(normalizeRichText(prompt), prompt);
});
