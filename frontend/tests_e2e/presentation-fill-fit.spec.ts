import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a `fill` block in a body shrunk to fit settles.
//
// Two measured passes act on a slide body: the shrink-to-fit search
// (`fit.ts`), which holds a `metrics_grid` at its size by scaling its
// lengths with the inverse of the ratio, and the bounded spread
// (`spread.ts`), which caps a `fill` block - and a `distribute` run -
// with a `max-height` measured off its rows. Left to their resize observers,
// the search read the grid through a cap measured on the previous layout: the
// cap clipped the grid short of its tiles, the body "fitted", the cap then grew
// with the tiles, and the two passes chased each other on every frame - the
// tiles blinked forever and the last row was cut off under the footer block.
//
// The search now re-measures the caps on each layout it probes, so `fill`
// never changes what the fit decides: a slide settles in one pass, at the
// fit ratio and hold of the same slide without `fill`, every row inside its
// body. This spec samples the slide on screen frame by frame to pin both.
//
// The deck is imported through the API from a bare spec (no LLM call), owned
// by the admin and deleted afterwards; the file runs SERIALLY so the import
// and the login happen once (see `presentation-fill.spec.ts`).
// ---------------------------------------------------------------------------

const TILES = [
  { icon: 'users', value: '15,706', label: 'Net new contacts', sublabel: 'Marketing Overview' },
  { icon: 'target', value: '27.9M', label: 'Sourced pipeline', sublabel: '49.7% of pipeline, target 50%', tone: 'success' },
  { icon: 'git-branch', value: '13.35M', label: 'Direct pipeline', sublabel: '98 deals, 25% of the pipeline value' },
  { icon: 'globe', value: '30.0%', label: 'Website direct share', sublabel: 'Top of the benchmark band', tone: 'warning' },
  { icon: 'layers', value: '50.6%', label: 'All-sites direct share', sublabel: 'Not comparable across sites' },
  { icon: 'user-check', value: '2,648', label: 'Net new MQLs', sublabel: 'Marketing Overview' },
];

const grid = (fill: boolean) => ({ type: 'metrics_grid', columns: 3, ...(fill ? { fill: true } : {}), items: TILES });

const BULLET = 'A point long enough to wrap onto a second line when the body is narrow';
const list = (fill: boolean) => ({ type: 'list', ...(fill ? { fill: true } : {}), items: [BULLET, BULLET, BULLET, BULLET] });

/** A footer tall enough that the body above it overflows its room and is shrunk to fit. */
const FOOTER = {
  type: 'callout',
  icon: 'info',
  title: 'Year to date, 1 Jan - 5 Oct',
  content: Array.from({ length: 5 }, () => 'The share of pipeline is on the analysis tab.').join(' '),
};

const slide = (id: string, blocks: Record<string, unknown>[], extra: Record<string, unknown> = {}) => ({
  id,
  layout: 'content',
  title: 'Year to date: marketing on target, direct share not comparable',
  blocks,
  footer_block: FOOTER,
  ...extra,
});

const FIXTURE_SPEC = {
  title: 'E2E fill in a fitted body',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Fill in a fitted body', subtitle: 'Settled in one pass' },
    slide('grid-packed', [grid(false)]),
    slide('grid-fill', [grid(true)]),
    slide('run-packed', [grid(false), list(false)]),
    slide('run-distribute', [grid(true), list(true)], { distribute: true }),
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((s) => s.id === id) + 1;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-fill-fit.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

async function openStage(page: Page, deckId: string, slideNumber: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${slideNumber - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slideNumber} of \\d+\\.`) })).toBeVisible();
  await expect(page.locator('.reveal .slides > section.present .deck-fit-content > .deck-blocks-slide')).toBeVisible();
}

type Settled = {
  /** Distinct body states seen over the sampled frames: 1 once settled. */
  states: string[];
  fit: string | null;
  released: boolean;
  /** How far the lowest row of the body ends below the body's room, in browser px. */
  overflow: number;
};

/**
 * Samples the slide on screen for `frames` animation frames after it had time
 * to settle: the fit ratio, the hold, every spread cap in the body and the
 * box of every row, as one state string per frame.
 */
async function sampleBody(page: Page, frames = 60): Promise<Settled> {
  await page.waitForTimeout(500);
  return page.locator('.reveal .slides > section.present').evaluate(
    (section, count) =>
      new Promise<Settled>((resolve) => {
        const host = section.querySelector<HTMLElement>('.deck-fit');
        if (!host) throw new Error('No slide body on the slide on screen');
        const rows = () =>
          Array.from(host.querySelectorAll<HTMLElement>('.deck-blocks-slide > * > *')).filter(
            (el) => el.getBoundingClientRect().height > 0,
          );
        const read = () =>
          [
            host.dataset.fit ?? '1',
            host.hasAttribute('data-fit-released') ? 'released' : 'held',
            ...Array.from(host.querySelectorAll<HTMLElement>('[data-spread-cap]')).map((el) => el.dataset.spreadCap),
            ...rows().map((el) => Math.round(el.getBoundingClientRect().bottom)),
          ].join('|');
        const states = new Set<string>();
        let left = count;
        const tick = () => {
          states.add(read());
          left -= 1;
          if (left > 0) {
            window.requestAnimationFrame(tick);
            return;
          }
          const lowest = Math.max(...rows().map((el) => el.getBoundingClientRect().bottom));
          resolve({
            states: [...states],
            fit: host.dataset.fit ?? null,
            released: host.hasAttribute('data-fit-released'),
            overflow: lowest - host.getBoundingClientRect().bottom,
          });
        };
        window.requestAnimationFrame(tick);
      }),
    frames,
  );
}

/** Browser px on the scaled stage: a hair of rounding, never a clipped row. */
const TOLERANCE = 2;

test.describe('Presentations - fill in a body shrunk to fit', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token);
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

  for (const [packedId, filledId, what] of [
    ['grid-packed', 'grid-fill', 'a fill metrics grid'],
    ['run-packed', 'run-distribute', 'a distribute run around a fill grid and a fill list'],
  ] as const) {
    test(`${what} settles where the packed slide does, every row inside the body`, async ({ page }) => {
      await openStage(page, deckId, position(packedId));
      const packed = await sampleBody(page);
      await openStage(page, deckId, position(filledId));
      const filled = await sampleBody(page);

      // The fixture overflows: the body is shrunk, which is what exercises the hold.
      expect(packed.fit).not.toBeNull();
      expect(packed.states).toHaveLength(1);

      // One state over every sampled frame: nothing moves once the slide is shown.
      expect(filled.states).toHaveLength(1);
      // `fill` spreads free room; a shrunk body has none, so the fit is the packed one.
      expect(filled.fit).toBe(packed.fit);
      expect(filled.released).toBe(packed.released);
      // No row is clipped under the footer block.
      expect(filled.overflow).toBeLessThanOrEqual(TOLERANCE);
    });
  }
});
