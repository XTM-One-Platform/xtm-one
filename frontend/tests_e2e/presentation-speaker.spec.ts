import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations — the speaker view and its bridge to the deck.
//
// Two windows of the same origin: the viewer plays the deck (the STAGE), the
// speaker view follows it over a `BroadcastChannel` keyed on the deck id
// (`speaker-sync.ts`). Playwright pages in one context share that origin, so
// each page here is one such window. The deck is a persisted presentation
// imported through the API from a bare spec (no LLM call), owned by the admin
// and deleted afterwards.
// ---------------------------------------------------------------------------

/**
 * Mirrors `STAGE_HEARTBEAT_MS` in `speaker-sync.ts`: waiting one heartbeat
 * plus a margin is how the test proves a heartbeat does NOT move the window.
 */
const STAGE_HEARTBEAT_MS = 4000;

const FIXTURE_SPEC = {
  title: 'E2E speaker bridge deck',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    {
      layout: 'title',
      title: 'E2E speaker bridge',
      subtitle: 'Two windows, one channel',
      notes: 'Open with the frame: the speaker window follows this deck.',
    },
    {
      layout: 'content',
      title: 'Slide two',
      blocks: [{ type: 'text', content: 'The second slide of the fixture deck.' }],
      notes: 'Say the second thing, slowly.',
    },
    {
      layout: 'content',
      title: 'Slide three',
      blocks: [{ type: 'list', items: ['One point', 'Another point'] }],
      notes: 'Say the third thing.',
    },
    { layout: 'closing', title: 'Thank you', notes: 'Close on the ask.' },
  ],
};

async function importFixtureDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-speaker-bridge.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

/** The viewer's counter button reads `3 / 4` and is named "Slide 3 of 4. Go to slide". */
async function expectStageSlide(toolbar: Locator, current: number): Promise<void> {
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${current} of \\d+\\.`) })).toBeVisible();
}

/** The speaker view's own position line under the current slide. */
function speakerSlide(speaker: Page, current: number): Locator {
  return speaker.getByText(new RegExp(`^Slide ${current} of \\d+$`));
}

async function openStage(page: Page, deckId: string): Promise<Locator> {
  await page.goto(`/presentations/${deckId}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expectStageSlide(toolbar, 1);
  return toolbar;
}

async function openSpeaker(page: Page, deckId: string): Promise<void> {
  await page.goto(`/presentations/${deckId}/speaker`);
  await expect(page.getByTestId('presentation.speaker')).toBeVisible();
}

test.describe('Presentations — speaker view bridge', () => {
  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importFixtureDeck(apiCtx, token);
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

  test('should follow a stage opened before it, drive it both ways and fall back to rehearsal', async ({
    page,
    context,
  }) => {
    const toolbar = await openStage(page, deckId);

    // Opened AFTER the stage: the window's `hello` is answered at once.
    const speaker = await context.newPage();
    await openSpeaker(speaker, deckId);
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await expect(speakerSlide(speaker, 1)).toBeVisible();

    // Stage -> speaker: a key pressed on the deck moves the window.
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, 2);
    await expect(speakerSlide(speaker, 2)).toBeVisible();

    // Speaker -> stage: the window's transport drives the deck.
    await speaker.getByTestId('presentation.speaker.next').click();
    await expectStageSlide(toolbar, 3);
    await expect(speakerSlide(speaker, 3)).toBeVisible();

    // The deck's keys work in the window too, `B` included: the room's screen
    // goes black and the window says so over the current slide.
    await speaker.keyboard.press('b');
    await expect(page.locator('.reveal.ready')).toHaveClass(/paused/);
    await expect(speaker.getByText('Screen is black')).toBeVisible();
    await speaker.keyboard.press('b');
    await expect(page.locator('.reveal.ready')).not.toHaveClass(/paused/);
    await expect(speaker.getByText('Screen is black')).toBeHidden();

    // The stage goes away (the viewer leaves the deck, so the stage unmounts
    // and says `bye`): rehearsal mode, on the slide the presenter was on — the
    // talk is not lost with the window.
    await page.getByRole('button', { name: 'Back to Presentations' }).click();
    await expect(page).toHaveURL(/\/presentations$/);
    await expect(speaker.getByText('Rehearsal - no presentation window connected')).toBeVisible();
    await expect(speakerSlide(speaker, 3)).toBeVisible();

    // Rehearsal still turns the pages, on its own position.
    await speaker.getByTestId('presentation.speaker.next').click();
    await expect(speakerSlide(speaker, 4)).toBeVisible();
  });

  test('should sync at once with a stage opened after it', async ({ page, context }) => {
    // Opened BEFORE any stage: rehearsal until one appears...
    const speaker = await context.newPage();
    await openSpeaker(speaker, deckId);
    await expect(speaker.getByText('Rehearsal - no presentation window connected')).toBeVisible();

    // ...then the stage's own `hello` puts the window on the deck's slide.
    const toolbar = await openStage(page, deckId);
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, 2);
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await expect(speakerSlide(speaker, 2)).toBeVisible();
  });

  test('should follow the stage that moved last and hand over when it leaves', async ({ page, context }) => {
    // One clock for every page of the context: a heartbeat is run, not waited out.
    await context.clock.install();
    const toolbarA = await openStage(page, deckId);
    const speaker = await context.newPage();
    await openSpeaker(speaker, deckId);
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await expect(speakerSlide(speaker, 1)).toBeVisible();

    // A second stage on the same deck (the deck open in a chat card and in the
    // viewer, say). Its arrival is a `hello`, not a move: the window stays with
    // the first stage.
    const stageB = await context.newPage();
    const toolbarB = await openStage(stageB, deckId);
    await expect(speakerSlide(speaker, 1)).toBeVisible();

    // The presenter turns the page on B: the window follows B.
    await stageB.keyboard.press('ArrowRight');
    await expectStageSlide(toolbarB, 2);
    await expect(speakerSlide(speaker, 2)).toBeVisible();

    // ...then on A: the window follows A, and B's heartbeats (still on slide
    // 2) must not pull it back.
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbarA, 3);
    await expect(speakerSlide(speaker, 3)).toBeVisible();
    // Every stage's heartbeat fires on the clock; the messages they post cross
    // to the window in real time, which the short wait leaves them.
    await context.clock.runFor(STAGE_HEARTBEAT_MS + 1500);
    await speaker.waitForTimeout(1000);
    await expect(speakerSlide(speaker, 3)).toBeVisible();

    // The window's transport drives the followed stage only.
    await speaker.getByTestId('presentation.speaker.next').click();
    await expectStageSlide(toolbarA, 4);
    await expectStageSlide(toolbarB, 2);
    await expect(speakerSlide(speaker, 4)).toBeVisible();

    // The followed stage's tab closes (a `bye` on `pagehide`; the watchdog
    // would catch a lost one within `STAGE_TIMEOUT_MS`): the other live stage
    // takes over instead of dropping the window into rehearsal.
    await page.close();
    await expect(speakerSlide(speaker, 2)).toBeVisible({ timeout: 20_000 });
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await speaker.getByTestId('presentation.speaker.next').click();
    await expectStageSlide(toolbarB, 3);
  });

  test('should hand back to the stage it just left when the new one closes within a heartbeat', async ({
    page,
    context,
  }) => {
    await openStage(page, deckId);
    const speaker = await context.newPage();
    await openSpeaker(speaker, deckId);
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await expect(speakerSlide(speaker, 1)).toBeVisible();

    // A second stage moves: the window switches to it...
    const stageB = await context.newPage();
    const toolbarB = await openStage(stageB, deckId);
    await stageB.keyboard.press('ArrowRight');
    await expectStageSlide(toolbarB, 2);
    await expect(speakerSlide(speaker, 2)).toBeVisible();

    // ...and closes at once, before the first stage's next heartbeat could
    // re-list it. The first stage is still live: the window goes back to it
    // (its slide, still following) instead of falling to rehearsal beside it.
    await stageB.close();
    await expect(speakerSlide(speaker, 1)).toBeVisible();
    await expect(speaker.getByText('Following the presentation')).toBeVisible();
    await expect(speaker.getByText('Rehearsal - no presentation window connected')).toBeHidden();
  });
});
