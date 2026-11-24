import {
  test,
  expect,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type Route,
} from '@playwright/test';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { ADMIN_EMAIL, ADMIN_PASSWORD } from './constants';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';
import { ChatPage } from './model/chat.page';
import { ScriptedModel, SCRIPTED_MODEL, SCRIPTED_PROVIDER, marker } from './scripted-llm/client';

// ---------------------------------------------------------------------------
// Content-Security-Policy - the key pages load nothing the policy forbids.
//
// The platform sends its policy report-only by default: the browser blocks
// nothing, but it still fires a `securitypolicyviolation` event for every
// resource the policy would block once enforced. Zero events on these pages is
// the proof that CONTENT_SECURITY_POLICY_MODE=enforce would break none of them.
// A failure names the directive, the blocked resource and the page: add the
// missing source to the policy (backend/app/core/csp.py) with its reason -
// never relax this spec.
// ---------------------------------------------------------------------------

interface Violation {
  page: string;
  directive: string;
  blocked: string;
  source: string;
}

type Detail = Omit<Violation, 'page'>;

/**
 * Records every violation of every document of the context, across navigations.
 *
 * The event listener runs in the page and hands each violation to the test
 * through a binding (a page-side array would be lost on the next navigation);
 * the console catches what the event cannot reach, such as a worker's.
 */
async function watchViolations(context: BrowserContext): Promise<Violation[]> {
  const violations: Violation[] = [];
  await context.exposeBinding('__e2eCspViolation', ({ page }, detail: Detail) => {
    violations.push({ page: page.url(), ...detail });
  });
  await context.addInitScript(() => {
    document.addEventListener(
      'securitypolicyviolation',
      (event) => {
        const report = (window as unknown as { __e2eCspViolation?: (detail: Detail) => void }).__e2eCspViolation;
        report?.({
          directive: event.effectiveDirective,
          blocked: event.blockedURI,
          source: `${event.sourceFile}:${event.lineNumber}`,
        });
      },
      true,
    );
  });
  context.on('console', (message) => {
    const text = message.text();
    if (/Content[ -]Security[ -]Policy/i.test(text)) {
      violations.push({ page: message.page()?.url() ?? '', directive: 'console', blocked: text, source: '' });
    }
  });
  return violations;
}

function describe(violations: Violation[]): string {
  return violations.map((v) => `${v.directive} blocked ${v.blocked} on ${v.page} (${v.source})`).join('\n');
}

/** Lets late resources (lazy chunks, fonts, deferred images) load, then checks. */
async function expectNoViolations(page: Page, violations: Violation[]): Promise<void> {
  await page.waitForTimeout(1_500);
  expect(violations, describe(violations)).toEqual([]);
}

/** Opens a document and checks it carries the platform's policy (else zero violations proves nothing). */
async function visit(page: Page, path: string): Promise<void> {
  const response = await page.goto(path);
  expect(response, `no response for ${path}`).not.toBeNull();
  const headers = response!.headers();
  const policy = headers['content-security-policy-report-only'] ?? headers['content-security-policy'] ?? '';
  // The Vite dev server serves its own document, with no policy: locally the
  // spec only means something against the platform image. In CI a missing
  // policy is a failure.
  test.skip(
    !policy && !process.env.CI,
    'No Content-Security-Policy on this server (the Vite dev server sends none): run against the platform image with E2E_BASE_URL.',
  );
  expect(policy, `${path} must carry the platform's Content-Security-Policy`).toContain("default-src 'self'");
}

interface RedirectingClient {
  /** The registered `redirect_uri`. */
  callback: string;
  /** Where the callback sends the browser next: another origin. */
  frontEnd: string;
  /** The query of every callback the client received. */
  received: URLSearchParams[];
  close: () => Promise<void>;
}

/**
 * An MCP client whose OAuth callback redirects again, to its own front end, as
 * XTM One's own callback does: `http://127.0.0.1:<a>/callback` answers 307 to
 * `http://127.0.0.1:<b>/landed`, an origin no policy of the consent page can
 * name. Real servers, not routes: Playwright does not intercept the hops that
 * follow a redirect.
 */
async function startRedirectingClient(): Promise<RedirectingClient> {
  const received: URLSearchParams[] = [];
  const listen = async (server: Server): Promise<string> => {
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  };
  const frontEndServer = createServer((_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end('<!doctype html><title>Client</title><h1>Client front end</h1>');
  });
  const frontEnd = `${await listen(frontEndServer)}/landed`;
  const callbackServer = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://client.invalid');
    if (url.pathname !== '/callback') {
      res.writeHead(404);
      res.end();
      return;
    }
    received.push(url.searchParams);
    res.writeHead(307, { Location: frontEnd });
    res.end();
  });
  const callback = `${await listen(callbackServer)}/callback`;
  const stop = (server: Server) => new Promise<void>((resolve) => server.close(() => resolve()));
  return {
    callback,
    frontEnd,
    received,
    close: async () => {
      callbackServer.closeAllConnections();
      frontEndServer.closeAllConnections();
      await Promise.all([stop(callbackServer), stop(frontEndServer)]);
    },
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

/**
 * A conversation with the agent, created over the API so the page opens it
 * settled: one created by the page itself re-mounts the composer while it
 * settles, wiping what was typed.
 */
async function startConversation(agentId: string, title: string): Promise<string> {
  return withAdmin(async (apiCtx, token) => {
    const resp = await apiCtx.post('/api/v1/chat/conversations', {
      headers: { Authorization: `Bearer ${token}` },
      data: { agent_id: agentId, title },
    });
    expect(resp.ok(), await resp.text()).toBe(true);
    return ((await resp.json()) as { id: string }).id;
  });
}

/**
 * Types into the chat composer and sends, then waits for the message to show
 * in the transcript. Every attempt is bounded (an action without a timeout of
 * its own would outlast the whole test) and retried as a whole.
 */
async function sendChatMessage(page: Page, text: string): Promise<void> {
  const chat = new ChatPage(page);
  const composer = chat.composer();
  const send = chat.sendButton();
  await expect(async () => {
    await composer.fill(text, { timeout: 5_000 });
    await send.click({ timeout: 2_000 });
  }).toPass({ timeout: 45_000 });
  await expect(page.getByText(text).first()).toBeVisible();
}

// A 1x1 PNG, shown the way a code-interpreter chart reaches the chat.
const PIXEL_PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
// A remote image an agent links to. The policy is checked before the request,
// which the test answers itself (`.invalid` never resolves): the chat swaps an
// image that fails to load for a placeholder.
const REMOTE_IMAGE = 'https://csp-e2e.invalid/pixel.png';

// Light: the scheme whose palette Mermaid can read (it cannot parse the dark
// scheme's `color-mix()` surface), so the diagram is really drawn here.
const DECK_SPEC = {
  title: 'E2E Content-Security-Policy deck',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'E2E CSP', subtitle: 'Every block the renderer draws', notes: 'Open with the frame.' },
    {
      layout: 'content',
      title: 'A Mermaid diagram',
      blocks: [{ type: 'diagram', source: 'flowchart LR\n  A[Browser] --> B[XTM One]\n  B --> C[(Database)]' }],
      notes: 'Mermaid draws an SVG with its own style element.',
    },
    {
      layout: 'content',
      title: 'A chart',
      blocks: [
        {
          type: 'chart',
          kind: 'bar',
          series: [{ name: 'Trials', values: [31, 44, 52, 63] }],
          categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
        },
      ],
    },
    {
      layout: 'content',
      title: 'An architecture',
      blocks: [
        {
          type: 'architecture',
          nodes: [
            { id: 'cti', label: 'OpenCTI', icon: 'brain', row: 1, col: 1 },
            { id: 'core', label: 'XTM One', icon: 'sparkles', shape: 'circle', emphasis: 'core', row: 1, col: 2 },
            { id: 'aev', label: 'OpenAEV', icon: 'crosshair', row: 1, col: 3 },
          ],
          edges: [
            { source: 'core', target: 'cti' },
            { source: 'core', target: 'aev' },
          ],
        },
      ],
    },
    { layout: 'closing', title: 'Thank you' },
  ],
};

// ---------------------------------------------------------------------------
// Signed out
// ---------------------------------------------------------------------------

test.describe('Content-Security-Policy - signed out', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('the watch is live: a violation is caught', async ({ page, context }) => {
    const violations = await watchViolations(context);
    await visit(page, '/login');
    await page.evaluate(() => fetch('https://csp-canary.invalid/probe').catch(() => undefined));
    await expect
      .poll(() => violations.some((v) => v.directive === 'connect-src' && v.blocked.includes('csp-canary.invalid')))
      .toBe(true);
  });

  test('the login page', async ({ page, context }) => {
    const violations = await watchViolations(context);
    await visit(page, '/login');
    await expect(page.getByPlaceholder('user@mail.com')).toBeVisible();
    await expectNoViolations(page, violations);
  });

  test('the MCP OAuth consent page, its sign-in included', async ({ page, context, request }) => {
    const violations = await watchViolations(context);
    const redirectUri = 'http://localhost:8765/callback';
    const registration = await request.post('/mcp/oauth/register', {
      data: { client_name: 'E2E CSP client', redirect_uris: [redirectUri] },
    });
    expect(registration.status(), await registration.text()).toBe(201);
    const { client_id: clientId } = (await registration.json()) as { client_id: string };
    const query = new URLSearchParams({
      response_type: 'code',
      client_id: clientId,
      redirect_uri: redirectUri,
      code_challenge: 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
      code_challenge_method: 'S256',
      state: 'csp-e2e',
    });
    await visit(page, `/mcp/oauth/authorize?${query}`);
    await expect(page.getByRole('heading', { name: 'Authorize Application' })).toBeVisible();
    // The page's controls are bound by its script (an inline handler cannot be
    // allowed by a hash): the form still validates and still posts.
    await page.locator('#email').fill('csp-e2e@invalid.example');
    await page.locator('#password').fill('not-the-password');
    await Promise.all([
      page.waitForResponse((r) => r.url().includes('/mcp/oauth/authorize') && r.request().method() === 'POST'),
      page.locator('#local-auth-form').getByRole('button', { name: 'Authorize' }).click(),
    ]);
    await expect(page.getByText('Invalid email or password.')).toBeVisible();
    await expectNoViolations(page, violations);
  });

  test('the MCP OAuth consent sign-in reaches a client whose callback redirects again', async ({
    page,
    context,
    request,
  }) => {
    // Browsers check form-action on every redirect of the navigation a form
    // submission starts: a 302 from the sign-in to the callback, which
    // redirects again, met the consent page's form-action on that second hop
    // and stopped there once the policy was enforced. The E2E admin
    // signs in with a password alone: with a second factor enrolled, the
    // consent page would ask for a code here.
    expect(ADMIN_EMAIL && ADMIN_PASSWORD, 'E2E_USER_EMAIL / E2E_USER_PASSWORD must be set').toBeTruthy();
    const client = await startRedirectingClient();
    try {
      const violations = await watchViolations(context);
      const registration = await request.post('/mcp/oauth/register', {
        data: { client_name: 'E2E CSP multi-hop client', redirect_uris: [client.callback] },
      });
      expect(registration.status(), await registration.text()).toBe(201);
      const { client_id: clientId } = (await registration.json()) as { client_id: string };
      const query = new URLSearchParams({
        response_type: 'code',
        client_id: clientId,
        redirect_uri: client.callback,
        code_challenge: 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
        code_challenge_method: 'S256',
        state: 'csp-e2e-multi-hop',
      });
      await visit(page, `/mcp/oauth/authorize?${query}`);
      await page.locator('#email').fill(ADMIN_EMAIL!);
      await page.locator('#password').fill(ADMIN_PASSWORD!);
      await Promise.all([
        page.waitForURL(client.frontEnd),
        page.locator('#local-auth-form').getByRole('button', { name: 'Authorize' }).click(),
      ]);
      await expect(page.getByRole('heading', { name: 'Client front end' })).toBeVisible();
      expect(client.received.map((params) => params.get('state'))).toEqual(['csp-e2e-multi-hop']);
      expect(client.received[0].get('code')).toBeTruthy();
      await expectNoViolations(page, violations);
    } finally {
      await client.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Signed in
// ---------------------------------------------------------------------------

test.describe('Content-Security-Policy - signed in', () => {
  test('chat: a streamed answer with images and a code block', async ({ page, context }) => {
    test.setTimeout(120_000);
    const scripted = await ScriptedModel.connect();
    const label = marker('csp-chat');
    const agent = await withAdmin(async (apiCtx, token) => {
      const resp = await apiCtx.post('/api/v1/agents', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: `E2E CSP chat ${label}`,
          persona: 'Answers the Content-Security-Policy spec.',
          llm_provider: SCRIPTED_PROVIDER,
          llm_model: SCRIPTED_MODEL,
        },
      });
      expect(resp.ok(), await resp.text()).toBe(true);
      return (await resp.json()) as { id: string };
    });
    try {
      await scripted.arm(label, [
        {
          text: [
            'CSP-E2E streamed answer.',
            '',
            `![remote image](${REMOTE_IMAGE})`,
            '',
            `![chart](data:image/png;base64,${PIXEL_PNG})`,
            '',
            '```python',
            'print("csp")',
            '```',
          ].join('\n'),
        },
      ]);
      const violations = await watchViolations(context);
      await page.route(REMOTE_IMAGE, (route) =>
        route.fulfill({ contentType: 'image/png', body: Buffer.from(PIXEL_PNG, 'base64') }),
      );
      const conversation = await startConversation(agent.id, label);
      await visit(page, `/chat?c=${conversation}`);
      await sendChatMessage(page, `Show the CSP fixtures ${label}`);
      // The conversation list previews the last message, so the answer's text
      // appears there too: look for it in the transcript only.
      const transcript = page.getByTestId('filigran-chat-transcript');
      await expect(transcript.getByText('CSP-E2E streamed answer.')).toBeVisible({ timeout: 60_000 });
      await expect(transcript.getByText('print("csp")')).toBeVisible();
      // Lazy images only load (and meet the policy) once on screen.
      const remote = transcript.locator(`img[src="${REMOTE_IMAGE}"]`);
      await remote.scrollIntoViewIfNeeded();
      await expect.poll(() => remote.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(1);
      await expectNoViolations(page, violations);
    } finally {
      await scripted.dispose();
      await withAdmin((apiCtx, token) =>
        apiCtx.delete(`/api/v1/agents/${agent.id}`, { headers: { Authorization: `Bearer ${token}` } }),
      );
    }
  });

  test('reports: generated by an agent, read and printed', async ({ page, context }) => {
    test.setTimeout(120_000);
    const scripted = await ScriptedModel.connect();
    const label = marker('csp-report');
    const reportTitle = `E2E CSP report ${label}`;
    const agent = await withAdmin(async (apiCtx, token) => {
      const resp = await apiCtx.post('/api/v1/agents', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: `E2E CSP reporter ${label}`,
          persona: 'Writes the Content-Security-Policy spec report.',
          llm_provider: SCRIPTED_PROVIDER,
          llm_model: SCRIPTED_MODEL,
          builtin_tool_ids: ['generate_report'],
          // The report under the policy, not an approval prompt for it.
          approve_tool_calls: false,
        },
      });
      expect(resp.ok(), await resp.text()).toBe(true);
      return (await resp.json()) as { id: string };
    });
    try {
      await scripted.arm(label, [
        {
          tool: 'generate_report',
          arguments: {
            title: reportTitle,
            header: { title: reportTitle, eyebrow: 'E2E', tags: ['TLP:CLEAR'] },
            blocks: [
              {
                block: 'prose',
                title: 'Findings',
                lead: '<p>The report renders its blocks <strong>under the policy</strong>.</p>',
                rich_text: '<p>Second paragraph with a <a href="https://filigran.io">link</a>.</p>',
              },
            ],
          },
        },
        { text: 'CSP-E2E report ready.' },
      ]);
      const violations = await watchViolations(context);
      const conversation = await startConversation(agent.id, label);
      await visit(page, `/chat?c=${conversation}`);
      await sendChatMessage(page, `Write the CSP report ${label}`);
      // This test is about the report pages, not the chat: wait on what the
      // backend owns (both armed turns served, the report row written), never
      // on the closing prose being painted in the transcript.
      try {
        await expect
          .poll(() => scripted.turnsServed(label), {
            message: 'the agent took both scripted turns',
            timeout: 60_000,
          })
          .toBeGreaterThanOrEqual(2);
      } catch (error) {
        // Name what the model was asked for this marker: whether the follow-up
        // call after the tool result never came, or came without the marker.
        const { calls, served } = await scripted.calls();
        const mine = calls.filter((call) => call.matched === label);
        throw new Error(
          `${(error as Error).message}\nscripted calls for ${label} (served ${served[label] ?? 0}): ` +
            JSON.stringify(mine) +
            `\nunmatched calls: ${calls.filter((call) => call.matched === null).length}`,
        );
      }
      const findReport = () =>
        withAdmin(async (apiCtx, token) => {
          const resp = await apiCtx.get('/api/v1/reports', { headers: { Authorization: `Bearer ${token}` } });
          expect(resp.ok(), await resp.text()).toBe(true);
          const reports = (await resp.json()) as Array<{ id: string; title: string }>;
          return reports.find((r) => r.title === reportTitle)?.id ?? null;
        });
      await expect
        .poll(findReport, { message: 'the agent generated the report', timeout: 30_000 })
        .not.toBeNull();
      const reportId = (await findReport())!;
      await visit(page, `/reports/${reportId}`);
      await expect(page.getByRole('heading', { name: reportTitle }).first()).toBeVisible();
      await visit(page, `/reports/${reportId}?print`);
      await expect(page.getByRole('heading', { name: reportTitle }).first()).toBeVisible();
      await expectNoViolations(page, violations);
    } finally {
      await scripted.dispose();
      await withAdmin((apiCtx, token) =>
        apiCtx.delete(`/api/v1/agents/${agent.id}`, { headers: { Authorization: `Bearer ${token}` } }),
      );
    }
  });

  test('presentations: viewer, editor, speaker view, notes and the static export page', async ({
    page,
    context,
  }) => {
    test.setTimeout(120_000);
    const templateName = `E2E CSP template ${Date.now()}`;
    const { templateId, deckId } = await withAdmin(async (apiCtx, token) => {
      const template = await createTwoSchemeTemplate(apiCtx, token, templateName);
      const deck = await importDeckOnTemplate(apiCtx, token, DECK_SPEC, templateName, 'e2e-csp.json');
      return { templateId: template, deckId: deck };
    });
    try {
      const violations = await watchViolations(context);

      await visit(page, `/presentations/${deckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      // Slide 2 holds the Mermaid diagram: wait for Mermaid to have drawn it.
      await page.getByRole('button', { name: 'Next slide' }).click();
      await expect(page.locator('figure.deck-diagram[data-state="ready"]').first()).toBeAttached({ timeout: 30_000 });
      for (let slide = 3; slide <= DECK_SPEC.slides.length; slide += 1) {
        await page.getByRole('button', { name: 'Next slide' }).click();
      }

      await visit(page, `/presentations/${deckId}?panel=editor`);
      await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();

      await visit(page, `/presentations/${deckId}/speaker`);
      await expect(page.getByTestId('presentation.speaker')).toBeVisible();

      await visit(page, `/presentations/${deckId}/notes`);
      await expect(page.getByText('Open with the frame.').first()).toBeVisible();

      // The export's headless Chromium loads this page with a render token the
      // platform mints; here its two data calls are answered from the
      // authenticated API. The policy is enforced in the renderer before any
      // request reaches the route, so this changes nothing about the check.
      await withAdmin(async (apiCtx, token) => {
        const auth = { Authorization: `Bearer ${token}` };
        await page.route(
          (url) => url.pathname.endsWith(`/presentations/${deckId}/render-spec`),
          async (route: Route) => {
            const resp = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: auth });
            await route.fulfill({ status: resp.status(), contentType: 'application/json', body: await resp.body() });
          },
        );
        await page.route((url) => url.pathname.includes(`/presentations/${deckId}/render-asset/`), async (route: Route) => {
          const fileId = new URL(route.request().url()).pathname.split('/').pop();
          const resp = await apiCtx.get(`/api/v1/chat/files/${fileId}/download`, { headers: auth });
          await route.fulfill({
            status: resp.status(),
            contentType: resp.headers()['content-type'],
            body: await resp.body(),
          });
        });
        await visit(page, `/presentations/${deckId}/static?render_token=e2e`);
        await expect
          .poll(() => page.evaluate(() => document.documentElement.dataset.deckReady ?? ''), { timeout: 30_000 })
          .toBe('true');
        await expect(page.locator('.deck-static-slide')).toHaveCount(DECK_SPEC.slides.length);
        await expectNoViolations(page, violations);
      });
    } finally {
      await withAdmin((apiCtx, token) =>
        apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } }),
      );
      await deleteTemplate(templateId);
    }
  });

  test('traces, the flow builder, settings, the catalog and the desktop page', async ({ page, context }) => {
    test.setTimeout(120_000);
    const flowId = await withAdmin(async (apiCtx, token) => {
      const resp = await apiCtx.post('/api/v1/flows', {
        headers: { Authorization: `Bearer ${token}` },
        data: { name: `E2E CSP flow ${Date.now()}` },
      });
      expect(resp.ok(), await resp.text()).toBe(true);
      return ((await resp.json()) as { id: string }).id;
    });
    try {
      const violations = await watchViolations(context);

      await visit(page, '/observability/traces');
      await expect(page.getByRole('heading', { name: 'Observability', level: 1 })).toBeVisible();

      await visit(page, `/flows/library/${flowId}`);
      await expect(page.locator('.react-flow__pane')).toBeVisible();
      await page.locator('.react-flow__pane').dblclick({ position: { x: 260, y: 140 } });
      const quickAdd = page.getByTestId('flow-quick-add');
      await expect(quickAdd).toBeVisible();
      await quickAdd.locator('input').fill('manual');
      await quickAdd.getByText('Manual', { exact: true }).first().click();
      await expect(page.locator('.react-flow__node').first()).toBeVisible();

      await visit(page, '/settings');
      await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();

      // The catalog answers only when the AI catalog is enabled; otherwise the
      // route lands on the dashboard. Either way it is a document to check.
      await visit(page, '/catalog');
      await expect(page).toHaveURL(/\/(catalog)?$/);
      await expect(page.locator('main').first()).toBeVisible();

      await visit(page, '/desktop');
      await expect(page.getByRole('heading', { name: 'XTM One Desktop', level: 1 })).toBeVisible();

      await expectNoViolations(page, violations);
    } finally {
      await withAdmin((apiCtx, token) =>
        apiCtx.delete(`/api/v1/flows/${flowId}`, { headers: { Authorization: `Bearer ${token}` } }),
      );
    }
  });

  test('only documents carry the policy: the API and the chat widget script do not', async ({ request }) => {
    const config = await request.get('/api/v1/platform/config');
    expect(config.ok()).toBe(true);
    expect(config.headers()['content-security-policy-report-only']).toBeUndefined();
    expect(config.headers()['content-security-policy']).toBeUndefined();
    // The embeddable widget runs inside the page of the site that embeds it,
    // under that site's policy: nothing the platform sends constrains it.
    const widget = await request.get('/api/v1/public/widget.js');
    expect(widget.ok()).toBe(true);
    expect(widget.headers()['content-type']).toContain('javascript');
    expect(widget.headers()['content-security-policy-report-only']).toBeUndefined();
    expect(widget.headers()['content-security-policy']).toBeUndefined();
  });
});
