import { test, expect, type APIRequestContext } from '@playwright/test';
import { IDLE_HIDE_MS } from '../src/components/presentation/deck-idle';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';

// ---------------------------------------------------------------------------
// A poll controlled from ONE screen in fullscreen present mode (xtm-one #10235):
// the presenter shares a single screen, with no speaker view and no bar under
// the slide. A bar along the bottom edge shows on pointer activity and hides
// when the stage goes idle; L / V / Shift+R / N / P drive the poll without
// leaving fullscreen; the arrows still move slides. An autonomous poll
// (layout all) needs none of it; a guided one (layout active) still steps.
// ---------------------------------------------------------------------------

const IDLE_SLACK_MS = 1500;

function deckSpec(layout: 'active' | 'all') {
  return {
    title: `E2E poll present ${layout}`,
    theme: 'dark',
    aspect_ratio: '16:9',
    slides: [
      {
        layout: 'content',
        title: 'Poll',
        blocks: [
          {
            type: 'poll',
            id: 'e2epoll',
            layout,
            questions: [
              { id: 'q1', kind: 'choice', prompt: 'Colour', options: ['Red', 'Blue'] },
              { id: 'q2', kind: 'quiz', prompt: 'Two plus two', options: ['3', '4'], correct: 1 },
              { id: 'q3', kind: 'choice', prompt: 'Size', options: ['S', 'L'] },
            ],
          },
        ],
      },
      { layout: 'title', title: 'After the poll' },
    ],
  };
}

async function withAdmin<T>(run: (apiCtx: APIRequestContext, token: string) => Promise<T>): Promise<T> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    return await run(apiCtx, token);
  } finally {
    await apiCtx.dispose();
  }
}

type PollState = { poll_id: string; locked: boolean; revealed: boolean; active_question: string | null };

async function pollState(deckId: string): Promise<PollState> {
  return withAdmin(async (apiCtx, token) => {
    const resp = await apiCtx.get(`/api/v1/presentations/${deckId}/polls`, { headers: { Authorization: `Bearer ${token}` } });
    const body = (await resp.json()) as { polls: PollState[] };
    return body.polls.find((p) => p.poll_id === 'e2epoll') as PollState;
  });
}

for (const layout of ['all', 'active'] as const) {
  test(`fullscreen present mode drives a ${layout === 'all' ? 'autonomous' : 'guided'} poll from one screen`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const templateName = `E2E poll present ${layout} ${Date.now()}`;
    const { templateId, deckId } = await withAdmin(async (apiCtx, token) => {
      const template = await createTwoSchemeTemplate(apiCtx, token, templateName);
      const deck = await importDeckOnTemplate(apiCtx, token, deckSpec(layout), templateName, `e2e-poll-${layout}.json`);
      return { templateId: template, deckId: deck };
    });
    try {
      await page.clock.install();
      await page.goto(`/presentations/${deckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
      const stage = page.locator('.deck-stage').first();
      await toolbar.getByTestId('presentation.present').click();
      await expect(stage).toHaveAttribute('data-deck-fullscreen', /^(native|viewport)$/);

      const bar = stage.getByTestId('presentation.poll-present-bar');
      // Hover near the bottom edge: the bar is up.
      const viewport = page.viewportSize() ?? { width: 1280, height: 720 };
      await page.mouse.move(viewport.width / 2, viewport.height - 20);
      await expect(bar).toBeVisible();
      // Nobody touches the laptop: the bar goes, nothing of it stays on the slide.
      await page.mouse.move(viewport.width / 2, viewport.height / 2);
      await page.clock.fastForward(IDLE_HIDE_MS);
      await expect(stage).toHaveAttribute('data-deck-idle', 'true', { timeout: IDLE_SLACK_MS });
      await expect(bar).toHaveCSS('opacity', '0');

      // L locks, with a toast, without leaving fullscreen.
      await page.keyboard.press('l');
      await expect(stage.getByTestId('presentation.poll-toast')).toHaveText('Voting locked');
      await expect.poll(async () => (await pollState(deckId)).locked).toBe(true);
      // V reveals the quiz answer.
      await page.keyboard.press('v');
      await expect.poll(async () => (await pollState(deckId)).revealed).toBe(true);
      // Shift+R asks; Esc cancels, Enter resets (a reset reopens voting).
      await page.keyboard.press('Shift+R');
      await expect(bar).toHaveAttribute('data-confirm', 'true');
      await page.keyboard.press('Escape');
      await expect(bar).not.toHaveAttribute('data-confirm', 'true');
      await expect(stage).toHaveAttribute('data-deck-fullscreen', /^(native|viewport)$/);
      await page.keyboard.press('Shift+R');
      await page.keyboard.press('Enter');
      await expect.poll(async () => (await pollState(deckId)).locked).toBe(false);

      if (layout === 'active') {
        // A guided poll steps with N and P.
        await page.keyboard.press('n');
        await expect.poll(async () => (await pollState(deckId)).active_question).toBe('q2');
        // P steps back from the question the screen shows: wait for the bar to read q2 first.
        await expect(bar).toContainText('Question 2 of 3');
        await page.keyboard.press('p');
        await expect.poll(async () => (await pollState(deckId)).active_question).toBe('q1');
      } else {
        // An autonomous poll has no question to step to and no outline on the slide.
        await page.keyboard.press('n');
        await expect.poll(async () => (await pollState(deckId)).active_question).toBeNull();
        await expect(stage.locator('.deck-poll-cell[data-open]')).toHaveCount(0);
      }

      // The arrows still move slides.
      await page.keyboard.press('ArrowRight');
      // The heading, not the aria-live status that announces the same title.
      await expect(stage.getByRole('heading', { name: 'After the poll' })).toBeVisible();
      await expect(bar).toHaveCount(0);
    } finally {
      await withAdmin((apiCtx, token) =>
        apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } }),
      );
      await deleteTemplate(templateId);
    }
  });
}
