import { readdirSync } from 'node:fs';
import path from 'node:path';
import { test, expect, type Page } from '@playwright/test';
import { IntegrationPage } from './model/integration.page';
import { E2E_DIR } from './shards';

// ---------------------------------------------------------------------------
// Provider logos read on both themes. The logos under public/logos/
// are drawn for the dark UI; the rule in src/index.css darkens, in light mode
// only, the ones drawn white or pale on a transparent background. Measured
// for EVERY file on the pixels the browser draws, against the light theme's
// card (white):
//  - a logo the rule treats is light as drawn - a quarter of it or more
//    vanishes on white (treating a dark mark would make IT vanish) - and once
//    treated, at most a tenth of it still does;
//  - a logo the rule leaves alone is never almost wholly unseen on white with
//    nothing dark in it: a new white mark has to be listed;
//  - the rule applies in light mode only, never in dark mode and never inside
//    a forced-dark subtree (`data-mode="dark"`), and nothing on the catalog
//    page overrides it.
// ---------------------------------------------------------------------------

const LOGOS = readdirSync(path.join(E2E_DIR, '..', 'public', 'logos'))
  .filter((file) => /\.(png|svg)$/.test(file))
  .sort();

/** Below this contrast with white a pixel vanishes into the light card. */
const UNSEEN_BELOW = 1.5;
/** At this contrast with white a pixel reads. */
const INK_FROM = 3;

interface Reading {
  file: string;
  /** The computed `filter` in light mode, in dark mode, and in a forced-dark subtree of a light page. */
  light: string;
  dark: string;
  forcedDark: string;
  /** Shares of the logo's opaque pixels: under UNSEEN_BELOW on white as drawn, the same once the light-mode filter applies, and at INK_FROM or more as drawn. */
  unseen: number;
  unseenTreated: number;
  ink: number;
}

async function readLogos(page: Page, files: string[]): Promise<Reading[]> {
  return page.evaluate(
    async ({ files, unseenBelow, inkFrom }) => {
      const root = document.documentElement;
      const mode = root.dataset.mode;
      const base = document.querySelector<HTMLMetaElement>('meta[name="base-path"]')?.content ?? '';
      const host = document.createElement('div');
      host.style.cssText = 'position:fixed;left:-10000px;top:0';
      const forced = document.createElement('div');
      forced.dataset.mode = 'dark';
      host.append(forced);
      document.body.append(host);

      const channel = (v: number) => {
        const n = v / 255;
        return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
      };
      const contrastOnWhite = (r: number, g: number, b: number) =>
        1.05 / (0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b) + 0.05);
      const measure = (img: HTMLImageElement, filter: string) => {
        const w = img.naturalWidth || 128;
        const h = img.naturalHeight || 128;
        const scale = Math.min(128 / w, 128 / h);
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
        ctx.filter = filter;
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let opaque = 0;
        let unseen = 0;
        let ink = 0;
        for (let i = 0; i < data.length; i += 4) {
          const a = data[i + 3] / 255;
          if (a < 0.5) continue;
          opaque += 1;
          // Composited on the white card, the way the screen shows an anti-aliased edge.
          const contrast = contrastOnWhite(
            a * data[i] + (1 - a) * 255,
            a * data[i + 1] + (1 - a) * 255,
            a * data[i + 2] + (1 - a) * 255,
          );
          if (contrast < unseenBelow) unseen += 1;
          if (contrast >= inkFrom) ink += 1;
        }
        return { unseen: opaque ? unseen / opaque : 0, ink: opaque ? ink / opaque : 0 };
      };

      try {
        const logos = files.map((file) => {
          const img = new Image();
          img.src = `${base}/logos/${file}`;
          const twin = new Image();
          twin.src = img.src;
          host.append(img);
          forced.append(twin);
          return { file, img, twin };
        });
        // Named, as the sequential decode was: `decode()` alone rejects with no file.
        await Promise.all(
          logos.map(({ file, img }) =>
            img.decode().catch((error: unknown) => {
              throw new Error(`${file}: ${error instanceof Error ? error.message : String(error)}`);
            }),
          ),
        );
        // Each mode switch restyles the whole page: switch once and read every
        // logo, rather than switching twice per logo.
        root.dataset.mode = 'light';
        const inLight = logos.map(({ img, twin }) => ({
          light: getComputedStyle(img).filter,
          forcedDark: getComputedStyle(twin).filter,
        }));
        delete root.dataset.mode;
        const dark = logos.map(({ img }) => getComputedStyle(img).filter);
        return logos.map(({ file, img }, index) => {
          const { light, forcedDark } = inLight[index];
          const drawn = measure(img, 'none');
          const treated = measure(img, light);
          return {
            file,
            light,
            dark: dark[index],
            forcedDark,
            unseen: drawn.unseen,
            unseenTreated: treated.unseen,
            ink: drawn.ink,
          };
        });
      } finally {
        if (mode) root.dataset.mode = mode;
        else delete root.dataset.mode;
        host.remove();
      }
    },
    { files, unseenBelow: UNSEEN_BELOW, inkFrom: INK_FROM },
  );
}

const percent = (share: number) => `${Math.round(share * 100)}%`;

test.describe('Provider logos - legible on the light theme', () => {
  test('the light-mode rule treats the light logos, only them, and only in light mode', async ({ page }) => {
    // Any page of the app: the logos and index.css are served with it.
    await new IntegrationPage(page).gotoList();
    const readings = await readLogos(page, LOGOS);
    expect(readings).toHaveLength(LOGOS.length);

    const failures: string[] = [];
    for (const r of readings) {
      if (r.dark !== 'none' || r.forcedDark !== 'none') {
        failures.push(`${r.file}: filtered outside light mode (dark: ${r.dark}, forced-dark subtree: ${r.forcedDark})`);
      }
      if (r.light !== 'none') {
        if (r.unseen < 0.25) {
          failures.push(`${r.file}: treated, but only ${percent(r.unseen)} of it vanishes on white - it is drawn dark, and inverting it hides it`);
        }
        if (r.unseenTreated > 0.1) {
          failures.push(`${r.file}: ${percent(r.unseenTreated)} of it still vanishes on white once treated`);
        }
      } else if (r.unseen >= 0.9 && r.ink < 0.02) {
        failures.push(`${r.file}: ${percent(r.unseen)} of it vanishes on white and nothing in it is dark - list it in the light-theme logo rule of src/index.css`);
      }
    }
    expect(failures).toEqual([]);
    // The rule is live, not a selector list that matches nothing.
    expect(readings.filter((r) => r.light !== 'none').map((r) => r.file)).toContain('github.svg');
  });

  test('a catalog card draws the treated logo in light mode and the logo as drawn in dark mode', async ({ page }) => {
    await new IntegrationPage(page).gotoList();
    const logo = page.getByTestId('provider-card').locator('img[src$="/logos/github.svg"]').first();
    await expect(logo).toBeVisible();
    const filterIn = (mode: 'light' | null) =>
      logo.evaluate((img, m) => {
        const root = document.documentElement;
        const before = root.dataset.mode;
        if (m) root.dataset.mode = m;
        else delete root.dataset.mode;
        const filter = getComputedStyle(img).filter;
        if (before) root.dataset.mode = before;
        else delete root.dataset.mode;
        return filter;
      }, mode);
    expect(await filterIn('light')).toContain('invert(1)');
    expect(await filterIn(null)).toBe('none');
  });
});
