/**
 * The outbound client surface: what a partner registration knows, and when it learns it.
 *
 * These branches belong in one file because they are one story told at one
 * seam. Everything an outbound registration does — offering a tool, refusing a
 * destination, suspending on a changed card, pausing after failures — is
 * decided by ONE piece of state, the verdict on a target row, and that verdict
 * is written by exactly three callers. So the order in which those callers run
 * is the subject: nothing verifies on save, an operator's Verify does, the
 * sweep does on its own, and a delegation does before it submits. Splitting
 * "what Verify records" from "what a save leaves behind" would leave the most
 * expensive misunderstanding in the feature — that a saved registration is a
 * working one — asserted nowhere.
 *
 * The suite parks the re-verification sweep — `CLOCK.sweepIntervalSeconds` is an
 * hour — and that is what makes the first branch observable at all. Staleness
 * cannot substitute: `due_targets` treats a NULL verdict as due whatever the
 * threshold says, so a running sweep verifies every registration a spec creates
 * within seconds and "nothing verified it" becomes unobservable even though it
 * is true. Parked, the never-verified state is a plain read rather than a race.
 * The one branch whose SUBJECT is the sweep skips itself unless the deployment was
 * given a short `A2A_CLIENT_SWEEP_INTERVAL_SECONDS`.
 *
 * Two things asserted here contradict what the surrounding docs say, and both
 * are pinned deliberately rather than quietly worked around — see the comments
 * on "not verified until something verifies it" and on the tools panel.
 *
 * No branch here opens a page or needs the worker, so `WORKER_UP` gates
 * nothing. Only two need the model: the two that ask
 * what an agent was actually OFFERED, which is the one question the control
 * plane cannot answer about itself.
 */

import type { APIRequestContext } from '@playwright/test';
import { expect, test } from './fixtures';
import { CLOCK, SWEEP_IS_FAST } from './instance';
import { marker } from '../scripted-llm/client';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  createOutboundIntegration,
  exposeAgent,
  mintCredential,
  registerPartner,
  registrationState,
  stamp,
  toolNames,
  tracker,
  verifyNow,
} from './seed';
import { eventually, requestToken } from './wire';

// ---------------------------------------------------------------------------
// Local helpers. Nothing here is reusable enough to belong in the harness: the
// slug rule is being ASSERTED rather than used, and the rest are one-line
// readers for endpoints only this file touches.
// ---------------------------------------------------------------------------

/**
 * The naming rule, restated rather than imported.
 *
 * A tool name reaches the model provider's schema and an operator's screen, so
 * it is a contract with two audiences and worth pinning independently. Deriving
 * the expectation from the product's own helper would assert only that a
 * function equals itself.
 */
function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

type Target = Record<string, unknown>;

function targetsOf(state: Record<string, unknown>): Target[] {
  return (state.targets as Target[]) ?? [];
}

/** The one target for `cardUrl`, or a failure naming what the registration does hold. */
function targetFor(state: Record<string, unknown>, cardUrl: string): Target {
  const found = targetsOf(state).find((t) => t.card_url === cardUrl);
  if (!found) {
    throw new Error(
      `No target for ${cardUrl}. The registration holds: ` +
        targetsOf(state)
          .map((t) => String(t.card_url))
          .join(', '),
    );
  }
  return found;
}

/** Test Connection, which reports the STORED verdict rather than deriving one. */
async function testConnection(
  rest: APIRequestContext,
  integrationId: string,
): Promise<{ status: string; message: string }> {
  const resp = await rest.post(`/api/v1/integrations/${integrationId}/test`);
  if (!resp.ok()) throw new Error(`probe failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

/**
 * One chat turn, for the two branches that need to know what the agent was
 * OFFERED. The answer is read from the scripted model's record rather than from
 * the response, because the chat response's `tool_names` comes back empty even
 * when a tool really was offered and called.
 *
 * A turn is not necessarily one request to the model, and every request that
 * carries the user's text matches the same marker. The conversation is
 * created with a title of its own, which is what keeps the AI titling pass
 * out of it — that pass fires only on the literal title "New conversation" —
 * but nothing else about the turn count is this file's to promise. Both
 * callers therefore arm several identical steps and take the UNION of what
 * was offered across every matching request, rather than assuming which one
 * was the agent's own.
 */
async function chatTurn(
  rest: APIRequestContext,
  agentId: string,
  content: string,
): Promise<void> {
  const created = await rest.post('/api/v1/chat/conversations', {
    data: { agent_id: agentId, title: stamp('A2A e2e') },
  });
  if (!created.ok()) {
    throw new Error(`conversation failed: ${created.status()} ${await created.text()}`);
  }
  const conversationId = (await created.json()).id as string;
  const answered = await rest.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
    data: { content },
  });
  if (!answered.ok()) {
    throw new Error(`chat turn failed: ${answered.status()} ${await answered.text()}`);
  }
}

// ---------------------------------------------------------------------------

test.describe.serial('The outbound partner registration', () => {
  let available = false;

  const exposedSide = tracker();
  const callingSide = tracker();

  // The partner's side of the relationship — family 1, exposed.
  let primaryId = '';
  let primaryName = '';
  let primaryCard = '';
  let secondId = '';
  let secondName = '';
  let secondCard = '';
  let inboundId = '';
  let credential = { clientId: '', clientSecret: '' };
  let emptyCredential = { clientId: '', clientSecret: '' };
  let tokenUrl = '';

  // The calling side — family 2, enrolled.
  let partnerId = '';
  let partnerName = '';
  let brokenId = '';
  let emptyPartnerId = '';
  let sweptId = '';
  let callerId = '';

  // The card URLs the broken registration points at, one per failure class.
  // Only the three classified here are named; the destination refusals it also
  // carries are asserted in 10-negatives.spec.ts.
  let unknownCard = '';
  let notACard = '';
  let unresolvable = '';

  // Captured in `beforeAll`, asserted in the first two tests — see the header.
  let freshState: Record<string, unknown> = {};
  let freshProbe = { status: '', message: '' };

  test.beforeAll(async ({ one, model }) => {
    available = (await a2aAvailable(one.rest)) && (await a2aAvailable(one.rest));
    if (!available) return;
    await model.reset();

    // -- A exposes ---------------------------------------------------------
    const inbound = await createInboundIntegration(one.rest, exposedSide, stamp('A inbound'));
    inboundId = inbound.id;
    credential = await mintCredential(one.rest, inbound.id);

    // Short labels on purpose. The name the MODEL sees is capped at 64
    // characters, and `delegate_to_` plus two stamped slugs passes that easily
    // — which would make the tool-name assertions test the truncation rather
    // than the rule.
    const primary = await createAgent(one.rest, exposedSide, { name: stamp('Rules') });
    primaryId = primary.id;
    primaryName = primary.name;
    primaryCard = await exposeAgent(one.rest, primary.id, inbound.id);

    // Created now, bound later: the discovery branch is about an agent that
    // appears at the partner AFTER B has already looked.
    const second = await createAgent(one.rest, exposedSide, { name: stamp('Triage') });
    secondId = second.id;
    secondName = second.name;

    // A second inbound registration with a credential and nothing bound to it.
    // Its whole purpose is to be a partner whose credential reaches no agent,
    // which is the only way to see an empty discovery answer without unbinding
    // the agent every other test in this file depends on.
    const emptyInbound = await createInboundIntegration(
      one.rest,
      exposedSide,
      stamp('A inbound empty'),
    );
    emptyCredential = await mintCredential(one.rest, emptyInbound.id);

    // -- B reaches out -----------------------------------------------------
    tokenUrl = `${one.origin}/mcp/oauth/token`;

    unknownCard = `${one.origin}/a2a/e2e-no-such-exposure/.well-known/agent-card.json`;
    // 200, JSON, and an object — but no endpoint anywhere in it. On the
    // trusted origin, so the destination guard lets it through and the
    // projection is what refuses.
    notACard = `${one.origin}/api/health`;
    unresolvable = 'https://xtm-e2e-partner.invalid/.well-known/agent-card.json';

    // Credentials that cannot mint, so discovery on this registration is a
    // no-op and its target list stays exactly the six typed here. The three
    // inline ones are the destination guard's classes — a scheme nobody
    // vouches for, a loopback literal, the cloud metadata address — and they
    // are here for the count and for the aggregated probe below; what each is
    // refused WITH is 10-negatives.spec.ts's table. Hard-coded ports are
    // fine in an address that must be REFUSED: no port change can make a
    // refusal pass wrongly.
    const broken = await createOutboundIntegration(one.rest, callingSide, {
      name: stamp('B to nowhere'),
      cardUrls: [
        unknownCard,
        notACard,
        'http://xtm-e2e-partner.invalid/.well-known/agent-card.json',
        'https://127.0.0.1:8100/.well-known/agent-card.json',
        'https://169.254.169.254/.well-known/agent-card.json',
        unresolvable,
      ],
      tokenUrl,
      clientId: 'e2e-not-a-client',
      clientSecret: 'e2e-not-a-secret',
    });
    brokenId = broken.id;

    const emptyPartner = await createOutboundIntegration(one.rest, callingSide, {
      name: stamp('B to empty'),
      cardUrls: primaryCard,
      tokenUrl,
      clientId: emptyCredential.clientId,
      clientSecret: emptyCredential.clientSecret,
    });
    emptyPartnerId = emptyPartner.id;

    // Never verified by this spec. Whatever verdict it ends up carrying was
    // written by the sweep, which is the point of it — and on a deployment with the
    // sweep parked it carries none, which is why the branch reading it skips.
    const swept = await createOutboundIntegration(one.rest, callingSide, {
      name: stamp('B swept'),
      cardUrls: primaryCard,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
    });
    sweptId = swept.id;

    // LAST, and read back immediately. With the sweep parked nothing could
    // write a verdict here anyway, but a deployment given a short `A2A_CLIENT_SWEEP_INTERVAL_SECONDS`
    // would verify a registration left sitting between its save and the first
    // test, and saving last costs nothing.
    const partner = await createOutboundIntegration(one.rest, callingSide, {
      name: stamp('B to A'),
      cardUrls: primaryCard,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
    });
    partnerId = partner.id;
    partnerName = partner.name;
    freshState = await registrationState(one.rest, partner.id);
    freshProbe = await testConnection(one.rest, partner.id);

    const caller = await createAgent(one.rest, callingSide, { name: stamp('B caller') });
    callerId = caller.id;
  });

  test.afterAll(async ({ one }) => {
    await cleanup(one.rest, callingSide);
    await cleanup(one.rest, exposedSide);
  });

  // -------------------------------------------------------------------------
  // What a save alone leaves behind
  // -------------------------------------------------------------------------

  test('a saved registration is not verified until something verifies it', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // a2a-review-scenarios.md §2 says the registration verifies on save. It
    // does not: `verify_registration` has three callers and none of them is
    // the create path, so the row lands with the column default and stays
    // there until an operator presses Verify, an agent delegates, or the sweep
    // comes round. On this deployment that last one is `CLOCK.sweepIntervalSeconds`
    // away, so nothing about this is a race — the row is still untouched when
    // the assertion reads it, and the only file that asserts it is this one.
    const targets = targetsOf(freshState);
    expect(targets, 'the operator typed one card URL, so there is one target').toHaveLength(1);
    expect(targets[0].usability).toBe('unusable');
    expect(targets[0].unusable_reason, 'unusable with no reason is "never looked"').toBeNull();
    expect(targets[0].last_verified_at).toBeNull();
    expect(targets[0].signature_state).toBeNull();
    expect(targets[0].card_fingerprint).toBeNull();
  });

  test('Test Connection on a never-verified partner cannot say so', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The probe carries advice for "never_verified" — "Not verified yet — use
    // Test Connection to verify now." — keyed by `unusable_reason`. A target
    // that has never been verified carries a NULL reason, so the lookup misses
    // and the operator is shown the generic fallback instead. The one message
    // written for the commonest first-run state is unreachable; asserted as it
    // behaves so the day it is fixed is a deliberate one.
    expect(freshProbe.status).toBe('error');
    expect(freshProbe.message).toBe('Not usable (unknown reason).');
  });

  test('verifying is not found for a registration facing the other way', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Asserted from A, where the registration really is inbound. The 404 is
    // not a gap in the surface, it is how the frontend LEARNS the direction:
    // only a 404 reads as inbound, and any other failure leaves the direction
    // undefined so a save cannot strip the live variant's fields. The read
    // half of the same rule — and the non-A2A provider — belong to
    // 10-negatives.spec.ts, which compares them byte for byte; the write
    // half is here because acting on the wrong direction is this file's
    // subject.
    const verify = await one.rest.post(`/api/v1/a2a/client/${inboundId}/verify`);
    expect(verify.status()).toBe(404);
  });

  // -------------------------------------------------------------------------
  // What Verify records
  // -------------------------------------------------------------------------

  test('verifying agrees a version and a transport, and records the card as unsigned', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await verifyNow(one.rest, partnerId);
    const target = targetFor(state, primaryCard);

    expect(target.usability).toBe('usable');
    // Nothing on this platform signs a card, so the suite can only ever produce
    // the unsigned verdict — and unsigned is usable by design, trusted on the
    // transport and on the URL the operator supplied. An assertion that the
    // state is recorded rather than omitted is the point: silence would read
    // as the stronger claim.
    expect(target.signature_state).toBe('unsigned');
    expect(['0.3', '1.0']).toContain(target.agreed_protocol_version);
    expect(target.transport).toBe('JSONRPC');
    expect(target.card_fingerprint).toBeTruthy();
    expect(target.last_verified_at).toBeTruthy();
    // A first verification accepts the card it saw, so nothing is pending.
    expect(target.card_change_pending).toBe(false);
    expect(target.unusable_reason).toBeNull();
    expect(target.last_verification_error).toBeNull();
    // Verification is not a failure, so the breaker has nothing to say.
    expect(target.circuit_open_until).toBeNull();
  });

  test('verifying records what the partner declares about itself', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(one.rest, partnerId);
    const target = targetFor(state, primaryCard);

    expect(target.declared_name, "the agent's own name, read off its card").toBe(primaryName);
    expect(target.declared_input_types).toContain('text/plain');
    expect(target.declared_output_types).toContain('text/plain');
    expect(
      (target.declared_skills as unknown[]).length,
      'an agent with no tools still declares "converse"',
    ).toBeGreaterThan(0);
    // Read as `is True` from a partner-authored field, and recorded on every
    // successful verification so a partner that drops the capability stops
    // being registered with. Deliberately outside the fingerprint: gaining or
    // losing callbacks must not suspend the registration.
    expect(target.declares_callbacks).toBe(true);
  });

  test('the calling deployment declares itself reachable for callbacks', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Both halves are the same loopback deployment, so the destination guard's
    // own verdict on BASE_URL would read "unreachable" and no callback branch
    // could ever run. The operator's explicit setting is what overrules it.
    const state = await registrationState(one.rest, partnerId);
    expect(state.callbacks_reachable).toBe(true);
  });

  // -------------------------------------------------------------------------
  // What the registration confers
  // -------------------------------------------------------------------------

  test('the tools are named for the registration and for the partner agent', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(one.rest, partnerId);

    // One delegation tool per partner agent, so a model choosing between two of
    // them has something to choose on; one cancel tool for the registration,
    // because the hand-back hands it a task id and it needs no target.
    expect(toolNames(state)).toEqual([
      `delegate_to_${slug(partnerName)}_${slug(primaryName)}`,
    ]);
    expect(state.cancel_tool_name).toBe(`cancel_task_at_${slug(partnerName)}`);
  });

  test('an agent bound to the registration is offered those tools', async ({ one, model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    await bind(one.rest, callerId, { oauth_a2a: [partnerId] });

    const m = marker('offered');
    await model.arm(m, [{ text: 'Nothing.' }, { text: 'Nothing.' }, { text: 'Nothing.' }]);
    await chatTurn(one.rest, callerId, `Say nothing at all. ${m}`);

    const turns = await model.agentTurns(m);
    expect(turns.length, 'the agent never reached the model').toBeGreaterThan(0);
    const offered = new Set(turns.flatMap((t) => t.tools_offered));
    // The name the API reports and the name the model is given are computed by
    // one helper on two paths, and this is the only place the two paths meet.
    expect([...offered]).toContain(`delegate_to_${slug(partnerName)}_${slug(primaryName)}`);
    expect([...offered]).toContain(`cancel_task_at_${slug(partnerName)}`);
  });

  test('a name too long for a tool schema is cut for the model and not for the API', async ({
    one,
    model,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The two paths agree only while the name fits. Every tool name is
    // sanitised on its way to a provider, which truncates at 64 characters
    // (`app/tools/base.py::sanitize_tool_name` — Gemini's limit, applied to
    // every provider; Anthropic and OpenAI allow 128), while
    // `/api/v1/a2a/client/{id}` reports the uncut name. So an operator with
    // long names reads a tool name in the outbound panel that the model is
    // never given. Asserted as observed: the two disagree, and a fix cannot
    // land silently.
    const longPartner = await registerPartner(one.rest, callingSide, {
      cardUrls: primaryCard,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E outbound with a deliberately long registration name'),
    });
    const reported = toolNames(longPartner.state)[0];
    expect(reported.length, 'the fixture must produce an over-long name').toBeGreaterThan(64);

    const longCaller = await createAgent(one.rest, callingSide, { name: stamp('E2E long-name caller') });
    await bind(one.rest, longCaller.id, { oauth_a2a: [longPartner.id] });

    const m = marker('long-name');
    await model.arm(m, [{ text: 'Nothing.' }]);
    await chatTurn(one.rest, longCaller.id, `Say nothing at all. ${m}`);

    const offered = new Set(
      (await model.agentTurns(m)).flatMap((t) => t.tools_offered),
    );
    expect([...offered], 'the uncut name should not reach the model').not.toContain(reported);
    expect(
      [...offered],
      'and what does reach it is the same name, cut to the schema ceiling',
    ).toContain(reported.slice(0, 64));
  });

  // -------------------------------------------------------------------------
  // How a registration fails, and which failure it is called
  // -------------------------------------------------------------------------

  test('a partner that is not there reads as card_unfetchable', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await verifyNow(one.rest, brokenId);

    // Two ways to not be there, one classification. A name that will not
    // resolve is deliberately NOT a destination refusal: it is a partner an
    // operator fixes by correcting the URL, not a policy decision.
    const gone = targetFor(state, unknownCard);
    expect(gone.usability).toBe('unusable');
    expect(gone.unusable_reason).toBe('card_unfetchable');
    expect(gone.last_verification_error).toBe('HTTP 404 from card URL');

    const nowhere = targetFor(state, unresolvable);
    expect(nowhere.unusable_reason).toBe('card_unfetchable');
    expect(String(nowhere.last_verification_error)).toContain('Cannot resolve');
  });

  test('a URL that answers with something that is not a card reads as card_malformed', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(one.rest, brokenId);
    const target = targetFor(state, notACard);

    // The body parses, it is an object, and it still describes nowhere to send
    // work — which is decided by the interface selection rather than by the
    // parse, and lands on the same reason.
    expect(target.unusable_reason).toBe('card_malformed');
    expect(String(target.last_verification_error)).toContain('declares no endpoint');
  });

  // The destination guard's own refusals — a scheme nobody vouches for, a
  // private address, the cloud metadata endpoint, a name that will not resolve
  // — are asserted target by target in 10-negatives.spec.ts, one
  // machine-readable reason and one exact message each. This file keeps only
  // the two classifications that table does not draw: card_unfetchable above,
  // and card_malformed, which are verdicts about a card that WAS fetched
  // rather than decisions taken before the request left.
  //
  // One message is asserted nowhere as a result: the loopback literal in the
  // fixture above is refused as "loopback address", and that table's private
  // -address row is a different check. Its reason class is still covered by
  // the aggregated probe below.

  test('Test Connection aggregates every target rather than reporting the first', async ({
    one,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const probe = await testConnection(one.rest, brokenId);
    expect(probe.status).toBe('error');
    expect(probe.message).toContain('None of 6 agents is reachable');
    // The reason per agent, not one verdict for the partner: a registration
    // reaching six agents has six verdicts and an operator has to know which.
    expect(probe.message).toContain('card_unfetchable');
    expect(probe.message).toContain('transport_unverified');
    expect(probe.message).toContain('destination_not_permitted');
  });

  test('the tools panel names nothing for targets no agent can use', async ({ one, model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(one.rest, brokenId);

    // The page cannot show a name the model never sees, which is TargetState's
    // own contract. The builder skips a target that is not usable and offers a
    // cancel only where it built a delegation tool, so a partner none of whose
    // agents can be reached has no tools at all — and the Config tab's empty
    // state, "None yet — no agent at this partner has verified.", is what it
    // reports. The rows themselves are still listed: an operator cannot press
    // Verify on what the page hides.
    expect(toolNames(state), 'no name for a target nobody can use').toEqual([]);
    expect(state.cancel_tool_name, 'nothing to cancel, so nothing named').toBeNull();
    expect(targetsOf(state), 'the six rows are still shown').toHaveLength(6);

    await bind(one.rest, callerId, { oauth_a2a: [brokenId] });
    const m = marker('unusable');
    await model.arm(m, [{ text: 'Nothing.' }, { text: 'Nothing.' }, { text: 'Nothing.' }]);
    await chatTurn(one.rest, callerId, `Say nothing at all. ${m}`);

    const turns = (await model.calls()).calls.filter((c) => c.matched === m);
    expect(turns.length, 'the agent never reached the model').toBeGreaterThan(0);
    const offered = turns.flatMap((t) => t.tools_offered);
    expect(
      offered.filter((n) => n.startsWith('delegate_to_')),
      'no target verified, so no delegation tool is built',
    ).toEqual([]);
    expect(
      offered.filter((n) => n.startsWith('cancel_task_at_')),
      'nothing to cancel, so no cancel tool either',
    ).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // Who else writes a verdict
  // -------------------------------------------------------------------------

  test('the sweep verifies a registration nobody pressed Verify on', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The suite parks the sweep so that the never-verified branch at the top of
    // this file is observable at all — `due_targets` serves a NULL verdict
    // whatever the staleness threshold is, so only the interval can hold it
    // off. That leaves this branch, whose subject IS the sweep, with nothing
    // to watch unless the deployment was started the other way.
    test.skip(!SWEEP_IS_FAST, 'the sweep is parked on this deployment');
    // Nothing in this spec ever verifies this registration, and reading it
    // runs discovery rather than verification — so a verdict on it can only
    // have been written by the background sweep. Polled rather than asserted
    // outright: the sweep is best-effort background work, and the tests above
    // may have taken less than one cycle.
    const verdict = await eventually(
      async () => {
        const state = await registrationState(one.rest, sweptId);
        const target = targetFor(state, primaryCard);
        expect(target.last_verified_at).toBeTruthy();
        return target;
      },
      {
        // Two cycles, plus the round trip to A that verifying one card costs.
        timeoutMs: CLOCK.sweepIntervalSeconds * 2_000 + 15_000,
        what: 'the sweep verifying an untouched registration',
      },
    );
    expect(verdict.usability).toBe('usable');
    expect(verdict.declared_name).toBe(primaryName);
  });

  // -------------------------------------------------------------------------
  // Discovery
  // -------------------------------------------------------------------------

  test('a Verify picks up an agent bound at the partner since the last look', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    secondCard = await exposeAgent(one.rest, secondId, inboundId);

    // Verify runs discovery unconditionally before verifying, which is what
    // makes this deterministic; the read path would get there too, but only
    // once past its 10s throttle, and the sweep only on its next cycle.
    const state = await verifyNow(one.rest, partnerId);
    expect(targetsOf(state), 'the partner now exposes two agents').toHaveLength(2);

    const added = targetFor(state, secondCard);
    expect(added.usability, 'discovered and verified in the same call').toBe('usable');
    expect(added.declared_name).toBe(secondName);
    // A discovered agent is a full target, so it confers its own tool.
    expect(toolNames(state)).toContain(
      `delegate_to_${slug(partnerName)}_${slug(secondName)}`,
    );
  });

  test('a credential that reaches no agent cannot mint a token at all', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The discovery endpoint has an empty-list branch, and a credential can
    // never reach it: the grant refuses before issuing, because a token is
    // only minted while the credential still addresses something that answers
    // (`resolvable_exposures_for_integration`, backend/app/a2a/grants.py). So
    // "authenticated but reaching nothing" is not a state a partner can hold.
    //
    // That is the stronger property and the one worth pinning — revocation
    // needs no separate step, because unbinding the last agent stops the
    // credential itself rather than merely emptying what it can see.
    const refused = await requestToken(one.wire, emptyCredential);
    expect(refused.status()).toBe(401);
    expect(await refused.json()).toEqual({ error: 'invalid_client' });

    // Which is byte-identical to what an unknown client id gets, so a caller
    // cannot tell "your partner unbound every agent" from "no such credential".
    const unknown = await requestToken(one.wire, {
      clientId: 'a2a-does-not-exist',
      clientSecret: 'irrelevant',
    });
    expect(unknown.status()).toBe(refused.status());
    expect(await unknown.json()).toEqual(await refused.json());

    // And with no credential at all the surface says nothing either.
    const anonymous = await one.wire.get('/a2a/partner/agents');
    expect(anonymous.status()).toBe(404);
  });

  test('a partner listing no agents leaves the registration alone', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The destructive case: an empty or odd answer must never strip a
    // registration of every tool, so only a well-formed list with at least one
    // agent reconciles. The operator's own URL keeps its verdict.
    const state = await verifyNow(one.rest, emptyPartnerId);
    expect(targetsOf(state)).toHaveLength(1);
    const target = targetFor(state, primaryCard);
    expect(target.usability).toBe('usable');
    expect(target.declared_name).toBe(primaryName);
  });

  test('verifying one target names that target, or nothing', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const before = await registrationState(one.rest, partnerId);
    const namedTargetId = String(targetFor(before, secondCard).id);

    const unknown = await one.rest.post(
      `/api/v1/a2a/client/${partnerId}/verify?target_id=${crypto.randomUUID()}`,
    );
    expect(unknown.status(), 'an id this registration does not own is not found').toBe(404);

    const named = await one.rest.post(
      `/api/v1/a2a/client/${partnerId}/verify?target_id=${namedTargetId}`,
    );
    expect(named.ok()).toBe(true);
    const state = (await named.json()) as Record<string, unknown>;
    // The whole registration is reported back even though one card was
    // verified, so a UI refreshing from this response cannot lose a sibling.
    expect(targetsOf(state)).toHaveLength(2);
    expect(targetFor(state, secondCard).usability).toBe('usable');
    // And the sibling was left alone, which is the reason the parameter
    // exists: verifying the whole partner to check one card would re-open
    // another target's cooldown as a side effect of an operator asking about
    // something else. Only observable with the sweep parked — a running one
    // moves every stamp on its own, so an unchanged stamp would prove nothing.
    if (!SWEEP_IS_FAST) {
      expect(targetFor(state, primaryCard).last_verified_at).toBe(
        targetFor(before, primaryCard).last_verified_at,
      );
    }
  });

  // -------------------------------------------------------------------------
  // Card drift, and the operator's acceptance
  // -------------------------------------------------------------------------

  test('a material change to the partner card suspends delegation', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Output content types are fingerprinted; the agent's own configuration is
    // what decides them, so switching its output format is the smallest real
    // edit at the partner that moves a MATERIAL field.
    const edited = await one.rest.put(`/api/v1/agents/${primaryId}`, {
      data: { output_format: 'json' },
    });
    expect(edited.ok()).toBe(true);

    const state = await verifyNow(one.rest, partnerId);
    const target = targetFor(state, primaryCard);
    expect(target.usability).toBe('unusable');
    expect(target.unusable_reason).toBe('card_change_unaccepted');
    expect(target.card_change_pending).toBe(true);
    // Per target, not per partner: the agent whose card did not move carries on.
    expect(targetFor(state, secondCard).usability).toBe('usable');
  });

  test('Test Connection reports a partly reachable partner as neither', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const probe = await testConnection(one.rest, partnerId);
    // "degraded" reaches the wire as "info". A partner with one agent suspended
    // is neither healthy nor broken, and reporting either sends an operator to
    // the wrong place.
    expect(probe.status).toBe('info');
    expect(probe.message).toContain('1 of 2 reachable');
    expect(probe.message).toContain('The partner changed its card');
  });

  // What acceptance REFUSES — a stale fingerprint, a target belonging to
  // another registration, and nothing to accept — is one table in
  // 10-negatives.spec.ts, byte for byte. The positive branch is here,
  // because resuming a suspended target is what the rest of this file is
  // about.

  test('accepting the observed card resumes the target', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const state = await registrationState(one.rest, partnerId);
    const target = targetFor(state, primaryCard);

    const accepted = await one.rest.post(`/api/v1/a2a/client/${partnerId}/accept-card`, {
      data: { target_id: target.id, fingerprint: target.card_fingerprint },
    });
    expect(accepted.ok()).toBe(true);
    const resumed = (await accepted.json()) as Record<string, unknown>;
    expect(resumed.usability).toBe('usable');
    expect(resumed.unusable_reason).toBeNull();
    expect(resumed.card_change_pending).toBe(false);
    // Acceptance has to take effect at once — the pre-flight reads usability
    // before it compares fingerprints — so this is read from the write's own
    // answer rather than from a later poll.
    expect(resumed.last_verification_error).toBeNull();
  });

  test('a cosmetic change to the partner card does not suspend it', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Description, name and skills are outside the fingerprint on purpose:
    // suspending because a partner reworded itself would train an operator to
    // accept without reading, which is worse than not suspending at all.
    const description = `Reworded by the e2e suite at ${Date.now()}`;
    const edited = await one.rest.put(`/api/v1/agents/${primaryId}`, { data: { description } });
    expect(edited.ok()).toBe(true);

    const state = await verifyNow(one.rest, partnerId);
    const target = targetFor(state, primaryCard);
    expect(target.usability).toBe('usable');
    expect(target.card_change_pending).toBe(false);
    expect(target.declared_description).toBe(description);
  });
});
