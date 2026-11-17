import { expect, request, type APIRequestContext, type APIResponse, type Page } from '@playwright/test';
import { ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL } from './constants';
import { SCRIPTED_LLM, SCRIPTED_MODEL, SCRIPTED_PROVIDER, SERVED_MODEL } from './scripted-llm/client';

/**
 * The deck's poll: mirrors `REFETCH_INTERVAL_STANDARD` in `src/lib/constants.ts`,
 * which a spec cannot import. A spec brings the poll forward on the page clock
 * (`page.clock.fastForward(DECK_POLL_MS)`) instead of waiting it out.
 */
export const DECK_POLL_MS = 15_000;

/**
 * The slide editor saves on its own: resolves once its status line
 * reads "All changes saved" - every change made so far is on the server.
 */
export async function expectEditorSaved(page: Page, timeout = 15_000): Promise<void> {
  await expect(page.getByTestId('presentation.saveStatus')).toHaveAttribute('data-state', 'saved', { timeout });
}

/**
 * Expands a collapsible sidebar group so its child links become clickable.
 *
 * The left nav collapses its groups (Agent Studio, Tools & Integrations,
 * Observability, Admin), so a destination inside one is not in the DOM until
 * its group is open. No-op when the group is already expanded — the state is
 * remembered per browser profile, and the group holding the current page opens
 * itself.
 */
export async function expandNavGroup(page: Page, label: string): Promise<void> {
  // The group row is the design system's NavbarSubmenu trigger: its accessible
  // name is the group label and its state is `aria-expanded`. Before the
  // migration the chevron was a separate button labelled "<label> — Expand",
  // which is what this helper used to look for.
  const trigger = page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('button', { name: label });

  // Wait for the row to exist before deciding. A bare attribute read right after
  // `goto()` resolves against a sidebar the app has not rendered yet, returns
  // null, and silently skips the click — after which the caller waits out its
  // timeout on a child link that never appears.
  await expect(trigger).toBeVisible();
  if ((await trigger.getAttribute('aria-expanded')) !== 'true') await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
}

// Avoid login bursts during parallel E2E runs by reusing one admin token per worker process.
const LOGIN_MAX_ATTEMPTS = 5;
const TOKEN_EXPIRY_SKEW_MS = 60_000;
let cachedAdminToken: string | null = null;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseRetryAfterMs(retryAfterHeader?: string): number | undefined {
  if (!retryAfterHeader) return undefined;

  // Supports both Retry-After formats: delta-seconds and HTTP-date.
  const seconds = Number(retryAfterHeader);
  if (Number.isFinite(seconds) && seconds > 0) {
    return Math.ceil(seconds * 1000);
  }

  const dateMs = Date.parse(retryAfterHeader);
  if (!Number.isNaN(dateMs)) {
    const delayMs = dateMs - Date.now();
    return delayMs > 0 ? delayMs : undefined;
  }

  return undefined;
}

function isTokenExpiringSoon(token: string): boolean {
  try {
    // JWT format is "<header>.<payload>.<signature>" — we only need the payload.
    const parts = token.split('.');
    // If the token shape is unexpected, force a refresh instead of reusing it.
    if (parts.length < 2) return true;

    // JWT payload is base64url; normalize to base64 before decoding.
    const payloadB64 = parts[1]
      .replace(/-/g, '+')
      .replace(/_/g, '/')
      .padEnd(Math.ceil(parts[1].length / 4) * 4, '=');

    const payload = JSON.parse(Buffer.from(payloadB64, 'base64').toString('utf8')) as { exp?: number };
    // Missing exp means we can't trust token lifetime, so refresh.
    if (typeof payload.exp !== 'number') return true;

    // Refresh slightly before expiry to avoid mid-request expiration races.
    return (payload.exp * 1000) <= (Date.now() + TOKEN_EXPIRY_SKEW_MS);
  } catch {
    // Any parse/decode issue is treated as non-reusable.
    return true;
  }
}

async function loginAdmin(apiCtx: APIRequestContext): Promise<string> {
  for (let attempt = 1; attempt <= LOGIN_MAX_ATTEMPTS; attempt += 1) {
    const resp = await apiCtx.post('/api/v1/auth/login', {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });

    if (resp.ok()) {
      const { access_token: token } = await resp.json();
      return token;
    }

    const errorBody = await resp.text();
    const isLastAttempt = attempt === LOGIN_MAX_ATTEMPTS;
    if (resp.status() !== 429 || isLastAttempt) {
      throw new Error(`Admin login failed: ${resp.status()} ${errorBody}`);
    }

    // On rate-limit, honor server hint when present; otherwise use bounded exponential backoff + jitter.
    const retryAfterMs = parseRetryAfterMs(resp.headers()['retry-after']);
    const backoffMs = retryAfterMs ?? (250 * (2 ** (attempt - 1)) + Math.floor(Math.random() * 200));
    await sleep(backoffMs);
  }

  throw new Error('Admin login failed: rate-limited after maximum retry attempts');
}

/**
 * Creates an authenticated API context using admin credentials.
 * Callers must dispose() the returned context after use.
 */
export async function createAdminApiContext(): Promise<{ apiCtx: APIRequestContext; token: string }> {
  const apiCtx = await request.newContext({ baseURL: BASE_URL });
  try {
    // Refresh only when missing/near expiry to keep cleanup hooks fast and reduce /auth/login pressure.
    if (!cachedAdminToken || isTokenExpiringSoon(cachedAdminToken)) {
      cachedAdminToken = await loginAdmin(apiCtx);
    }
    return { apiCtx, token: cachedAdminToken };
  } catch (error) {
    await apiCtx.dispose();
    throw error;
  }
}

const TEMPLATE_FONTS = {
  heading_font: "'Geologica', system-ui, sans-serif",
  body_font: "'IBM Plex Sans', system-ui, sans-serif",
  mono_font: "'IBM Plex Mono', ui-monospace, monospace",
};

const DARK_PALETTE = {
  background: '#070d18',
  surface: '#0d172b',
  line: '#1f3965',
  foreground: '#f2f2f3',
  muted: '#afb0b6',
  accent: '#42caff',
  info: '#42caff',
  success: '#17ab1f',
  warning: '#e6700f',
  error: '#f14337',
  gradient_from: '#42caff',
  gradient_to: '#00f0bc',
  chart_1: '#42caff',
  chart_2: '#00f0bc',
  chart_3: '#a47af0',
  chart_4: '#f2be3a',
  chart_5: '#f2933a',
  chart_6: '#7587ff',
};

const LIGHT_PALETTE = {
  background: '#f2f2f3',
  surface: '#ffffff',
  line: '#cacbce',
  foreground: '#18191b',
  muted: '#494a50',
  accent: '#0015a8',
  info: '#0079a8',
  success: '#117916',
  warning: '#b8550a',
  error: '#b8180a',
  gradient_from: '#0015a8',
  gradient_to: '#009474',
  chart_1: '#0015a8',
  chart_2: '#009474',
  chart_3: '#651fe5',
  chart_4: '#eaa510',
  chart_5: '#e6700f',
  chart_6: '#0079a8',
};

/**
 * Creates a private presentation template that dresses BOTH colour schemes and
 * returns its id, for the specs that put a deck in the light scheme.
 *
 * A deck imported without a template lands on the install's default template,
 * and that one depends on the platform mode: the dark-only XTM One Brand Deck
 * on an XTM One platform (the mode CI runs), the Filigran Brand Deck on Filigran
 * Copilot. Delete it with `deleteTemplate` once the decks wearing it are gone.
 * `palettes` overrides keys of either scheme's palette (a gradient background).
 */
export async function createTwoSchemeTemplate(
  apiCtx: APIRequestContext,
  token: string,
  name: string,
  palettes: { dark?: Record<string, string>; light?: Record<string, string> } = {},
): Promise<string> {
  const resp = await apiCtx.post('/api/v1/template-containers', {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      name,
      kind: 'presentation',
      aspect_ratio: '16:9',
      theme_config: { ...DARK_PALETTE, ...TEMPLATE_FONTS, ...palettes.dark },
      extra_metadata: {
        supported_schemes: ['dark', 'light'],
        theme_variants: { light: { ...LIGHT_PALETTE, ...palettes.light } },
      },
    },
  });
  if (!resp.ok()) throw new Error(`Creating the two-scheme template failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

/**
 * Imports a fixture deck on the caller's template named `templateName`, through
 * the package envelope `POST /api/v1/presentations/import` reads (a bare spec
 * would land on the install's default template). Throws when the deck did not
 * land on that template.
 */
export async function importDeckOnTemplate(
  apiCtx: APIRequestContext,
  token: string,
  spec: object,
  templateName: string,
  fileName: string,
): Promise<string> {
  const envelope = {
    export_type: 'presentation',
    version: 1,
    presentation: { spec, template: { name: templateName }, assets: [] },
  };
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name: fileName, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(envelope), 'utf8') },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id, import_report: report } = (await resp.json()) as { id: string; import_report?: { template?: string } };
  if (report?.template !== 'matched') {
    throw new Error(`The fixture deck did not land on "${templateName}": ${JSON.stringify(report)}`);
  }
  return id;
}

/** Deletes a template created by a spec; a deck still wearing it only loses the link. */
export async function deleteTemplate(templateId: string): Promise<void> {
  if (!templateId) return;
  const { apiCtx, token } = await createAdminApiContext();
  try {
    await apiCtx.delete(`/api/v1/template-containers/${templateId}`, { headers: { Authorization: `Bearer ${token}` } });
  } finally {
    await apiCtx.dispose();
  }
}


// ---------------------------------------------------------------------------
// The scripted model (docker-compose.e2e.yml's `scripted-llm`).
// ---------------------------------------------------------------------------

/** Why the scripted server cannot serve the suite, or '' when it can. */
async function probeScriptedServer(): Promise<string> {
  const control = await request.newContext({ baseURL: SCRIPTED_LLM });
  try {
    const resp = await control.get('/v1/models').catch(() => null);
    if (!resp?.ok()) return UNREACHABLE;
    const models: unknown = (await resp.json().catch(() => null))?.data;
    if (!Array.isArray(models)) {
      return `${SCRIPTED_LLM}/v1/models answers without a model list: is another server on that port?`;
    }
    const served = models.map((m: { id?: unknown }) => m?.id);
    if (served.includes(SERVED_MODEL)) return '';
    return (
      `the scripted model lists ${JSON.stringify(served)}, not "${SERVED_MODEL}". The image's default ` +
      'model id changed: update SERVED_MODEL in scripted-llm/client.ts and ' +
      'LLM_PROVIDER_CUSTOM_OPENAI_MODELS in .github/workflows/e2e.yml.'
    );
  } finally {
    await control.dispose();
  }
}

const UNREACHABLE =
  `the scripted model is not answering at ${SCRIPTED_LLM}. Start the e2e stack: ` +
  '`docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d ' +
  'minio postgres redis scripted-llm platform worker`.';

let scriptedState: { ready: boolean; why: string } | null = null;

/**
 * Is this deployment answering from the scripted model? Each question has a
 * different fix, so each gets its own message. Only success is cached:
 * a blip while the stack warms must not skip every later spec.
 */
export async function scriptedModelReady(): Promise<{ ready: boolean; why: string }> {
  if (scriptedState) return scriptedState;
  const unservable = await probeScriptedServer();
  if (unservable) return { ready: false, why: unservable };

  return withAdmin(async (apiCtx, headers) => {
    const resp = await apiCtx.get('/api/v1/settings/llm-providers', { headers });
    if (!resp.ok()) {
      // Not an empty list: a warm-up 503 or a rejected token would otherwise
      // be reported as a missing provider, sending the reader to the wrong fix.
      return { ready: false, why: `the provider list answered ${resp.status()}.` };
    }
    const providers: { provider: string; available_models: string[] }[] = await resp.json();
    const scripted = providers.find(
      (p) => p.provider === SCRIPTED_PROVIDER && (p.available_models ?? []).includes(SCRIPTED_MODEL),
    );
    if (!scripted) {
      return {
        ready: false,
        why:
          `the deployment has no ${SCRIPTED_PROVIDER} provider serving "${SCRIPTED_MODEL}". Start ` +
          'the platform with LLM_PROVIDER_CUSTOM_OPENAI_API_KEY=none||http://scripted-llm:9099/v1 ' +
          `and LLM_PROVIDER_CUSTOM_OPENAI_MODELS=${SCRIPTED_MODEL}.`,
      };
    }
    // A turn names no provider, and `get_llm_for_user` resolves one through
    // agent, user preference, GA override and platform default before routing
    // by the model's owner. Rather than predict that, require the scripted
    // provider to be the only one there is.
    if (providers.length > 1) {
      return {
        ready: false,
        why:
          `${providers.length} providers are configured, so a turn naming none of them need not be ` +
          `scripted. Leave ${SCRIPTED_PROVIDER} as the only one.`,
      };
    }

    scriptedState = { ready: true, why: '' };
    return scriptedState;
  });
}

/**
 * A requirement rather than an option: a skip reason locally, a throw in CI.
 *
 * CI starts the service, so absence there is a broken job and skipping would
 * report success for a suite that asserted nothing. `a2a-setup` does the same.
 */
function fatalInCI(state: { ready: boolean; why: string }): string {
  if (state.ready) return '';
  if (process.env.CI) throw new Error(`The scripted model is required in CI, but ${state.why}`);
  return state.why;
}

/** The whole arrangement: the suite can arm it and the platform answers from it. */
export async function requireScriptedModel(): Promise<string> {
  return fatalInCI(await scriptedModelReady());
}

/** Reachability alone, for a spec that dials the endpoint rather than the platform. */
export async function requireScriptedServer(): Promise<string> {
  const unservable = await probeScriptedServer();
  return fatalInCI({ ready: !unservable, why: unservable });
}

/**
 * The gate every arming spec opens with: the model, then one platform flag.
 * Returns a skip reason, or '' to proceed.
 */
export async function scriptedSkipReason(flag: string, whenOff: string): Promise<string> {
  const missing = await requireScriptedModel();
  if (missing) return missing;
  return (await platformConfig())[flag] === false ? whenOff : '';
}

/** Throw with the body when a response failed. */
export async function ok(resp: APIResponse, what: string): Promise<APIResponse> {
  if (!resp.ok()) throw new Error(`${what} failed: ${resp.status()} ${await resp.text()}`);
  return resp;
}

/** Run `fn` with an admin context, disposing it afterwards. */
export async function withAdmin<T>(
  fn: (apiCtx: APIRequestContext, headers: Record<string, string>) => Promise<T>,
): Promise<T> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    return await fn(apiCtx, { Authorization: `Bearer ${token}` });
  } finally {
    await apiCtx.dispose();
  }
}

/**
 * An agent pinned to the scripted model. Named on the agent rather than left
 * to the platform default, which a second provider resolves by seeding order.
 */
export async function createScriptedAgent(name: string, persona: string): Promise<string> {
  return withAdmin(async (apiCtx, headers) => {
    const resp = await ok(
      await apiCtx.post('/api/v1/agents', {
        headers,
        data: { name, persona, llm_provider: SCRIPTED_PROVIDER, llm_model: SCRIPTED_MODEL },
      }),
      'create agent',
    );
    return (await resp.json()).id as string;
  });
}

/** Best-effort delete, for an `afterAll` that must not fail the run. */
export async function deleteAgent(agentId: string): Promise<void> {
  await withAdmin((apiCtx, headers) =>
    apiCtx.delete(`/api/v1/agents/${agentId}`, { headers }),
  ).catch(() => undefined);
}

let platformFlags: Record<string, unknown> | null = null;

/** Platform feature gates. Several agentic surfaces 403 rather than skip. */
export async function platformConfig(): Promise<Record<string, unknown>> {
  if (platformFlags) return platformFlags;
  return withAdmin(async (apiCtx, headers) => {
    const resp = await apiCtx.get('/api/v1/platform/config', { headers });
    // Only a real answer is cached: a memoised `{}` reads as "enabled" for
    // every later gate check.
    if (!resp.ok()) return {};
    const flags: Record<string, unknown> = await resp.json();
    platformFlags = flags;
    return flags;
  });
}
