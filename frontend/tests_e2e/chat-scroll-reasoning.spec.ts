import { test, expect, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { ChatPage } from './model/chat.page';

// ---------------------------------------------------------------------------
// The reader scrolls up while the agent reasons, on the chat
// panel. The reasoning streamed under the status bubble must not pull the
// transcript back to its end - after a wheel tick or an arrow key, nor after
// PageUp or Home - and coming back to the end (End, a scroll down) follows it
// again. A press on the scrollbar is left to the package's scrollFollow.test.ts:
// headless Chromium draws no scrollbar to press.
// ---------------------------------------------------------------------------

// Chromium animates a key or wheel scroll on its frame clock, and on a loaded
// runner a reasoning chunk's layout cut a Home animation short with the view
// still part-way up (28 to 382 px, #11383): browser timing, not the follow
// this spec checks. Without smooth scrolling a key or a wheel tick lands in
// one step, so each position read is the one the gesture asked for.
test.use({ launchOptions: { args: ['--disable-smooth-scrolling'] } });

/** The message route the panel streams a turn from (`POST /api/v1/web/chat/messages`, SSE). */
const MESSAGES_PATH = '/api/v1/web/chat/messages';

/**
 * The message stream as the test drives it. `page.route` answers a request
 * with one finished body, and this turn must stream for as long as the test
 * scrolls: the page's own `fetch` answers the send with an SSE body the test
 * writes to (`window.__chatStream`), and the turn is left open. Every other
 * request reaches the stack.
 */
async function scriptedMessageStream(page: Page): Promise<void> {
  await page.addInitScript((messagesPath) => {
    type Frame = Record<string, unknown>;
    const encoder = new TextEncoder();
    let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
    let reasoningTimer: number | null = null;
    const write = (frame: Frame) => {
      try {
        controller?.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      } catch {
        // The panel let the stream go: nothing left to write to.
      }
    };
    const stream = {
      sent: false,
      /** The reasoning chunks written so far: the test counts chunks, not milliseconds. */
      steps: 0,
      push: write,
      /** A line of reasoning every 100 ms until stopped. */
      startReasoning() {
        reasoningTimer = window.setInterval(() => {
          stream.steps += 1;
          write({ type: 'status', status: 'thinking_text', content: `Reasoning step ${stream.steps}.\n` });
        }, 100);
      },
      stopReasoning() {
        if (reasoningTimer !== null) window.clearInterval(reasoningTimer);
        reasoningTimer = null;
      },
    };
    (window as unknown as { __chatStream: typeof stream }).__chatStream = stream;
    const realFetch = window.fetch.bind(window);
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), window.location.href);
      const method = (init?.method ?? request?.method ?? 'GET').toUpperCase();
      if (method !== 'POST' || !url.pathname.endsWith(messagesPath)) return realFetch(input, init);
      stream.sent = true;
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          controller = c;
          write({ type: 'status', status: 'thinking' });
        },
      });
      return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    };
  }, MESSAGES_PATH);
}

type ChatStreamHandle = {
  sent: boolean;
  steps: number;
  push: (frame: Record<string, unknown>) => void;
  startReasoning: () => void;
  stopReasoning: () => void;
};

// The stream the page's `fetch` answered the send with (`scriptedMessageStream`),
// driven in the page: each helper runs there, so it names `window` itself.
const sendWasStreamed = (page: Page) =>
  page.evaluate(() => (window as unknown as { __chatStream: ChatStreamHandle }).__chatStream.sent);
const startReasoning = (page: Page) =>
  page.evaluate(() => (window as unknown as { __chatStream: ChatStreamHandle }).__chatStream.startReasoning());
const stopReasoning = (page: Page) =>
  page.evaluate(() => (window as unknown as { __chatStream: ChatStreamHandle }).__chatStream.stopReasoning());
const pushFrame = (page: Page, frame: Record<string, unknown>) =>
  page.evaluate((f) => (window as unknown as { __chatStream: ChatStreamHandle }).__chatStream.push(f), frame);
const reasoningSteps = (page: Page) =>
  page.evaluate(() => (window as unknown as { __chatStream: ChatStreamHandle }).__chatStream.steps);

/** Waits until `count` more reasoning chunks have been written, however slow the runner is. */
async function chunksLater(page: Page, count: number): Promise<void> {
  const from = await reasoningSteps(page);
  await expect.poll(() => reasoningSteps(page), { timeout: 15_000 }).toBeGreaterThanOrEqual(from + count);
}

function distanceFromEnd(scroller: Locator): Promise<number> {
  return scroller.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
}

/** Ten reasoning chunks later, the reader is still at least `min` px above the end. */
async function expectStaysUp(page: Page, scroller: Locator, min: number): Promise<void> {
  await chunksLater(page, 10);
  await expect.poll(() => distanceFromEnd(scroller), { timeout: 15_000 }).toBeGreaterThanOrEqual(min);
}

/**
 * Home reaches the top and ten reasoning chunks later it is still there.
 * The top is awaited, never read after a fixed delay: the scroll event and
 * the follow it ends land a frame or more after the key.
 */
async function expectStaysAtTop(page: Page, scroller: Locator): Promise<void> {
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeLessThanOrEqual(2);
  await chunksLater(page, 10);
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop), { timeout: 15_000 }).toBeLessThanOrEqual(2);
}

async function expectFollowsEnd(scroller: Locator): Promise<void> {
  await expect.poll(() => distanceFromEnd(scroller)).toBeLessThanOrEqual(2);
}

/** Back down to the end by a scroll of the reader's own. */
async function scrollBackDown(scroller: Locator): Promise<void> {
  await scroller.evaluate((el) => el.scrollTo({ top: el.scrollHeight }));
  await expectFollowsEnd(scroller);
}

/** One wheel tick up (60 px) with the pointer `fromTop` of the way down `target`. */
async function wheelUpOver(page: Page, target: Locator, fromTop = 0.5): Promise<void> {
  const box = (await target.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height * fromTop);
  await page.mouse.wheel(0, -60);
}

test.describe('Chat - the reader scrolls up while the agent reasons', () => {
  const conversationIds: string[] = [];

  test.afterAll(async () => {
    if (conversationIds.length === 0) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      for (const id of conversationIds) {
        await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers: { Authorization: `Bearer ${token}` } });
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('scrolling up by wheel or key is not undone by the next chunk, and the end is followed again', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1280, height: 640 });
    await scriptedMessageStream(page);
    const chat = new ChatPage(page);
    await chat.goto();
    await chat.waitForComposer();
    // A question long enough for the transcript to scroll by its text alone:
    // the panel's question bubble does not keep its line breaks.
    await chat.composer().fill(
      Array.from({ length: 200 }, (_, i) => `Paragraph ${i + 1} of a long question.`).join('\n\n'),
    );
    await chat.sendButton().click();
    await expect.poll(() => sendWasStreamed(page), { timeout: 15_000 }).toBe(true);
    await expect.poll(() => new URL(page.url()).searchParams.get('c'), { timeout: 15_000 }).not.toBeNull();
    conversationIds.push(new URL(page.url()).searchParams.get('c')!);

    await startReasoning(page);
    try {
      const scroller = chat.transcript();
      // The clipped window, not the text it holds: the text runs above it.
      const reasoningWindow = scroller.getByText(/Reasoning step \d+\./).first().locator('..');
      await expect(scroller.getByText(/Reasoning step 3\./)).toBeVisible({ timeout: 15_000 });
      await expect.poll(
        () => scroller.evaluate((el) => el.scrollHeight - el.clientHeight),
        { timeout: 15_000 },
      ).toBeGreaterThan(300);
      // The turn opens at its end, and follows it.
      await expectFollowsEnd(scroller);

      // One wheel tick up over the question: 60 px, a small move up.
      await wheelUpOver(page, scroller, 0.3);
      await expectStaysUp(page, scroller, 30);
      await scrollBackDown(scroller);

      // The same over the reasoning window, which clips its text but does not scroll.
      await wheelUpOver(page, reasoningWindow);
      await expectStaysUp(page, scroller, 30);
      await scrollBackDown(scroller);

      // The keyboard: the transcript takes the focus. One arrow up (40 px),
      // then a page, then the top; End brings the follow back.
      await scroller.focus();
      await page.keyboard.press('ArrowUp');
      await expectStaysUp(page, scroller, 20);
      await page.keyboard.press('End');
      await expectFollowsEnd(scroller);
      await page.keyboard.press('PageUp');
      await expectStaysUp(page, scroller, 100);
      await page.keyboard.press('End');
      await expectFollowsEnd(scroller);
      await page.keyboard.press('Home');
      await expectStaysAtTop(page, scroller);
      await page.keyboard.press('End');
      await expectFollowsEnd(scroller);

      // The answer streaming in below is followed to its last line.
      await stopReasoning(page);
      for (let line = 1; line <= 30; line += 1) {
        await pushFrame(page, { type: 'stream', content: `Answer paragraph ${line}.\n\n` });
        await page.waitForTimeout(30);
      }
      await expect(scroller.getByText('Answer paragraph 30.')).toBeVisible({ timeout: 15_000 });
      await expect.poll(() => distanceFromEnd(scroller), { timeout: 10_000 }).toBeLessThanOrEqual(2);
    } finally {
      await stopReasoning(page).catch(() => undefined);
    }
  });
});
