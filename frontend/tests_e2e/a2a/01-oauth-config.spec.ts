/**
 * The OAuth story between a partner and a caller: how the relationship is
 * configured, and what the credential in the middle of it does.
 *
 * These branches belong together because they are one lifecycle rather than
 * one screen. A registration is created in a direction; the inbound side
 * mints a secret it can show exactly once; the outbound side is handed that
 * secret plus a card URL and a token URL; the grant turns the two into a
 * bearer; and every later act — regenerating, unbinding, deleting, flipping
 * the direction — is a different way of taking the same credential away. A
 * spec that tested any one of those alone would assert on the state and miss
 * the transition, which is where all of the surprises live.
 *
 * Three of those surprises stand as the CODE behaves, not as the review
 * script describes it; the last two are asserted here:
 *
 *  - nothing verifies an outbound registration on save, so a fresh one reads
 *    "unusable" with no verification error to explain it. Asserted in
 *    03-outbound-registration.spec.ts, which captures a fresh
 *    registration's state in its own `beforeAll` and so cannot be overtaken
 *    by whatever verifies next;
 *  - an outbound registration with no credentials verifies as unusable
 *    (credential_cannot_mint_token): the card is fetched anonymously, but
 *    Verify mints a token to test the credential (#105);
 *  - regenerating the inbound credential rotates the secret and keeps the
 *    client id (#105), so the partner's history survives it; only the
 *    explicit client id rotation starts it over.
 *
 * The regenerate branch is asserted against the token endpoint and the task
 * surface rather than against a delegation: the caller caches its bearer on
 * the credential material, so the moment a stale secret becomes visible on
 * the wire is a refusal at /mcp/oauth/token, not a failed delegation some
 * seconds later.
 */

import type { APIRequestContext } from '@playwright/test';
import { expect, test } from './fixtures';
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
  registrationState,
  stamp,
  tracker,
  verifyNow,
  type Created,
} from './seed';
import { PartnerClient, requestToken } from './wire';
import { marker } from '../scripted-llm/client';

// ---------------------------------------------------------------------------
// Three local helpers the harness does not carry, kept here because they are
// this file's business and nobody else's.
// ---------------------------------------------------------------------------

/**
 * One target out of a registration's state, found by the URL it was
 * configured with.
 *
 * Targets come back ordered by creation time and then by id, so a spec that
 * indexed into the list would be asserting on a tie-break. The failure names
 * what the registration actually holds, because "undefined is not an object"
 * says nothing about a card URL that was silently normalised away.
 */
function targetFor(state: Record<string, unknown>, cardUrl: string): Record<string, unknown> {
  const targets = (state.targets as Record<string, unknown>[]) ?? [];
  const found = targets.find((t) => t.card_url === cardUrl);
  if (!found) {
    const listed = targets.map((t) => String(t.card_url)).join(', ') || '<none>';
    throw new Error(`No target for ${cardUrl}. The registration holds: ${listed}`);
  }
  return found;
}

/** A JWT's claims, read without verifying it — the signature is the platform's own. */
function claimsOf(jwt: string): Record<string, unknown> {
  const payload = jwt.split('.')[1];
  if (!payload) throw new Error(`Not a JWT: ${jwt.slice(0, 40)}…`);
  return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
}

/**
 * A whole exposing side of one's own: registration, agent, card URL and a
 * freshly minted credential.
 *
 * Most of the branches below destroy the credential they are given — that is
 * what they are about — so they cannot share the file's main fixture, and
 * building the same four calls inline in each of them would bury the subject
 * of the test under its setup.
 */
async function freshExposure(
  rest: APIRequestContext,
  created: Created,
  label: string,
): Promise<{
  integrationId: string;
  agentId: string;
  agentName: string;
  cardUrl: string;
  clientId: string;
  clientSecret: string;
}> {
  const integration = await createInboundIntegration(rest, created, stamp(`E2E inbound ${label}`));
  const agent = await createAgent(rest, created, { name: stamp(`E2E exposed ${label}`) });
  const cardUrl = await exposeAgent(rest, agent.id, integration.id);
  const credential = await mintCredential(rest, integration.id);
  return {
    integrationId: integration.id,
    agentId: agent.id,
    agentName: agent.name,
    cardUrl,
    ...credential,
  };
}

test.describe.serial('A2A OAuth configuration, both directions', () => {
  const exposedSide = tracker();
  const callingSide = tracker();

  let available = false;

  // A — the side that exposes.
  let agentAName = '';
  let cardUrl = '';
  let credential = { clientId: '', clientSecret: '' };
  let tokenUrl = '';

  // B — the side that reaches out.
  let outboundId = '';
  let boundOutboundId = '';

  test.beforeAll(async ({ one, model }) => {
    available = (await a2aAvailable(one.rest)) && (await a2aAvailable(one.rest));
    if (!available) return;
    // A previous file's scripts would otherwise still answer for markers this
    // one never armed.
    await model.reset();

    const inbound = await createInboundIntegration(one.rest, exposedSide, stamp('E2E inbound main'));
    const agentA = await createAgent(one.rest, exposedSide, { name: stamp('E2E exposed main') });
    agentAName = agentA.name;
    cardUrl = await exposeAgent(one.rest, agentA.id, inbound.id);
    credential = await mintCredential(one.rest, inbound.id);
    tokenUrl = `${one.origin}/mcp/oauth/token`;

    const outbound = await createOutboundIntegration(one.rest, callingSide, {
      cardUrls: cardUrl,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E outbound main'),
    });
    outboundId = outbound.id;

    // A second outbound registration, this one bound to an agent: a
    // direction re-assigned to the value it already holds is only interesting
    // on a registration something binds.
    const boundOutbound = await createOutboundIntegration(one.rest, callingSide, {
      cardUrls: cardUrl,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E outbound bound'),
    });
    boundOutboundId = boundOutbound.id;
    const agentB = await createAgent(one.rest, callingSide, { name: stamp('E2E delegating') });
    await bind(one.rest, agentB.id, { oauth_a2a: [boundOutboundId] });
  });

  test.afterAll(async ({ one }) => {
    await cleanup(one.rest, exposedSide);
    await cleanup(one.rest, callingSide);
  });

  // -------------------------------------------------------------------------
  // Creating a registration in each direction
  // -------------------------------------------------------------------------

  test('an inbound registration is connected before it has a credential', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const bare = await createInboundIntegration(one.rest, exposedSide, stamp('E2E inbound bare'));

    const read = await one.rest.get(`/api/v1/integrations/${bare.id}`);
    expect(read.ok(), await read.text()).toBeTruthy();
    const body = await read.json();
    // "none" auth with no extra fields, so the catalog has nothing to wait
    // for — the status says configured, never that a partner can reach it.
    expect(body.status).toBe('connected');
    expect((body.extra_config ?? {}).client_id).toBeUndefined();
    // The one way the direction is legible from outside — the client surface
    // answers not-found for an inbound registration — is asserted byte for
    // byte in 10-negatives.spec.ts.
  });

  test('omitting the direction registers a partner that may task us', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await one.rest.post('/api/v1/integrations', {
      data: { provider: 'oauth_a2a', name: stamp('E2E inbound default'), auth_type: 'none' },
    });
    expect(resp.status(), await resp.text()).toBe(201);
    const id = (await resp.json()).id as string;
    exposedSide.integrations.push(id);

    // The server defaults to inbound, which is the exposing direction: an API
    // caller that forgets the field gets the one that can be reached, not the
    // one that reaches out.
    expect((await one.rest.get(`/api/v1/a2a/client/${id}`)).status()).toBe(404);
    const minted = await one.rest.post(`/api/v1/integrations/${id}/a2a-credential`);
    expect(minted.status(), 'only an inbound registration mints').toBe(200);
  });

  test('each configured card URL becomes its own target, however the list is written', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const second = `${one.origin}/a2a/e2e-not-a-real-card-token/.well-known/agent-card.json`;
    // Commas and new lines both separate, entries are trimmed, and a repeat is
    // one target rather than two — the ordinary text input has to carry a list.
    const registration = await createOutboundIntegration(one.rest, callingSide, {
      cardUrls: `${cardUrl},\n  ${second}\n${cardUrl}  `,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E outbound list'),
    });

    const state = await registrationState(one.rest, registration.id);
    expect((state.targets as unknown[]).length).toBe(2);
    expect(targetFor(state, cardUrl)).toBeTruthy();
    expect(targetFor(state, second)).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // The inbound credential
  // -------------------------------------------------------------------------

  test('the secret is returned once and is never readable again', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const integration = await createInboundIntegration(one.rest, exposedSide, stamp('E2E inbound once'));
    const minted = await mintCredential(one.rest, integration.id);
    expect(minted.clientId).toMatch(/^a2a-/);
    expect(minted.clientSecret.length).toBeGreaterThan(20);

    const read = await one.rest.get(`/api/v1/integrations/${integration.id}`);
    const raw = await read.text();
    // The client id is an identifier the partner sends in clear on every
    // exchange, so it re-displays; the secret exists only as a bcrypt hash and
    // must appear nowhere in the response at all.
    expect(JSON.parse(raw).extra_config.client_id).toBe(minted.clientId);
    expect(raw).not.toContain(minted.clientSecret);
  });

  test('minting is refused on an outbound registration', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await one.rest.post(`/api/v1/integrations/${outboundId}/a2a-credential`);
    expect(resp.status()).toBe(422);
    // Minting writes its client id into extra_config, which on an outbound
    // registration is where the PARTNER-issued one lives — one press would
    // overwrite a working credential with a meaningless one.
    expect((await resp.json()).detail).toContain(
      'Only an inbound A2A integration mints a partner credential',
    );
  });

  test('Test Connection reports the credential and the binding, and claims nothing more', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const integration = await createInboundIntegration(one.rest, exposedSide, stamp('E2E inbound probe'));
    const probe = async () => (await one.rest.post(`/api/v1/integrations/${integration.id}/test`)).json();

    const noCredential = await probe();
    expect(noCredential.status).toBe('error');
    expect(noCredential.message).toContain('No credential has been generated yet');

    const minted = await mintCredential(one.rest, integration.id);
    const unbound = await probe();
    // Degraded reaches the wire as "info": it works, with a caveat.
    expect(unbound.status).toBe('info');
    expect(unbound.message).toContain(`Credential ${minted.clientId} is ready, but no agent binds`);

    const agent = await createAgent(one.rest, exposedSide, { name: stamp('E2E probe agent') });
    await bind(one.rest, agent.id, { oauth_a2a: [integration.id] });
    const bound = await probe();
    expect(bound.status).toBe('ok');
    expect(bound.message).toContain(`Credential ${minted.clientId} is ready and exposes 1 agent`);
    // The one thing the probe refuses to claim, because nothing local can
    // know it.
    expect(bound.message).toContain('proven only by its first token exchange');
  });

  test('the credential exchanges for a partner token confined to the A2A surface', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await requestToken(one.wire, credential);
    expect(resp.status(), await resp.text()).toBe(200);
    expect(resp.headers()['cache-control']).toContain('no-store');

    const body = await resp.json();
    expect(body.token_type).toBe('Bearer');
    expect(body.expires_in).toBe(3600);
    expect(body.scope).toBe('a2a:task');

    const claims = claimsOf(body.access_token);
    // The two claims that confine the token: `type` closes /mcp/*, and the
    // presence of `scope` closes /api/v1/*. There is no audience at all — the
    // card token in the path is what addresses an exposure.
    expect(claims.type).toBe('a2a_client');
    expect(claims.scope).toBe('a2a:task');
    expect(claims.client_id).toBe(credential.clientId);
    expect(claims.aud).toBeUndefined();
  });

  test('a requested scope is ignored', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await one.wire.post('/mcp/oauth/token', {
      form: {
        grant_type: 'client_credentials',
        client_id: credential.clientId,
        client_secret: credential.clientSecret,
        scope: 'a2a:task admin',
        resource: 'https://elsewhere.example',
      },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    // The grant reads the two credential fields and nothing else, so neither
    // parameter can widen what it issues.
    expect(body.scope).toBe('a2a:task');
    expect(claimsOf(body.access_token).aud).toBeUndefined();
  });

  test('the credential stops minting when nothing it reaches resolves', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'unbind');
    expect((await requestToken(one.wire, ex)).status()).toBe(200);

    await bind(one.rest, ex.agentId, {});
    // No revocation step: minting asks the same question the task surface
    // resolves on, so the last binding going away is the whole of it.
    expect((await requestToken(one.wire, ex)).status()).toBe(401);

    await bind(one.rest, ex.agentId, { oauth_a2a: [ex.integrationId] });
    expect((await requestToken(one.wire, ex)).status()).toBe(200);
    // Re-binding mints a new exposure rather than reviving the revoked one,
    // so the partner's card URL changed underneath a credential that did not.
    expect((await exposureOf(one.rest, ex.agentId)).card_url).not.toBe(ex.cardUrl);
  });

  test('regenerating rotates the secret and keeps the client id (#105)', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'regen');
    expect((await requestToken(one.wire, ex)).status()).toBe(200);

    const first = await mintCredential(one.rest, ex.integrationId);
    const second = await mintCredential(one.rest, ex.integrationId);
    // The partner keeps the client id it holds and pastes the secret alone.
    expect(first.clientId).toBe(ex.clientId);
    expect(second.clientId).toBe(ex.clientId);
    // One revocation for a leaked secret: only the latest one mints.
    expect((await requestToken(one.wire, ex)).status()).toBe(401);
    expect((await requestToken(one.wire, first)).status()).toBe(401);
    expect((await requestToken(one.wire, second)).status()).toBe(200);
    // A secret-only paste: the old client id with the new secret works.
    expect(
      (await requestToken(one.wire, { clientId: ex.clientId, clientSecret: second.clientSecret })).status(),
    ).toBe(200);
  });

  test('the explicit rotation changes the client id and the secret', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'rotate-id');
    const rotated = await mintCredential(one.rest, ex.integrationId, { rotateClientId: true });
    expect(rotated.clientId).not.toBe(ex.clientId);
    expect(
      (await requestToken(one.wire, { clientId: ex.clientId, clientSecret: rotated.clientSecret })).status(),
    ).toBe(401);
    expect((await requestToken(one.wire, rotated)).status()).toBe(200);
    // Its managers learn why the old id was refused; the caller only that it was.
    const read = await (await one.rest.get(`/api/v1/integrations/${ex.integrationId}`)).json();
    expect(read.a2a_credential?.last_refused_token_request?.reason).toBe('unknown_client');
    expect(read.a2a_credential?.last_refused_token_request?.client_id).toBe(ex.clientId);
  });

  test('a wrong secret is recorded as a secret mismatch for the managers', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'mismatch');
    const refused = await requestToken(one.wire, { clientId: ex.clientId, clientSecret: 'not-the-secret' });
    expect(refused.status()).toBe(401);
    expect((await refused.json()).error).toBe('invalid_client');
    const read = await (await one.rest.get(`/api/v1/integrations/${ex.integrationId}`)).json();
    const last = read.a2a_credential?.last_refused_token_request;
    expect(last?.reason).toBe('secret_mismatch');
    expect(JSON.stringify(last)).not.toContain('not-the-secret');
  });

  test('a bearer minted before the regenerate is refused by the task surface', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'stale');
    const partner = await PartnerClient.connect(ex.cardUrl, ex);
    try {
      // iat is whole seconds: let the bearer's second pass before rotating.
      await new Promise((resolve) => setTimeout(resolve, 1100));
      await mintCredential(one.rest, ex.integrationId);
      // The bearer is a self-contained JWT that has not expired and names a
      // client id that still exists (#105): the rotation stamp on the row is
      // what retires it, on every request.
      const { status, body } = await partner.getTask('e2e-any-task-id');
      expect(status).toBe(401);
      expect(body.error?.message).toBe('Unauthenticated');
    } finally {
      await partner.dispose();
    }
  });

  test("regenerating the secret keeps the partner's recorded history", async ({ one, model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'history');
    const m = marker('activity');
    await model.arm(m, [{ text: 'noted' }]);

    const partner = await PartnerClient.connect(ex.cardUrl, ex);
    const activity = async () =>
      (await one.rest.get(`/api/v1/integrations/${ex.integrationId}/a2a-activity`)).json();
    try {
      const sent = await partner.send(`Record this. ${m}`);
      expect(sent.status, JSON.stringify(sent.body)).toBe(200);
      expect((await activity()).last_activity_at).not.toBeNull();

      // The traffic is this partner's by its client id, which a secret
      // rotation keeps (#105): the Activity tab survives it.
      await mintCredential(one.rest, ex.integrationId);
      expect((await activity()).last_activity_at).not.toBeNull();

      // Only the explicit client id rotation starts the history over.
      await mintCredential(one.rest, ex.integrationId, { rotateClientId: true });
      expect((await activity()).last_activity_at).toBeNull();
    } finally {
      await partner.dispose();
    }
  });

  test('deleting the registration takes its credential and its card with it', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'delete');
    const partner = await PartnerClient.connect(ex.cardUrl, ex);
    try {
      expect((await partner.card()).status()).toBe(200);

      const deleted = await one.rest.delete(`/api/v1/integrations/${ex.integrationId}`);
      expect(deleted.ok(), await deleted.text()).toBeTruthy();

      expect((await requestToken(one.wire, ex)).status()).toBe(401);
      // The delete strips the id out of every agent's bindings, and losing the
      // last inbound binding is what retires the card.
      expect((await partner.card()).status()).toBe(404);
      const agent = await (await one.rest.get(`/api/v1/agents/${ex.agentId}`)).json();
      const bound = Object.values(agent.integration_bindings ?? {}).flat();
      expect(bound).not.toContain(ex.integrationId);
    } finally {
      await partner.dispose();
    }
  });

  // -------------------------------------------------------------------------
  // What the partner is given: the card URL, and what a credential may ask
  // -------------------------------------------------------------------------

  test('the card URL the platform publishes carries its own origin', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Built from the request's own base URL.
    expect(cardUrl.startsWith(`${one.origin}/a2a/`)).toBeTruthy();
    expect(cardUrl.endsWith('/.well-known/agent-card.json')).toBeTruthy();
  });

  test('partner discovery answers a credential, and only for what that credential reaches', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'discovery');
    const partner = await PartnerClient.connect(ex.cardUrl, ex);
    try {
      // Not 401: a caller that does not authenticate learns nothing about
      // what exists here, the endpoint included.
      expect((await partner.discover({ authenticated: false })).status()).toBe(404);

      const listed = await partner.discover();
      expect(listed.status()).toBe(200);
      const agents = (await listed.json()).agents as { name: string; card_url: string }[];
      expect(agents.map((a) => a.name)).toContain(ex.agentName);
      expect(agents.map((a) => a.card_url)).toContain(ex.cardUrl);

      await bind(one.rest, ex.agentId, {});
      const emptied = await partner.discover();
      // Still 200, still holding a valid bearer, and now naming nothing: the
      // answer is a restatement of an authorisation, so it shrinks to an empty
      // list rather than becoming the anonymous not-found.
      expect(emptied.status()).toBe(200);
      expect((await emptied.json()).agents).toEqual([]);
    } finally {
      await partner.dispose();
    }
  });

  // -------------------------------------------------------------------------
  // What an outbound registration is given, and what it refuses
  // -------------------------------------------------------------------------

  test('an outbound registration saved with no credentials does not verify as usable (#105)', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await one.rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name: stamp('E2E outbound credentialless'),
        auth_type: 'none',
        a2a_direction: 'outbound',
        extra_config: { card_urls: cardUrl },
      },
    });
    // Nothing validates the outbound fields on save: the refusal is deferred
    // to the moment a token is actually needed.
    expect(resp.status(), await resp.text()).toBe(201);
    const id = (await resp.json()).id as string;
    callingSide.integrations.push(id);

    const target = targetFor(await verifyNow(one.rest, id), cardUrl);
    // The card is still fetched anonymously, but Verify now tests the
    // credential too by minting a token: with none configured it cannot, and
    // says so, instead of answering usable on a registration that would be
    // refused at the first send.
    expect(target.usability).toBe('unusable');
    expect(target.unusable_reason).toBe('credential_cannot_mint_token');
    expect(target.last_verified_at).not.toBeNull();
  });

  test('saving an edit reconciles the targets rather than replacing them', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const before = targetFor(await verifyNow(one.rest, outboundId), cardUrl);
    expect(before.usability).toBe('usable');

    const saved = await one.rest.put(`/api/v1/integrations/${outboundId}`, {
      data: {
        extra_config: {
          card_urls: cardUrl,
          token_url: tokenUrl,
          client_id: credential.clientId,
          client_secret: 'rotated-by-the-partner',
        },
      },
    });
    expect(saved.ok(), await saved.text()).toBeTruthy();

    const after = targetFor(await registrationState(one.rest, outboundId), cardUrl);
    // A save that rotated a secret must not re-open the card-change gate on
    // every agent this partner exposes: the row is the same row, verdict,
    // fingerprint and all.
    expect(after.id).toBe(before.id);
    expect(after.usability).toBe('usable');
    expect(after.card_fingerprint).toBe(before.card_fingerprint);
    // But it is due again (#105): the verdict was judged on the old secret,
    // so the next delegation or sweep re-verifies, minting with the new one.
    expect(after.last_verified_at).toBeNull();
  });

  test('the stored secret comes back masked while the addresses come back in clear', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const body = await (await one.rest.get(`/api/v1/integrations/${outboundId}`)).json();
    // Masking is by name, and a key ending in _id, _url or _urls is exempt —
    // hiding those would hide which partner a registration points at.
    expect(body.extra_config.client_secret).toBe('********');
    expect(body.extra_config.card_urls).toBe(cardUrl);
    expect(body.extra_config.token_url).toBe(tokenUrl);
    expect(body.extra_config.client_id).toBe(credential.clientId);
  });

  // -------------------------------------------------------------------------
  // Direction is exclusive, and changing it clears what the old one held
  // -------------------------------------------------------------------------

  // Changing direction while an agent binds the registration is refused with
  // a 409 naming that agent, and the refusal writes nothing — asserted in
  // 10-negatives.spec.ts.

  test('assigning the direction it already has is not a change', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const resp = await one.rest.put(`/api/v1/integrations/${boundOutboundId}`, {
      data: { a2a_direction: 'outbound' },
    });
    // Otherwise an ordinary save of a bound registration — the form round-trips
    // the direction it read — would be refused as a direction change.
    expect(resp.status(), await resp.text()).toBe(200);
    const state = await registrationState(one.rest, boundOutboundId);
    expect((state.targets as unknown[]).length).toBe(1);
  });

  test('switching direction clears the registration and deletes its targets', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const registration = await createOutboundIntegration(one.rest, callingSide, {
      cardUrls: cardUrl,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E outbound switching'),
    });
    expect((await verifyNow(one.rest, registration.id)).targets).toHaveLength(1);

    const switched = await one.rest.put(`/api/v1/integrations/${registration.id}`, {
      data: { a2a_direction: 'inbound' },
    });
    expect(switched.status(), await switched.text()).toBe(200);

    const body = await (await one.rest.get(`/api/v1/integrations/${registration.id}`)).json();
    // Everything the previous direction stored is now meaningless: a stored
    // secret with no purpose and no expiry is the thing being avoided.
    expect(body.extra_config ?? {}).toEqual({});
    // Targets are deleted rather than blanked — a row with nothing verified
    // about it is not a cleared registration, it is an agent nobody asked to
    // reach — and the surface reporting on them 404s for an inbound one.
    expect((await one.rest.get(`/api/v1/a2a/client/${registration.id}`)).status()).toBe(404);
  });

  test('switching direction retires the credential the partner already holds', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const ex = await freshExposure(one.rest, exposedSide, 'switch');
    expect((await requestToken(one.wire, ex)).status()).toBe(200);

    // Unbound first, or the change is refused; the token stops minting here
    // simply because nothing resolves, which is the previous test's subject.
    await bind(one.rest, ex.agentId, {});
    for (const direction of ['outbound', 'inbound']) {
      const resp = await one.rest.put(`/api/v1/integrations/${ex.integrationId}`, {
        data: { a2a_direction: direction },
      });
      expect(resp.status(), await resp.text()).toBe(200);
    }
    await bind(one.rest, ex.agentId, { oauth_a2a: [ex.integrationId] });

    // Back where it started, bound and exposing again — and the credential the
    // partner holds is gone anyway. The OAuthClient row is admitted on binding
    // alone with no direction check, so a registration flipped and rebound
    // would otherwise still take the old partner's tasks.
    expect((await requestToken(one.wire, ex)).status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // What the configuration surface itself declares
  // -------------------------------------------------------------------------

  test('the served catalog carries every A2A key the configuration pages read', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const catalog = (await (await one.rest.get('/api/v1/integrations/catalog')).json()) as {
      provider: string;
      available_tools?: { name: string }[];
      a2a_inbound_agent_tools?: { name: string }[];
      a2a_outbound_exposed?: { name: string }[];
    }[];

    for (const provider of ['oauth_a2a', 'entra_a2a', 'google_a2a']) {
      const item = catalog.find((entry) => entry.provider === provider);
      expect(item, `${provider} is missing from the catalog`).toBeTruthy();
      const operations = (item?.available_tools ?? []).map((t) => t.name);
      // A key the response model does not declare is dropped by Pydantic with
      // every backend test still passing, and the symptom is a blank panel
      // rather than an error — so the served shape is what has to be pinned.
      expect(operations).toContain('tasks/pushNotificationConfig/set');
      expect(operations).toContain('tasks/pushNotificationConfig/delete');
      expect(operations).toHaveLength(7);
      expect((item?.a2a_inbound_agent_tools ?? []).map((t) => t.name)).toEqual(['ask_the_caller']);
      expect((item?.a2a_outbound_exposed ?? []).map((t) => t.name)).toEqual([
        'POST /a2a/callbacks/{delegation_id}',
      ]);
    }
  });

  // What each federation provider refuses on save — an entra_a2a tenant that
  // is not a GUID or is missing, a missing or shared audience, a google_a2a
  // caller pin that is absent or that no token could carry — is one table in
  // 10-negatives.spec.ts, which also exercises the update path.
});
