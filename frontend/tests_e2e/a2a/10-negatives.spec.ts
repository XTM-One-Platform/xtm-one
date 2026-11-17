/**
 * The refusal surface, as one table.
 *
 * Every branch here answers "no", and they belong in one file for a reason
 * that is not filing convenience: A2A's security properties are stated almost
 * entirely as *sameness*. An unknown card token, a revoked exposure, a
 * disabled agent and a deleted agent must be byte-identical; a missing bearer,
 * a platform session JWT and another partner's credential must be
 * byte-identical; every token-endpoint rejection must be byte-identical. A
 * refusal asserted on its own passes while its neighbour has drifted into
 * being more helpful, and "more helpful" here means enumerable. So the
 * negatives are compared against each other, in one place, rather than each
 * against a remembered string.
 *
 * The bearer family is the one comparison split across two files:
 * 02-inbound-surface.spec.ts holds a missing bearer, a platform API key of
 * either prefix and another registration's partner token against one another,
 * and this file adds the platform session JWT. The two assert the same
 * literal, which is what keeps the four uniform.
 *
 * The second reason is cost. Each of these is one line of setup and one
 * assertion given a pair of live deployments, and a person walking the review
 * script skips them by the tenth walk-through. This is the half of the
 * feature nobody checks by hand.
 *
 * WHAT IS ASSERTED THROUGH WHICH SURFACE
 * --------------------------------------
 * Nearly everything here is API- or wire-observable, and is read there: a
 * refusal is a status code and a body, and asserting one through a rendered
 * page is a slower test of the same fact. The single UI test is the AI-gate
 * notice, which is the only place the *reason* A2A is unavailable is stated to
 * a person at all — the cards render themselves away and say nothing.
 *
 * Two branches in this area are observable from neither and are deliberately
 * absent rather than faked: the metric labels behind each refusal
 * (`/metrics` is off unless `PROMETHEUS_ENABLED`, and the audit log carries
 * the same facts), and the production SPA catch-all's protection of `/a2a`
 * (it is registered only when `backend/frontend_dist` exists, which it does
 * not in a dev checkout — what the suite takes from it is a rule, tested
 * here, not the catch-all itself).
 *
 * WHY THIS FILE SIGNS ITSELF IN
 * -----------------------------
 * The fixtures below — four registrations, five agents, two credentials and
 * two accounts — are far too expensive to rebuild per test, and `openMember`
 * is the non-admin context the harness has no notion of. `openAdmin` is a
 * local re-do of what `fixtures.ts` builds per test.
 */

import {
  request,
  type APIRequestContext,
  type APIResponse,
} from '@playwright/test';
import { test, expect, wireContext } from './fixtures';
import { SELF } from './instance';
import { marker } from '../scripted-llm/client';
import {
  METHOD,
  PartnerClient,
  cardTokenOf,
  mintToken,
  requestToken,
  sleep,
  type JsonRpcEnvelope,
} from './wire';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  createOutboundIntegration,
  exposeAgent,
  mintCredential,
  registrationState,
  stamp,
  tracker,
  verifyNow,
  type Created,
} from './seed';

// ---------------------------------------------------------------------------
// Local helpers — see the file header for why these are not in the harness.
// ---------------------------------------------------------------------------

/**
 * An admin control-plane context, signed in over the API.
 *
 * The session token comes back alongside it because one test presents it on
 * the A2A wire — where it must be refused exactly as a missing bearer is.
 */
async function openAdmin(): Promise<{ rest: APIRequestContext; token: string }> {
  const email = process.env.E2E_USER_EMAIL;
  const password = process.env.E2E_USER_PASSWORD;
  if (!email || !password) {
    throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set.');
  }
  const anon = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await anon.post('/api/v1/auth/login', { data: { email, password } });
    if (!login.ok()) {
      throw new Error(`admin login: ${login.status()} ${await login.text()}`);
    }
    const token = (await login.json()).access_token as string;
    return {
      token,
      rest: await request.newContext({
        baseURL: SELF.origin,
        extraHTTPHeaders: { Authorization: `Bearer ${token}` },
      }),
    };
  } finally {
    await anon.dispose();
  }
}

/**
 * A non-admin member, created and signed in, past the Terms of Use gate.
 *
 * The gate is chained into `get_current_user`, so a freshly created user's
 * every `/api/v1` call answers 403 "Terms of Use acceptance required" — which
 * would make each RBAC assertion below pass for entirely the wrong reason.
 * `a2a.setup.ts` clears it for the admin only.
 */
async function openMember(
  admin: APIRequestContext,
  email: string,
  password: string,
): Promise<{ id: string; rest: APIRequestContext }> {
  // A previous run that never reached its afterAll leaves the account behind,
  // and the email is unique — so clear it rather than fail on the create.
  const existing = await admin.get('/api/v1/users?limit=500');
  if (existing.ok()) {
    const body: unknown = await existing.json();
    const users = (Array.isArray(body) ? body : (body as { items?: unknown }).items) as
      | { id: string; email: string }[]
      | undefined;
    const leftover = users?.find((u) => u.email === email);
    if (leftover) await admin.delete(`/api/v1/users/${leftover.id}`);
  }
  const created = await admin.post('/api/v1/users', {
    data: { email, password, full_name: 'E2E A2A viewer', role: 'user', account_type: 'local' },
  });
  if (!created.ok()) {
    throw new Error(`member creation: ${created.status()} ${await created.text()}`);
  }
  const id = (await created.json()).id as string;

  const anon = await request.newContext({ baseURL: SELF.origin });
  let token: string;
  try {
    const login = await anon.post('/api/v1/auth/login', { data: { email, password } });
    if (!login.ok()) {
      throw new Error(`member login: ${login.status()} ${await login.text()}`);
    }
    token = (await login.json()).access_token as string;
    const terms = await anon.get('/api/v1/auth/terms', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (terms.ok()) {
      const state = await terms.json();
      if (state.required && !state.accepted) {
        await anon.post('/api/v1/auth/terms/accept', {
          headers: { Authorization: `Bearer ${token}` },
          data: { version: state.version },
        });
      }
    }
  } finally {
    await anon.dispose();
  }
  return {
    id,
    rest: await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    }),
  };
}

/** One JSON-RPC call whose body this file controls byte for byte. */
async function postRaw(
  ctx: APIRequestContext,
  cardToken: string,
  bearer: string,
  body: unknown,
): Promise<{ status: number; envelope: JsonRpcEnvelope }> {
  const resp = await ctx.post(`/a2a/${cardToken}/jsonrpc`, {
    headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
    // A Buffer, so the bytes reach the server exactly as written. Handing
    // Playwright a string with an application/json content type makes it
    // JSON-ENCODE that string — `{` arrives as `"{"`, which is valid JSON, and
    // the parse-error case quietly becomes the not-an-object case instead.
    // Both refusals exist, so the test still passed something; it simply
    // stopped testing the branch it names.
    data: Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8'),
  });
  return { status: resp.status(), envelope: (await resp.json()) as JsonRpcEnvelope };
}

/** Status, content type and body together — what "byte-identical" is asserted on. */
async function fingerprint(resp: APIResponse): Promise<string> {
  return `${resp.status()} ${resp.headers()['content-type']} ${await resp.text()}`;
}

function taskIdOf(envelope: JsonRpcEnvelope): string {
  const result = envelope.result as Record<string, unknown> | undefined;
  const id = result?.id;
  if (typeof id !== 'string') {
    throw new Error(`No task id in ${JSON.stringify(envelope).slice(0, 300)}`);
  }
  return id;
}

const VIEWER_PASSWORD = 'E2eA2aViewer!7';

/**
 * How long a web worker reuses its resolved AI state (`CACHE_TTL_SECONDS` in
 * backend/app/services/ai_state.py). A switch drops the cache of the worker
 * that served it only; every other worker converges within this bound.
 */
const AI_STATE_TTL_MS = 5_000;

test.describe.serial('A2A negatives', () => {
  let available = false;

  let rest: APIRequestContext;
  let wire: APIRequestContext;
  let sessionToken = '';
  const created: Created = tracker();

  // Family 1 — what a partner can address.
  let inbound: { id: string; name: string };
  let exposed: { id: string; name: string };
  let exposedCardUrl = '';
  let exposedTools: string[] = [];
  let partner: PartnerClient;
  /** The one real task this file submits, so the isolation test has a live id. */
  let submittedTaskId = '';

  // A second registration, bound to a second agent: the only way to hold a
  // credential that is genuinely valid and genuinely not for this exposure.
  let neighbourReg: { id: string; name: string };
  let neighbour: { id: string; name: string };
  let stranger: PartnerClient;

  // A third, whose binding this file takes away while its token is still
  // unexpired.
  let spareReg: { id: string; name: string };
  let spare: { id: string; name: string };
  let spareCredential: { clientId: string; clientSecret: string };
  let spareCardUrl = '';
  let spareToken = '';

  // Agents whose exposures exist only to be destroyed, on a registration of
  // their own so that revoking them cannot perturb what discovery reports for
  // `inbound`.
  let attic: { id: string; name: string };
  let dormantCardUrl = '';
  let orphanCardUrl = '';
  let ghostCardUrl = '';
  let dormant: { id: string; name: string };
  let orphan: { id: string; name: string };
  let ghost: { id: string; name: string };

  let companyInbound: { id: string; name: string };
  let outboundRegistration: { id: string; name: string };
  let plain: { id: string; name: string };

  let memberId = '';
  let member: APIRequestContext;

  // B — the side that reaches out.
  let partnerReg: { id: string; name: string };
  let partnerTargetId = '';
  let partnerFingerprint = '';
  let badReg: { id: string; name: string };
  let badTargetId = '';
  let caller: { id: string; name: string };

  /** The four card URLs an outbound registration is pointed at, one refusal each. */
  const badCardUrls = [
    // Loopback and http, on a port nothing can bind — the sharpest available
    // proof that the development exemption is origin-EXACT rather than a
    // blanket loopback hole. Derived from this deployment's own host rather
    // than written out: whoever starts it chooses the port, so a literal
    // `localhost:4000` would be the trusted origin itself on a run that bound
    // 4000, and the case would silently stop testing the refusal it names.
    `http://${new URL(SELF.origin).hostname}:1/a2a/nope/.well-known/agent-card.json`,
    'https://10.0.0.5/a2a/nope/.well-known/agent-card.json',
    'https://169.254.169.254/a2a/nope/.well-known/agent-card.json',
    'https://no-such-host.invalid/a2a/nope/.well-known/agent-card.json',
  ];

  test.beforeAll(async () => {
    const admin = await openAdmin();
    rest = admin.rest;
    sessionToken = admin.token;
    wire = await wireContext();
    available = await a2aAvailable(rest);
    if (!available) return;

    // ── A ────────────────────────────────────────────────────────────────
    inbound = await createInboundIntegration(rest, created, stamp('E2E negatives inbound'));
    // Named tools, and a management-capable one among them on purpose: the
    // card is asserted to advertise the list unnarrowed, which is what the
    // absence of a tool allow-list looks like from outside.
    // The Traces group id, not a deprecated alias: an alias is stored as the
    // group, so the card would not echo it back.
    exposedTools = ['observability', 'platform'];
    exposed = await createAgent(rest, created, {
      name: stamp('E2E negatives exposed'),
      extra: { builtin_tool_ids: exposedTools },
    });
    exposedCardUrl = await exposeAgent(rest, exposed.id, inbound.id);
    const inboundCredential = await mintCredential(rest, inbound.id);
    partner = await PartnerClient.connect(exposedCardUrl, inboundCredential);

    neighbourReg = await createInboundIntegration(rest, created, stamp('E2E negatives neighbour'));
    neighbour = await createAgent(rest, created, { name: stamp('E2E negatives neighbour agent') });
    const neighbourCardUrl = await exposeAgent(rest, neighbour.id, neighbourReg.id);
    stranger = await PartnerClient.connect(neighbourCardUrl,
      await mintCredential(rest, neighbourReg.id),
    );

    spareReg = await createInboundIntegration(rest, created, stamp('E2E negatives spare'));
    spare = await createAgent(rest, created, { name: stamp('E2E negatives spare agent') });
    spareCardUrl = await exposeAgent(rest, spare.id, spareReg.id);
    spareCredential = await mintCredential(rest, spareReg.id);
    spareToken = await mintToken(wire, spareCredential);

    attic = await createInboundIntegration(rest, created, stamp('E2E negatives attic'));
    dormant = await createAgent(rest, created, { name: stamp('E2E negatives dormant') });
    orphan = await createAgent(rest, created, { name: stamp('E2E negatives orphan') });
    ghost = await createAgent(rest, created, { name: stamp('E2E negatives ghost') });
    dormantCardUrl = await exposeAgent(rest, dormant.id, attic.id);
    orphanCardUrl = await exposeAgent(rest, orphan.id, attic.id);
    ghostCardUrl = await exposeAgent(rest, ghost.id, attic.id);

    // Company-managed, so a non-admin can SEE it and still be refused the
    // edit — the distinction the regenerate route makes.
    const company = await rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name: stamp('E2E negatives company inbound'),
        auth_type: 'none',
        a2a_direction: 'inbound',
        is_company_managed: true,
      },
    });
    expect(company.ok(), `company-managed inbound: ${company.status()} ${await company.text()}`).toBeTruthy();
    const companyBody = await company.json();
    companyInbound = { id: companyBody.id, name: companyBody.name };
    created.integrations.push(companyInbound.id);

    outboundRegistration = await createOutboundIntegration(rest, created, {
      cardUrls: exposedCardUrl,
      tokenUrl: `${SELF.origin}/mcp/oauth/token`,
      clientId: 'unused',
      clientSecret: 'unused',
      name: stamp('E2E negatives outbound'),
    });

    // A non-A2A provider that needs no credentials at all, so the "wrong
    // provider" refusals can be exercised without a vendor account.
    const mitre = await rest.post('/api/v1/integrations', {
      data: { provider: 'mitre_attack', name: stamp('E2E negatives non-A2A'), auth_type: 'none' },
    });
    expect(mitre.ok(), `non-A2A integration: ${mitre.status()} ${await mitre.text()}`).toBeTruthy();
    const mitreBody = await mitre.json();
    plain = { id: mitreBody.id, name: mitreBody.name };
    created.integrations.push(plain.id);

    const viewer = await openMember(rest, 'e2e-a2a-viewer@e2e-example.com', VIEWER_PASSWORD);
    memberId = viewer.id;
    member = viewer.rest;

    // ── B ────────────────────────────────────────────────────────────────
    const partnerRegName = stamp('E2E negatives partner');
    const registration = await rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name: partnerRegName,
        auth_type: 'none',
        a2a_direction: 'outbound',
        is_company_managed: true,
        extra_config: {
          card_urls: exposedCardUrl,
          token_url: `${SELF.origin}/mcp/oauth/token`,
          client_id: inboundCredential.clientId,
          client_secret: inboundCredential.clientSecret,
        },
      },
    });
    expect(
      registration.ok(),
      `outbound registration: ${registration.status()} ${await registration.text()}`,
    ).toBeTruthy();
    partnerReg = { id: (await registration.json()).id, name: partnerRegName };
    created.integrations.push(partnerReg.id);
    // Verified here rather than in a test: the accept-card refusals need a
    // target that HAS an observed fingerprint, and nothing on the create path
    // produces one.
    const verified = await verifyNow(rest, partnerReg.id);
    const verifiedTargets = (verified.targets as Record<string, unknown>[]) ?? [];
    partnerTargetId = String(verifiedTargets[0]?.id ?? '');
    partnerFingerprint = String(verifiedTargets[0]?.card_fingerprint ?? '');
    expect(
      partnerFingerprint,
      `the card was never read, so the accept-card refusals would test the wrong branch: ` +
        `${JSON.stringify(verifiedTargets[0] ?? {})}`,
    ).toBeTruthy();

    badReg = await createOutboundIntegration(rest, created, {
      cardUrls: badCardUrls,
      tokenUrl: `${SELF.origin}/mcp/oauth/token`,
      clientId: 'unused',
      clientSecret: 'unused',
      name: stamp('E2E negatives bad partner'),
    });
    // Target rows exist from the save, unverified — which is itself one of
    // the things asserted below, and is what makes an id available here.
    const badTargets = ((await registrationState(rest, badReg.id)).targets ??
      []) as Record<string, unknown>[];
    badTargetId = String(badTargets[0]?.id ?? '');

    caller = await createAgent(rest, created, { name: stamp('E2E negatives caller') });
    await bind(rest, caller.id, { oauth_a2a: [partnerReg.id] });

  });

  /**
   * The card, once EVERY web worker serves the AI switch flipped at
   * `switchedAt`.
   *
   * `ai_state` caches the resolved answer per process, and a switch drops the
   * cache of the worker that served it only. The stack runs two web workers
   * (`WEB_WORKERS`), so one answer in the new state proves one of them; the
   * other converges within `AI_STATE_TTL_MS` of the commit. So the card is
   * polled until it answers `status` AND that bound has passed: a worker still
   * holding the old answer would refuse the next request that lands on it,
   * in this file or in the next one, which verifies its partner against the
   * same deployment and gets a 403 it cannot explain.
   */
  async function awaitAiState(status: number, switchedAt: number): Promise<APIResponse> {
    const converged = switchedAt + AI_STATE_TTL_MS;
    let card = await wire.get(exposedCardUrl);
    while ((card.status() !== status || Date.now() < converged) && Date.now() < converged + 10_000) {
      await sleep(500);
      card = await wire.get(exposedCardUrl);
    }
    return card;
  }

  /**
   * Switch AI back on, and wait until every worker serves the card again.
   * Answers `undefined` when there is nothing to poll, which is the case where
   * the licence probe failed and no exposure was ever built.
   */
  async function restoreAi(): Promise<APIResponse | undefined> {
    if (!rest) return undefined;
    await rest.put('/api/v1/settings/platform', { data: { ai_enabled: true } });
    const switchedAt = Date.now();
    if (!exposedCardUrl) return undefined;
    return awaitAiState(200, switchedAt);
  }

  test.afterAll(async () => {
    // Unconditional: the AI-gate tests below leave a deployment-wide switch
    // off if either fails partway, or if the UI half skips for want of a
    // frontend.
    await restoreAi().catch(() => undefined);
    if (rest) await cleanup(rest, created);
    if (rest && memberId) await rest.delete(`/api/v1/users/${memberId}`).catch(() => undefined);
    await Promise.all(
      [member, partner, stranger].map((c) => c?.dispose().catch(() => undefined)),
    );
    await Promise.all([rest?.dispose(), wire?.dispose()]);
  });

  // -------------------------------------------------------------------------
  // The gate, and the thing that is not the gate
  // -------------------------------------------------------------------------

  test('the catalog offers the A2A tiles whatever the licence says', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The tile is not the gate, and a suite that skips on the tile is vacuous
    // on a deployment where every A2A call is about to 403. Asserted here so
    // that `a2aAvailable`'s choice of predicate has a test behind it.
    const catalog = await rest.get('/api/v1/integrations/catalog?category=integration');
    expect(catalog.ok()).toBeTruthy();
    const tiles = (await catalog.json()) as { provider: string; a2a_directions?: string[] }[];
    const tile = tiles.find((t) => t.provider === 'oauth_a2a');
    expect(tile, 'the catalog names oauth_a2a').toBeTruthy();
    expect(tile?.a2a_directions).toEqual(['inbound', 'outbound']);

    const config = await (await rest.get('/api/v1/platform/config')).json();
    expect(config.chat_enabled, 'chat_enabled is what actually gates A2A').toBe(true);
  });

  // -------------------------------------------------------------------------
  // The uniform not-found families
  // -------------------------------------------------------------------------

  test('an unknown token, a revoked, a disabled and a deleted exposure give the same card 404', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Three ways for a live exposure to stop existing, driven through the
    // real flush hook rather than simulated — which is the half the python
    // suite explicitly does not reach.
    const disabled = await rest.put(`/api/v1/agents/${dormant.id}`, { data: { enabled: false } });
    expect(disabled.ok(), `disabling the dormant agent: ${disabled.status()}`).toBeTruthy();
    await bind(rest, orphan.id, {});
    const removed = await rest.delete(`/api/v1/agents/${ghost.id}`);
    expect(removed.ok(), `deleting the ghost agent: ${removed.status()}`).toBeTruthy();
    created.agents = created.agents.filter((id) => id !== ghost.id);

    const unknown = `${SELF.origin}/a2a/${'0'.repeat(64)}/.well-known/agent-card.json`;
    const seen = await Promise.all(
      [unknown, dormantCardUrl, orphanCardUrl, ghostCardUrl].map(async (url) =>
        fingerprint(await wire.get(url)),
      ),
    );
    // Byte equality across the four, not merely four 404s: the property is
    // that a caller cannot tell "never existed" from "you lost access".
    expect(new Set(seen).size, `four card refusals, ${seen.length} distinct: ${seen.join(' | ')}`).toBe(1);
    expect(seen[0]).toBe('404 application/json {"detail":"Not Found"}');
  });

  test('a platform session JWT is refused on the A2A wire like no credential at all', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The bearer an operator's browser carries: signed by this deployment,
    // unexpired, and belonging to a real user who can administer the exposure
    // from the control plane. On the partner surface it is nobody, and it is
    // told so in the words a caller with no credential at all gets — the
    // literal below is the same one 02-inbound-surface.spec.ts asserts for a
    // missing bearer, a platform API key of either prefix and another
    // registration's partner token, which is what makes the four uniform.
    const session = await postRaw(wire, cardTokenOf(exposedCardUrl), sessionToken, {
      jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: crypto.randomUUID() },
    });
    expect(session.status).toBe(401);
    expect(session.envelope.error).toEqual({ code: -32600, message: 'Unauthenticated' });
  });

  // -------------------------------------------------------------------------
  // Protocol shaping
  // -------------------------------------------------------------------------

  // A method this surface does not serve, and a protocol version it does not
  // know, are refused in 02-inbound-surface.spec.ts — which names the real
  // v1.0 methods deliberately not served (`ListTasks`,
  // `agent/getAuthenticatedExtendedCard`) and pins the choice of -32009 over
  // -32006 for the version, both of which this file only sampled.
  test('a malformed request is answered in the envelope, one message per mistake', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const cardToken = cardTokenOf(exposedCardUrl);
    const bearer = partner.bearer;

    const cases: [string, unknown, number, string][] = [
      ['body is not JSON', '{', -32700, 'Malformed JSON'],
      // Batches are legal JSON-RPC 2.0 and are not served here; without the
      // shape check this raised an AttributeError 500 rather than refusing.
      ['body is a batch array', [{ jsonrpc: '2.0', id: 1, method: METHOD.get }], -32600,
        'Request must be a single JSON-RPC request object'],
      // Legal JSON that is no kind of request object. Same guard as the batch
      // and the same reason it exists: a `.get` on a str or an int is a 500.
      ['body is a bare JSON string', '"a string"', -32600,
        'Request must be a single JSON-RPC request object'],
      ['body is a bare JSON number', '42', -32600,
        'Request must be a single JSON-RPC request object'],
      ['params is not an object', { jsonrpc: '2.0', id: 1, method: METHOD.send, params: 5 }, -32602,
        'params must be an object'],
      ['message is not an object', { jsonrpc: '2.0', id: 1, method: METHOD.send, params: { message: 'x' } },
        -32602, 'message must be an object'],
      ['parts is not an array',
        { jsonrpc: '2.0', id: 1, method: METHOD.send, params: { message: { parts: 'x' } } },
        -32602, 'parts must be an array'],
      ['messageId is absent',
        { jsonrpc: '2.0', id: 1, method: METHOD.send, params: { message: { parts: [{ kind: 'text', text: 'hi' }] } } },
        -32602, 'messageId is required'],
      // 129 characters. The column is String(128), and the refusal has to
      // land before the dedup claim: reaching the flush would be a DataError,
      // a 500 with the claim already taken.
      ['messageId is over the column width',
        {
          jsonrpc: '2.0', id: 1, method: METHOD.send,
          params: { message: { parts: [{ kind: 'text', text: 'hi' }], messageId: 'x'.repeat(129) } },
        },
        -32602, 'messageId must be at most 128 characters'],
    ];

    // The id comes back only when there was a single request object to read
    // one from; a parse failure, a batch and a bare scalar all answer null,
    // because there is nothing to echo and inventing one would make the
    // refusal look like an answer to a request the server never saw.
    const echoedId = (body: unknown): number | null =>
      typeof body === 'object' && body !== null && !Array.isArray(body)
        ? ((body as { id?: number }).id ?? null)
        : null;

    for (const [what, body, code, message] of cases) {
      const { status, envelope } = await postRaw(wire, cardToken, bearer, body);
      // HTTP 200 with an error envelope, not an HTTP error: partner tooling
      // branches on error.code.
      expect(status, what).toBe(200);
      expect(envelope.jsonrpc, what).toBe('2.0');
      expect(envelope.id, what).toBe(echoedId(body));
      expect(envelope.error, what).toMatchObject({ code, message });
    }
  });

  // -------------------------------------------------------------------------
  // Discovery
  // -------------------------------------------------------------------------

  test('partner discovery answers not-found to a caller that does not authenticate', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const anonymous = await partner.discover({ authenticated: false });
    const garbage = await wire.get('/a2a/partner/agents', {
      headers: { Authorization: 'Bearer not-a-token' },
    });
    const unknownPath = await wire.get('/a2a/partner/nothing-here');

    expect(await fingerprint(anonymous)).toBe('404 application/json {"detail":"Not Found"}');
    expect(await fingerprint(garbage)).toBe(await fingerprint(anonymous));
    // Byte-identical to a request for a path that does not exist: an
    // unauthenticated caller learns nothing, not even that discovery is here.
    expect(await unknownPath.text()).toBe(await anonymous.text());
  });

  test('partner discovery names only the agents this credential reaches', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await partner.discover();
    expect(resp.status()).toBe(200);
    const agents = ((await resp.json()).agents ?? []) as { name: string; card_url: string }[];
    // Exactly one. The neighbour is exposed on another registration and the
    // attic's agents on a third, so anything else here is the route reporting
    // on the deployment rather than on this credential's own reach.
    expect(agents.map((a) => a.name)).toEqual([exposed.name]);
    expect(agents[0].card_url).toBe(exposedCardUrl);
  });

  // -------------------------------------------------------------------------
  // Submission-time refusals, and what they write
  // -------------------------------------------------------------------------

  test('a platform API key is refused like any bad bearer, and audited as itself', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const query = '/api/v1/audit-logs?entity_type=a2a_exposure&action=reject&limit=1';
    const before = (await (await rest.get(query)).json()).total as number;

    const refused = await postRaw(wire, cardTokenOf(exposedCardUrl), 'fcp-anything-at-all', {
      jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: crypto.randomUUID() },
    });
    // Identical to a missing bearer. Only the trail knows the difference —
    // and it is the signal an operator needs during a migration, an internal
    // integration still presenting its unscoped key where it does not belong.
    expect(refused.status).toBe(401);
    expect(refused.envelope.error).toEqual({ code: -32600, message: 'Unauthenticated' });

    const after = await (await rest.get(query)).json();
    expect(after.total).toBe(before + 1);
    const newest = (after.items as {
      user_email: string; summary: string; details: Record<string, unknown> | null;
    }[])[0];
    expect(newest.details?.reason).toBe('platform_api_key');
    // Anchored on the run-as user, with the partner named in the email slot.
    expect(newest.user_email).toBe('a2a:anonymous');
    expect(newest.summary).toBe(
      'Rejected a platform API key presented to the external A2A task surface; ' +
        'only partner credentials are accepted',
    );
  });

  // Two files own two halves of deduplication and neither is the other's
  // duplicate: this one owns "the retry started no second run" and the pair of
  // audit rows, 02-inbound-surface.spec.ts owns the callback the retry
  // declares and silently loses. Deleting either as redundant loses a branch.
  test('a repeated messageId returns the first task, starts no second run and is audited as a duplicate', async ({
    model,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The one real submission in this file. Scripted so that "how many turns
    // did the agent take" is a number this test owns, which is what turns
    // "the duplicate started no run" into an assertion rather than a hope.
    await model.reset();
    const mark = marker('negatives-dup');
    await model.arm(mark, [{ text: 'Answered once.' }]);

    const messageId = `dup-${crypto.randomUUID()}`;
    const first = await partner.send(`Please note ${mark} and answer.`, { messageId });
    const taskId = taskIdOf(first.body);
    submittedTaskId = taskId;
    await partner.waitForState(taskId, ['completed', 'failed']);
    const turnsAfterFirst = await model.turnsServed(mark);
    expect(turnsAfterFirst, 'the first submission ran the agent').toBeGreaterThanOrEqual(1);

    const repeat = await partner.send(`Please note ${mark} and answer.`, { messageId });
    expect(taskIdOf(repeat.body), 'a duplicate returns the FIRST task').toBe(taskId);
    // The dedup claim is a Redis SET NX with the (client_id, messageId)
    // unique index as the real guarantee, so only a live Postgres and a live
    // Redis prove the two agree — which is the gap the python suite's
    // test_concurrent_retries_start_exactly_one_run declares it cannot close.
    expect(await model.turnsServed(mark), 'a duplicate takes no second run').toBe(turnsAfterFirst);

    const audit = await rest.get('/api/v1/audit-logs?entity_type=a2a_task&action=trigger&limit=200');
    const mine = ((await audit.json()).items as {
      entity_id: string; user_email: string; details: Record<string, unknown> | null;
    }[]).filter((row) => row.entity_id === taskId);
    // Both attempts are recorded: an operator must be able to see a partner
    // retrying, not only that a task exists.
    expect(mine.length, 'one submission row per attempt, duplicate included').toBe(2);
    expect(mine.map((r) => r.details?.duplicate === true).sort()).toEqual([false, true]);
    expect(new Set(mine.map((r) => r.user_email)).size, 'both rows name the same partner').toBe(1);
    expect(mine[0].user_email).toMatch(/^a2a:/);
  });

  test('a real task belonging to another partner is as absent as one that never existed', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    expect(submittedTaskId, 'the dedup test must have submitted a task first').toBeTruthy();

    // The interesting half, and the one no single-registration test can
    // reach: `stranger` holds a valid, unexpired credential for a DIFFERENT
    // inbound registration and asks for a task id that genuinely exists.
    // The client id is a predicate in the lookup query rather than a check
    // applied to the row that was found, so the row simply does not resolve.
    const mine = await partner.getTask(submittedTaskId);
    expect(mine.body.error, 'the owner still reads its own task').toBeUndefined();

    const theirs = await stranger.getTask(submittedTaskId);
    const invented = await partner.getTask(crypto.randomUUID());
    expect(theirs.status).toBe(200);
    expect(theirs.body.error).toEqual({
      code: -32001,
      message: 'Not found',
      data: { '@type': 'TaskNotFoundError' },
    });
    expect(theirs.body.error, 'ownership must not be probeable').toEqual(invented.body.error);

    // Same code, same @type, DIFFERENT message: `message/send` naming an
    // unknown taskId says "Task not found" where `tasks/get` says "Not
    // found". A partner branching on prose sees a distinction the contract
    // says is not there — pinned as observed, and reported as a defect.
    const continued = await partner.send('anything', { taskId: crypto.randomUUID() });
    expect(continued.body.error?.code).toBe(-32001);
    expect(continued.body.error?.message).toBe('Task not found');
  });

  test('a file part is rejected, and the same messageId may then be resent', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const messageId = `reject-${crypto.randomUUID()}`;
    const { envelope } = await postRaw(wire, cardTokenOf(exposedCardUrl), partner.bearer, {
      jsonrpc: '2.0', id: 1, method: METHOD.send,
      params: { message: { role: 'user', messageId, parts: [{ kind: 'file', file: { name: 'x.bin' } }] } },
    });
    expect(envelope.error).toEqual({
      code: -32005,
      message: 'This agent accepts text and structured data only',
      data: { '@type': 'ContentTypeNotSupportedError' },
    });

    // The rejection was recorded as a task, and dedup excludes rejected rows,
    // so the corrected retry starts fresh rather than returning the refusal.
    // The partial unique index (`rejected_reason IS NULL`) is what makes the
    // second insert legal, and only a real Postgres proves it.
    const retried = await partner.send('Plain text this time.', { messageId });
    expect(retried.body.error, JSON.stringify(retried.body.error)).toBeUndefined();
    expect(taskIdOf(retried.body)).toBeTruthy();
  });

  test('a callback declared on the submission itself is refused by the destination guard', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const cardToken = cardTokenOf(exposedCardUrl);

    // `configuration.pushNotificationConfig` on `message/send` is the second
    // call site of the guard and the easier one to leave unguarded, because
    // the submission succeeds in every other respect. The `pushSet` call site,
    // the scheme refusal and the five-configuration ceiling are in
    // 02-inbound-surface.spec.ts, whose subject is the inbound surface.
    // https on the address cases, deliberately. The guard checks the SCHEME
    // first and returns on it, so an http cloud-metadata URL is refused for
    // being http and the address check never runs — a test written with http
    // throughout would pass while proving only that http is refused, three
    // times over. The last row pins that ordering rather than leaving it as
    // folklore.
    for (const [url, why] of [
      ['https://169.254.169.254/latest/meta-data/', 'Refusing to reach 169.254.169.254 — cloud metadata endpoint.'],
      ['https://10.0.0.5/hook', 'Refusing to reach 10.0.0.5 — private address.'],
      ['http://169.254.169.254/latest/meta-data/', "A2A partner URLs must use https, got 'http'."],
    ] as const) {
      const { envelope } = await postRaw(wire, cardToken, partner.bearer, {
        jsonrpc: '2.0', id: 1, method: METHOD.send,
        params: {
          message: { role: 'user', messageId: crypto.randomUUID(), parts: [{ kind: 'text', text: 'hi' }] },
          configuration: { pushNotificationConfig: { url } },
        },
      });
      expect(envelope.error?.code, url).toBe(-32602);
      expect(envelope.error?.message, url).toBe(why);
    }
  });

  // -------------------------------------------------------------------------
  // Credentials and the identity boundary
  // -------------------------------------------------------------------------

  test('every token-endpoint rejection is the same invalid_client', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const unknownClient = await requestToken(wire, {
      clientId: `no-such-${crypto.randomUUID()}`,
      clientSecret: 'whatever',
    });
    const wrongSecret = await requestToken(wire, {
      clientId: spareCredential.clientId,
      clientSecret: 'definitely-not-the-secret',
    });
    expect(await fingerprint(unknownClient)).toBe('401 application/json {"error":"invalid_client"}');
    // Byte equality, not merely two 401s: telling "no such client" from
    // "wrong secret" apart would make registrations enumerable for free.
    expect(await fingerprint(wrongSecret)).toBe(await fingerprint(unknownClient));

    // The one rejection that is deliberately distinct — the request is
    // malformed rather than unauthorised, which reveals nothing.
    const missing = await wire.post('/mcp/oauth/token', {
      form: { grant_type: 'client_credentials', client_id: spareCredential.clientId },
    });
    expect(await fingerprint(missing)).toBe('400 application/json {"error":"invalid_request"}');
  });

  test('a partner token authenticates nowhere but the A2A surface', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The token's `sub` IS the exposure's run-as user, so without the `type`
    // and `scope` claims it would authenticate as that person everywhere.
    // This is the single most security-relevant negative in the feature.
    const carrying = await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${partner.bearer}` },
    });
    try {
      for (const path of [
        '/api/v1/agents',
        '/api/v1/users',
        '/api/v1/integrations',
        `/api/v1/agents/${exposed.id}/a2a`,
      ]) {
        const resp = await carrying.get(path);
        expect(resp.status(), path).toBe(401);
        expect((await resp.json()).detail, path).toBe('Invalid token');
      }
      // Including the write that would let a partner expose further agents or
      // mint further credentials.
      const escalate = await carrying.post('/api/v1/integrations', {
        data: { provider: 'oauth_a2a', name: 'should never exist', auth_type: 'none', a2a_direction: 'inbound' },
      });
      expect(escalate.status()).toBe(401);
    } finally {
      await carrying.dispose();
    }
  });

  test('the card advertises every built-in the agent carries, narrowed by nothing', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const card = await (await wire.get(exposedCardUrl)).json();
    const skills = (card.skills as { id: string }[]).map((s) => s.id);
    // There is no tool allow-list on external work: the run is bounded by the
    // run-as identity alone, and the card says so by listing the agent's own
    // built-ins in full, `platform` (the agent builder) included. The two places that once
    // claimed otherwise now say this instead, and the Config tab says it as a
    // warning where an operator binds: see the "Bounds enforced on every
    // request" assertion in 04.
    expect(skills).toEqual(exposedTools);
  });

  test('unbinding retires the card, the credential and the discovery entry at once', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const before = await wire.get(spareCardUrl);
    expect(before.status(), 'the spare exposure served before unbinding').toBe(200);

    await bind(rest, spare.id, {});

    // The exposure is resolved from the card token BEFORE the credential is
    // authenticated, so the still-unexpired token cannot tell this apart from
    // an unknown task — which is exactly what the uniform envelope is for.
    expect(await fingerprint(await wire.get(spareCardUrl))).toBe(
      '404 application/json {"detail":"Not Found"}',
    );
    const rpc = await postRaw(wire, cardTokenOf(spareCardUrl), spareToken, {
      jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: crypto.randomUUID() },
    });
    expect(rpc.envelope.error?.code).toBe(-32001);

    // The token still authenticates — it is a signed bearer, not a session —
    // and reaches nothing. 200 with an empty list, distinguishable from the
    // 404 an unauthenticated caller gets, which is the discovery route's only
    // positive statement about a credential.
    const discovery = await wire.get('/a2a/partner/agents', {
      headers: { Authorization: `Bearer ${spareToken}` },
    });
    expect(discovery.status()).toBe(200);
    expect((await discovery.json()).agents).toEqual([]);

    // And no NEW token can be minted, because the credential no longer
    // addresses a surface that answers.
    expect(await fingerprint(await requestToken(wire, spareCredential))).toBe(
      '401 application/json {"error":"invalid_client"}',
    );
  });

  // -------------------------------------------------------------------------
  // Rate limiting, and where the protocol lives
  // -------------------------------------------------------------------------

  test('the anonymous budget refuses with Retry-After once it is spent', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Driven deliberately rather than asserted from a constant, and bounded:
    // the anonymous bucket is per client IP, every worker of this suite is one
    // address, and a spent bucket would poison every other A2A spec in the
    // same minute. 90 is comfortably past the 60/min default and cheap.
    let throttled: APIResponse | null = null;
    for (let i = 0; i < 90 && throttled === null; i += 1) {
      const resp = await wire.get(exposedCardUrl);
      if (resp.status() === 429) throttled = resp;
    }
    test.skip(
      throttled === null,
      'the run raises A2A_UNAUTHENTICATED_RATE_LIMIT to 100000 — the partner is this same ' +
        'deployment, so every delegation shares the bucket — and nothing lets a spec lower it, ' +
        'so the refusal is out of reach here; it is pinned in ' +
        'backend/tests/test_a2a_dedup_and_limits.py',
    );
    // REST-shaped, not JSON-RPC: the card route has no envelope contract, so
    // the refusal is a plain HTTPException with the retry hint intact.
    expect(throttled?.headers()['retry-after']).toBeTruthy();
    expect((await throttled!.json()).detail).toBe(
      'Too many requests. Please slow down and try again shortly.',
    );
  });

  test('an unknown card id answers not-found as JSON', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const card = await wire.get(`${SELF.origin}/a2a/${'0'.repeat(64)}/.well-known/agent-card.json`);
    expect(card.status()).toBe(404);
    expect(card.headers()['content-type']).toContain('application/json');
  });

  // -------------------------------------------------------------------------
  // Authority
  // -------------------------------------------------------------------------

  test('an agent the caller may not manage answers not-found, never forbidden', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const notYours = await member.get(`/api/v1/agents/${exposed.id}/a2a`);
    const notThere = await member.get(`/api/v1/agents/${crypto.randomUUID()}/a2a`);
    // "Not exposed", "not yours" and "does not exist" must stay
    // indistinguishable, so the two answers are compared to each other.
    expect(await fingerprint(notYours)).toBe('404 application/json {"detail":"Agent not found"}');
    expect(await fingerprint(notThere)).toBe(await fingerprint(notYours));
  });

  test('regenerating a credential is refused three ways', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const wrongProvider = await rest.post(`/api/v1/integrations/${plain.id}/a2a-credential`);
    expect(wrongProvider.status()).toBe(422);
    expect((await wrongProvider.json()).detail).toBe(
      'Only OAuth A2A integrations carry a partner credential.',
    );

    // Direction, not just provider: an outbound registration presents a
    // credential the partner issued, and minting would overwrite it.
    const wrongDirection = await rest.post(`/api/v1/integrations/${outboundRegistration.id}/a2a-credential`);
    expect(wrongDirection.status()).toBe(422);
    expect((await wrongDirection.json()).detail).toBe(
      'Only an inbound A2A integration mints a partner credential. ' +
        'An outbound one presents credentials the partner issued you — ' +
        "edit them in the integration's configuration.",
    );

    const notAnAdmin = await member.post(`/api/v1/integrations/${companyInbound.id}/a2a-credential`);
    expect(notAnAdmin.status()).toBe(403);
    expect((await notAnAdmin.json()).detail).toBe('Only admins can edit company-managed integrations');
  });

  test('the client surface is not-found for an inbound registration and for a non-A2A provider', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Reported as not-found rather than as a direction error, so this surface
    // never speaks about the other direction. The frontend reads exactly this
    // 404 as "inbound".
    for (const id of [inbound.id, plain.id]) {
      const resp = await rest.get(`/api/v1/a2a/client/${id}`);
      expect(await fingerprint(resp), id).toBe('404 application/json {"detail":"Not Found"}');
    }
  });

  test('a viewer reads a registration\'s verification state and may not act on it', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Relaxed on exactly one read, because a 403 there replaced the whole
    // detail page for a viewer who could already see the integration.
    const read = await member.get(`/api/v1/a2a/client/${partnerReg.id}`);
    expect(read.status()).toBe(200);
    expect(Array.isArray((await read.json()).targets)).toBe(true);

    // Everything else keeps manage authority — the delegation history
    // included, because it names agents, which are scoped in their own right.
    const refusals: [string, APIResponse][] = [
      ['delegations', await member.get(`/api/v1/a2a/client/${partnerReg.id}/delegations`)],
      ['verify', await member.post(`/api/v1/a2a/client/${partnerReg.id}/verify`)],
      ['accept-card', await member.post(`/api/v1/a2a/client/${partnerReg.id}/accept-card`, {
        data: { target_id: partnerTargetId, fingerprint: partnerFingerprint },
      })],
    ];
    for (const [what, resp] of refusals) {
      expect(resp.status(), what).toBe(403);
      expect((await resp.json()).detail, what).toBe(
        "You don't have permission to manage this integration.",
      );
    }
  });

  // -------------------------------------------------------------------------
  // The outbound side's refusals
  // -------------------------------------------------------------------------

  test('an unverified partner is advertised as bringing nothing', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(rest, badReg.id);
    const targets = (state.targets as Record<string, unknown>[]) ?? [];
    expect(targets.length).toBe(badCardUrls.length);
    // The panel says what the model is offered and nothing more: the builder
    // skips a target that is not usable, so naming one would promise a tool no
    // bound agent gets. The cancel tool follows the delegation tools rather
    // than the rows, which is what keeps the panel's own empty state — "None
    // yet — no agent at this partner has verified." — reachable. The rows stay
    // listed: an operator cannot press Verify on what the page hides.
    expect(targets.every((t) => t.tool_name === null || t.tool_name === undefined)).toBe(true);
    expect(state.cancel_tool_name ?? null).toBeNull();
    expect(targets.every((t) => t.usability === 'unusable')).toBe(true);
  });

  test('an outbound destination is refused with one machine-readable reason each', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await verifyNow(rest, badReg.id);
    const byUrl = new Map(
      ((state.targets as Record<string, unknown>[]) ?? []).map((t) => [String(t.card_url), t]),
    );

    const expected: [string, string, string][] = [
      [badCardUrls[0], 'transport_unverified', "A2A partner URLs must use https, got 'http'."],
      [badCardUrls[1], 'destination_not_permitted', 'Refusing to reach 10.0.0.5 — private address.'],
      // Exempt from the trusted-origins escape hatch by construction: this is
      // where cloud credentials live, so no environment variable may reach it.
      [badCardUrls[2], 'destination_not_permitted',
        'Refusing to reach 169.254.169.254 — cloud metadata endpoint.'],
      [badCardUrls[3], 'card_unfetchable', 'Cannot resolve no-such-host.invalid.'],
    ];
    for (const [url, reason, error] of expected) {
      const target = byUrl.get(url);
      expect(target, url).toBeTruthy();
      expect(target?.usability, url).toBe('unusable');
      expect(target?.unusable_reason, url).toBe(reason);
      expect(String(target?.last_verification_error ?? ''), url).toContain(error);
    }
  });

  test('accept-card refuses a foreign target, a stale fingerprint and nothing to accept', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // A target id arrives in a request body, so belonging to THIS registration
    // is checked as well as existing — and answered as not-found, because
    // another registration's target is someone else's trust decision.
    const foreign = await rest.post(`/api/v1/a2a/client/${partnerReg.id}/accept-card`, {
      data: { target_id: badTargetId, fingerprint: partnerFingerprint },
    });
    expect(await fingerprint(foreign)).toBe('404 application/json {"detail":"Not Found"}');

    // A trust decision applies to ONE card, not to whichever arrives next.
    const stale = await rest.post(`/api/v1/a2a/client/${partnerReg.id}/accept-card`, {
      data: { target_id: partnerTargetId, fingerprint: 'nope' },
    });
    expect(stale.status()).toBe(409);
    expect((await stale.json()).detail).toBe(
      'That fingerprint is not the card currently observed. Re-read the change before accepting it.',
    );

    const nothing = await rest.post(`/api/v1/a2a/client/${badReg.id}/accept-card`, {
      data: { target_id: badTargetId, fingerprint: 'anything' },
    });
    expect(nothing.status()).toBe(409);
    expect((await nothing.json()).detail).toBe('Nothing to accept.');
  });

  // -------------------------------------------------------------------------
  // Direction and federation configuration
  // -------------------------------------------------------------------------

  test('a2a_direction is validated on create and on update', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const refused = await rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name: stamp('E2E negatives sideways'),
        auth_type: 'none',
        a2a_direction: 'sideways',
      },
    });
    expect(refused.status()).toBe(400);
    expect((await refused.json()).detail).toBe("a2a_direction must be 'inbound' or 'outbound'.");

    const updated = await rest.put(`/api/v1/integrations/${plain.id}`, {
      data: { a2a_direction: 'inbound' },
    });
    expect(updated.status()).toBe(400);
    expect((await updated.json()).detail).toBe('a2a_direction applies only to A2A integrations.');
  });

  test('direction cannot change while an agent binds the registration, and the refusal writes nothing', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const renamed = `${partnerReg.name} renamed`;
    const resp = await rest.put(`/api/v1/integrations/${partnerReg.id}`, {
      data: { a2a_direction: 'inbound', name: renamed },
    });
    // 409, not 400: the request is well formed and the operator has authority.
    expect(resp.status()).toBe(409);
    expect((await resp.json()).detail).toBe(
      `Unbind this integration before changing its direction. Currently bound to: ${caller.name}.`,
    );

    // The direction is applied before any other field precisely so a refusal
    // leaves the integration untouched — this half is what a unit test on the
    // service cannot see.
    const after = await (await rest.get(`/api/v1/integrations/${partnerReg.id}`)).json();
    expect(after.name).not.toBe(renamed);
  });

  test('inbound federation pins are refused on save, one message per mistake', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The same parser the auth path runs per request, so an integration this
    // API accepts is exactly one whose tokens the task surface will consider.
    // No vendor account is needed to prove it: the refusals are all local.
    const cases: [string, Record<string, unknown>, string][] = [
      ['entra_a2a', { client_id: crypto.randomUUID(), audience: 'api://xtm' },
        'The integration is missing its partner tenant (directory) ID.'],
      ['entra_a2a', { tenant_id: 'not-a-guid', client_id: crypto.randomUUID(), audience: 'api://xtm' },
        "The integration's partner tenant (directory) ID is not a GUID."],
      ['entra_a2a', { tenant_id: crypto.randomUUID(), client_id: crypto.randomUUID() },
        'The integration is missing its audience.'],
      ['entra_a2a',
        { tenant_id: crypto.randomUUID(), client_id: crypto.randomUUID(), audience: 'https://graph.microsoft.com' },
        'This audience is shared by every application in the tenant; ' +
          'use a dedicated app registration representing this platform.'],
      ['google_a2a', { audience: 'https://xtm.example' },
        'The integration is missing its allowed caller (service account). ' +
          'Google signs tokens for every identity with one keyset, so ' +
          'without this pin any Google account could call.'],
      ['google_a2a', { audience: 'https://xtm.example', service_account: 'foundry-a2a' },
        "The allowed caller 'foundry-a2a' is neither a service account address " +
          '(name@project.iam.gserviceaccount.com) nor a numeric subject id — ' +
          "no Google token can carry it. Copy the exact value from the partner's service account."],
    ];
    for (const [provider, extra_config, detail] of cases) {
      const resp = await rest.post('/api/v1/integrations', {
        data: {
          provider,
          name: stamp(`E2E negatives ${provider}`),
          auth_type: 'none',
          a2a_direction: 'inbound',
          extra_config,
        },
      });
      expect(resp.status(), detail).toBe(422);
      expect((await resp.json()).detail, detail).toBe(detail);
    }

    // The same pins are validated on the update path, which is a separate
    // call site and has been missed before.
    const good = await rest.post('/api/v1/integrations', {
      data: {
        provider: 'google_a2a',
        name: stamp('E2E negatives google ok'),
        auth_type: 'none',
        a2a_direction: 'inbound',
        extra_config: { audience: 'https://xtm.example', service_account: 'a@p.iam.gserviceaccount.com' },
      },
    });
    expect(good.status(), await good.text()).toBe(201);
    const goodId = (await good.json()).id as string;
    created.integrations.push(goodId);
    const broken = await rest.put(`/api/v1/integrations/${goodId}`, {
      data: { extra_config: { audience: 'https://xtm.example', service_account: 'foundry-a2a' } },
    });
    expect(broken.status()).toBe(422);
  });

  // -------------------------------------------------------------------------
  // The AI gate. Last, because it turns a deployment-wide switch off, and
  // `afterAll` turns it back on whatever happens here.
  // -------------------------------------------------------------------------

  test('AI switched off refuses both A2A surfaces with a stated code', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const off = await rest.put('/api/v1/settings/platform', { data: { ai_enabled: false } });
    const switchedAt = Date.now();
    expect(off.ok(), `disabling AI: ${off.status()} ${await off.text()}`).toBeTruthy();

    // Every worker, not only the one that served the switch: the jsonrpc call
    // below lands on either.
    const card = await awaitAiState(403, switchedAt);
    // Stated rather than folded into the uniform 404, deliberately: the AI
    // state is already public on /platform/config, so saying so reveals
    // nothing, and a partner whose integration worked yesterday is told why.
    expect(card.status()).toBe(403);
    expect((await card.json()).detail).toEqual({
      code: 'ai_disabled',
      message: 'AI is disabled on this deployment.',
    });

    const rpc = await wire.post(`/a2a/${cardTokenOf(exposedCardUrl)}/jsonrpc`, {
      headers: { Authorization: `Bearer ${partner.bearer}` },
      data: { jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: crypto.randomUUID() } },
    });
    // Not a JSON-RPC envelope: the refusal happens before the body is parsed
    // or the exposure resolved, so there is no request to answer in kind.
    expect(rpc.status()).toBe(403);
    expect((await rpc.json()).detail).toEqual({
      code: 'ai_disabled',
      message: 'AI is disabled on this deployment.',
    });
  });

  test('the UI names AI as the reason A2A is unavailable', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Every A2A card renders itself away while chat is unavailable; this
    // notice is the only line that says why, and it is the one thing in this
    // file that no API assertion can stand in for. Skipping it must not leave
    // AI off for the next file, which is why `restoreAi` is the afterAll's job
    // as well as this test's tail.
    await one.page.goto(`/integrations/${inbound.id}`);
    await expect(
      one.page.getByText('Agent-to-Agent interoperability is unavailable while AI is disabled.'),
    ).toBeVisible();
    await expect(
      one.page.getByText(
        'This partner registration is kept, but nothing is served to or sent to the partner ' +
          'until agentic chat is available again.',
      ),
    ).toBeVisible();

    expect(
      (await restoreAi())?.status(),
      'AI back on restores the card with no other action',
    ).toBe(200);
  });
});
