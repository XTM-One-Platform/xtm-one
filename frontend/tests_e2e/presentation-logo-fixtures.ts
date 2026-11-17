import { expect, type APIRequestContext, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Synthetic logos for the presentation logo specs.
//
// Wordmarks drawn as letter bars (ratio >= 3, so they sit at exactly the
// target height), seals, badges and combos around them, each padded with
// transparent margin so the trim has work to do. Uploaded as template assets
// of a private template, so a deck on it may place them.
// ---------------------------------------------------------------------------

const PALETTE = ['#2563EB', '#DC2626', '#059669', '#D97706', '#7C3AED', '#0891B2', '#DB2777'];

export type LogoKind = 'wordmark' | 'combo' | 'seal' | 'badge';

const KINDS: LogoKind[] = ['wordmark', 'wordmark', 'combo', 'seal', 'wordmark', 'badge', 'combo', 'wordmark'];

/** A padded SVG: the drawing sits in a margin of `pad` of its size on every side. */
function padded(width: number, height: number, pad: number, body: string): string {
  const totalW = width * (1 + 2 * pad);
  const totalH = height * (1 + 2 * pad);
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${totalW} ${totalH}" width="${totalW}" height="${totalH}">` +
    `<g transform="translate(${width * pad} ${height * pad})">${body}</g></svg>`
  );
}

/** Letter bars from `x0`, deterministic for `seed`; returns the bars and their right edge. */
function letters(count: number, x0: number, color: string, seed: number): [string, number] {
  let x = x0;
  let bars = '';
  for (let i = 0; i < count; i += 1) {
    const width = 24 + ((i * 5 + seed) % 4) * 3;
    const height = 80 * (0.75 + 0.25 * (((i * 7 + seed) % 5) / 4));
    bars += `<rect x="${x}" y="${80 - height}" width="${width}" height="${height}" rx="4" fill="${color}"/>`;
    x += width + 9;
  }
  return [bars, x - 9];
}

export function logoSvg(kind: LogoKind, index: number): string {
  const color = PALETTE[index % PALETTE.length];
  const pad = 0.1 + (index % 4) * 0.08;
  switch (kind) {
    case 'wordmark': {
      const [bars, right] = letters(6 + (index % 2), 0, color, index);
      return padded(right, 80, pad, bars);
    }
    case 'combo': {
      const [bars, right] = letters(4, 100, color, index);
      return padded(right, 80, pad, `<circle cx="40" cy="40" r="40" fill="${color}"/>${bars}`);
    }
    case 'seal':
      return padded(
        120,
        120,
        pad,
        `<circle cx="60" cy="60" r="56" fill="none" stroke="${color}" stroke-width="8"/>` +
          `<circle cx="60" cy="60" r="34" fill="${color}"/>`,
      );
    case 'badge':
      return padded(
        120,
        120,
        pad,
        `<path fill-rule="evenodd" fill="${color}" d="M16 0h88a16 16 0 0 1 16 16v88a16 16 0 0 1-16 16H16` +
          `a16 16 0 0 1-16-16V16A16 16 0 0 1 16 0zM36 36v48h48V36z"/>`,
      );
  }
}

export type Logo = { file_id: string; kind: LogoKind; label: string };

/** `count` logos uploaded to the template, their kinds cycling through `kinds` (the mixed set by default). */
export async function uploadLogos(
  apiCtx: APIRequestContext,
  token: string,
  templateId: string,
  count: number,
  kinds: LogoKind[] = KINDS,
): Promise<Logo[]> {
  const logos: Logo[] = [];
  for (let index = 0; index < count; index += 1) {
    const kind = kinds[index % kinds.length];
    const label = `${kind} ${index + 1}`;
    const resp = await apiCtx.post(`/api/v1/template-containers/${templateId}/assets`, {
      headers: { Authorization: `Bearer ${token}` },
      params: { asset_role: 'customer_logo' },
      multipart: {
        file: { name: `${kind}-${index + 1}.svg`, mimeType: 'image/svg+xml', buffer: Buffer.from(logoSvg(kind, index), 'utf8') },
      },
    });
    if (!resp.ok()) throw new Error(`Uploading logo ${index + 1} failed: ${resp.status()} ${await resp.text()}`);
    const { id } = (await resp.json()) as { id: string };
    logos.push({ file_id: id, kind, label });
  }
  return logos;
}

export const logoItems = (logos: Logo[]) => logos.map(({ file_id, label }) => ({ file_id, label }));

/** The viewer on slide `slide` (1-based), every logo decoded and sized: the export waits on the same attribute. */
export async function openStage(page: Page, deckId: string, slide: number) {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
  const present = page.locator('.reveal .slides > section.present');
  await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
  return present;
}
