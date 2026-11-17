import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Public chat widget XSS - the LIVE half of the guarantee (#security).
//
// `frontend/tests/embedded-markdown-xss.test.ts` runs the same payloads through
// the widget's `render-core` block under Node and asserts the OUTPUT STRING is
// inert. That is necessary but not sufficient: the widget assigns that string to
// `innerHTML`, and only a real browser re-parses it - an mXSS payload can be
// inert as text yet build an active node when the HTML tree-builder gets hold of
// it (a foreign-content or raw-text boundary the string scanner cannot model).
//
// This spec closes that gap. It fetches the SERVED `widget.js` from the running
// platform (so it also proves the deployed artifact carries the fix, not just
// the source tree), injects its `render-core` into a real Chromium page, and for
// every payload - at EVERY streaming prefix, because the widget re-renders the
// whole accumulated answer on each chunk - writes the sanitizer's output into a
// live `innerHTML`, in default and in strict rendering, and asserts:
//   * no forbidden element (script / iframe / style / foreignObject / animate /
//     use / ...) survives the browser's own parse,
//   * no event-handler attribute survives, and no URL attribute carries a
//     scheme that runs script (javascript / vbscript / data) or launches a
//     Windows program (ms-msdt / search-ms / ...),
//   * a kept `style` cannot reach script, and an SVG or MathML drawing holds
//     no link and loads nothing from outside the page, whether written on it
//     or inherited (its computed paint, marker, clip, mask and filter values),
//   * strict rendering keeps no `style`, no drawing and no scheme outside the
//     http/https/mailto/tel allow-list, and
//   * nothing executes (a surviving `<img onerror>` fires on the failed load;
//     `window.alert` is trapped and must never be called).
//
// The payloads are the shared JSON fixture the Node test also reads, so the two
// halves can never drift.
// ---------------------------------------------------------------------------

const PAYLOADS: string[] = JSON.parse(
  readFileSync(fileURLToPath(new URL('./fixtures/embedded-markdown-xss-payloads.json', import.meta.url)), 'utf8'),
);

/** Fetch the served widget.js and lift the `render-core` block out of the IIFE. */
async function loadServedRenderCore(page: Page): Promise<string> {
  const res = await page.request.get('/api/v1/public/widget.js');
  expect(res.ok(), `widget.js should be served (status ${res.status()})`).toBe(true);
  const source = await res.text();
  const start = source.indexOf('// render-core:start');
  const end = source.indexOf('// render-core:end');
  expect(start, 'served widget.js has lost its render-core:start marker').toBeGreaterThan(0);
  expect(end, 'served widget.js has lost its render-core:end marker').toBeGreaterThan(start);
  // The block is self-contained (ALLOWED_TAGS ... renderAssistant) and contains
  // no literal "</script>", so it is safe to inject as a script element.
  return source.slice(start, end);
}

interface Probe {
  findings: string[];
  fired: string[];
}

/** Run every payload, at every prefix, through a live innerHTML and report. */
function probeInBrowser(payloads: string[]): Probe {
  // The trap array lives on `window` so the spec can read it AGAIN after a
  // short wait - a surviving `<img onerror>` fires asynchronously on the
  // failed load, after this function has already returned.
  const fired: string[] = [];
  (window as unknown as { __xssFired: string[] }).__xssFired = fired;
  // alert() is what every payload's handler / script body calls.
  (window as unknown as { alert: (msg?: unknown) => void }).alert = (msg?: unknown) => {
    fired.push(`alert:${String(msg)}`);
  };
  // Bubble phase only (no capture): catches an uncaught error thrown by an
  // injected handler that DID run, but NOT a resource-load error from an inert
  // `<img src="x">` (those do not bubble), so a stripped payload is no false
  // positive.
  window.addEventListener('error', (event) => {
    fired.push(`error:${event.message}`);
  });

  const renders = window as unknown as {
    __render: (text: string) => string;
    __renderStrict: (text: string) => string;
  };
  const sink = document.getElementById('sink') as HTMLElement;

  const FORBIDDEN = new Set([
    'applet', 'base', 'embed', 'form', 'frame', 'frameset', 'iframe', 'link',
    'meta', 'noembed', 'noframes', 'noscript', 'object', 'plaintext',
    'script', 'select', 'style', 'template', 'textarea', 'title', 'xmp',
    'animate', 'animatemotion', 'animatetransform', 'annotation-xml', 'discard',
    'feimage', 'foreignobject', 'handler', 'image', 'maction', 'mglyph', 'set', 'use',
  ]);
  const URL_ATTRS = new Set([
    'action', 'background', 'cite', 'data', 'formaction', 'href', 'poster', 'src', 'xlink:href',
  ]);
  const SCRIPT_SCHEMES = new Set(['javascript', 'vbscript', 'data']);
  // Windows protocol handlers that launch or drive a program and have a public
  // exploitation history: refused in every mode, like the script schemes.
  const PROGRAM_SCHEMES = new Set([
    'hcp', 'its', 'mhtml', 'mk', 'ms-appinstaller', 'ms-cxh', 'ms-cxh-full', 'ms-its', 'ms-itss',
    'ms-msdt', 'ms-officecmd', 'search', 'search-ms', 'shell',
  ]);
  const SAFE_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);
  const SCRIPT_CSS = /expression\(|javascript:|vbscript:|(?:^|;)(?:-ms-)?behaviou?r:|(?:^|;)-moz-binding:/;
  const DRAWING_REFS = /url\(|image-set\(|image\(|cross-fade\(|element\(|src\(/;
  // What a drawing inherits from the HTML around it.
  const DRAWING_INHERITED = /(?:^|;)(?:--|fill:|stroke:|marker(?:-start|-mid|-end)?:)/;
  // The drawing properties that load what they name, as the browser computes
  // them (custom properties and inheritance resolved).
  const DRAWING_URL_PROPERTIES = ['fill', 'stroke', 'marker-start', 'marker-mid', 'marker-end', 'clip-path', 'mask', 'filter'];
  // attr.value is already reference-decoded by the DOM; the CSS parser also
  // decodes escapes, and a comment or whitespace never joins two tokens.
  const cssReading = (value: string) =>
    value
      .replace(/\\(?:([0-9a-f]{1,6})[ \t\n\r\f]?|([\s\S]))/gi, (_, hex: string | undefined, ch: string) => {
        if (!hex) return /[\n\r\f]/.test(ch) ? '' : ch;
        const code = parseInt(hex, 16);
        return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : '\ufffd';
      })
      .replace(/[\u0000-\u0020\u007f-\u009f]+/g, '')
      .toLowerCase();

  const findings: string[] = [];
  const note = (why: string, payload: string, prefix: string, strict: boolean) =>
    findings.push(`${strict ? 'strict: ' : ''}${why} <= ${JSON.stringify(payload)} @${prefix.length}`);

  const inspect = (payload: string, prefix: string, strict: boolean) => {
    sink.innerHTML = (strict ? renders.__renderStrict : renders.__render)(prefix);
    for (const el of Array.from(sink.querySelectorAll('*'))) {
      const tag = el.localName.toLowerCase();
      const inDrawing = el.namespaceURI !== 'http://www.w3.org/1999/xhtml';
      if (FORBIDDEN.has(tag)) note(`<${tag}> survived`, payload, prefix, strict);
      if (strict && inDrawing) note(`<${tag}> drawn`, payload, prefix, strict);
      if (inDrawing && tag === 'a') note('a link inside a drawing', payload, prefix, strict);
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        if (name.startsWith('on')) note(`${name}= survived`, payload, prefix, strict);
        if (strict && name === 'style') note('style= survived', payload, prefix, strict);
        if (URL_ATTRS.has(name)) {
          if (inDrawing) note(`${name}= inside a drawing`, payload, prefix, strict);
          // Strip the whitespace/controls the URL parser ignores, then read the scheme.
          const probe = attr.value.replace(/[\u0000-\u0020\u007f-\u009f]+/g, '');
          const scheme = /^([^:/?#]*):/.exec(probe)?.[1].toLowerCase();
          if (scheme && (strict ? !SAFE_SCHEMES.has(scheme) : SCRIPT_SCHEMES.has(scheme) || PROGRAM_SCHEMES.has(scheme))) {
            note(`${name}=${scheme}: URL`, payload, prefix, strict);
          }
        }
        if (name === 'style' || inDrawing) {
          const css = cssReading(attr.value);
          if (SCRIPT_CSS.test(css)) note(`${name}= reaches script`, payload, prefix, strict);
          if ((inDrawing || DRAWING_INHERITED.test(css)) && DRAWING_REFS.test(css.replace(/url\((['"]?)#[^'"()]*\1\)/g, ''))) {
            note(`${name}= loads something a drawing can use`, payload, prefix, strict);
          }
          if (inDrawing && css.includes('inherit')) note(`${name}= inherits into a drawing`, payload, prefix, strict);
        }
      }
      if (inDrawing) {
        const computed = getComputedStyle(el);
        for (const property of DRAWING_URL_PROPERTIES) {
          for (const [, url] of computed.getPropertyValue(property).matchAll(/url\(\s*["']?([^"')]*)/g)) {
            if (!/^(?:about:blank)?#/.test(url)) note(`${property} computes to ${url}`, payload, prefix, strict);
          }
        }
      }
    }
  };

  for (const payload of payloads) {
    for (let end = 1; end <= payload.length; end++) {
      inspect(payload, payload.slice(0, end), false);
      inspect(payload, payload.slice(0, end), true);
    }
  }
  sink.innerHTML = '';
  return { findings, fired };
}

/** Ordinary markdown a real answer carries - must survive the browser parse. */
function probeLegitimate(): { html: string; anchors: number; safeAnchors: number } {
  const render = (window as unknown as { __render: (text: string) => string }).__render;
  const sink = document.getElementById('sink') as HTMLElement;
  sink.innerHTML = render('Hello **world**, see [the docs](https://docs.example.com/a?b=1&c=2).');
  const anchors = Array.from(sink.querySelectorAll('a'));
  const safeAnchors = anchors.filter((a) => a.protocol === 'https:').length;
  const html = sink.innerHTML;
  sink.innerHTML = '';
  return { html, anchors: anchors.length, safeAnchors };
}

/** How the browser builds the drawing and the formula default rendering keeps. */
function probeDrawing(answer: string) {
  const renders = window as unknown as {
    __render: (text: string) => string;
    __renderStrict: (text: string) => string;
  };
  const sink = document.getElementById('sink') as HTMLElement;
  sink.innerHTML = renders.__render(answer);
  const svg = sink.querySelector('svg');
  const math = sink.querySelector('math');
  const built = {
    svgNamespace: svg?.namespaceURI ?? null,
    viewBox: svg?.getAttribute('viewBox') ?? null,
    // Found only by the name the parser gives it, not the lower-case one written.
    gradient: svg?.querySelector('defs > linearGradient > stop') ? 'linearGradient' : null,
    fill: svg?.querySelector('rect')?.getAttribute('fill') ?? null,
    label: svg?.querySelector('text')?.textContent ?? null,
    mathNamespace: math?.namespaceURI ?? null,
    fraction: math?.querySelector('mfrac > mi')?.textContent ?? null,
    last: sink.lastElementChild?.textContent ?? null,
    strictDrawings: -1,
  };
  sink.innerHTML = renders.__renderStrict(answer);
  built.strictDrawings = sink.querySelectorAll('svg, math').length;
  sink.innerHTML = '';
  return built;
}

// Default and strict rendering, as the widget calls them.
const EXPOSE_RENDERERS =
  ';window.__render = function (text) { return renderAssistant(text, "", false); };' +
  'window.__renderStrict = function (text) { return renderAssistant(text, "", true); };';

test.describe('Public chat widget - render-core is inert in a real browser', () => {
  test('every payload stays inert through a live innerHTML, streamed and final', async ({ page }) => {
    const renderCore = await loadServedRenderCore(page);
    await page.setContent('<!doctype html><html><head><meta charset="utf-8"></head><body><div id="sink"></div></body></html>');
    await page.addScriptTag({ content: `${renderCore}\n${EXPOSE_RENDERERS}` });
    await expect.poll(() => page.evaluate(() => typeof (window as unknown as { __render?: unknown }).__render)).toBe('function');

    const { findings, fired } = await page.evaluate(probeInBrowser, PAYLOADS);
    // Give any async handler (a surviving <img onerror> fires on the failed
    // load) a chance to run before the verdict.
    await page.waitForTimeout(300);
    const firedFinal = await page.evaluate(() => (window as unknown as { __xssFired?: string[] }).__xssFired ?? []);

    expect(findings, findings.slice(0, 20).join('\n')).toEqual([]);
    expect(fired, `handlers fired: ${fired.slice(0, 20).join(', ')}`).toEqual([]);
    expect(firedFinal, `handlers fired (incl. async): ${firedFinal.slice(0, 20).join(', ')}`).toEqual([]);
  });

  test('ordinary markdown still renders as a safe link', async ({ page }) => {
    const renderCore = await loadServedRenderCore(page);
    await page.setContent('<!doctype html><html><head><meta charset="utf-8"></head><body><div id="sink"></div></body></html>');
    await page.addScriptTag({ content: `${renderCore}\n${EXPOSE_RENDERERS}` });
    const { anchors, safeAnchors } = await page.evaluate(probeLegitimate);
    expect(anchors).toBe(1);
    expect(safeAnchors).toBe(1);
  });

  test('a drawing written over several lines is built whole as SVG, a formula as MathML', async ({ page }) => {
    const renderCore = await loadServedRenderCore(page);
    await page.setContent('<!doctype html><html><head><meta charset="utf-8"></head><body><div id="sink"></div></body></html>');
    await page.addScriptTag({ content: `${renderCore}\n${EXPOSE_RENDERERS}` });
    const answer = [
      '<svg width="120" height="40" viewBox="0 0 120 40">',
      '  <defs><linearGradient id="g"><stop offset="0" stop-color="#0fbcff"/></linearGradient></defs>',
      '  <rect width="80" height="40" fill="url(#g)"/>',
      '  <text x="40" y="25">Q3</text>',
      '</svg>',
      '<math><mfrac><mi>a</mi><mn>2</mn></mfrac></math>',
      '',
      'The trend is up.',
    ].join('\n');
    const built = await page.evaluate(probeDrawing, answer);
    expect(built).toEqual({
      svgNamespace: 'http://www.w3.org/2000/svg',
      viewBox: '0 0 120 40',
      gradient: 'linearGradient',
      fill: 'url(#g)',
      label: 'Q3',
      mathNamespace: 'http://www.w3.org/1998/Math/MathML',
      fraction: 'a',
      last: 'The trend is up.',
      strictDrawings: 0,
    });
  });
});
