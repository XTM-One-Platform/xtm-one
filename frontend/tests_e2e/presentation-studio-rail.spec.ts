import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the studio rail is the shared chat panel, bound to the deck
// on the stage (#3358, #6291).
//
// What is the studio's own, around the panel: the picker lists the agents
// that carry the deck tool (`GET /web/chat/agents?studio=presentation`), the
// conversation the rail starts is bound to the deck by the session POST that
// creates it (`?presentation_id=`), and every message says which slide is on
// the stage (`studio_slide`). A file pasted in the rail goes up through the
// web chat's upload route and leaves with the message.
//
// The message route is answered here with one `done` frame: no model is
// called. The deck and the agent are created through the API and deleted
// afterwards, with the conversations the rail started.
// ---------------------------------------------------------------------------

const FIXTURE_SPEC = {
  title: 'E2E studio rail deck',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'Studio rail', subtitle: 'Bound to the deck' },
    { layout: 'closing', title: 'Thank you' },
  ],
};

async function importFixtureDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-studio-rail.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  return ((await resp.json()) as { id: string }).id;
}

/** An agent the studio offers: one that carries the deck tool. */
async function createDeckAgent(apiCtx: APIRequestContext, token: string, name: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/agents', {
    headers: { Authorization: `Bearer ${token}` },
    data: { name, persona: 'You edit presentations.', builtin_tool_ids: ['generate_presentation'] },
  });
  if (!resp.ok()) throw new Error(`Creating the deck agent failed: ${resp.status()} ${await resp.text()}`);
  return ((await resp.json()) as { id: string }).id;
}

/** Answer every send with one final frame, and keep the conversation's event stream open and quiet. */
async function answerSends(page: Page, sent: Record<string, unknown>[]): Promise<void> {
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
        body: 'data: {"type":"done","content":"Noted, slide by slide."}\n\n',
      });
    },
  );
}

/** Paste a screenshot into the composer, as the clipboard hands one over. */
async function pasteScreenshot(composer: Locator): Promise<void> {
  await composer.evaluate(async (textarea) => {
    const canvas = document.createElement('canvas');
    canvas.width = 320;
    canvas.height = 160;
    const ctx = canvas.getContext('2d')!;
    ctx.fillStyle = '#0fbcff';
    ctx.fillRect(0, 0, 320, 160);
    const blob = await new Promise<Blob>((resolve) => canvas.toBlob((b) => resolve(b!), 'image/png'));
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File([blob], 'image.png', { type: 'image/png' }));
    (textarea as HTMLTextAreaElement).focus();
    textarea.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
  });
}

test.describe('Presentations - the studio rail is the chat panel, bound to the deck (#6291)', () => {
  let deckId = '';
  let agentId = '';
  const agentName = `E2E studio rail ${Date.now()}`;
  const conversationIds: string[] = [];

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importFixtureDeck(apiCtx, token);
      agentId = await createDeckAgent(apiCtx, token, agentName);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    try {
      for (const id of conversationIds) await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      if (deckId) await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers });
      if (agentId) await apiCtx.delete(`/api/v1/agents/${agentId}`, { headers });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('the picker lists the deck agents, and a message starts a conversation bound to the deck', async ({ page }) => {
    const sent: Record<string, unknown>[] = [];
    await answerSends(page, sent);
    const agents = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/v1/web/chat/agents' && new URL(r.url()).searchParams.get('studio') === 'presentation',
    );

    await page.goto(`/presentations/${deckId}?studio=1`);
    const rail = page.getByTestId('presentation.studio.rail');
    const offered = (await (await agents).json()) as { id: string; name: string }[];
    expect(offered.map((a) => a.id)).toContain(agentId);

    const composer = rail.getByTestId('filigran-chat-composer').locator('textarea');
    await expect(composer).toBeEnabled({ timeout: 15_000 });

    // A file pasted here starts the conversation (bound to the deck) and goes
    // up through the web chat's upload route.
    const session = page.waitForResponse(
      (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/web/chat/sessions',
    );
    const upload = page.waitForResponse(
      (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/web/chat/upload',
    );
    await pasteScreenshot(composer);
    const created = await session;
    expect(new URL(created.url()).searchParams.get('presentation_id')).toBe(deckId);
    const conversationId = ((await created.json()) as { conversation_id: string }).conversation_id;
    conversationIds.push(conversationId);
    const uploaded = await upload;
    expect(uploaded.ok()).toBe(true);
    const fileId = ((await uploaded.json()) as { file_ids: string[] }).file_ids[0];
    await expect(rail.getByText('image.png')).toBeVisible();

    await composer.fill('Tighten the wording on this slide');
    await composer.press('Enter');
    await expect(rail.getByText('Noted, slide by slide.')).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(new RegExp(`[?&]c=${conversationId}`));

    // The message names the slide on the stage and carries the file.
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ conversation_id: conversationId, studio_slide: 1, content: 'Tighten the wording on this slide' });
    expect(sent[0].file_ids).toEqual([fileId]);

    // The conversation is the deck's: it is listed in the rail's history.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get(`/api/v1/chat/conversations/${conversationId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(((await resp.json()) as { presentation_id: string | null }).presentation_id).toBe(deckId);
    } finally {
      await apiCtx.dispose();
    }
    await page.getByTestId('presentation.studio.history').click();
    // "New conversation", then the one just started.
    await expect(rail.getByRole('menuitem')).toHaveCount(2);
  });
});
