import { test, expect, type Locator } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a Mermaid diagram on a dark slide.
//
// Mermaid parses its theme colours itself and throws on `color-mix()`, which is
// what the dark scheme's `--deck-surface-solid` is: the diagram then fell back
// to its error block. The deck is imported through the API (no LLM call) on the
// install's default template, owned by the admin and deleted afterwards.
//
// The same deck checks the deck theme on the drawing in a real browser
// (the geometry the unit snapshot cannot measure): rounded surface nodes with
// the deck's border, an opaque pill behind the edge label, the heading font on
// a node label, the accent on the arrowheads.
// ---------------------------------------------------------------------------

const FIXTURE_SPEC = {
  title: 'E2E dark diagram',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    {
      layout: 'content',
      title: 'A diagram on a dark slide',
      blocks: [{ type: 'diagram', source: 'flowchart LR\n  A[Collect] -->|feeds| B[Analyse]\n  B --> C[Act]' }],
    },
  ],
};

/**
 * A `font-family` list as its family names: a custom property keeps the
 * author's spelling (`'Geologica', system-ui`), a computed style the
 * browser's serialisation (unquoted when the name is an identifier, double
 * quotes otherwise), so the two are compared name by name, quotes and spacing
 * aside.
 */
function families(value: string): string[] {
  return value.split(',').map((name) => name.trim().replace(/^(["'])(.*)\1$/, '$2'));
}

/**
 * The pixel the colour `value` paints, resolved inside `scope`: one canvas for
 * a token (`color-mix()`, `hsl()`) and for a computed `fill` / `stroke`
 * (`rgb()` / `rgba()`), so two spellings of one colour compare equal.
 */
async function painted(scope: Locator, value: string): Promise<string> {
  return scope.evaluate((el, color) => {
    const probe = document.createElement('span');
    probe.style.color = color;
    el.appendChild(probe);
    const ctx = document.createElement('canvas').getContext('2d') as CanvasRenderingContext2D;
    ctx.fillStyle = getComputedStyle(probe).color;
    ctx.fillRect(0, 0, 1, 1);
    probe.remove();
    return Array.from(ctx.getImageData(0, 0, 1, 1).data).join(',');
  }, value);
}

test.describe('Presentations - Mermaid on a dark slide', () => {
  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/presentations/import', {
        headers: { Authorization: `Bearer ${token}` },
        multipart: {
          file: {
            name: 'e2e-dark-diagram.json',
            mimeType: 'application/json',
            buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
          },
        },
      });
      expect(resp.ok(), `importing the fixture deck failed: ${resp.status()} ${await resp.text()}`).toBe(true);
      deckId = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (!deckId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('the diagram renders as an SVG, not the error block', async ({ page }) => {
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    const diagram = page.locator('.deck-diagram').first();
    await expect(diagram.locator('.deck-diagram-svg svg')).toBeVisible({ timeout: 15_000 });
    await expect(diagram.locator('.deck-diagram-error')).toHaveCount(0);
    // The slide really is drawn in the dark scheme, where the surface is a color-mix().
    const surface = await diagram.evaluate((el) => getComputedStyle(el).getPropertyValue('--deck-surface-solid').trim());
    expect(surface).toContain('color-mix');
  });

  test('the drawing wears the deck theme', async ({ page }) => {
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    const diagram = page.locator('.deck-diagram').first();
    const svg = diagram.locator('.deck-diagram-svg svg');
    await expect(svg).toBeVisible({ timeout: 15_000 });
    const token = (name: string) => diagram.evaluate((el, prop) => getComputedStyle(el).getPropertyValue(prop).trim(), name);
    const computed = (locator: Locator, property: string) =>
      locator.evaluate((el, prop) => getComputedStyle(el).getPropertyValue(prop), property);
    const paintOf = async (locator: Locator, property: string) => painted(diagram, await computed(locator, property));
    const surface = await painted(diagram, await token('--deck-surface-solid'));
    const line = await painted(diagram, await token('--deck-line'));
    const accent = await painted(diagram, await token('--deck-accent'));
    const heading = await token('--deck-heading-font');

    // Every node: a 12px-rounded rect filled with the surface, bordered by the line at the deck's weight.
    const nodes = svg.locator('.node > rect.label-container');
    expect(await nodes.count()).toBe(3);
    for (const rect of await nodes.all()) {
      expect(await rect.getAttribute('rx')).toBe('12');
      expect(await paintOf(rect, 'fill')).toBe(surface);
      expect(await paintOf(rect, 'stroke')).toBe(line);
      expect(await computed(rect, 'stroke-width')).toBe('2px');
    }
    // Its label in the heading font.
    const label = svg.locator('.node .label text').first();
    expect(families(await computed(label, 'font-family'))).toEqual(families(heading));

    // The edge label: an opaque surface pill with round ends, wider than its text.
    const pill = svg.locator('.edgeLabel rect.background[width]');
    await expect(pill).toHaveCount(1);
    const box = await pill.evaluate((el) => ({ w: Number(el.getAttribute('width')), h: Number(el.getAttribute('height')), rx: Number(el.getAttribute('rx')) }));
    expect(box.rx).toBeCloseTo(box.h / 2, 5);
    const text = await svg.locator('.edgeLabel text').first().evaluate((el) => (el as SVGGraphicsElement).getBBox().width);
    expect(box.w).toBeGreaterThan(text + 20);
    expect(await paintOf(pill, 'fill')).toBe(surface);
    expect(await computed(pill, 'opacity')).toBe('1');

    // Arrowheads in the accent.
    const head = svg.locator('marker.marker .arrowMarkerPath').first();
    expect(await paintOf(head, 'fill')).toBe(accent);
  });
});
