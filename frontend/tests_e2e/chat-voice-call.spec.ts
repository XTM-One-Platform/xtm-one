import { test, expect, type Page } from '@playwright/test';
import { withAdmin } from './helpers';
import { ChatPage } from './model/chat.page';

/**
 * A voice call on /chat is the chat panel's own (#6291): started from the
 * composer's toolbar, it listens, sends a phrase after a second of silence,
 * reads the answer aloud through the platform's voice (`/api/v1/chat/tts`)
 * and stops speaking when the user talks over it.
 *
 * The browser's speech recognition is a stand-in the spec speaks through
 * (`__say`), playback is stubbed (it starts and never ends, so the call stays
 * speaking until something pauses it), and the turn is answered here with one
 * `done` frame: no model, no microphone.
 */

const ANSWER = 'You have two meetings today, both after lunch.';

/** Speech recognition the spec controls, and audio that plays until paused. */
async function fakeVoice(page: Page): Promise<void> {
  await page.addInitScript(() => {
    type Handler = ((event: unknown) => void) | null;
    class FakeRecognition {
      continuous = false;
      interimResults = false;
      lang = '';
      running = false;
      onresult: Handler = null;
      onend: Handler = null;
      onerror: Handler = null;
      onstart: Handler = null;
      constructor() {
        const w = window as unknown as { __recognitions: FakeRecognition[] };
        (w.__recognitions ??= []).push(this);
      }
      start() {
        this.running = true;
        this.onstart?.({});
      }
      stop() {
        if (!this.running) return;
        this.running = false;
        setTimeout(() => this.onend?.({}), 0);
      }
      abort() {
        this.stop();
      }
    }
    const w = window as unknown as Record<string, unknown>;
    w.SpeechRecognition = FakeRecognition;
    w.webkitSpeechRecognition = FakeRecognition;
    w.__say = (text: string) => {
      const all = (w.__recognitions as FakeRecognition[] | undefined) ?? [];
      const rec = [...all].reverse().find((r) => r.running);
      if (!rec) return false;
      const result = Object.assign([{ transcript: text, confidence: 1 }], { isFinal: true });
      rec.onresult?.({ resultIndex: 0, results: [result] });
      return true;
    };
    const audio = { plays: 0, pauses: 0 };
    w.__audio = audio;
    HTMLMediaElement.prototype.play = function play() {
      audio.plays += 1;
      return Promise.resolve();
    };
    HTMLMediaElement.prototype.pause = function pause() {
      audio.pauses += 1;
    };
  });
}

const listening = (page: Page) =>
  page.evaluate(() =>
    ((window as unknown as { __recognitions?: { running: boolean }[] }).__recognitions ?? []).some((r) => r.running),
  );
const say = (page: Page, text: string) => page.evaluate((t) => (window as unknown as { __say: (t: string) => boolean }).__say(t), text);
const audio = (page: Page) => page.evaluate(() => (window as unknown as { __audio: { plays: number; pauses: number } }).__audio);

test.describe('Chat - a voice call on the panel (#6291)', () => {
  const conversations: string[] = [];

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of conversations) await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
    }).catch(() => undefined);
  });

  test('a spoken phrase is sent, its answer is read aloud, and speaking over it stops the voice', async ({ page }) => {
    await fakeVoice(page);
    const sent: Record<string, unknown>[] = [];
    await page.route(
      (url) => /\/api\/v1\/web\/chat\/conversations\/[^/]+\/events$/.test(url.pathname),
      () => new Promise<void>(() => {}),
    );
    await page.route(
      (url) => url.pathname === '/api/v1/web/chat/messages',
      async (route) => {
        sent.push(route.request().postDataJSON() as Record<string, unknown>);
        await route.fulfill({
          status: 200,
          headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
          body: `data: ${JSON.stringify({ type: 'done', content: ANSWER })}\n\n`,
        });
      },
    );
    const spoken: string[] = [];
    await page.route(
      (url) => url.pathname === '/api/v1/chat/tts',
      async (route) => {
        spoken.push(String((route.request().postDataJSON() as { text?: string }).text ?? ''));
        await route.fulfill({ status: 200, headers: { 'Content-Type': 'audio/mpeg' }, body: Buffer.from('ID3fake-audio') });
      },
    );
    page.on('response', async (r) => {
      if (r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/web/chat/sessions' && r.ok()) {
        const id = ((await r.json().catch(() => ({}))) as { conversation_id?: string }).conversation_id;
        if (id && !conversations.includes(id)) conversations.push(id);
      }
    });

    const chat = new ChatPage(page);
    await chat.goto();
    await chat.clickNewConversation();
    await chat.waitForComposer();

    await page.getByTestId('chat.voice-call.start').click();
    await expect(page.getByTestId('filigran-chat-voice-call')).toBeVisible();
    await expect.poll(() => listening(page)).toBe(true);

    // A phrase, then a second of silence: it is sent as the user's message.
    expect(await say(page, 'what is on my agenda today')).toBe(true);
    await expect.poll(() => sent.length, { timeout: 10_000 }).toBe(1);
    expect(sent[0].content).toBe('what is on my agenda today');

    // The answer is read through the platform's voice, and plays.
    await expect.poll(() => spoken.join(' '), { timeout: 10_000 }).toContain('two meetings');
    await expect.poll(async () => (await audio(page)).plays, { timeout: 10_000 }).toBeGreaterThan(0);
    await expect(chat.transcript().getByText(ANSWER)).toBeVisible();

    // Talking over it takes the turn back: the voice stops.
    const before = (await audio(page)).pauses;
    await expect.poll(() => listening(page)).toBe(true);
    expect(await say(page, 'stop, I have another question instead')).toBe(true);
    await expect.poll(async () => (await audio(page)).pauses, { timeout: 5_000 }).toBeGreaterThan(before);
  });
});
