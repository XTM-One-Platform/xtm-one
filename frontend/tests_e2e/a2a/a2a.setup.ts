/**
 * One gate in front of the whole suite.
 *
 * It does three things the specs cannot do for themselves, and every one of
 * them turns an obscure failure into a sentence:
 *
 *  1. Waits for READINESS. `/api/health` answers as soon as uvicorn accepts a
 *     connection, while migrations and seeding still run and every other route
 *     answers 503 "Server is initializing". A suite that starts on that signal
 *     fails its first request in a way that reads as a broken platform.
 *  2. Refuses to run unlicensed. A2A rides on agentic chat, which needs
 *     Enterprise Edition, and a fresh database has neither a licence nor a
 *     registered platform — so without one, every assertion fails at
 *     fixture creation with 403. That is worth one clear message here rather
 *     than N confusing ones later.
 *  3. Accepts the Terms of Use over the API, then signs in and saves a storage
 *     state. The gate is not just a screen: it is chained into
 *     `get_current_user`, so on a fresh licensed database EVERY `/api/v1` call
 *     answers 403 "Terms of Use acceptance required" — including the ones a
 *     spec makes long before it opens a browser. Clearing it through the API
 *     rather than only through the login page is what makes the API-first
 *     specs work at all.
 *
 * It also proves the one thing this suite cannot work around: that the A2A
 * wire is reachable at all. The partner-facing surface is HTTPS-only, so a
 * deployment started without `A2A_ALLOW_INSECURE_LOOPBACK` (the platform's
 * own loopback calls) or without `TEST_ONLY_INSECURE_TRANSPORT_PEERS` naming
 * the network this suite arrives from answers 403 to every card fetch — which
 * looks exactly like a broken feature, in two hundred places at once.
 */

import { expect, request, test as setup } from '@playwright/test';
import { SELF, WIRE_HEADERS } from './instance';
import { LoginPage } from '../model/login.page';

const READY_TIMEOUT_MS = 180_000;

async function waitForReady(): Promise<void> {
  const ctx = await request.newContext({ baseURL: SELF.origin });
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let last = 'never answered';
  try {
    while (Date.now() < deadline) {
      try {
        const resp = await ctx.get('/api/health/ready', { timeout: 5_000 });
        if (resp.ok()) return;
        last = `${resp.status()}`;
      } catch (error) {
        last = String(error).slice(0, 120);
      }
      await new Promise((r) => setTimeout(r, 1_000));
    }
  } finally {
    await ctx.dispose();
  }
  throw new Error(
    `The deployment never became ready at ${SELF.origin}/api/health/ready (last: ${last}). ` +
      'Start one with: docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d ' +
      'minio postgres redis scripted-llm platform worker',
  );
}

async function assertLicensed(): Promise<void> {
  const ctx = await request.newContext({ baseURL: SELF.origin });
  try {
    const resp = await ctx.get('/api/v1/platform/config');
    expect(resp.ok(), 'platform config').toBeTruthy();
    const config = await resp.json();
    expect(
      config.chat_enabled,
      `The deployment reports chat_enabled=${config.chat_enabled} (edition ` +
        `"${config.edition}"). A2A is offered exactly while agentic chat is, so the whole ` +
        'suite would fail at fixture creation. Set ENTERPRISE_LICENSE before starting it.',
    ).toBe(true);
  } finally {
    await ctx.dispose();
  }
}

/**
 * Clear the Terms of Use gate for the admin, over the API.
 *
 * The version is echoed back deliberately — acceptance records *what* was
 * accepted, and a mismatch is a 409 rather than a silent consent to a
 * document nobody read. Reading it from `/auth/terms` means a version bump
 * does not break the suite.
 */
async function acceptTerms(): Promise<void> {
  const email = process.env.E2E_USER_EMAIL;
  const password = process.env.E2E_USER_PASSWORD;
  if (!email || !password) {
    throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set.');
  }
  const ctx = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await ctx.post('/api/v1/auth/login', { data: { email, password } });
    expect(login.ok(), `admin login: ${login.status()}`).toBeTruthy();
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };

    const state = await ctx.get('/api/v1/auth/terms', { headers });
    expect(state.ok(), 'terms state').toBeTruthy();
    const terms = await state.json();
    if (!terms.required || terms.accepted) return;

    const accepted = await ctx.post('/api/v1/auth/terms/accept', {
      headers,
      data: { version: terms.version },
    });
    expect(
      accepted.ok(),
      `accepting Terms of Use ${terms.version}: ${accepted.status()} ${await accepted.text()}`,
    ).toBeTruthy();
  } finally {
    await ctx.dispose();
  }
}

/**
 * Answer the first-login organization setup proposal (#9173) over the API.
 *
 * On a fresh XTM One database the first administrator to sign in gets a
 * modal dialog proposing the organization profile setup; it covers the app
 * shell, so the sign-in below would never see the Dashboard link. The answer
 * is recorded once per instance and the dialog never comes back; the first
 * answer wins, so a re-run answers the recorded state unchanged.
 */
async function answerOrganizationSetupPrompt(): Promise<void> {
  const email = process.env.E2E_USER_EMAIL;
  const password = process.env.E2E_USER_PASSWORD;
  const ctx = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await ctx.post('/api/v1/auth/login', { data: { email, password } });
    expect(login.ok(), `admin login: ${login.status()}`).toBeTruthy();
    const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
    const answered = await ctx.post('/api/v1/organization-context/setup-prompt', {
      headers,
      data: { outcome: 'dismissed' },
    });
    expect(
      answered.ok(),
      `answering the organization setup proposal: ${answered.status()} ${await answered.text()}`,
    ).toBeTruthy();
  } finally {
    await ctx.dispose();
  }
}

/**
 * Prove the A2A wire answers before two hundred specs assume it does.
 *
 * Partner discovery is the right probe: it needs no exposure and no
 * credential, it is behind the transport gate a misconfigured deployment
 * fails, and it answers the uniform not-found to anyone who gets past that —
 * so 403 means "the transport gate refused this suite" and 404 means "the
 * wire is reachable and nothing is exposed yet", which is exactly the
 * distinction worth making once rather than in every later failure.
 */
async function assertWireReachable(): Promise<void> {
  const ctx = await request.newContext({
    baseURL: SELF.origin,
    extraHTTPHeaders: { ...WIRE_HEADERS },
  });
  try {
    const resp = await ctx.get('/a2a/partner/agents');
    expect(
      resp.status(),
      `GET ${SELF.origin}/a2a/partner/agents answered ${resp.status()}: ` +
        `${(await resp.text()).slice(0, 200)}\n` +
        'The partner-facing surface is HTTPS-only, with test peers exempt where the deployment says ' +
        'so. A 403 "https required" here means TEST_ONLY_INSECURE_TRANSPORT_PEERS does not name the ' +
        'address this suite reaches the platform from (the Docker bridge gateway, or 127.0.0.1 with ' +
        'A2A_ALLOW_INSECURE_LOOPBACK=true for a platform run on the host). A 200 with HTML means ' +
        'E2E_BASE_URL names a dev server serving the SPA rather than the platform.',
    ).toBe(404);
  } finally {
    await ctx.dispose();
  }
}

setup('the deployment is ready, licensed, past the terms gate and answering on the A2A wire', async () => {
  setup.setTimeout(READY_TIMEOUT_MS + 30_000);
  await waitForReady();
  await assertLicensed();
  await acceptTerms();
  await answerOrganizationSetupPrompt();
  await assertWireReachable();
});

setup('sign in', async ({ browser }) => {
  const context = await browser.newContext({ baseURL: SELF.origin });
  const page = await context.newPage();
  await new LoginPage(page).goto();
  await new LoginPage(page).login();
  await context.storageState({ path: SELF.authFile });
  await context.close();
});
