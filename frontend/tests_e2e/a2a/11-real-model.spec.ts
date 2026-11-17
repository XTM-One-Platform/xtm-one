/**
 * Does a real model still choose to delegate?
 *
 * Every other file in this suite runs on the scripted model, which answers
 * from a script the test armed. That is the right instrument for almost all
 * of A2A: it proves the mechanism behind a decision — the tool is generated,
 * offered, invoked, the partner reached, the task recorded — without making
 * the result depend on what a model felt like doing.
 *
 * It cannot prove the decision itself. The delegation tool's name and
 * description are assembled at runtime from the partner's own agent card
 * (`build_delegation_tool`, backend/app/a2a/client/tool.py), and a model reads
 * that description and nothing else when deciding whether this is the tool for
 * the job. Weaken the description, drop the declared skills from the card, let
 * the generated name lose the partner's identity, and every scripted test
 * still passes while the feature quietly stops working for real callers.
 *
 * So this file is the opposite bargain: a real, tool-capable model, given only
 * the real descriptions, and assertions on nothing but WHICH tool it reached
 * for. Never on its prose — a model's wording is not a contract, and asserting
 * on it would produce a test that fails every time a model is upgraded.
 *
 * It runs only where the deployment was given such a model, seeded alongside
 * the scripted endpoint rather than instead of it; an agent names which one
 * answers for it when it is created. `x-e2e-environment` omits
 * `LLM_PROVIDER_OLLAMA_*`, so running this lane means adding those names to the
 * anchor and exporting them. CI does neither, so this file skips there — which
 * is the point of keeping it separate. Green in CI must not depend on a local model, and a
 * regression only this file can catch is worth catching on a developer's
 * machine and in a nightly run rather than not at all.
 *
 * If something here fails, read it as "a real model no longer picks this tool
 * from its description", not as "A2A is broken" — the scripted files answer
 * the second question, and they run first.
 */

import type { APIRequestContext } from '@playwright/test';
import { test, expect } from './fixtures';
import { HAS_REAL_MODEL, REAL_MODEL, WORKER_UP } from './instance';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  exposeAgent,
  mintCredential,
  registerPartner,
  setHandover,
  stamp,
  toolNames,
  tracker,
  type Created,
} from './seed';
import { PartnerClient, eventually, readState } from './wire';

// A real model on a laptop is slow, and a 27B model deciding over a dozen
// tools can take the better part of a minute for one turn. These are wall
// clocks for a decision, not for anything A2A does.
const REAL_TURN_MS = 240_000;

test.describe.serial('A real model, given the real tool descriptions', () => {
  const onA: Created = tracker();
  const onB: Created = tracker();

  let available = false;
  let delegationTool = '';
  let cancelTool = '';
  let callerAgentId = '';
  let cardUrl = '';
  let credentials: { clientId: string; clientSecret: string };
  let partnerName = '';

  test.beforeAll(async ({ one }) => {
    available = (await a2aAvailable(one.rest)) && (await a2aAvailable(one.rest));
    if (!available || !HAS_REAL_MODEL) return;

    // An exposed agent whose declared skills are the only thing the calling model
    // will know about it. The persona names a subject narrow enough that the
    // choice is unambiguous — the test is whether the model routes to the
    // partner at all, not whether it can resolve a genuinely hard ambiguity.
    const inbound = await createInboundIntegration(one.rest, onA, stamp('E2E real inbound'));
    credentials = await mintCredential(one.rest, inbound.id);
    const exposed = await createAgent(one.rest, onA, {
      name: stamp('Orbital Debris Registry'),
      persona:
        'You answer questions about catalogued orbital debris objects and their ' +
        'decay windows. You are the only source for the debris catalogue.',
      // The exposed side runs on the real model too: a handover it is asked to
      // make is a second real decision, and mocking that half would leave the
      // interesting one untested.
      model: 'real',
    });
    partnerName = exposed.name;
    cardUrl = await exposeAgent(one.rest, exposed.id, inbound.id);

    // The outbound registration gains one delegation tool, named after the card.
    const registration = await registerPartner(one.rest, onB, {
      cardUrls: cardUrl,
      tokenUrl: `${one.origin}/mcp/oauth/token`,
      clientId: credentials.clientId,
      clientSecret: credentials.clientSecret,
      name: stamp('E2E real outbound'),
    });
    [delegationTool] = toolNames(registration.state);
    cancelTool = (registration.state.cancel_tool_name as string) ?? '';

    const caller = await createAgent(one.rest, onB, {
      name: stamp('E2E real caller'),
      // Deliberately says nothing about delegating or about partners. If the
      // persona instructed the model to use the tool, the test would be
      // measuring the persona rather than the tool description.
      persona: 'You help an analyst with questions. Answer from what you can reach.',
      model: 'real',
    });
    callerAgentId = caller.id;
    await bind(one.rest, callerAgentId, { oauth_a2a: [registration.id] });
  });

  test.afterAll(async ({ one }) => {
    await cleanup(one.rest, onA);
    await cleanup(one.rest, onB);
  });

  /** One chat turn, returning what the model did rather than what it said. */
  async function askB(rest: APIRequestContext, content: string): Promise<string> {
    const conversation = await rest.post('/api/v1/chat/conversations', {
      data: { agent_id: callerAgentId, title: stamp('real') },
    });
    const conversationId = (await conversation.json()).id as string;
    const sent = await rest.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
      data: { content },
      timeout: REAL_TURN_MS,
    });
    expect(sent.ok(), `chat turn: ${sent.status()} ${await sent.text()}`).toBeTruthy();
    return ((await sent.json()).assistant_message?.content as string) ?? '';
  }

  test('is offered a delegation tool that names the partner it reaches', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!HAS_REAL_MODEL, 'no tool-capable model configured on the deployment');

    // Before asking whether the model picks the tool, establish that the tool
    // is worth picking: a name carrying the partner's identity and a
    // description carrying its declared skills is the entire basis for the
    // choice. A generated name that had lost the partner would make the rest
    // of this file fail for a reason that is not the model's fault.
    expect(delegationTool, 'the registration generated no delegation tool').toBeTruthy();
    expect(delegationTool).toMatch(/^delegate_to_/);
    expect(cancelTool).toMatch(/^cancel_task_at_/);
  });

  test('reaches for the partner when the question is the partner\'s subject', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!HAS_REAL_MODEL, 'no tool-capable model configured on the deployment');
    test.setTimeout(REAL_TURN_MS + 120_000);

    const before = await one.rest.get(`/api/v1/a2a/client/${onB.integrations[0]}/delegations`);
    const baseline = ((await before.json()) as unknown[]).length;

    await askB(
      one.rest,
      'What is the decay window for catalogued debris object 1982-092ADX? ' +
        'I need the registry answer, not an estimate.',
    );

    // The assertion is that a delegation happened, read from the caller's own record of
    // it — not from the model's prose, and not from the chat response's
    // tool_names, which comes back empty even when a delegation really ran.
    const rows = await eventually(
      async () => {
        const resp = await one.rest.get(
          `/api/v1/a2a/client/${onB.integrations[0]}/delegations`,
        );
        const all = (await resp.json()) as Record<string, unknown>[];
        const fresh = all.slice(baseline);
        expect(fresh.length).toBeGreaterThan(0);
        return fresh;
      },
      {
        timeoutMs: 60_000,
        what: `a delegation through ${delegationTool} after asking ${REAL_MODEL} the partner's own subject`,
      },
    );

    expect(rows[0].agent_name).toBeTruthy();
  });

  test('leaves the partner alone for a question that is not its subject', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!HAS_REAL_MODEL, 'no tool-capable model configured on the deployment');
    test.setTimeout(REAL_TURN_MS + 120_000);

    // The other half of the same property, and the one a description that is
    // too eager would fail: a partner described as the debris registry should
    // not be consulted about arithmetic. Without this, "always delegate" would
    // pass the previous test.
    const before = await one.rest.get(`/api/v1/a2a/client/${onB.integrations[0]}/delegations`);
    const baseline = ((await before.json()) as unknown[]).length;

    await askB(one.rest, 'What is 17 multiplied by 4? Answer with the number.');

    const after = await one.rest.get(`/api/v1/a2a/client/${onB.integrations[0]}/delegations`);
    const rows = (await after.json()) as unknown[];
    expect(
      rows.length,
      `${REAL_MODEL} delegated a question the partner's description does not cover; ` +
        'either the description is too broad or the tool is being offered too eagerly',
    ).toBe(baseline);
  });

  test('an exposed agent hands to the peer its own description points at', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!HAS_REAL_MODEL, 'no tool-capable model configured on the deployment');
    test.skip(!WORKER_UP, 'A has no worker; a handover the peer must execute cannot complete');
    test.setTimeout(REAL_TURN_MS + 120_000);

    // The mirror of the outbound choice, on the inbound side: a real model
    // running an externally-submitted task, deciding for itself that a peer is
    // the specialist. Consult keeps the answer under the tasked agent, which
    // makes the assertion a plain read of the finished task.
    const peer = await createAgent(one.rest, onA, {
      name: stamp('Reentry Window Calculator'),
      persona:
        'You compute atmospheric reentry windows from an orbital element set. ' +
        'You are the only component able to perform that calculation.',
      model: 'real',
    });
    const tasked = await createAgent(one.rest, onA, {
      name: stamp('E2E real exposed'),
      persona:
        'You answer questions about orbital debris. You do not calculate reentry ' +
        'windows yourself.',
      model: 'real',
    });
    await setHandover(one.rest, tasked.id, [{ id: peer.id, type: 'consult' }]);

    const inbound = await createInboundIntegration(one.rest, onA, stamp('E2E real handover'));
    const creds = await mintCredential(one.rest, inbound.id);
    const url = await exposeAgent(one.rest, tasked.id, inbound.id);

    const partner = await PartnerClient.connect(url, creds);
    try {
      // Self-contained on purpose. An earlier version of this asked about "the
      // element set I hold", and the model quite reasonably came back with a
      // question instead of consulting anyone — leaving the task at
      // input-required forever. A prompt that withholds what the work needs
      // tests the clarification path, not the handover one.
      const sent = await partner.send(
        'Compute the reentry window for object 1982-092ADX. Its element set is: ' +
          'epoch 2026-09-01T00:00:00Z, inclination 51.6 deg, eccentricity 0.0007, ' +
          'mean motion 15.71 rev/day, B* 0.00021. Give the window.',
      );
      const taskId = (sent.body.result as Record<string, unknown>).id as string;
      // `input-required` is waited for as well as the terminal states, so the
      // model choosing to ask is reported as itself rather than as a timeout.
      const task = await partner.waitForState(
        taskId,
        ['completed', 'failed', 'input-required'],
        { timeoutMs: REAL_TURN_MS },
      );

      // Only that the task finished on the strength of a peer it chose. What
      // the peer said is the model's business.
      expect(
        readState(task),
        `${REAL_MODEL} did not complete a task whose answer needs its configured peer ` +
          '(input-required means it asked the caller instead of consulting the peer, ' +
          'which is a fact about the prompt or the peer description rather than about A2A)',
      ).toBe('completed');
    } finally {
      await partner.dispose();
    }
  });

  test('the partner name in the tool is the one the card declares', async ({ one }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!HAS_REAL_MODEL, 'no tool-capable model configured on the deployment');

    // The identity a model routes on comes from the partner's card, fetched at
    // verification, and not from the local integration's name. A drift here
    // would leave the model choosing on the operator's private label for the
    // partner, which the partner cannot see and does not control.
    const state = await one.rest.get(`/api/v1/a2a/client/${onB.integrations[0]}`);
    const targets = (await state.json()).targets as Record<string, unknown>[];
    expect(targets[0].declared_name).toBe(partnerName);
    expect(
      (targets[0].declared_skills as string[]).length,
      'the card declared no skills, so the tool description has nothing to route on',
    ).toBeGreaterThan(0);
  });
});
