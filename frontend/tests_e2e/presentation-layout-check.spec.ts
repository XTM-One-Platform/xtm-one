import { test, expect, type APIRequestContext } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the layout check, end to end.
//
// `POST /presentations/{id}/layout-check` renders the saved deck in the
// platform's headless Chromium and runs `LAYOUT_AUDIT_JS` over the real DOM.
// Every backend test of the audit reads the script as text or feeds the grader
// hand-written observations; none runs the script in a browser. A syntax
// error, a selector that no longer matches, or a script that returns nothing
// would still pass them - and the product would answer `skipped` or "no
// findings" on every save. This spec runs the real script in the E2E stack's
// Chromium and asserts on what it measured:
//
// - a slide whose body cannot fit is reported (`fit`) - the script ran and
//   returned observations;
// - a logo grid that does not `fill` and ends a `fill` callout stretched to
//   the taller card of its equal-height row is measured against the card it
//   was given, not its own rows (`logo`, advisory);
// - the same grid alone in a short `fill` callout is not reported: the card
//   hugs its logos, no room is left below them;
// - the same grid in a callout that does not stretch, or followed by another
//   block, is not reported - the room below is not the logos' to fill;
// - a grid that itself `fill`s the card keeps its own field's measure, never
//   the callout remedy.
//
// The deck and its template are created through the API (no LLM call), owned
// by the admin and deleted afterwards.
// ---------------------------------------------------------------------------

/** A wordmark: letter bars on a transparent ground, deterministic per index. */
function wordmark(index: number): string {
  const colors = ['#2563EB', '#DC2626', '#059669', '#D97706'];
  let x = 0;
  let bars = '';
  for (let i = 0; i < 6; i += 1) {
    const width = 24 + ((i + index) % 3) * 4;
    bars += `<rect x="${x}" y="${10 + ((i * 7 + index) % 4) * 4}" width="${width}" height="${70 - ((i * 7 + index) % 4) * 4}" rx="4" fill="${colors[index % colors.length]}"/>`;
    x += width + 10;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${x - 10} 80" width="${x - 10}" height="80">${bars}</svg>`;
}

async function uploadLogos(apiCtx: APIRequestContext, token: string, templateId: string, count: number) {
  const items: { file_id: string; label: string }[] = [];
  for (let index = 0; index < count; index += 1) {
    const resp = await apiCtx.post(`/api/v1/template-containers/${templateId}/assets`, {
      headers: { Authorization: `Bearer ${token}` },
      params: { asset_role: 'customer_logo' },
      multipart: {
        file: { name: `wordmark-${index + 1}.svg`, mimeType: 'image/svg+xml', buffer: Buffer.from(wordmark(index), 'utf8') },
      },
    });
    if (!resp.ok()) throw new Error(`Uploading logo ${index + 1} failed: ${resp.status()} ${await resp.text()}`);
    const { id } = (await resp.json()) as { id: string };
    items.push({ file_id: id, label: `Customer ${index + 1}` });
  }
  return items;
}

const LIST = { type: 'list', items: ['First point of the card', 'Second point, a little longer'] };

function buildSlides(logos: { file_id: string; label: string }[]) {
  const grid = { type: 'logo_grid', columns: 4, items: logos };
  const callout = (fill: boolean, children: object[]) => ({
    type: 'callout',
    variant: 'card',
    header: 'inline',
    icon: 'shield-check',
    title: 'Trusted by',
    ...(fill ? { fill: true } : {}),
    children,
  });
  const content = (id: string, title: string, block: object) => ({ id, layout: 'content', title, blocks: [block] });
  // A card far taller than one row of logos: the row's cards share its height.
  const tall = callout(false, [{ type: 'list', items: Array.from({ length: 9 }, (_, i) => `Requirement ${i + 1} of the programme`) }]);
  return [
    { id: 's-title', layout: 'title', title: 'Layout check' },
    {
      id: 's-overflow',
      layout: 'content',
      title: 'Far too much to say',
      // Four lists of twelve (a list's maximum) long points: far past the body's room.
      blocks: Array.from({ length: 4 }, (_, group) => ({
        type: 'list',
        items: Array.from(
          { length: 12 },
          (_, i) => `Point ${group * 12 + i + 1}: a long sentence that wraps across the width of the slide, one of many that cannot fit`,
        ),
      })),
    },
    // An equal-height row: the logo card stretches to the list card beside it.
    content('s-callout-packed', 'Logos packed at the top of a tall card', {
      type: 'columns',
      equal_height: true,
      columns: [[callout(true, [grid])], [tall]],
    }),
    content('s-callout-short', 'Logos alone in a short fill card', callout(true, [grid])),
    content('s-callout-hug', 'Logos in a card that hugs them', callout(false, [grid])),
    content('s-callout-followed', 'Logos followed by a list in a tall card', callout(true, [grid, LIST])),
    content('s-callout-filled-grid', 'A grid that fills its tall card', callout(true, [{ ...grid, fill: true }])),
  ];
}

interface Finding {
  slide_id: string | null;
  slide_number: number;
  kind: string;
  severity: string;
  message: string;
}

interface Report {
  slide_count: number;
  skipped: string | null;
  findings: Finding[];
}

test.describe('Presentations - the layout check runs the real audit', () => {
  let deckId = '';
  let templateId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E layout check ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      const logos = await uploadLogos(apiCtx, token, templateId, 4);
      const shell = { title: 'E2E layout check', theme: 'light', aspect_ratio: '16:9', transition: 'none' };
      deckId = await importDeckOnTemplate(
        apiCtx,
        token,
        { ...shell, slides: [{ id: 's-title', layout: 'title', title: 'Layout check' }] },
        templateName,
        'e2e-layout-check.json',
      );
      // The logos are template assets: a deck may name them once it wears the template.
      const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      const { revision } = (await deck.json()) as { revision: number };
      const patched = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { slides: buildSlides(logos), expected_revision: revision },
      });
      if (!patched.ok()) throw new Error(`Writing the fixture slides failed: ${patched.status()} ${await patched.text()}`);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      if (deckId) await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
    await deleteTemplate(templateId);
  });

  test('should measure the rendered deck and name what the audience would see go wrong', async () => {
    // A server-side render of every slide through the platform's headless browser.
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let report: Report;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      report = (await response.json()) as Report;
    } finally {
      await apiCtx.dispose();
    }
    const dump = JSON.stringify(report, null, 1);
    // A script that throws, or a browser that is missing, is a skipped check - never a pass.
    expect(report.skipped, dump).toBeNull();
    expect(report.slide_count, dump).toBe(7);
    const on = (slideId: string) => report.findings.filter((finding) => finding.slide_id === slideId);

    // The script ran and returned observations: the overloaded body is reported.
    expect(on('s-overflow').map((finding) => finding.kind), dump).toContain('fit');

    // A packed grid ending a stretched card is measured against the card.
    const packed = on('s-callout-packed').filter((finding) => finding.kind === 'logo');
    expect(packed, dump).toHaveLength(1);
    expect(packed[0].severity, dump).toBe('advisory');
    expect(packed[0].message, dump).toContain('set fill on the logo grid');

    // A short `fill` card alone in the body hugs its logos: nothing below to fill.
    expect(on('s-callout-short').filter((finding) => finding.kind === 'logo'), dump).toEqual([]);

    // No new finding where the room below is not the logos' to fill.
    expect(on('s-callout-hug').filter((finding) => finding.kind === 'logo'), dump).toEqual([]);
    expect(on('s-callout-followed').filter((finding) => finding.kind === 'logo'), dump).toEqual([]);

    // A grid that fills the card already spreads over it: it keeps its own field's
    // measure and remedy, never the callout one (it may still be flagged by it).
    const filled = on('s-callout-filled-grid').filter((finding) => finding.kind === 'logo');
    expect(filled.filter((finding) => finding.message.includes('set fill on the logo grid')), dump).toEqual([]);
  });
});
