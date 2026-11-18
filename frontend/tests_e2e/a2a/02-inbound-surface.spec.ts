/**
 * The inbound surface, played from the wire.
 *
 * WHY THESE BRANCHES BELONG TOGETHER
 * ----------------------------------
 * Everything here is reachable by holding a URL and, for half of it, a
 * credential. That is the whole definition of this file's subject: what a
 * stranger, or a partner, can learn and do by addressing this deployment
 * directly. The test *is* the partner — it mints a credential over the control
 * plane, exchanges it at the token endpoint and drives JSON-RPC — so no model
 * takes a turn and no outbound registration is involved. That makes this the
 * fastest lane in the suite and the one with the least to flake on.
 *
 * It is deliberately mostly negatives. The positives here are a handful of
 * shapes a partner's parser depends on; the refusals are the security
 * property, and they are exactly what a person walking the review script
 * skips on the tenth pass. A retired card token, a deactivated run-as user,
 * another registration's credential and another registration's task must be
 * indistinguishable from one another, and only a live surface proves that — a
 * unit test asserting each call site returns the same helper does not prove
 * the call sites are all reached. The byte-for-byte comparison of the whole
 * not-found family, and of the token endpoint's rejections, is in
 * 10-negatives.spec.ts; what is here is the arms that need this file's
 * fixtures.
 *
 * WHAT IS ASSERTED, AND AGAINST WHAT
 * ----------------------------------
 * The code's behaviour, not the docs'. Three places where they disagree are
 * pinned here with the disagreement stated in a comment rather than quietly
 * asserted away: the module docstring's claim that every "what exists here"
 * negative is one uniform 404 (a known card token with no credential answers
 * 401 while an unknown one answers 200/-32001 — see the authentication test);
 * a2a-review-scenarios.md's claim that card skills are narrowed to what the
 * exposure permits (they are not); and the silent loss of a callback declared
 * on a duplicate submission.
 */

import { request, type APIRequestContext } from '@playwright/test';
import { test, expect } from './fixtures';
import { SELF } from './instance';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  createOutboundIntegration,
  exposeAgent,
  exposureOf,
  mintCredential,
  stamp,
  tracker,
} from './seed';
import {
  METHOD,
  PartnerClient,
  cardTokenOf,
  requestToken,
  type JsonRpcEnvelope,
} from './wire';

// ---------------------------------------------------------------------------
// Local helpers.
//
// These four are here rather than in the harness because they serve this file
// alone: signing in as somebody other than the admin is what the deactivated
// run-as branch needs and nothing else does, and the wire shapes below are the
// inbound server's own — no other spec reads a card or an artifact.
// ---------------------------------------------------------------------------

/**
 * A control-plane context for one account.
 *
 * The `one` fixture's `rest` is the admin's and is test-scoped, so it cannot
 * be used from `beforeAll` — a test-scoped fixture resolved in a hook is torn
 * down the moment the hook returns. Fixtures are built here instead, and the
 * tests themselves use `one.rest` / `one.wire` as usual.
 */
async function signIn(email: string, password: string): Promise<APIRequestContext> {
  if (!email || !password) {
    throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set — this file signs in to A.');
  }
  const login = await request.newContext({ baseURL: SELF.origin });
  try {
    const resp = await login.post('/api/v1/auth/login', { data: { email, password } });
    if (!resp.ok()) {
      throw new Error(`Sign-in as ${email} failed: ${resp.status()} ${await resp.text()}`);
    }
    const token = (await resp.json()).access_token as string;
    return request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    });
  } finally {
    await login.dispose();
  }
}

/**
 * Clear the Terms of Use gate for whoever `ctx` is signed in as.
 *
 * The gate is chained into `get_current_user`, so a freshly created account's
 * every `/api/v1` call answers 403 until it is accepted — long before any
 * browser is involved.
 */
async function clearTerms(ctx: APIRequestContext): Promise<void> {
  const state = await ctx.get('/api/v1/auth/terms');
  if (!state.ok()) return;
  const terms = await state.json();
  if (!terms.required || terms.accepted) return;
  await ctx.post('/api/v1/auth/terms/accept', { data: { version: terms.version } });
}

/** The Agent Card, as much of it as this file reads. */
interface AgentCard {
  name: string;
  description: string;
  protocolVersion: string;
  url?: string;
  preferredTransport?: string;
  additionalInterfaces?: { url: string; transport: string }[];
  supportedInterfaces?: { url: string; protocolBinding: string; protocolVersion: string }[];
  security?: unknown;
  securityRequirements?: unknown;
  securitySchemes: {
    oauth2ClientCredentials: {
      type: string;
      flows: { clientCredentials: { tokenUrl: string; scopes: Record<string, unknown> } };
    };
  };
  capabilities: {
    streaming: boolean;
    pushNotifications: boolean;
    extensions: { uri: string; required: boolean; params: { path: string } }[];
  };
  skills: { id: string; name: string; description: string; tags: string[] }[];
  defaultInputModes: string[];
  defaultOutputModes: string[];
}

/** A rendered Task, in either vocabulary. */
interface WireTask {
  id: string;
  contextId: string;
  kind?: string;
  status: { state: string; message?: { messageId: string; parts: { text?: string }[] } };
  artifacts: { artifactId: string; name: string; parts: { kind?: string; text?: string }[] }[];
  history?: { messageId: string; parts: { text?: string }[] }[];
}

/** One push configuration, as `set`/`get`/`list` render it. */
interface PushConfigResult {
  taskId: string;
  pushNotificationConfig: { id: string; url: string; token?: string };
}

function resultOf<T>(body: JsonRpcEnvelope): T {
  expect(body.error, `expected a result, got error ${JSON.stringify(body.error)}`).toBeUndefined();
  return body.result as T;
}

function errorOf(body: JsonRpcEnvelope): { code: number; message: string; data?: { '@type'?: string } } {
  expect(body.error, `expected an error, got result ${JSON.stringify(body.result)}`).toBeTruthy();
  return body.error as { code: number; message: string; data?: { '@type'?: string } };
}

/** The extension URI names the SPEC, so it is a fixed string and not built from an origin. */
const PARTNER_DISCOVERY_URI = 'https://filigran.io/a2a/extensions/partner-agent-discovery/v1';

/** The one negative body the card route and the SPA guard both produce. */
const UNIFORM_404 = { detail: 'Not Found' };

/**
 * An address the destination guard admits.
 *
 * A2A_CLIENT_DEV_TRUSTED_ORIGINS names this deployment's own origin and
 * nothing else, so this is the only plain-http callback the push family will
 * accept. Built from `SELF.origin` rather than typed out, because the trusted
 * origin is an exact scheme/host/port match and a hard-coded port stops
 * matching the moment the deployment moves. Nothing listens on that path —
 * these tests assert
 * registration, never delivery.
 */
const ADMITTED_CALLBACK = `${SELF.origin}/e2e-inbound-callback`;

test.describe.serial('The inbound surface', () => {
  let admin: APIRequestContext;
  let available = false;
  const created = tracker();

  // The stable pair of registrations. Nothing in the file re-binds these two,
  // so every test may assume both cards resolve.
  let agentA = { id: '', name: '' };
  let agentB = { id: '', name: '' };
  let cardA = '';
  let cardB = '';
  let credentialA = { clientId: '', clientSecret: '' };
  let credentialB = { clientId: '', clientSecret: '' };
  let partnerA: PartnerClient;
  let partnerB: PartnerClient;
  let integrationA = { id: '', name: '' };
  let integrationB = { id: '', name: '' };

  // A completed task of the first registration's and an unfinished one of the
  // second's — the two ends of every ownership assertion below.
  let ownTask = '';
  let foreignTask = '';

  // The revocation fixture: one agent, two registrations, so unbinding one
  // leaves the exposure alive and isolates the credential's own failure.
  let agentZ = { id: '', name: '' };
  let cardZ = '';
  let partnerX: PartnerClient;
  let partnerY: PartnerClient;
  let integrationY = { id: '', name: '' };
  let taskX = '';
  let taskY = '';

  // Owned by the deactivation test, torn down separately because a user is
  // not something `cleanup` knows about.
  let runAs: APIRequestContext | undefined;
  let runAsUserId = '';
  const runAsCreated = tracker();

  test.beforeAll(async () => {
    admin = await signIn(process.env.E2E_USER_EMAIL ?? '', process.env.E2E_USER_PASSWORD ?? '');
    available = await a2aAvailable(admin);
    if (!available) return;

    integrationA = await createInboundIntegration(admin, created, stamp('E2E inbound A'));
    credentialA = await mintCredential(admin, integrationA.id);
    agentA = await createAgent(admin, created, { name: stamp('E2E exposed tooled') });
    cardA = await exposeAgent(admin, agentA.id, integrationA.id);

    integrationB = await createInboundIntegration(admin, created, stamp('E2E inbound B'));
    credentialB = await mintCredential(admin, integrationB.id);
    // No built-ins, so its card falls back to the single "converse" skill —
    // the other half of what the skills test compares.
    agentB = await createAgent(admin, created, {
      name: stamp('E2E exposed toolless'),
      extra: { builtin_tool_ids: [] },
    });
    cardB = await exposeAgent(admin, agentB.id, integrationB.id);

    partnerA = await PartnerClient.connect(cardA, credentialA);
    partnerB = await PartnerClient.connect(cardB, credentialB);

    // One completed task to read results off, one foreign task to be refused.
    // The scripted model answers an unarmed prompt with prose and stops, so
    // this finishes in a turn without any spec arming anything.
    ownTask = resultOf<WireTask>((await partnerA.send('Say hello.')).body).id;
    foreignTask = resultOf<WireTask>((await partnerB.send('Say hello.')).body).id;
    await partnerA.waitForState(ownTask, ['completed']);

    const integrationX = await createInboundIntegration(admin, created, stamp('E2E inbound X'));
    integrationY = await createInboundIntegration(admin, created, stamp('E2E inbound Y'));
    const credentialX = await mintCredential(admin, integrationX.id);
    const credentialY = await mintCredential(admin, integrationY.id);
    agentZ = await createAgent(admin, created, { name: stamp('E2E exposed shared') });
    await bind(admin, agentZ.id, { oauth_a2a: [integrationX.id, integrationY.id] });
    cardZ = (await exposureOf(admin, agentZ.id)).card_url as string;
    partnerX = await PartnerClient.connect(cardZ, credentialX);
    partnerY = await PartnerClient.connect(cardZ, credentialY);
    taskX = resultOf<WireTask>((await partnerX.send('Say hello.')).body).id;
    taskY = resultOf<WireTask>((await partnerY.send('Say hello.')).body).id;
    await partnerY.waitForState(taskY, ['completed']);
  });

  test.afterAll(async () => {
    await Promise.all(
      [partnerA, partnerB, partnerX, partnerY].map((p) => p?.dispose() ?? Promise.resolve()),
    );
    if (runAs) {
      await cleanup(runAs, runAsCreated);
      await runAs.dispose();
    }
    if (admin) {
      await cleanup(admin, created);
      if (runAsUserId) await admin.delete(`/api/v1/users/${runAsUserId}`).catch(() => undefined);
      await admin.dispose();
    }
  });

  // One place rather than thirty. The probe has to be a `beforeAll` because
  // the licence decides whether the fixtures above can exist at all.
  test.beforeEach(() => {
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  // -------------------------------------------------------------------------
  // The card
  // -------------------------------------------------------------------------

  test('serves the card to anyone holding the URL, naming its own JSON-RPC endpoint', async ({ one }) => {
    const resp = await one.wire.get(cardA);

    expect(resp.status()).toBe(200);
    expect(resp.headers()['content-type']).toContain('application/json');
    const card = (await resp.json()) as AgentCard;
    expect(card.name).toBe(agentA.name);
    expect(card.protocolVersion).toBe('1.0');
    // The endpoint is built from the host the card was fetched on, so it names
    // the origin the partner already reached.
    expect(card.supportedInterfaces).toEqual([
      {
        url: `${one.origin}/a2a/${cardTokenOf(cardA)}/jsonrpc`,
        protocolBinding: 'JSONRPC',
        protocolVersion: '1.0',
      },
    ]);
    // And the v0.3 names beside them: the platforms fetching without a
    // header require different ones, so this document carries both. Same
    // endpoint under each, or which name a partner reads decides where its
    // work is sent.
    expect(card.url).toBe(card.supportedInterfaces?.[0].url);
    expect(card.preferredTransport).toBe('JSONRPC');
    expect(card.additionalInterfaces).toEqual([{ url: card.url, transport: 'JSONRPC' }]);
    expect(card.security).toEqual(card.securityRequirements);
  });

  test('declares how a partner authenticates and what it may expect', async ({ one }) => {
    const card = (await (await one.wire.get(cardA)).json()) as AgentCard;

    const scheme = card.securitySchemes.oauth2ClientCredentials;
    expect(scheme.type).toBe('oauth2');
    expect(scheme.flows.clientCredentials.tokenUrl).toBe(`${one.origin}/mcp/oauth/token`);
    expect(card.securityRequirements).toEqual([{ oauth2ClientCredentials: [] }]);

    // Both flags are stated rather than omitted: a partner reading a missing
    // field may probe, and one reading false will not try.
    expect(card.capabilities.streaming).toBe(false);
    expect(card.capabilities.pushNotifications).toBe(true);

    // The one thing served here that A2A does not define is advertised the
    // spec's own way, so a partner learns discovery exists by reading rather
    // than by probing a path with its bearer attached.
    expect(card.capabilities.extensions).toHaveLength(1);
    expect(card.capabilities.extensions[0].uri).toBe(PARTNER_DISCOVERY_URI);
    expect(card.capabilities.extensions[0].required).toBe(false);
    expect(card.capabilities.extensions[0].params.path).toBe('/a2a/partner/agents');

    // Unsigned this phase, and no tenant concept exists to project.
    expect(card).not.toHaveProperty('signature');
    expect(card).not.toHaveProperty('signatures');
    expect(card).not.toHaveProperty('tenant');
  });

  test("advertises the agent's own built-ins, and 'converse' when it has none", async ({ one }) => {
    const tooled = (await (await one.wire.get(cardA)).json()) as AgentCard;
    const toolless = (await (await one.wire.get(cardB)).json()) as AgentCard;

    // a2a-review-scenarios.md says skills are "narrowed to what the exposure
    // permits". The code says the opposite in as many words, and this is the
    // code: an agent created with the platform's default built-in advertises
    // it in full. External work is bounded by the run-as identity, not by a
    // tool list, so the doc line is stale prose rather than a spec. The default
    // trace built-in is stored and advertised as the one Traces group.
    expect(tooled.skills.map((s) => s.id)).toContain('observability');
    const builtin = tooled.skills.find((s) => s.id === 'observability');
    expect(builtin?.tags).toEqual(['builtin']);
    expect(builtin?.description).toBe(`${agentA.name} capability: observability`);

    // An agent with no tools is still a usable conversational agent, and the
    // card has to describe something or a partner cannot register it.
    expect(toolless.skills).toHaveLength(1);
    expect(toolless.skills[0].id).toBe('converse');
    expect(toolless.skills[0].tags).toEqual(['conversation']);
  });

  test('is shaped by A2A-Version, and refuses one it does not know', async ({ one }) => {
    // v1.0 is what an undeclared fetch gets, asserted above. What this proves
    // is that a v0.3 partner still reaches its own shape by asking.
    const legacy = (await (
      await one.wire.get(cardA, { headers: { 'A2A-Version': '0.3' } })
    ).json()) as AgentCard;

    expect(legacy.protocolVersion).toBe('0.3');
    expect(legacy.security).toEqual([{ oauth2ClientCredentials: [] }]);
    expect(legacy.url).toBe(`${one.origin}/a2a/${cardTokenOf(cardA)}/jsonrpc`);
    expect(legacy.preferredTransport).toBe('JSONRPC');
    expect(legacy.additionalInterfaces).toEqual([{ url: legacy.url, transport: 'JSONRPC' }]);
    // Each version carries its own names and only those. A reader handed the
    // other version's name does not see an unknown field, it sees NO security
    // requirement — i.e. that the agent takes no credential.
    expect(legacy).not.toHaveProperty('securityRequirements');
    expect(legacy).not.toHaveProperty('supportedInterfaces');

    // Negotiated before any lookup, so the refusal discloses nothing about
    // what exists and names what would have worked.
    const refused = await one.wire.get(cardA, { headers: { 'A2A-Version': '9.9' } });
    expect(refused.status()).toBe(400);
    expect((await refused.json()).detail).toBe(
      "Unsupported A2A protocol version '9.9'. Supported: 1.0, 0.3",
    );
  });

  test('answers the namespace root with JSON, never with the SPA', async ({ one }) => {
    // The uniform not-found itself — an unknown card token against a revoked,
    // a disabled and a deleted exposure, compared byte for byte — belongs to
    // 10-negatives.spec.ts. What is left here is the paths with no
    // exposure behind them at all.
    //
    // No listing exists at the namespace root, and — the regression this
    // guards — the SPA catch-all must not answer any of these with index.html,
    // which would break a partner's registration tooling on parse and turn the
    // uniform not-found into something enumerable.
    for (const path of ['/a2a', '/a2a/', '/a2a/.well-known/agent-card.json']) {
      const resp = await one.wire.get(path);
      expect([404, 405], `${path} answered ${resp.status()}`).toContain(resp.status());
      expect(resp.headers()['content-type'] ?? '', path).not.toContain('text/html');
    }
  });

  test('retires the card URL when unbound, and mints a different one when bound again', async ({ one }) => {
    const integration = await createInboundIntegration(one.rest, created, stamp('E2E inbound rebind'));
    const agent = await createAgent(one.rest, created, { name: stamp('E2E rebound agent') });
    const first = await exposeAgent(one.rest, agent.id, integration.id);
    expect((await one.wire.get(first)).status()).toBe(200);

    // Removing the last inbound binding revokes the exposure. There is no
    // other way to retire a card, which is the point of implicit exposure.
    await bind(one.rest, agent.id, {});
    const retired = await one.wire.get(first);
    expect(retired.status()).toBe(404);
    expect(await retired.json()).toEqual(UNIFORM_404);
    expect(await exposureOf(one.rest, agent.id)).toMatchObject({ exposed: false, card_url: null });

    // Revocation retires the token for good; the next binding mints a fresh
    // exposure, so a partner holding the old URL must be sent the new one.
    // Two separate requests: the flush hook sees nothing changed if a single
    // PUT unbinds and re-binds at once.
    const second = await exposeAgent(one.rest, agent.id, integration.id);
    expect(second).not.toBe(first);
    expect((await one.wire.get(second)).status()).toBe(200);
    expect((await one.wire.get(first)).status()).toBe(404);
  });

  test('stops resolving when the run-as user is deactivated, while the operator still sees it', async ({ one }) => {
    // The exposure has to belong to somebody other than the admin: the toggle
    // refuses both self-deactivation and the ADMIN_EMAIL account.
    const email = `e2e-a2a-runas-${Date.now()}@e2e-example.com`;
    const password = 'E2eA2aRunAs2026';
    const createUser = await one.rest.post('/api/v1/users', {
      data: { email, full_name: 'E2E A2A run-as', password, role: 'user', account_type: 'local' },
    });
    expect(createUser.status(), await createUser.text()).toBe(201);
    runAsUserId = (await createUser.json()).id as string;

    runAs = await signIn(email, password);
    await clearTerms(runAs);
    const integration = await createInboundIntegration(runAs, runAsCreated, stamp('E2E inbound run-as'));
    const credential = await mintCredential(runAs, integration.id);
    const agent = await createAgent(runAs, runAsCreated, { name: stamp('E2E run-as agent') });
    const card = await exposeAgent(runAs, agent.id, integration.id);
    expect((await one.wire.get(card)).status()).toBe(200);

    await one.rest.put(`/api/v1/users/${runAsUserId}/active`);

    // Outside: gone, in the same words as an unknown token — the resolvable
    // -exposure query inner-joins the user on is_active, so the card, the task
    // surface and the token endpoint all fall quiet together.
    const gone = await one.wire.get(card);
    expect(gone.status()).toBe(404);
    expect(await gone.json()).toEqual(UNIFORM_404);
    const refusedToken = await requestToken(one.wire, credential);
    expect(refusedToken.status()).toBe(401);
    expect(await refusedToken.json()).toEqual({ error: 'invalid_client' });

    // Inside: still exposed. The operator read deliberately omits the active
    // filter, so an administrator can see why a partner stopped being served.
    const seenByAdmin = await one.rest.get(`/api/v1/agents/${agent.id}/a2a`);
    expect(await seenByAdmin.json()).toMatchObject({ exposed: true, card_url: card });

    // Reversible, and on the same URL: nothing was revoked.
    await one.rest.put(`/api/v1/users/${runAsUserId}/active`);
    expect((await one.wire.get(card)).status()).toBe(200);
  });

  // -------------------------------------------------------------------------
  // Partner discovery
  // -------------------------------------------------------------------------

  test('tells an unauthenticated caller nothing — 404, never 401', async ({ one }) => {
    const attempts: Record<string, string>[] = [
      {},
      { Authorization: 'Bearer garbage' },
      { Authorization: 'Basic nope' },
    ];
    for (const headers of attempts) {
      const resp = await one.wire.get('/a2a/partner/agents', { headers });
      expect(resp.status(), JSON.stringify(headers)).toBe(404);
      expect(await resp.json()).toEqual(UNIFORM_404);
    }
  });

  test('lists the agents the presenting credential reaches, and follows a new binding', async ({ one }) => {
    const first = await partnerA.discover();
    expect(first.status()).toBe(200);
    const before = (await first.json()) as { agents: { name: string; card_url: string }[] };
    expect(before.agents.map((a) => a.name)).toEqual([agentA.name]);
    expect(before.agents[0].card_url).toBe(cardA);
    // Every address it names has to answer, or discovery is worse than no
    // discovery: a partner would register a URL that 404s.
    expect((await one.wire.get(before.agents[0].card_url)).status()).toBe(200);

    // The server half of "one credential, several agents, nobody pastes a
    // URL": binding the same registration to a second agent is the whole act.
    const second = await createAgent(one.rest, created, { name: stamp('E2E second exposed') });
    await exposeAgent(one.rest, second.id, integrationA.id);

    const after = (await (await partnerA.discover()).json()) as { agents: { name: string }[] };
    expect(after.agents.map((a) => a.name).sort()).toEqual([agentA.name, second.name].sort());
  });

  test("restates the caller's own authorisation rather than listing the deployment", async ({ one }) => {
    // The second registration's credential sees its own agent and nothing of
    // the first's, though both live on the same deployment and the listing is
    // the same endpoint.
    const listing = (await (await partnerB.discover()).json()) as { agents: { name: string }[] };
    expect(listing.agents.map((a) => a.name)).toEqual([agentB.name]);

    // A bound agent with no live exposure is omitted rather than named with an
    // address that answers 404.
    const disabled = await createAgent(one.rest, created, { name: stamp('E2E discovery hidden') });
    await exposeAgent(one.rest, disabled.id, integrationB.id);
    expect(
      ((await (await partnerB.discover()).json()) as { agents: { name: string }[] }).agents.map((a) => a.name),
    ).toContain(disabled.name);

    await one.rest.put(`/api/v1/agents/${disabled.id}`, { data: { enabled: false } });
    const trimmed = (await (await partnerB.discover()).json()) as { agents: { name: string }[] };
    expect(trimmed.agents.map((a) => a.name)).not.toContain(disabled.name);

    // A credential that now reaches nothing is answered with an empty list,
    // which is distinguishable from the unauthenticated 404 — so a partner can
    // tell "you are nobody here" from "you reach nothing here".
    //
    // Note the brief's trigger for this ("mint a bearer for a registration
    // bound to nothing") cannot be run: the grant refuses to issue a token
    // while no exposure is reachable. The only way in is a bearer minted while
    // bound, which stays cryptographically valid for its full hour.
    const orphan = await createInboundIntegration(one.rest, created, stamp('E2E inbound orphan'));
    const credential = await mintCredential(one.rest, orphan.id);
    const agent = await createAgent(one.rest, created, { name: stamp('E2E orphan agent') });
    const card = await exposeAgent(one.rest, agent.id, orphan.id);
    const orphaned = await PartnerClient.connect(card, credential);
    try {
      await bind(one.rest, agent.id, {});
      const empty = await orphaned.discover();
      expect(empty.status()).toBe(200);
      expect(await empty.json()).toEqual({ agents: [] });
    } finally {
      await orphaned.dispose();
    }
  });

  // -------------------------------------------------------------------------
  // The token endpoint
  //
  // The uniformity of its refusals, and the containment of the bearer it
  // issues to the A2A surface alone, are both asserted byte-exactly in
  // 10-negatives.spec.ts. What is here is the shape of a successful
  // exchange and the one refusal arm that needs a registration to have
  // existed first.
  // -------------------------------------------------------------------------

  test('issues one token shape, and refuses a secret a regenerate retired', async ({ one }) => {
    const issued = await requestToken(one.wire, credentialA);
    expect(issued.status()).toBe(200);
    expect(issued.headers()['cache-control']).toContain('no-store');
    const token = await issued.json();
    expect(token.token_type).toBe('Bearer');
    expect(token.expires_in).toBe(3600);
    expect(token.scope).toBe('a2a:task');
    expect(typeof token.access_token).toBe('string');

    // Minting again replaces the secret on the same client id (#105), so a
    // secret an operator copied a minute ago is refused in the same words as
    // one that never existed. This is the arm the byte-exact comparison in 10 cannot reach:
    // it needs a registration that exists and a secret that used to be its.
    const stale = await createInboundIntegration(one.rest, created, stamp('E2E inbound stale'));
    const original = await mintCredential(one.rest, stale.id);
    const agent = await createAgent(one.rest, created, { name: stamp('E2E stale agent') });
    await exposeAgent(one.rest, agent.id, stale.id);
    await mintCredential(one.rest, stale.id); // rotates the secret `original` carries

    const refused = await requestToken(one.wire, original);
    expect(refused.status(), original.clientId).toBe(401);
    expect(await refused.json()).toEqual({ error: 'invalid_client' });
  });

  // -------------------------------------------------------------------------
  // Refusals on the task surface, before anything is dispatched
  // -------------------------------------------------------------------------

  // Every other undispatchable body — a parse failure, a batch, a bare
  // scalar, params or a message of the wrong type, a missing or overlong
  // messageId — is one table in 10-negatives.spec.ts, which also pins the
  // HTTP-200 framing and the echoed id. Only the size guard is here, because
  // it refuses before the body is read at all.
  test('refuses an oversize body on its declared length, before reading it', async ({ one }) => {
    // Starlette imposes no default cap, so without this an anonymous caller's
    // multi-megabyte payload is buffered and parsed whatever the rate limit
    // says. Playwright sets Content-Length for a string body, which is the
    // header the check reads.
    const padding = 'x'.repeat(1_200_000);
    const resp = await one.wire.post(`/a2a/${cardTokenOf(cardA)}/jsonrpc`, {
      headers: { 'content-type': 'application/json' },
      data: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: METHOD.send,
        params: { message: { messageId: 'oversize', role: 'user', parts: [{ kind: 'text', text: padding }] } },
      }),
    });

    expect(resp.status()).toBe(413);
    expect(await resp.json()).toEqual({
      jsonrpc: '2.0',
      id: null,
      error: { code: -32600, message: 'Request body exceeds 1048576 bytes' },
    });
  });

  test('refuses every bad credential identically — but not an unknown card token', async ({ one }) => {
    // No credential, a platform API key of either prefix, and a credential
    // minted for a different registration are one answer on the wire. Only the
    // audit trail and the refusal metric know a platform key from the rest.
    const bad: (string | null)[] = [null, 'fcp-anything', 'mcp-anything', partnerB.bearer];
    for (const bearer of bad) {
      const { status, body } = await partnerA.getTask(ownTask, { bearer });
      expect(status, String(bearer)).toBe(401);
      expect(body, String(bearer)).toEqual({
        jsonrpc: '2.0',
        id: 1,
        error: { code: -32600, message: 'Unauthenticated' },
      });
    }

    // FINDING, pinned as current behaviour rather than as the documented one.
    // The module docstring says every "what exists here" negative is the same
    // answer. It is not: exposure resolution runs BEFORE authentication, so a
    // real card token with no credential answers 401/-32600 while a bogus one
    // answers 200/-32001. The pair differs in both status and code, which
    // makes a card token's existence checkable without a credential. Tokens
    // are 32 bytes of urandom so this is not practically enumerable, but the
    // asymmetry is real and the docstring should say so or the order change.
    const bogus = await one.wire.post('/a2a/not-a-real-card-token/jsonrpc', {
      data: { jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: ownTask } },
    });
    expect(bogus.status()).toBe(200);
    expect(await bogus.json()).toMatchObject({ error: { code: -32001, message: 'Not found' } });
  });

  test('refuses what it does not serve: an unknown version, and an unknown method', async ({ one }) => {
    // -32009 rather than -32006, which the reference SDK reserves for
    // InvalidAgentResponseError — emitting a version refusal there would tell
    // an SDK-based partner the agent answered invalidly.
    const versioned = await one.wire.post(`/a2a/${cardTokenOf(cardA)}/jsonrpc`, {
      headers: { Authorization: `Bearer ${partnerA.bearer}`, 'A2A-Version': '9.9' },
      data: { jsonrpc: '2.0', id: 3, method: METHOD.get, params: { id: ownTask } },
    });
    const versionError = errorOf((await versioned.json()) as JsonRpcEnvelope);
    expect(versionError.code).toBe(-32009);
    expect(versionError.data?.['@type']).toBe('VersionNotSupportedError');
    expect(versionError.message).toContain('Supported: 1.0, 0.3');

    // The fall-through sits after authentication, so these are sent with a
    // bearer — an unauthenticated probe would get the 401 instead and prove
    // nothing about the vocabulary. `ListTasks` is a real v1.0 method this
    // surface deliberately does not serve, and it is refused like a typo.
    for (const method of [
      'ListTasks',
      'message/stream',
      'tasks/resubscribe',
      'agent/getAuthenticatedExtendedCard',
      'nonsense',
    ]) {
      const { body } = await partnerA.rpc(method, {});
      const error = errorOf(body);
      expect(error.code, method).toBe(-32004);
      expect(error.data?.['@type'], method).toBe('UnsupportedOperationError');
      expect(error.message, method).toBe(`Unsupported method: ${method}`);
    }
  });

  // -------------------------------------------------------------------------
  // The vocabulary, and what each operation answers
  // -------------------------------------------------------------------------

  test('accepts all fourteen method names', async () => {
    // The two vocabularies are disjoint spellings of one set of operations,
    // and neither is privileged. What is asserted is that none of them falls
    // through to "unsupported" — the outcomes themselves belong to the tests
    // below, and several are refusals by design (cancelling a terminal task,
    // reading a push config that does not exist).
    const message = {
      role: 'user',
      parts: [{ kind: 'text', text: 'Say hello.' }],
      messageId: `e2e-vocab-${crypto.randomUUID()}`,
    };
    const calls: [string, unknown][] = [
      [METHOD.send, { message: { ...message, messageId: `e2e-vocab-03-${crypto.randomUUID()}` } }],
      [
        'SendMessage',
        {
          message: { ...message, messageId: `e2e-vocab-10-${crypto.randomUUID()}` },
          // Blocking is the v1.0 default and would hold the connection for the
          // length of a run; this test is about the name being served.
          configuration: { returnImmediately: true },
        },
      ],
      [METHOD.get, { id: ownTask }],
      ['GetTask', { id: ownTask }],
      [METHOD.cancel, { id: ownTask }],
      ['CancelTask', { id: ownTask }],
      [METHOD.pushSet, { taskId: ownTask, pushNotificationConfig: { url: ADMITTED_CALLBACK } }],
      ['CreateTaskPushNotificationConfig', { taskId: ownTask, pushNotificationConfig: { url: ADMITTED_CALLBACK } }],
      [METHOD.pushList, { taskId: ownTask }],
      ['ListTaskPushNotificationConfigs', { taskId: ownTask }],
      [METHOD.pushGet, { pushNotificationConfigId: crypto.randomUUID() }],
      ['GetTaskPushNotificationConfig', { pushNotificationConfigId: crypto.randomUUID() }],
      [METHOD.pushDelete, { pushNotificationConfigId: crypto.randomUUID() }],
      ['DeleteTaskPushNotificationConfig', { pushNotificationConfigId: crypto.randomUUID() }],
    ];

    for (const [method, params] of calls) {
      const { body } = await partnerA.rpc(method, params);
      expect(body.error?.code, `${method} answered ${JSON.stringify(body.error)}`).not.toBe(-32004);
    }
    // How the same operations are stated to an operator — the catalog's
    // `available_tools`, `a2a_inbound_agent_tools` and `a2a_outbound_exposed`
    // — is asserted in 01-oauth-config.spec.ts, across all three A2A
    // providers rather than only oauth_a2a.
  });

  test('answers message/send with the bare v0.3 Task and its kind discriminator', async () => {
    const { status, body } = await partnerA.send('Say hello.');

    expect(status).toBe(200);
    const task = resultOf<WireTask>(body);
    // v0.3's send result is a Task-or-Message union that strict parsers
    // discriminate by a REQUIRED `kind`. The reference SDK defaults it, so
    // only the wire proves it is there.
    expect(task.kind).toBe('task');
    expect(task.id).toMatch(/^[0-9a-f-]{36}$/);
    expect(task.contextId).toMatch(/^[0-9a-f-]{36}$/);
    expect(['submitted', 'working']).toContain(task.status.state);
    expect(task.artifacts).toEqual([]);
  });

  test('answers SendMessage with the v1.0 envelope, and lets the method outrank the header', async () => {
    // v1.0's result is a oneof, so the Task sits under `task` or the reference
    // client's ParseDict refuses the whole response; and returnImmediately
    // unset means blocking, which the reference ADK client relies on entirely
    // because it has no polling loop.
    const { body } = await partnerA.rpc('SendMessage', {
      message: {
        role: 'user',
        parts: [{ kind: 'text', text: 'Say hello.' }],
        messageId: `e2e-v1-${crypto.randomUUID()}`,
      },
    });
    const wrapped = resultOf<{ task: WireTask }>(body);
    expect(wrapped.task).toBeTruthy();
    expect(wrapped).not.toHaveProperty('id');
    // Proto-JSON carries the enum NAME, not v0.3's lowercase spelling, and
    // carries no `kind` at all.
    expect(wrapped.task.status.state).toBe('TASK_STATE_COMPLETED');
    expect(wrapped.task).not.toHaveProperty('kind');

    // The reference v1.0 client sends no version header at all, so the method
    // name is the more reliable statement of which parser awaits the answer —
    // and it wins over a header that disagrees, in both directions.
    const legacyHeader = await partnerA.rpc('GetTask', { id: ownTask });
    expect(resultOf<WireTask>(legacyHeader.body).status.state).toBe('TASK_STATE_COMPLETED');
    expect(resultOf<WireTask>(legacyHeader.body)).not.toHaveProperty('kind');
    const modernHeader = await partnerA.rpc(METHOD.get, { id: ownTask });
    expect(resultOf<WireTask>(modernHeader.body).status.state).toBe('completed');
    expect(resultOf<WireTask>(modernHeader.body).kind).toBe('task');
  });

  test('mirrors a completed result into artifacts, history and the status message', async () => {
    const task = resultOf<WireTask>((await partnerA.getTask(ownTask)).body);

    expect(task.status.state).toBe('completed');
    expect(task.artifacts).toHaveLength(1);
    // `artifactId` and `name`, never `id`/`title` — any other key fails a
    // partner's deserialisation of the one payload carrying the answer.
    expect(task.artifacts[0].artifactId).toMatch(/^[0-9a-f-]{36}$/);
    expect(task.artifacts[0].name).toBe('result');
    expect(task.artifacts[0].parts[0].kind).toBe('text');
    const answer = task.artifacts[0].parts[0].text;
    expect(answer).toBeTruthy();

    // Foundry reads the answer from history and status.message rather than
    // from artifacts, so the SAME message object backs all three and the
    // copies cannot drift.
    expect(task.history).toHaveLength(1);
    expect(task.history?.[0].messageId).toBe(task.status.message?.messageId);
    expect(task.history?.[0].parts[0].text).toBe(answer);
    expect(task.status.message?.parts[0].text).toBe(answer);
  });

  test('scopes tasks/get to the registration that created the task', async () => {
    expect(resultOf<WireTask>((await partnerA.getTask(ownTask)).body).id).toBe(ownTask);

    // Another registration's task, an id never issued, an id that is not a
    // uuid at all and a call with no id share one answer, byte for byte apart
    // from the echoed request id. The client is a predicate in the query, so
    // another partner's row is never in memory to leak.
    const refusals: [string, unknown][] = [
      ["another registration's task", { id: foreignTask }],
      ['an id never issued', { id: crypto.randomUUID() }],
      ['an id that is not a uuid', { id: 'not-a-uuid' }],
      ['no id at all', {}],
    ];
    for (const [what, params] of refusals) {
      const { status, body } = await partnerA.rpc(METHOD.get, params, { id: 11 });
      expect(status, what).toBe(200);
      expect(body, what).toEqual({
        jsonrpc: '2.0',
        id: 11,
        error: { code: -32001, message: 'Not found', data: { '@type': 'TaskNotFoundError' } },
      });
    }
  });

  test('refuses a send it cannot use, before any run starts', async () => {
    // Four parameter guards, each with its own message, all before a task row
    // exists — so a partner learns the shape is wrong without being charged
    // for a run that was never going to work.
    const parameterRefusals: [unknown, string][] = [
      [{ message: 'x' }, 'message must be an object'],
      [{ message: { messageId: 'e2e', parts: 'x' } }, 'parts must be an array'],
      [{ message: { role: 'user', parts: [] } }, 'messageId is required'],
      [
        { message: { messageId: 'm'.repeat(129), role: 'user', parts: [] } },
        'messageId must be at most 128 characters',
      ],
    ];
    for (const [params, message] of parameterRefusals) {
      const error = errorOf((await partnerA.rpc(METHOD.send, params)).body);
      expect(error.code, message).toBe(-32602);
      expect(error.message).toBe(message);
    }

    // Content refusals are a different class: the submission was well formed
    // and its content is not usable, so they carry ContentTypeNotSupportedError
    // and record a REJECTED task rather than nothing at all.
    const contentRefusals: [unknown[], string][] = [
      [[{ kind: 'file', file: { uri: 'https://example.invalid/x' } }], 'This agent accepts text and structured data only'],
      [[], 'Message carried no parts'],
      [[{ kind: 'text', text: 'y'.repeat(100_001) }], 'Message exceeds the 100000-character limit'],
    ];
    const messageId = `e2e-rejected-${crypto.randomUUID()}`;
    for (const [parts, message] of contentRefusals) {
      const { body } = await partnerA.rpc(METHOD.send, {
        message: { role: 'user', messageId, parts },
      });
      const error = errorOf(body);
      expect(error.code, message).toBe(-32005);
      expect(error.data?.['@type']).toBe('ContentTypeNotSupportedError');
      expect(error.message).toBe(message);
    }
    // That a rejection does not consume the message id — the corrected retry
    // under the same id starts a fresh task, which the partial unique index
    // on `rejected_reason IS NULL` is what makes legal — is asserted in
    // 10-negatives.spec.ts.
  });

  // Two files own two halves of deduplication and neither is the other's
  // duplicate: 10-negatives.spec.ts owns "the retry started no second
  // run" and the pair of audit rows, this one owns the callback the retry
  // declares and loses. Deleting either as redundant loses a branch.
  test('returns the first task for a repeated messageId, and drops the callback the retry declared', async () => {
    const messageId = `e2e-dedup-${crypto.randomUUID()}`;
    const first = resultOf<WireTask>((await partnerA.send('Say hello.', { messageId })).body);

    // FINDING. The retry is answered with the first task and no error, which
    // is the contract — but the callback it declares is registered only when
    // the submission actually created a task, so it is silently dropped. A
    // partner that retries its submission and then stops polling waits
    // forever, and nothing in the response says so.
    const retry = await partnerA.send('Say hello.', {
      messageId,
      pushNotificationConfig: { url: ADMITTED_CALLBACK, token: 'e2e-dedup-secret' },
    });
    const second = resultOf<WireTask>(retry.body);
    expect(second.id).toBe(first.id);
    expect(second.contextId).toBe(first.contextId);

    const configs = resultOf<PushConfigResult[]>(
      (await partnerA.rpc(METHOD.pushList, { taskId: first.id })).body,
    );
    expect(configs).toEqual([]);
  });

  test('refuses a continuation naming something it never issued, or a task that is not asking', async () => {
    // A message naming a task is an answer to what that task asked, so these
    // are refusals about a continuation rather than about a new submission.
    const unknownTask = await partnerA.send('An answer.', { taskId: crypto.randomUUID() });
    expect(errorOf(unknownTask.body)).toMatchObject({ code: -32001, message: 'Task not found' });

    // Owned by this client and not asking anything: a fact about its own task,
    // so it gets the reason rather than the uniform not-found.
    const notAsking = await partnerA.send('An answer.', { taskId: ownTask });
    expect(errorOf(notAsking.body)).toMatchObject({
      code: -32004,
      message: 'This task is not waiting on an answer.',
    });

    // A context never issued, or issued to another client, answers with the
    // task refusal byte for byte — not merely with the same code. Asserted as
    // an equality against the unknown-task envelope rather than against a
    // literal, because the property is that the two are indistinguishable: a
    // caller must not be able to learn that a context exists by noticing that
    // its refusal reads differently from a task's. A literal would still pass
    // if one of the two later drifted.
    const unknownContext = await partnerA.rpc(METHOD.send, {
      contextId: crypto.randomUUID(),
      message: {
        role: 'user',
        parts: [{ kind: 'text', text: 'A follow-up.' }],
        messageId: `e2e-context-${crypto.randomUUID()}`,
      },
    });
    expect(errorOf(unknownContext.body)).toEqual(errorOf(unknownTask.body));
  });

  test("refuses to cancel a terminal task, and another registration's task at all", async () => {
    // The live-cancel arm is deliberately not asserted: the scripted model
    // answers in one turn, so a task submitted here is usually terminal before
    // a cancel can land, and pinning "canceled" would be a race dressed up as
    // a branch. Cancellation of a running task belongs with the specs that can
    // hold one open. What is invariant is that a second cancel is refused with
    // a reason, and that the state spelling has one l.
    const fresh = resultOf<WireTask>((await partnerA.send('Say hello.')).body);
    const first = await partnerA.cancelTask(fresh.id);
    if (first.body.result) {
      expect(resultOf<WireTask>(first.body).status.state).toBe('canceled');
    } else {
      expect(errorOf(first.body).code).toBe(-32002);
    }

    await partnerA.waitForState(fresh.id, ['canceled', 'completed', 'failed']);
    const again = errorOf((await partnerA.cancelTask(fresh.id)).body);
    expect(again.code).toBe(-32002);
    expect(again.data?.['@type']).toBe('TaskNotCancelableError');
    expect(again.message).toBe('Task is already terminal');

    // Another registration's task is not cancellable and not acknowledged.
    expect(errorOf((await partnerA.cancelTask(foreignTask)).body)).toMatchObject({
      code: -32001,
      message: 'Not found',
    });
  });

  test('round-trips the four push operations without ever echoing the token', async () => {
    // Its own task, so the count and the listing below cannot be perturbed by
    // configurations another test registered.
    const task = resultOf<WireTask>((await partnerA.send('Say hello.')).body);

    const set = resultOf<PushConfigResult>(
      (
        await partnerA.rpc(METHOD.pushSet, {
          taskId: task.id,
          pushNotificationConfig: { url: ADMITTED_CALLBACK, token: 'e2e-push-secret' },
        })
      ).body,
    );
    expect(set.taskId).toBe(task.id);
    expect(set.pushNotificationConfig.url).toBe(ADMITTED_CALLBACK);
    // A partner that registered a token already knows it; returning it would
    // put a secret in a response body for the benefit of nobody.
    expect(set.pushNotificationConfig).not.toHaveProperty('token');

    const configId = set.pushNotificationConfig.id;
    const listed = resultOf<PushConfigResult[]>(
      (await partnerA.rpc(METHOD.pushList, { taskId: task.id })).body,
    );
    expect(listed).toEqual([set]);
    // `get` is addressed by the configuration alone — the row carries its own
    // client predicate, so no task has to be named to scope it.
    expect(resultOf<PushConfigResult>((await partnerA.rpc(METHOD.pushGet, { pushNotificationConfigId: configId })).body)).toEqual(set);

    // `delete`, unlike `get`, resolves the task first and refuses without it
    // (a2a_server.py:1220-1228), so the pair of ids is what this call takes.
    expect(
      resultOf(
        (await partnerA.rpc(METHOD.pushDelete, { taskId: task.id, pushNotificationConfigId: configId })).body,
      ),
    ).toEqual({});
    expect(resultOf<PushConfigResult[]>((await partnerA.rpc(METHOD.pushList, { taskId: task.id })).body)).toEqual([]);
    // Deleted, foreign and never-existed are one answer, so a partner cannot
    // enumerate another's callbacks through this surface.
    expect(
      errorOf((await partnerA.rpc(METHOD.pushGet, { pushNotificationConfigId: configId })).body),
    ).toMatchObject({ code: -32001, message: 'Not found' });
  });

  // FINDING. v0.3 addresses a task with `id` in every one of the four push
  // param objects — DeleteTaskPushNotificationConfigParams and
  // ListTaskPushNotificationConfigParams both spell it that way in the
  // reference SDK (a2a/compat/v0_3/types.py:291, :673) — but the surface
  // reads only `taskId` (a2a_server.py:1221-1223) and treats `id` as a
  // FALLBACK FOR THE CONFIG ID. So a partner built on the reference client
  // gets TaskNotFoundError for `list` and `delete` on a task it owns and a
  // configuration it just registered, against a v0.3 card that advertises
  // pushNotifications: true. `set` and `get` happen
  // to survive: set carries `taskId` inside the config object, and get is
  // answered off `pushNotificationConfigId` before the task is resolved.
  test.fixme('accepts the v0.3 spelling of the push params, where `id` is the task', async () => {
    const task = resultOf<WireTask>((await partnerA.send('Say hello.')).body);
    const set = resultOf<PushConfigResult>(
      (
        await partnerA.rpc(METHOD.pushSet, {
          taskId: task.id,
          pushNotificationConfig: { url: ADMITTED_CALLBACK },
        })
      ).body,
    );

    expect(resultOf<PushConfigResult[]>((await partnerA.rpc(METHOD.pushList, { id: task.id })).body)).toEqual([set]);
    expect(
      resultOf(
        (await partnerA.rpc(METHOD.pushDelete, { id: task.id, pushNotificationConfigId: set.pushNotificationConfig.id })).body,
      ),
    ).toEqual({});
  });

  test('refuses a callback address the destination guard will not admit', async () => {
    const task = resultOf<WireTask>((await partnerA.send('Say hello.')).body);
    const register = (url: string) =>
      partnerA.rpc(METHOD.pushSet, { taskId: task.id, pushNotificationConfig: { url } });

    // The scheme check fires before the address check, so an http loopback URL
    // never produces the loopback message — each of these asserts the refusal
    // that actually applies, and each reaches the partner verbatim rather than
    // as a 500. The same guard on the OTHER call site — a callback declared in
    // `configuration.pushNotificationConfig` on the submission itself — is in
    // 10-negatives.spec.ts.
    const refusals: [string, string][] = [
      ['http://198.51.100.7:9/cb', "A2A partner URLs must use https, got 'http'."],
      ['https://127.0.0.1/cb', 'Refusing to reach 127.0.0.1 — loopback address.'],
      ['https://169.254.169.254/latest', 'Refusing to reach 169.254.169.254 — cloud metadata endpoint.'],
    ];
    for (const [url, message] of refusals) {
      const error = errorOf((await register(url)).body);
      expect(error.code, url).toBe(-32602);
      expect(error.message, url).toBe(message);
    }

    // Refused rather than evicting the oldest: a partner that registered a
    // callback is relying on it.
    for (let i = 0; i < 5; i += 1) {
      expect((await register(`${ADMITTED_CALLBACK}-${i}`)).body.error, `config ${i}`).toBeUndefined();
    }
    expect(errorOf((await register(`${ADMITTED_CALLBACK}-overflow`)).body).message).toBe(
      'At most 5 push notification configurations per task',
    );
  });

  // -------------------------------------------------------------------------
  // Exposure, and what withdrawing it does
  // -------------------------------------------------------------------------

  test('binding an inbound registration is the entire opt-in', async ({ one }) => {
    const integration = await createInboundIntegration(one.rest, created, stamp('E2E inbound opt-in'));
    const agent = await createAgent(one.rest, created, { name: stamp('E2E opt-in agent') });
    expect(await exposureOf(one.rest, agent.id)).toMatchObject({ exposed: false, card_url: null });

    const card = await exposeAgent(one.rest, agent.id, integration.id);
    const exposure = await exposureOf(one.rest, agent.id);
    expect(exposure.exposed).toBe(true);
    expect(exposure.card_url).toBe(card);
    expect(exposure.token_url).toBe(`${one.origin}/mcp/oauth/token`);
    // Run-as is inherited from the agent's owner and stated, never nominated:
    // there is no parameter and no endpoint that could supply one.
    const me = await (await one.rest.get('/api/v1/auth/me')).json();
    expect(exposure.run_as_user_id).toBe(me.id);

    // The read is the whole admin surface. Nothing in the API or the UI mints
    // or revokes an exposure directly, so these have no handler at all.
    for (const resp of [
      await one.rest.post(`/api/v1/agents/${agent.id}/a2a`),
      await one.rest.delete(`/api/v1/agents/${agent.id}/a2a`),
    ]) {
      expect(resp.status()).toBe(405);
    }
  });

  test('binding an outbound registration exposes nothing', async ({ one }) => {
    // Direction is resolved from the settings row rather than from provider-key
    // membership, which would fail in the dangerous direction: an operator
    // registering a partner they call OUT to would find themselves reachable.
    // Pointed at this deployment's own card so the fields validate; nothing
    // verifies it.
    const outbound = await createOutboundIntegration(one.rest, created, {
      cardUrls: cardA,
      tokenUrl: `${one.origin}/mcp/oauth/token`,
      clientId: credentialA.clientId,
      clientSecret: credentialA.clientSecret,
      name: stamp('E2E outbound only'),
    });
    const agent = await createAgent(one.rest, created, { name: stamp('E2E outbound-bound agent') });
    await bind(one.rest, agent.id, { oauth_a2a: [outbound.id] });

    expect(await exposureOf(one.rest, agent.id)).toMatchObject({ exposed: false, card_url: null });
  });

  test('stops a live bearer on the very next request when its registration is unbound', async ({ one }) => {
    // The bearer stays cryptographically valid for its full hour; what stops
    // is the binding it is checked against, re-read per request. Two inbound
    // registrations on one agent isolate that from revocation: removing one
    // leaves the exposure alive, so the card token still resolves and the
    // refusal is authentication rather than not-found.
    expect(resultOf<WireTask>((await partnerX.getTask(taskX)).body).id).toBe(taskX);

    await bind(one.rest, agentZ.id, { oauth_a2a: [integrationY.id] });

    const stopped = await partnerX.getTask(taskX);
    expect(stopped.status).toBe(401);
    expect(stopped.body.error).toMatchObject({ code: -32600, message: 'Unauthenticated' });
    // The other registration is untouched, which is what makes the refusal
    // above about the credential and not about the exposure.
    expect(resultOf<WireTask>((await partnerY.getTask(taskY)).body).id).toBe(taskY);
  });

  test('hides a result the partner legitimately completed once the exposure is revoked', async ({ one }) => {
    const before = resultOf<WireTask>((await partnerY.getTask(taskY)).body);
    expect(before.status.state).toBe('completed');
    expect(before.artifacts).toHaveLength(1);

    // Unbinding the last inbound registration revokes the exposure, and the
    // card token is resolved before the request is authenticated — so every
    // method, including a read of a task this caller created and completed,
    // answers the uniform not-found. The result is not deleted; it is
    // unreachable, and nothing tells the partner which.
    await bind(one.rest, agentZ.id, {});

    const after = await partnerY.getTask(taskY);
    expect(after.status).toBe(200);
    expect(after.body.error).toMatchObject({ code: -32001, message: 'Not found' });
    expect((await one.wire.get(cardZ)).status()).toBe(404);
  });

  // -------------------------------------------------------------------------
  // Transport
  // -------------------------------------------------------------------------

  test('refuses every partner-facing route when the transport is unconfirmed', async () => {
    // The rest of the suite is exempt because its requests reach the
    // container from the bridge gateway, a peer named in
    // TEST_ONLY_INSECURE_TRANSPORT_PEERS. An exemption by address reads the
    // real socket peer only, so a request carrying `X-Forwarded-For` is never
    // exempt - whatever address it names. That is the lever here: the
    // same client, the same credentials, one header, and each route answers
    // the terse refusal before anything else runs.
    //
    // Which header is believed under which setting is pinned in
    // backend/tests/test_secure_transport.py.
    const external = await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { 'X-Forwarded-For': '127.0.0.1' },
    });
    try {
      const token = cardTokenOf(cardA);
      const refused = { detail: 'https required' };

      const card = await external.get(`/a2a/${token}/.well-known/agent-card.json`);
      expect(card.status()).toBe(403);
      expect(await card.json()).toEqual(refused);

      const discovery = await external.get('/a2a/partner/agents', {
        headers: { Authorization: `Bearer ${partnerA.bearer}` },
      });
      expect(discovery.status()).toBe(403);
      expect(await discovery.json()).toEqual(refused);

      const rpc = await external.post(`/a2a/${token}/jsonrpc`, {
        headers: { Authorization: `Bearer ${partnerA.bearer}` },
        data: { jsonrpc: '2.0', id: 1, method: METHOD.get, params: { id: ownTask } },
      });
      expect(rpc.status()).toBe(403);
      expect(await rpc.json()).toEqual(refused);

      const callback = await external.post('/a2a/callbacks/unknown-delegation', { data: {} });
      expect(callback.status()).toBe(403);
      expect(await callback.json()).toEqual(refused);

      // Without the header the same client is served: the refusal above is
      // the transport's, not a credential's.
      const served = await request.newContext({ baseURL: SELF.origin });
      try {
        const again = await served.get(`/a2a/${token}/.well-known/agent-card.json`);
        expect(again.status()).toBe(200);
      } finally {
        await served.dispose();
      }
    } finally {
      await external.dispose();
    }
  });
});
