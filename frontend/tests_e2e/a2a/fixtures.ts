/**
 * The `one` fixture: the deployment, addressable three ways.
 *
 * Which way in to use is not a matter of taste:
 *
 *   `page` — the browser, for the handful of genuinely visual facts. Built
 *            from the `browser` fixture rather than taken as Playwright's
 *            built-in `page`, because `page` and `context` are per-test by
 *            construction and every fixture depending on them is refused in
 *            `beforeAll` — where this suite builds its integrations, agents
 *            and bindings, since doing that per test would dominate the run.
 *            A context made from `browser` is still instrumented from
 *            `use.trace` / `use.video`, so nothing is lost but the shortcut.
 *   `rest` — the control plane (`/api/v1/**`) as the admin. Where fixtures are
 *            built and where most assertions read from.
 *   `wire` — anonymous, for the A2A protocol (`/a2a/**`) and the token
 *            endpoint. Separate from `rest` because it must carry no session,
 *            and because the partner-facing surface wants exactly
 *            `WIRE_HEADERS` - no forwarding header (see instance.ts).
 *   `model` — the scripted model's control plane.
 */

import {
  test as base,
  request,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type Request,
} from '@playwright/test';
import { ADMIN_EMAIL, ADMIN_PASSWORD, SELF, WIRE_HEADERS, type Instance } from './instance';
import { ScriptedModel } from '../scripted-llm/client';

export interface One {
  readonly meta: Instance;
  /** The deployment's origin — pages and the A2A wire alike. */
  readonly origin: string;
  readonly page: Page;
  readonly rest: APIRequestContext;
  readonly wire: APIRequestContext;
}

/**
 * The admin JWT, minted once per process.
 *
 * Cached because nearly every file signs in during `beforeAll` and a login is
 * a bcrypt round; keyed by origin because that is the only thing that could
 * ever make one token wrong for another context.
 */
const tokens = new Map<string, string>();

export async function adminToken(instance: Instance = SELF): Promise<string> {
  const cached = tokens.get(instance.origin);
  if (cached) return cached;

  if (!ADMIN_EMAIL || !ADMIN_PASSWORD) {
    throw new Error(
      'E2E_USER_EMAIL and E2E_USER_PASSWORD must be set — the suite signs in with them.',
    );
  }

  const ctx = await request.newContext({ baseURL: instance.origin });
  try {
    const resp = await ctx.post('/api/v1/auth/login', {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    if (!resp.ok()) {
      throw new Error(`Admin login failed: ${resp.status()} ${await resp.text()}`);
    }
    const token = (await resp.json()).access_token as string;
    tokens.set(instance.origin, token);
    return token;
  } finally {
    await ctx.dispose();
  }
}

/**
 * An admin control-plane context.
 *
 * Exported because most files stand their fixtures up in `beforeAll`, where a
 * test-scoped fixture cannot be asked for. Dispose it in `afterAll`.
 */
export async function adminContext(instance: Instance = SELF): Promise<APIRequestContext> {
  const token = await adminToken(instance);
  return request.newContext({
    baseURL: instance.origin,
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  });
}

/** An anonymous context carrying what the A2A surface wants. */
export async function wireContext(instance: Instance = SELF): Promise<APIRequestContext> {
  return request.newContext({ baseURL: instance.origin, extraHTTPHeaders: { ...WIRE_HEADERS } });
}

interface BrowserLog {
  /** Requests still without a response or a failure when the test ended. */
  pending: Array<{ method: string; url: string; resourceType: string; sinceMs: number }>;
  failed: Array<{ method: string; url: string; failure: string; atMs: number }>;
  console: Array<{ type: string; text: string; atMs: number }>;
  pageErrors: Array<{ message: string; atMs: number }>;
  /** Service workers running in the context (public/sw.js once registered). */
  serviceWorkers: number;
  /** What the page itself reports at failure, see `probeRouteModule`. */
  routeProbe: RouteProbe | { error: string };
}

interface RouteProbe {
  pathname: string;
  /** The newest page chunk (`/assets/<Capitalised>-<hash>.js`) the page loaded. */
  pageChunk: string | null;
  /** A fresh `import()` of that chunk: resolved, rejected or still pending. */
  importOutcome: string;
  /** Resource entries the browser started and never finished. */
  unanswered: string[];
}

/**
 * Ask the page whether the route module it last loaded still evaluates.
 *
 * #11139: the route chunk and all its dependencies answered 200, nothing was
 * pending or failed, no page error, yet the navigation never committed. A
 * fresh import of that chunk that stays pending means module evaluation is
 * stuck; one that resolves means the module is ready and the suspended
 * transition was never retried.
 */
async function probeRouteModule(page: Page): Promise<RouteProbe | { error: string }> {
  const probe = page.evaluate(async () => {
    const entries = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const unanswered = entries.filter((e) => e.responseEnd === 0).map((e) => e.name);
    const pageChunk = entries.filter((e) => /\/assets\/[A-Z][A-Za-z]*-[\w-]+\.js$/.test(e.name)).at(-1)?.name ?? null;
    let importOutcome = 'no page chunk';
    if (pageChunk) {
      importOutcome = await Promise.race([
        import(/* @vite-ignore */ pageChunk).then(
          () => 'resolved',
          (e: unknown) => `rejected: ${String(e).slice(0, 500)}`,
        ),
        new Promise<string>((resolve) => setTimeout(() => resolve('pending after 2 s'), 2000)),
      ]);
    }
    return { pathname: location.pathname, pageChunk, importOutcome, unanswered };
  });
  const timeout = new Promise<{ error: string }>((resolve) =>
    setTimeout(() => resolve({ error: 'page.evaluate did not answer within 5 s' }), 5000),
  );
  return Promise.race([probe, timeout]).catch((e: unknown) => ({ error: String(e).slice(0, 500) }));
}

/**
 * What the page asked for and said, kept for a failing test.
 *
 * The trace's network log holds finished responses only, so a request that
 * never answers (a stalled chunk, a hung API call) is invisible there, and a
 * page that silently stays where it was leaves no other clue (#11139).
 */
function recordBrowserLog(page: Page): {
  snapshot: (serviceWorkers: number, routeProbe: BrowserLog['routeProbe']) => BrowserLog;
} {
  const start = Date.now();
  const since = () => Date.now() - start;
  const inFlight = new Map<Request, number>();
  const failed: BrowserLog['failed'] = [];
  const consoleLines: BrowserLog['console'] = [];
  const pageErrors: BrowserLog['pageErrors'] = [];

  page.on('request', (r) => inFlight.set(r, since()));
  page.on('requestfinished', (r) => inFlight.delete(r));
  page.on('requestfailed', (r) => {
    inFlight.delete(r);
    failed.push({ method: r.method(), url: r.url(), failure: r.failure()?.errorText ?? '', atMs: since() });
  });
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') {
      consoleLines.push({ type: m.type(), text: m.text().slice(0, 2000), atMs: since() });
    }
  });
  page.on('pageerror', (e) => pageErrors.push({ message: String(e.stack ?? e.message).slice(0, 4000), atMs: since() }));

  return {
    snapshot: (serviceWorkers, routeProbe) => ({
      pending: [...inFlight].map(([r, at]) => ({
        method: r.method(),
        url: r.url(),
        resourceType: r.resourceType(),
        sinceMs: since() - at,
      })),
      failed,
      console: consoleLines,
      pageErrors,
      serviceWorkers,
      routeProbe,
    }),
  };
}

export const test = base.extend<{
  one: One;
  model: ScriptedModel;
  scriptedCallsOnFailure: void;
}>({
  one: async ({ browser }, use, testInfo) => {
    const rest = await adminContext();
    const wire = await wireContext();

    const context: BrowserContext = await browser.newContext({
      baseURL: SELF.origin,
      storageState: SELF.authFile,
      viewport: { width: 1920, height: 1080 },
      locale: 'en-US',
      timezoneId: 'UTC',
      // The installed app's service worker (`public/sw.js`, registered at boot by
      // `src/lib/pwa.ts`) has a fetch handler, so Chromium hands every request of
      // the page to it first. With `page.route` active on the same page (the
      // failed-direction test of 04-ui-inbound-vs-outbound) a lazy route chunk
      // could stay unanswered: merge_group run 45016235050, the click on the
      // binding moved the URL to /integrations/<id>, IntegrationDetail loaded,
      // its dependency circle-question-mark-*.js never answered and the old page
      // stayed on screen for 15 s (browser-log.json: serviceWorkers 1). Playwright
      // documents routing as unsupported under a service worker; no test of this
      // suite exercises it, so it is blocked here, as stale-build-notice does.
      serviceWorkers: 'block',
    });
    const page = await context.newPage();
    const browserLog = recordBrowserLog(page);

    await use({ meta: SELF, origin: SELF.origin, page, rest, wire });

    if (testInfo.status !== testInfo.expectedStatus) {
      // A diagnostic: nothing here may add an error to the failure it records.
      const routeProbe = await probeRouteModule(page);
      await testInfo
        .attach('browser-log.json', {
          body: JSON.stringify(browserLog.snapshot(context.serviceWorkers().length, routeProbe), null, 2),
          contentType: 'application/json',
        })
        .catch(() => undefined);
    }

    await context.close();
    await Promise.all([rest.dispose(), wire.dispose()]);
  },

  model: async ({}, use) => {
    const model = await ScriptedModel.connect();
    await use(model);
    await model.dispose();
  },

  /**
   * What the scripted model served, attached to a failing test.
   *
   * Which call took which step is the question behind nearly every agentic
   * failure here, and the scripted model logs nothing itself. Its call log is
   * cleared by the next file's `reset()`, so it is read the moment a test
   * fails, while it still holds this file's calls.
   */
  scriptedCallsOnFailure: [
    async ({}, use, testInfo) => {
      await use();
      if (testInfo.status === testInfo.expectedStatus) return;
      // A diagnostic: nothing here may add an error to the failure it records.
      let model: ScriptedModel | undefined;
      try {
        model = await ScriptedModel.connect();
        await testInfo.attach('scripted-llm-calls.json', {
          body: JSON.stringify(await model.calls(), null, 2),
          contentType: 'application/json',
        });
      } catch {
        // The scripted model is unreachable: the failure stands on its own.
      } finally {
        await model?.dispose().catch(() => undefined);
      }
    },
    { auto: true },
  ],
});

export { expect } from '@playwright/test';
