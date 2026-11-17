import { test, expect, type Page } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';
import { unzip } from './pptx-archive';

// ---------------------------------------------------------------------------
// Presentations - a palette colour on an architecture node and zone.
//
// A node or a zone may wear a colour of the deck's template palette (`color`:
// brand_1..4 / chart_1..6) in place of a tone. The stylesheet resolves it per
// SLIDE ROOT, so one block reads the template's dark hue on a dark slide and
// its light hue on a light one: the border wears the colour whole, the body an
// 18% (dark) / 10% (light) tint of it, and the icons, the chips and a core's
// glow follow. This spec reads what the browser COMPUTES on the
// two slides of a deck whose template gives brand_1 and brand_2 a different
// hex per scheme, then exports the deck as an editable PPTX and finds the same
// border and tint in the pictures of the diagram - the PPTX carries the colours
// the screen shows. The deck and its template are imported through the API (no
// LLM call), owned by the admin and deleted afterwards.
// ---------------------------------------------------------------------------

/** Test hues found nowhere else in the palettes, so a pixel match is unambiguous. */
const HUES = {
  dark: { brand_1: '#ff3fa4', brand_2: '#e8c100' },
  light: { brand_1: '#b1006a', brand_2: '#6b5a00' },
} as const;

const TINT = { dark: 0.18, light: 0.1 } as const;

const ARCHITECTURE = {
  type: 'architecture',
  nodes: [
    { id: 'cti', label: 'OpenCTI', icon: 'brain', color: 'brand_1', chips: ['Graph', 'Feeds'], row: 1, col: 1 },
    {
      id: 'core',
      label: 'XTM One',
      icon: 'sparkles',
      shape: 'circle',
      emphasis: 'core',
      color: 'brand_1',
      row: 1,
      col: 2,
    },
    // No colour and no tone: the neutral body the tint is mixed into.
    { id: 'aev', label: 'OpenAEV', icon: 'crosshair', row: 1, col: 3 },
  ],
  edges: [
    { source: 'core', target: 'cti', arrows: 'none' },
    { source: 'core', target: 'aev', arrows: 'none' },
  ],
  zones: [{ label: 'XTM Platform', icon: 'layers', color: 'brand_2', row: 1, col: 1, col_span: 3 }],
};

const FIXTURE_SPEC = {
  title: 'E2E architecture colours',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'content', title: 'Product colours on a dark slide', theme: 'dark', blocks: [ARCHITECTURE] },
    { layout: 'content', title: 'Product colours on a light slide', blocks: [ARCHITECTURE] },
  ],
};

// ---------------------------------------------------------------------------
// Colours
// ---------------------------------------------------------------------------

interface Rgba {
  r: number;
  g: number;
  b: number;
  a: number;
}

const hex = (value: string): Rgba => ({
  r: parseInt(value.slice(1, 3), 16),
  g: parseInt(value.slice(3, 5), 16),
  b: parseInt(value.slice(5, 7), 16),
  a: 1,
});

/** A `color(srgb ...)` channel or an alpha - a 0..1 number or a percentage - times `scale`. */
const fraction = (token: string, scale: number): number =>
  (token.endsWith('%') ? parseFloat(token) / 100 : parseFloat(token)) * scale;

/** A computed colour as Chromium serialises it: `rgb()` / `rgba()`, or `color(srgb ...)` for a `color-mix()`. */
function parseColor(css: string): Rgba {
  const value = css.trim();
  const rgb = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(value);
  if (rgb) {
    return { r: +rgb[1], g: +rgb[2], b: +rgb[3], a: rgb[4] === undefined ? 1 : fraction(rgb[4], 1) };
  }
  const srgb = /^color\(srgb\s+([-\d.e]+%?)\s+([-\d.e]+%?)\s+([-\d.e]+%?)(?:\s*\/\s*([\d.e]+%?))?\s*\)$/.exec(value);
  if (srgb) {
    return {
      r: fraction(srgb[1], 255),
      g: fraction(srgb[2], 255),
      b: fraction(srgb[3], 255),
      a: srgb[4] === undefined ? 1 : fraction(srgb[4], 1),
    };
  }
  throw new Error(`Unreadable computed colour: ${css}`);
}

/** `color-mix(in srgb, top share, bottom)` for two opaque colours. */
const mix = (top: Rgba, share: number, bottom: Rgba): Rgba => ({
  r: top.r * share + bottom.r * (1 - share),
  g: top.g * share + bottom.g * (1 - share),
  b: top.b * share + bottom.b * (1 - share),
  a: 1,
});

function expectColour(actual: string, expected: Rgba, what: string): void {
  const got = parseColor(actual);
  for (const channel of ['r', 'g', 'b'] as const) {
    expect(Math.abs(got[channel] - expected[channel]), `${what}: ${actual} vs ${JSON.stringify(expected)}`).toBeLessThanOrEqual(1.5);
  }
  expect(Math.abs(got.a - expected.a), `${what} alpha: ${actual}`).toBeLessThanOrEqual(0.01);
}

// ---------------------------------------------------------------------------
// What the screen computes
// ---------------------------------------------------------------------------

type Scheme = keyof typeof HUES;

interface SlidePaint {
  scheme: string | null;
  tone: string | null;
  border: string;
  body: string;
  neutralBody: string;
  icon: string;
  chipFill: string;
  chipStroke: string;
  coreBorder: string;
  coreBody: string;
  glow: string;
  zoneBorder: string;
  zoneBody: string;
  zoneIcon: string;
}

function readPaint(page: Page, index: number): Promise<SlidePaint> {
  return page.evaluate((slideIndex) => {
    const slide = document.querySelectorAll('.reveal .slides > section')[slideIndex];
    if (!slide) throw new Error(`no slide ${slideIndex + 1}`);
    const style = (selector: string, property: string): string => {
      const element = slide.querySelector(selector);
      if (!element) throw new Error(`no ${selector} on slide ${slideIndex + 1}`);
      return getComputedStyle(element).getPropertyValue(property);
    };
    const card = '.deck-arch-node[data-shape="card"][data-color="brand_1"]';
    const core = '.deck-arch-node[data-emphasis="core"]';
    const zone = '.deck-arch-zone[data-color="brand_2"]';
    return {
      scheme: slide.getAttribute('data-scheme'),
      tone: slide.querySelector(card)?.getAttribute('data-tone') ?? null,
      border: style(`${card} .deck-arch-node-shape`, 'stroke'),
      body: style(`${card} .deck-arch-node-shape`, 'fill'),
      neutralBody: style('.deck-arch-node:not([data-color]) .deck-arch-node-shape', 'fill'),
      icon: style(`${card} .deck-arch-icon`, 'color'),
      chipFill: style(`${card} .deck-arch-chip rect`, 'fill'),
      chipStroke: style(`${card} .deck-arch-chip rect`, 'stroke'),
      coreBorder: style(`${core} .deck-arch-node-shape`, 'stroke'),
      coreBody: style(`${core} .deck-arch-node-shape`, 'fill'),
      glow: style(`${core} .deck-arch-node-glow`, 'fill'),
      zoneBorder: style(`${zone} .deck-arch-zone-panel`, 'stroke'),
      zoneBody: style(`${zone} .deck-arch-zone-panel`, 'fill'),
      zoneIcon: style(`${zone} .deck-arch-icon`, 'color'),
    };
  }, index);
}

async function openDeck(page: Page, deckId: string): Promise<void> {
  await page.goto(`/presentations/${deckId}`);
  await expect(page.locator('.reveal.ready')).toBeVisible();
  // The Lucide icons are a lazy chunk: every node icon is drawn once it lands.
  await expect(page.locator('.reveal .slides > section .deck-arch-icon-pending')).toHaveCount(0);
  await expect(page.locator('.reveal .slides > section .deck-arch-node[data-color] .deck-arch-icon')).toHaveCount(4);
}

// ---------------------------------------------------------------------------
// What the PPTX carries
// ---------------------------------------------------------------------------

/** The PNG pictures slide `number` (1-based) of a PPTX places. */
function slidePictures(entries: Map<string, Buffer>, number: number): Buffer[] {
  const rels = entries.get(`ppt/slides/_rels/slide${number}.xml.rels`)?.toString('utf8') ?? '';
  return [...rels.matchAll(/Target="\.\.\/media\/([^"]+\.png)"/g)]
    .map((match) => entries.get(`ppt/media/${match[1]}`))
    .filter((picture): picture is Buffer => picture !== undefined);
}

/** Per picture, how many opaque pixels sit within 3 levels of each target colour. */
function countPixels(page: Page, pictures: Buffer[], targets: Rgba[]): Promise<number[][]> {
  return page.evaluate(
    async ({ encoded, colours }) => {
      const counts: number[][] = [];
      for (const data of encoded) {
        const image = new Image();
        image.src = `data:image/png;base64,${data}`;
        await image.decode();
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext('2d');
        if (!context) throw new Error('no 2d context');
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const hits = colours.map(() => 0);
        for (let i = 0; i < pixels.length; i += 4) {
          if (pixels[i + 3] < 250) continue;
          colours.forEach((colour, k) => {
            if (
              Math.abs(pixels[i] - colour.r) <= 3 &&
              Math.abs(pixels[i + 1] - colour.g) <= 3 &&
              Math.abs(pixels[i + 2] - colour.b) <= 3
            ) {
              hits[k] += 1;
            }
          });
        }
        counts.push(hits);
      }
      return counts;
    },
    { encoded: pictures.map((picture) => picture.toString('base64')), colours: targets },
  );
}

// ---------------------------------------------------------------------------

test.describe('Presentations - palette colours on architecture nodes and zones', () => {
  let deckId = '';
  let templateId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E architecture colours ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName, { dark: HUES.dark, light: HUES.light });
      deckId = await importDeckOnTemplate(apiCtx, token, FIXTURE_SPEC, templateName, 'e2e-architecture-colours.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (deckId) {
      const { apiCtx, token } = await createAdminApiContext();
      try {
        await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      } finally {
        await apiCtx.dispose();
      }
    }
    await deleteTemplate(templateId);
  });

  test('should paint a coloured node and zone in the palette hue of each slide scheme', async ({ page }) => {
    await openDeck(page, deckId);
    for (const [index, scheme] of [
      [0, 'dark'],
      [1, 'light'],
    ] as [number, Scheme][]) {
      const paint = await readPaint(page, index);
      const node = hex(HUES[scheme].brand_1);
      const zone = hex(HUES[scheme].brand_2);
      const tint = mix(node, TINT[scheme], parseColor(paint.neutralBody));
      const label = `slide ${index + 1} (${scheme})`;
      expect(paint.scheme, label).toBe(scheme);
      // The colour replaces the tone: no data-tone left for a tone rule to match.
      expect(paint.tone, label).toBeNull();
      // Border: the resolved hex itself. Body: the scheme's tint of it over the neutral surface.
      expectColour(paint.border, node, `${label} border`);
      expectColour(paint.body, tint, `${label} body`);
      // The icon and the chips inside the node take the same hue.
      expectColour(paint.icon, node, `${label} icon`);
      expectColour(paint.chipFill, { ...node, a: 0.12 }, `${label} chip fill`);
      expectColour(paint.chipStroke, { ...node, a: 0.35 }, `${label} chip border`);
      // A coloured core keeps its glow - in that colour - over the same tint.
      expectColour(paint.coreBorder, node, `${label} core border`);
      expectColour(paint.coreBody, tint, `${label} core body`);
      expectColour(paint.glow, { ...node, a: 0.38 }, `${label} core glow`);
      // The zone wears its own colour: border whole, panel tinted, icon in it.
      expectColour(paint.zoneBorder, zone, `${label} zone border`);
      expectColour(paint.zoneBody, { ...zone, a: TINT[scheme] }, `${label} zone panel`);
      expectColour(paint.zoneIcon, zone, `${label} zone icon`);
    }
  });

  test('should carry the same colours into the pictures of the exported PPTX', async ({ page }) => {
    // A server-side render of every slide through the platform's headless browser.
    test.setTimeout(180_000);
    await openDeck(page, deckId);
    const screen = { dark: await readPaint(page, 0), light: await readPaint(page, 1) };

    const { apiCtx, token } = await createAdminApiContext();
    let archive: Buffer;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { format: 'pptx_native' },
        timeout: 150_000,
      });
      expect(response.ok(), `export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      archive = await response.body();
    } finally {
      await apiCtx.dispose();
    }
    const entries = unzip(archive);

    for (const [number, scheme, other] of [
      [1, 'dark', 'light'],
      [2, 'light', 'dark'],
    ] as [number, Scheme, Scheme][]) {
      const pictures = slidePictures(entries, number);
      expect(pictures.length, `slide ${number} places the diagram as a picture`).toBeGreaterThan(0);
      const border = hex(HUES[scheme].brand_1);
      const body = parseColor(screen[scheme].body);
      const otherBorder = hex(HUES[other].brand_1);
      const counts = await countPixels(page, pictures, [border, body, otherBorder]);
      const best = (k: number) => Math.max(...counts.map((hits) => hits[k]));
      const label = `slide ${number} (${scheme})`;
      // The border and the tinted body the screen computed are painted in the picture...
      expect(best(0), `${label}: border ${HUES[scheme].brand_1} in ${JSON.stringify(counts)}`).toBeGreaterThan(300);
      expect(best(1), `${label}: body ${screen[scheme].body} in ${JSON.stringify(counts)}`).toBeGreaterThan(3000);
      // ... and resolved for THAT slide's scheme, never the other one's.
      expect(best(2), `${label}: the ${other} hue leaked in ${JSON.stringify(counts)}`).toBeLessThan(20);
    }
  });
});
