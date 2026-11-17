/**
 * A task that does not end when its run does.
 *
 * Two things on the inbound surface outlive a single run, and they are the
 * same shape underneath, which is why they share a file. An agent that calls
 * `ask_the_caller` ends its run without an answer, and the task stays open
 * waiting for the partner to say something; a `contextId` groups the tasks of
 * one conversation, so a partner may come back after a task has finished and
 * be answered by whoever the last transfer left responsible. Both are read
 * off the same projection — `resolve_state` — and both are continued by the
 * same method, `message/send`, distinguished only by whether the message
 * names a task or a context. Pinning them apart would mean building the same
 * fixture twice and would leave the interesting overlap untested: a `taskId`
 * is evaluated first and wins outright, and a context whose task is *asking*
 * reads as free rather than busy.
 *
 * The suite plays the partner itself here rather than driving a family-2
 * agent. That is not a shortcut, it is the only way these branches are
 * reachable: this deployment declares its callbacks reachable, so its own
 * client strips `blocking` from the submission and bounds the whole attempt by
 * `A2A_CLIENT_GRACE_SECONDS` — `CLOCK.graceSeconds`, a handful of seconds —
 * while the asking run takes a second or two: a race, not a test. Playing the
 * partner also gives a `contextId`, which the outbound client never sends and
 * never reads.
 *
 * Every decision an agent makes here is made by the scripted model. The
 * marker rides in the prompt and — this is the load-bearing detail for the
 * multi-round tests — a continuation run is handed the original prompt back
 * as history, so the same script keeps being served across rounds provided
 * every answer carries the marker too.
 */

import type { APIRequestContext } from '@playwright/test';
import { test, expect } from './fixtures';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  exposeAgent,
  exposureOf,
  mintCredential,
  setHandover,
  stamp,
  tracker,
  type Created,
} from './seed';
import { PartnerClient, readState, type JsonRpcEnvelope } from './wire';
import { marker, type ScriptedModel } from '../scripted-llm/client';

/**
 * What the agent writes after asking. A run that asks completes with its
 * `summary` unset, so this prose is discarded rather than sent — and a spec
 * can prove that by looking for the string anywhere in the response.
 */
const POST_ASK_PROSE = 'E2E-POST-ASK-PROSE-MUST-NOT-LEAK';

/** What the agent answers once the caller has replied. */
const CONTINUED_ANSWER = 'E2E-CONTINUED-ANSWER';

/** The A2A error codes this file asserts on, spelled once. */
const TASK_NOT_FOUND = -32001;
const UNSUPPORTED_OPERATION = -32004;

/**
 * How long a continuation may take before "returned at once" stops meaning it.
 *
 * Deliberately not a `CLOCK` value: what this has to beat is
 * `a2a_blocking_send_timeout_seconds`, a backend default of 120 s that the
 * launcher does not accelerate, so there is no configured clock to read it
 * from. Loose enough for a loaded box, an order of magnitude below the cap it
 * is proving was never reached.
 */
const CONTINUATION_RETURNS_BY_MS = 15_000;

// ---------------------------------------------------------------------------
// Local helpers. Deliberately in this file rather than in seed.ts or wire.ts:
// every one of them is about a task that is *asking*, which is this file's
// subject and nothing else's.
// ---------------------------------------------------------------------------

interface RpcError {
  code: number;
  message: string;
  data?: unknown;
}

/** The result of a call that was supposed to succeed, with the error in the failure. */
function resultOf(body: JsonRpcEnvelope): Record<string, unknown> {
  expect(body.error, `expected a result, got ${JSON.stringify(body.error)}`).toBeUndefined();
  return body.result as Record<string, unknown>;
}

/** The error envelope of a call that was supposed to be refused. */
function errorOf(body: JsonRpcEnvelope): RpcError {
  expect(
    body.error,
    `expected a refusal, got ${JSON.stringify(body.result)}`,
  ).toBeTruthy();
  return body.error as RpcError;
}

/** The question a task is asking: the text of its status message. */
function questionOf(task: Record<string, unknown>): string {
  const status = task.status as Record<string, unknown> | undefined;
  const message = status?.message as Record<string, unknown> | undefined;
  const parts = (message?.parts ?? message?.content) as Record<string, unknown>[] | undefined;
  return String(parts?.[0]?.text ?? '');
}

/** The AGENT turns served for one marker, each with the tools it was offered. */
async function offeredPerTurn(model: ScriptedModel, mark: string): Promise<string[][]> {
  return (await model.agentTurns(mark)).map((c) => c.tools_offered);
}

/**
 * The runs one partner's traffic produced for one exposed agent.
 *
 * Scoped by `a2a_integration_id` rather than by agent alone, because the same
 * agent is bound to two inbound registrations here and each would otherwise
 * report the other partner's submissions as its own.
 */
async function runIds(
  rest: APIRequestContext,
  integrationId: string,
  agentId: string,
  status?: string,
): Promise<string[]> {
  const query = new URLSearchParams({
    a2a_integration_id: integrationId,
    agent_id: agentId,
    limit: '500',
  });
  if (status) query.set('status', status);
  const resp = await rest.get(`/api/v1/assignments/runs/all?${query.toString()}`);
  if (!resp.ok()) throw new Error(`run listing failed: ${resp.status()} ${await resp.text()}`);
  return ((await resp.json()) as { id: string }[]).map((run) => run.id);
}

interface Parked {
  /** The marker every message of this exchange must carry to keep the script served. */
  marker: string;
  question: string;
  taskId: string;
  contextId: string;
  /** The task as it rendered at input-required. */
  task: Record<string, unknown>;
}

/**
 * Get a task to input-required and hand back everything about it.
 *
 * Three steps, not two: the run asks, then writes prose that must not reach
 * the caller, and the third is what the continuation run answers with. A
 * spec that never continues simply leaves the last step unserved.
 *
 * The submission is non-blocking (no `configuration`), so the task id comes
 * back at once and the wait below is an ordinary poll rather than a held
 * connection — see the blocking test at the end of the file for why that
 * distinction matters here more than anywhere else.
 */
async function park(
  partner: PartnerClient,
  model: ScriptedModel,
  opts: { label?: string; question?: string } = {},
): Promise<Parked> {
  const mark = marker(opts.label ?? 'ask');
  const question = opts.question ?? `Which tenant should this run against? ${mark}`;
  await model.arm(mark, [
    { tool: 'ask_the_caller', arguments: { question } },
    { text: POST_ASK_PROSE },
    { text: CONTINUED_ANSWER },
  ]);
  const submitted = resultOf((await partner.send(`${mark} enrich this indicator`)).body);
  const taskId = String(submitted.id);
  const task = await partner.waitForState(taskId, ['input-required']);
  return { marker: mark, question, taskId, contextId: String(task.contextId), task };
}

// ---------------------------------------------------------------------------

test.describe.serial('Asking the caller, and continuing a conversation', () => {
  const created: Created = tracker();
  let available = false;

  let inboundId = '';
  let strangerInboundId = '';
  let askerId = '';
  let peerId = '';
  let conversationId = '';

  /** The partner every test speaks as. */
  let partner: PartnerClient;
  /** A second partner on the same card, holding a different registration's credential. */
  let stranger: PartnerClient;

  /** The standing question the answering tests share. */
  let standing: Parked;
  /** The context a transfer moved to the peer, reused by the unbind refusal. */
  let transferContext = '';
  /** The two not-found envelopes, kept so the foreign case can be compared with the unissued one. */
  let unissuedTaskRefusal: RpcError;
  let unissuedContextRefusal: RpcError;

  test.beforeAll(async ({ one, model }) => {
    available = await a2aAvailable(one.rest);
    if (!available) return;
    await model.reset();

    const rest = one.rest;
    inboundId = (await createInboundIntegration(rest, created, stamp('E2E ask inbound'))).id;
    strangerInboundId = (
      await createInboundIntegration(rest, created, stamp('E2E ask inbound stranger'))
    ).id;

    // The peer exists first: the asker names it in its handover list.
    peerId = (
      await createAgent(rest, created, {
        name: stamp('E2E ask peer'),
        persona: 'A peer the exposed agent may transfer a conversation to.',
      })
    ).id;
    askerId = (
      await createAgent(rest, created, {
        name: stamp('E2E ask agent'),
        persona: 'An exposed agent that asks its caller for the details it was not given.',
      })
    ).id;
    await setHandover(rest, askerId, [{ id: peerId, type: 'transfer' }]);

    // Binding IS the exposure. The peer is bound to the first registration
    // too, because a follow-up is refused unless the partner's own binding
    // still covers the agent a transfer made responsible.
    await exposeAgent(rest, peerId, inboundId);
    await bind(rest, askerId, { oauth_a2a: [inboundId, strangerInboundId] });
    const exposure = await exposureOf(rest, askerId);
    expect(exposure.card_url, 'binding should have exposed the asking agent').toBeTruthy();
    const cardUrl = exposure.card_url as string;

    // One card, two credentials. Task and context ownership is scoped by the
    // client the bearer names, so this is what makes "another partner's id"
    // testable without a second exposure.
    partner = await PartnerClient.connect(cardUrl,
      await mintCredential(rest, inboundId),
    );
    stranger = await PartnerClient.connect(cardUrl,
      await mintCredential(rest, strangerInboundId),
    );
  });

  test.afterAll(async ({ one }) => {
    if (conversationId) {
      await one.rest.delete(`/api/v1/chat/conversations/${conversationId}`).catch(() => undefined);
    }
    await partner?.dispose();
    await stranger?.dispose();
    await cleanup(one.rest, created);
  });

  test.beforeEach(() => {
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  // -- the question ---------------------------------------------------------

  test('an external task run is offered ask_the_caller', async ({ model }) => {
    // The binding confers nothing. The tool is added per run, by the external
    // task executor, and only because that executor passed somewhere to put
    // the question — which is why this reads the offered list rather than the
    // agent's configuration.
    standing = await park(partner, model, { label: 'standing' });
    const turns = await offeredPerTurn(model, standing.marker);
    expect(turns.length, 'the run should have taken a tool turn and a reply turn').toBe(2);
    expect(turns[0]).toContain('ask_the_caller');
  });

  test('a run that asked leaves the task at input-required', async () => {
    // The run itself reached a terminal status — the question is what makes
    // the task non-terminal, and the state is projected from it rather than
    // stored, so there is one source of truth.
    expect(readState(standing.task)).toBe('input-required');
  });

  test('the question is the task status message', async () => {
    const status = standing.task.status as Record<string, unknown>;
    const message = status.message as Record<string, unknown>;
    expect(questionOf(standing.task)).toBe(standing.question);
    expect(message.kind).toBe('message');
    expect(message.role).toBe('agent');
  });

  test('a task waiting on its caller carries no result', async () => {
    expect(standing.task.artifacts).toEqual([]);
    // No `history` either: it is emitted only alongside artifacts, so a
    // partner cannot read a half-answer as the answer.
    expect(standing.task).not.toHaveProperty('history');
    // And the prose the agent wrote after asking is discarded, not sent —
    // the asking branch never sets the run's summary.
    const refetched = await partner.getTask(standing.taskId);
    expect(JSON.stringify(refetched.body)).not.toContain(POST_ASK_PROSE);
  });

  test('the recorded question is capped at a thousand characters', async ({ model }) => {
    const long = 'tenant-'.repeat(200);
    expect(long.length).toBeGreaterThan(1000);
    const parked = await park(partner, model, { label: 'long', question: long });
    expect(questionOf(parked.task)).toBe(long.slice(0, 1000));
  });

  test('an ordinary chat turn on the same agent is offered no ask_the_caller', async ({
    one,
    model,
  }) => {
    // A chat turn has a person in it, who is not the caller A2A means, so the
    // executor passes nowhere to put a question and the tool is never added.
    const mark = marker('chat');
    await model.arm(mark, [{ tool: 'ask_the_caller', arguments: { question: 'Which tenant?' } }]);

    const conversation = await one.rest.post('/api/v1/chat/conversations', {
      data: { agent_id: askerId, title: stamp('E2E ask in chat') },
    });
    expect(conversation.ok(), `create conversation: ${conversation.status()}`).toBe(true);
    conversationId = (await conversation.json()).id;

    const sent = await one.rest.post(
      `/api/v1/chat/conversations/${conversationId}/messages`,
      { data: { content: `${mark} ask the caller which tenant this is for` } },
    );
    expect(sent.ok(), `send chat message: ${sent.status()} ${await sent.text()}`).toBe(true);

    const turns = await offeredPerTurn(model, mark);
    expect(turns.length, 'the chat turn should have reached the model once').toBe(1);
    expect(turns[0]).not.toContain('ask_the_caller');
  });

  // -- answering ------------------------------------------------------------

  test('an answer naming the task continues that task rather than opening a second', async ({
    one,
  }) => {
    const before = await runIds(one.rest, inboundId, askerId);
    const continued = resultOf(
      (await partner.send(`${standing.marker} the EU tenant`, { taskId: standing.taskId })).body,
    );

    expect(continued.id, 'the continuation should answer under the original task id').toBe(
      standing.taskId,
    );
    // The continuation run is created before the response is written, so this
    // needs no poll: one more run, and only one.
    expect((await runIds(one.rest, inboundId, askerId)).length).toBe(before.length + 1);
  });

  test('the exchange keeps one context id from question to answer', async () => {
    const answered = await partner.getTask(standing.taskId);
    expect(resultOf(answered.body).contextId).toBe(standing.contextId);
  });

  test('the continued task completes with the answer the caller unblocked', async () => {
    const done = await partner.waitForState(standing.taskId, ['completed']);
    const artifacts = done.artifacts as Record<string, unknown>[];
    const parts = artifacts[0]?.parts as Record<string, unknown>[];
    expect(String(parts?.[0]?.text)).toBe(CONTINUED_ANSWER);
  });

  test('answering the same question twice is refused as not asking', async ({ one }) => {
    const before = await runIds(one.rest, inboundId, askerId);
    const again = errorOf(
      (await partner.send(`${standing.marker} the EU tenant`, { taskId: standing.taskId })).body,
    );

    // The taskId branch takes no dedup claim, so a replayed continuation is
    // stopped by nothing except the question having been cleared.
    expect(again.code).toBe(UNSUPPORTED_OPERATION);
    expect(again.message).toBe('This task is not waiting on an answer.');
    expect(again.data).toEqual({ '@type': 'UnsupportedOperationError' });
    expect((await runIds(one.rest, inboundId, askerId)).length).toBe(before.length);
  });

  test('answering a task this client never received is the uniform not-found', async () => {
    unissuedTaskRefusal = errorOf(
      (await partner.send('an answer to nothing', { taskId: crypto.randomUUID() })).body,
    );
    expect(unissuedTaskRefusal.code).toBe(TASK_NOT_FOUND);
    expect(unissuedTaskRefusal.message).toBe('Task not found');
    expect(unissuedTaskRefusal.data).toEqual({ '@type': 'TaskNotFoundError' });
  });

  test('answering another partner\'s task says exactly the same thing', async ({ model }) => {
    // A task genuinely issued on this deployment, to a second registration on
    // the same card. Ownership must not be probeable, so the answer has to be
    // indistinguishable from one for an id nobody was ever given.
    const mark = marker('foreign');
    await model.arm(mark, [{ text: 'not this partner\'s business' }]);
    const theirs = resultOf((await stranger.send(`${mark} work for the other partner`)).body);

    const refusal = errorOf(
      (await partner.send('an answer to their task', { taskId: String(theirs.id) })).body,
    );
    expect(refusal).toEqual(unissuedTaskRefusal);
  });

  test('a continuation honours blocking, like the submission it answers', async ({
    model,
  }) => {
    const parked = await park(partner, model, { label: 'nonblocking' });
    const started = Date.now();
    const continued = resultOf(
      (
        await partner.send(`${parked.marker} the EU tenant`, {
          taskId: parked.taskId,
          blocking: true,
        })
      ).body,
    );
    const elapsed = Date.now() - started;

    // An answer is a send like any other. Returning before the wait was
    // reached made rounds two and after ignore `configuration.blocking`, the
    // inverse of the first submission waiting out the full cap (120 s) on a
    // state — `input-required` — that nothing but the caller could move.
    expect(readState(continued)).toBe('completed');
    expect(elapsed).toBeLessThan(CONTINUATION_RETURNS_BY_MS);
  });

  test('a continuation naming a foreign context is never checked against it', async ({ model }) => {
    const parked = await park(partner, model, { label: 'outranks' });
    const continued = resultOf(
      (
        await partner.send(`${parked.marker} the EU tenant`, {
          taskId: parked.taskId,
          contextId: crypto.randomUUID(),
        })
      ).body,
    );

    // A taskId is evaluated first and returns, so none of the three context
    // refusals apply to a message that also names one — including a context
    // this deployment never issued.
    expect(continued.id).toBe(parked.taskId);
    expect(continued.contextId).toBe(parked.contextId);
  });

  test('a task waiting on its caller can be cancelled', async ({ model }) => {
    const parked = await park(partner, model, { label: 'cancel' });
    const cancelled = resultOf((await partner.cancelTask(parked.taskId)).body);

    // `input-required` is not terminal, so the terminal guard lets the cancel
    // through — and the run that asked is already 'completed', so there is
    // nothing to stop. Withdrawing the question is the whole of the cancel,
    // which is what the FR-025 teardown was written for: an unanswered
    // question has to be retirable by something.
    expect(readState(cancelled)).toBe('canceled');
    expect(readState(resultOf((await partner.getTask(parked.taskId)).body))).toBe(
      'canceled',
    );
  });

  test('one task may ask its caller three times and no more', async ({ model }) => {
    const mark = marker('bound');
    // Three complete rounds, then a fourth run that tries to ask again. Every
    // answer carries the marker because the continuation's history is what
    // keeps this script matched — the answer text alone would not.
    await model.arm(mark, [
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant? (1)' } },
      { text: POST_ASK_PROSE },
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant? (2)' } },
      { text: POST_ASK_PROSE },
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant? (3)' } },
      { text: POST_ASK_PROSE },
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant? (4)' } },
    ]);

    const submitted = resultOf((await partner.send(`${mark} enrich this indicator`)).body);
    const taskId = String(submitted.id);
    for (let round = 1; round <= 3; round += 1) {
      const asking = await partner.waitForState(taskId, ['input-required']);
      expect(questionOf(asking), `round ${round} should have asked`).toContain(`(${round})`);
      resultOf((await partner.send(`${mark} the EU tenant`, { taskId })).body);
    }
    await partner.waitForState(taskId, ['completed']);

    // Past the bound the executor passes nowhere to put a question, so the
    // tool is simply not registered and the agent must answer with what it
    // has. The scripted model asked for it anyway, which is what makes its
    // absence assertable rather than inferred.
    const turns = await offeredPerTurn(model, mark);
    expect(turns.length).toBe(7);
    expect(turns[0]).toContain('ask_the_caller');
    expect(turns[6]).not.toContain('ask_the_caller');
  });

  test('a transfer target is offered no way to ask the caller', async ({ model }) => {
    const mark = marker('peerask');
    await model.arm(mark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: peerId } },
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant?' } },
    ]);
    const submitted = resultOf((await partner.send(`${mark} route this to the specialist`)).body);

    // The peer answers with what it has, because `ask_caller_out` is passed
    // from exactly one call site — the addressed agent's own loop — and the
    // recursive call that runs a transfer target passes none. So the task
    // completes rather than parking, one hop down from the feature.
    const done = await partner.waitForState(String(submitted.id), ['completed']);
    expect(readState(done)).toBe('completed');
    const turns = await offeredPerTurn(model, mark);
    expect(turns.length).toBe(2);
    expect(turns[0]).toContain('ask_the_caller');
    expect(turns[1]).not.toContain('ask_the_caller');
  });

  // -- continuing a conversation -------------------------------------------

  test('a context this deployment never issued is the uniform not-found', async () => {
    unissuedContextRefusal = errorOf(
      (await partner.send('a follow-up to nothing', { contextId: crypto.randomUUID() })).body,
    );
    expect(unissuedContextRefusal.code).toBe(TASK_NOT_FOUND);
    expect(unissuedContextRefusal.message).toBe('Task not found');
    expect(unissuedContextRefusal.data).toEqual({ '@type': 'TaskNotFoundError' });
  });

  test('another partner\'s context answers exactly the same', async ({ model }) => {
    const mark = marker('foreignctx');
    await model.arm(mark, [{ text: 'not this partner\'s business' }]);
    const theirs = resultOf((await stranger.send(`${mark} work for the other partner`)).body);

    const refusal = errorOf(
      (await partner.send('a follow-up on their conversation', {
        contextId: String(theirs.contextId),
      })).body,
    );
    expect(refusal).toEqual(unissuedContextRefusal);
  });

  test('a follow-up while the previous task is still working is queued behind it', async ({
    model,
  }) => {
    // Since #11879 a busy context queues the follow-up instead of refusing it:
    // the second message is accepted as its own task in the same context, and
    // both run to completion in submission order. Only a full queue refuses.
    const mark = marker('busy');
    await model.arm(mark, [{ text: 'work in progress' }]);
    const first = resultOf((await partner.send(`${mark} a long piece of work`)).body);
    const second = resultOf(
      (
        await partner.send('and another thing', {
          contextId: String(first.contextId),
        })
      ).body,
    );
    expect(second.contextId).toBe(first.contextId);
    expect(second.id).not.toBe(first.id);
    await partner.waitForState(String(first.id), ['completed']);
    await partner.waitForState(String(second.id), ['completed']);
  });

  test('a follow-up while the previous task waits on its caller forks a second task', async ({
    model,
  }) => {
    const parked = await park(partner, model, { label: 'forked' });
    const forked = resultOf(
      (await partner.send('a separate question entirely', { contextId: parked.contextId })).body,
    );

    // "Busy" is read off the run, and the run that asked is 'completed' — so a
    // context holding a task at input-required reads as free, and only a
    // taskId continues the exchange. The parked task is untouched by the fork.
    expect(forked.id).not.toBe(parked.taskId);
    expect(forked.contextId).toBe(parked.contextId);
    expect(readState(resultOf((await partner.getTask(parked.taskId)).body))).toBe(
      'input-required',
    );
  });

  test('a follow-up is answered by the agent a transfer made responsible', async ({
    one,
    model,
  }) => {
    const mark = marker('transfer');
    await model.arm(mark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: peerId } },
      { text: 'the specialist answered' },
    ]);
    const submitted = resultOf((await partner.send(`${mark} route this to the specialist`)).body);
    const done = await partner.waitForState(String(submitted.id), ['completed']);
    transferContext = String(done.contextId);

    // Taken here rather than at the start: the transferred hop runs inside the
    // addressed agent's own run, so what this counts is the follow-up's run
    // and nothing else.
    const before = await runIds(one.rest, inboundId, peerId);
    const followUp = resultOf(
      (await partner.send('and what about the second one?', { contextId: transferContext })).body,
    );
    await partner.waitForState(String(followUp.id), ['completed']);

    expect(followUp.contextId).toBe(transferContext);
    expect((await runIds(one.rest, inboundId, peerId)).length).toBe(before.length + 1);
  });

  test('a follow-up is refused once the responsible agent is unbound from this partner', async ({
    one,
  }) => {
    // The unbind is on the inbound registration, which is where both the
    // exposure and the binding live — a2a-review-scenarios.md §9 says to
    // unbind the outbound one, which carries neither.
    await bind(one.rest, peerId, { oauth_a2a: [] });

    const refusal = errorOf(
      (await partner.send('one more thing', { contextId: transferContext })).body,
    );
    // Responsibility recorded once is not consent forever: the follow-up is
    // refused rather than answered by the agent that used to be addressed.
    expect(refusal.code).toBe(UNSUPPORTED_OPERATION);
    expect(refusal.message).toBe(
      'The agent responsible for this conversation is no longer available to this client',
    );
  });

  // -- blocking -------------------------------------------------------------

  test('a blocking submission is released by a question', async ({ model }) => {
    const mark = marker('blocking');
    await model.arm(mark, [
      { tool: 'ask_the_caller', arguments: { question: 'Which tenant?' } },
      { text: POST_ASK_PROSE },
    ]);

    const started = Date.now();
    const answered = resultOf(
      (await partner.send(`${mark} enrich this indicator`, { blocking: true })).body,
    );
    const elapsed = Date.now() - started;

    // A2A blocks "until an interrupted or terminal state", and a task asking
    // its caller is interrupted: nothing will move it until this partner
    // answers, so holding the connection to the 120 s cap would only delay the
    // question it is being held for.
    expect(readState(answered)).toBe('input-required');
    expect(questionOf(answered)).toContain('Which tenant?');
    expect(elapsed).toBeLessThan(CONTINUATION_RETURNS_BY_MS);
  });
});
