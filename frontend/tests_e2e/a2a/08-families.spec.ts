/**
 * Two families on one deployment, and the crossing between them.
 *
 * An agent's family is decided by which A2A registration it binds, and the two
 * are genuinely different roles rather than two labels:
 *
 *   family 1  bound to the INBOUND registration. The binding IS the exposure:
 *             the agent gains a card URL and a partner holding the credential
 *             may task it. It has no delegation tool and cannot reach outward.
 *   family 2  bound to the OUTBOUND registration. The agent gains one
 *             `delegate_to_*` tool per verified target and can reach the
 *             partner. It is not exposed and no partner can address it.
 *
 * WHAT THIS FILE OWNS
 * -------------------
 * Handover WITHIN family 1 is `05-handover.spec.ts`; the timing of one plain
 * crossing is `06-wait-cancel.spec.ts`; a cancel reaching a transfer's child
 * is `12-cancel-cascade.spec.ts`. Here: handover within family 2, and the
 * combinations that exist only because there are two families — a hand-off
 * before the crossing, one after it, one on each side at once, and a cancel
 * arriving at either side of the hand-back with a hand-off in the picture.
 *
 * WHERE A HAND-OFF LANDS
 * ----------------------
 * A `mandatory_transfer` fires after the entry agent's loop, so which side of
 * the crossing it lands on follows from which agent is enrolled:
 *
 *   before  the entry is NOT enrolled and its stage is — the entry answers,
 *           the stage takes the turn and calls the partner.
 *   after   the entry IS enrolled and its stage is not — the entry calls the
 *           partner, and the stage then writes the final answer.
 *
 * Everything is driven through the chat API and read back from the delegation
 * rows, the partner's own tasks and the conversation. The REST chat endpoint
 * records no tool trace (`tool_call_trace` is written only by the web chat's
 * streamed send), so what a tool returned to the model is not readable here.
 */

import { request, type APIRequestContext } from '@playwright/test';
import { expect, test } from './fixtures';
import { ADMIN_EMAIL, ADMIN_PASSWORD, CLOCK, SELF, WORKER_UP } from './instance';
import { marker, ScriptedModel } from '../scripted-llm/client';
import { PartnerClient, eventually, readState, sleep } from './wire';
import {
  a2aAvailable,
  cleanup,
  createAgent,
  federation,
  setHandover,
  stamp,
  tracker,
  type Federation,
} from './seed';

/** Long enough that the partner is still working when the grace expires. */
const LONG_WORK = '20s';
/** Long enough that a cancel arriving several seconds in still interrupts it. */
const WORK_TO_INTERRUPT = '25s';

interface DelegationRow {
  id: string;
  agent_name: string | null;
  outcome: string | null;
  partner_reason: string | null;
  mechanism: string | null;
  continuation_count: number;
  submitted_at: string;
}

interface ChatMessage {
  role: string;
  content: string | null;
}

const created = tracker();

let available = false;
let rest: APIRequestContext;
let script: ScriptedModel;
let world: Federation;
/** The suite as a partner, for reading a task's state over the protocol. */
let partner: PartnerClient;

// Family 1 — exposed, tasked by the partner.
let plain = { id: '', name: '' };
let pipelineEntry = { id: '', name: '' };
let pipelineStage = { id: '', name: '' };
let transferEntry = { id: '', name: '' };
let transferPeer = { id: '', name: '' };
let consultEntry = { id: '', name: '' };
let consultPeer = { id: '', name: '' };
let slowEntry = { id: '', name: '' };

// Family 2 — enrolled, reaching the partner.
let caller = { id: '', name: '' };
let frontDesk = { id: '', name: '' };
let backOffice = { id: '', name: '' };
let router = { id: '', name: '' };
let scribe = { id: '', name: '' };
let consulter = { id: '', name: '' };
let advisor = { id: '', name: '' };
let dispatcher = { id: '', name: '' };
let courier = { id: '', name: '' };
let backgroundCaller = { id: '', name: '' };
let backgroundWorker = { id: '', name: '' };

// ---------------------------------------------------------------------------
// Local helpers. Each is a reading of state that only this file's subject
// needs; the harness deliberately has no opinion about chat.
// ---------------------------------------------------------------------------

async function signIn(): Promise<APIRequestContext> {
  if (!ADMIN_EMAIL || !ADMIN_PASSWORD) {
    throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set.');
  }
  const anonymous = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await anonymous.post('/api/v1/auth/login', {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    if (!login.ok()) {
      throw new Error(`Admin login failed: ${login.status()} ${await login.text()}`);
    }
    const issued = ((await login.json()) as { access_token: string }).access_token;
    return await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${issued}` },
    });
  } finally {
    await anonymous.dispose();
  }
}

async function startConversation(agentId: string, title: string): Promise<string> {
  const resp = await rest.post('/api/v1/chat/conversations', {
    data: { agent_id: agentId, title: stamp(title) },
  });
  if (!resp.ok()) {
    throw new Error(`conversation create failed: ${resp.status()} ${await resp.text()}`);
  }
  return ((await resp.json()) as { id: string }).id;
}

/** One chat turn, returning how long the caller was held. */
async function say(conversationId: string, content: string): Promise<number> {
  const started = Date.now();
  const resp = await rest.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
    // Generous: an attempt with no callback polls to the ceiling before it
    // hands back, and a chain of hand-offs is several model round trips.
    data: { content },
    timeout: 150_000,
  });
  if (!resp.ok()) {
    throw new Error(`chat turn failed: ${resp.status()} ${await resp.text()}`);
  }
  return Date.now() - started;
}

async function conversationNow(
  conversationId: string,
): Promise<{ processing: boolean; assistant: string[] }> {
  const resp = await rest.get(`/api/v1/chat/conversations/${conversationId}`, {
    params: { limit: 200 },
  });
  if (!resp.ok()) {
    throw new Error(`conversation read failed: ${resp.status()} ${await resp.text()}`);
  }
  const body = (await resp.json()) as { is_processing: boolean; messages: ChatMessage[] };
  return {
    processing: body.is_processing,
    assistant: body.messages
      .filter((message) => message.role === 'assistant')
      .map((message) => message.content ?? ''),
  };
}

/** Every assistant reply in the conversation, joined — what the user ended up reading. */
async function saidBack(conversationId: string): Promise<string> {
  return (await conversationNow(conversationId)).assistant.join('\n');
}

/**
 * Abandon a conversation, the only way a user can: Stop.
 *
 * The cascade that cancels outstanding partner tasks hangs off the chat's
 * Stop (`POST /web/chat/messages/cancel`). Its answer means the Stop was
 * taken; the cascade runs in the background afterwards.
 */
async function abandon(conversationId: string): Promise<void> {
  const resp = await rest.post('/api/v1/web/chat/messages/cancel', {
    data: { conversation_id: conversationId },
  });
  expect(resp.ok(), `cancel: ${resp.status()} ${await resp.text()}`).toBe(true);
  expect(((await resp.json()) as { status: string }).status).toBe('cancelled');
}

/** What the outbound half recorded, newest first. */
async function rows(): Promise<DelegationRow[]> {
  const resp = await rest.get(`/api/v1/a2a/client/${world.outbound.id}/delegations`);
  if (!resp.ok()) {
    throw new Error(`delegations read failed: ${resp.status()} ${await resp.text()}`);
  }
  return (await resp.json()) as DelegationRow[];
}

async function rowIds(): Promise<Set<string>> {
  return new Set((await rows()).map((row) => row.id));
}

/** The rows this exchange added, oldest first — a crossing writes one, sometimes two. */
async function rowsSince(known: Set<string>): Promise<DelegationRow[]> {
  const fresh = (await rows()).filter((row) => !known.has(row.id));
  return fresh.reverse();
}

/**
 * The one row this exchange added, once it has an outcome.
 *
 * Polled rather than read once: a turn returns as soon as the delegation hands
 * back, and the row is concluded by whatever reports the outcome — the
 * partner's callback, or the polling floor.
 */
async function concludedRowSince(known: Set<string>, what: string): Promise<DelegationRow> {
  return eventually(
    async () => {
      const fresh = await rowsSince(known);
      expect(fresh.length, `${what}: no delegation row yet`).toBeGreaterThan(0);
      const concluded = fresh.find((row) => row.outcome !== null);
      expect(concluded, `${what}: the delegation is still outstanding`).toBeTruthy();
      return concluded as DelegationRow;
    },
    { what, timeoutMs: 90_000, intervalMs: 1_000 },
  );
}

/** Every task id the partner half recorded for this credential, newest first. */
async function partnerTaskIds(): Promise<string[]> {
  const resp = await rest.get('/api/v1/audit-logs', {
    params: { entity_type: 'a2a_task', action: 'trigger', limit: 200 },
  });
  if (!resp.ok()) throw new Error(`audit read failed: ${resp.status()} ${await resp.text()}`);
  const page = (await resp.json()) as {
    items: { entity_id: string | null; details: Record<string, unknown> | null }[];
  };
  return page.items
    .filter((item) => String((item.details ?? {}).client_id) === world.credentials.clientId)
    .map((item) => item.entity_id)
    .filter((id): id is string => Boolean(id));
}

/** The one task id that appeared since `known`. */
async function taskSubmittedSince(known: readonly string[], what: string): Promise<string> {
  return eventually(
    async () => {
      const fresh = (await partnerTaskIds()).filter((id) => !known.includes(id));
      expect(fresh, `${what}: the partner recorded no new task`).toHaveLength(1);
      return fresh[0];
    },
    { timeoutMs: 30_000, what },
  );
}

/**
 * What the partner says about one of its tasks, read the way a partner reads
 * it.
 *
 * Over the protocol rather than off a run row: `tasks/get` is scoped to the
 * registration that created the task, not to the card token in the path, so
 * one client serves every exposed agent — and the state it returns is the one
 * the caller's own client acted on, which is the thing worth asserting.
 */
async function partnerTaskState(taskId: string): Promise<string> {
  const { body } = await partner.getTask(taskId);
  return (
    readState(body.result) ?? `no state — error ${body.error?.code}: ${body.error?.message}`
  );
}

/**
 * The agent the partner says produced the answer, or null when it says nobody.
 *
 * This is the only partner-side fact a caller-driven test can read, and it is
 * what separates "the partner handed the work on" from "the partner answered
 * itself". A transfer names the peer; a consult deliberately names no one,
 * because a consult advises rather than moving responsibility. Without it a
 * crossing into a partner that never handed anything on passes exactly like
 * one that did.
 */
async function respondingAgentOf(taskId: string): Promise<{ id: string; name: string } | null> {
  const task = await partner.waitForState(taskId, ['completed', 'failed'], { timeoutMs: 60_000 });
  const artifacts = (task.artifacts as Record<string, unknown>[]) ?? [];
  const metadata = artifacts[0]?.metadata as Record<string, unknown> | undefined;
  return (metadata?.respondingAgent as { id: string; name: string } | undefined) ?? null;
}

/** The tools the scripted model was actually asked to call for one marker, in order. */
async function toolsCalledFor(mark: string): Promise<string[]> {
  return (await script.agentTurns(mark))
    .map((turn) => (turn.step && 'tool' in turn.step ? turn.step.tool : null))
    .filter((name): name is string => Boolean(name));
}

// ---------------------------------------------------------------------------

test.describe.serial('Two families, and the crossing between them', () => {
  test.beforeAll(async () => {
    rest = await signIn();

    available = await a2aAvailable(rest);
    if (!available) return;

    script = await ScriptedModel.connect();
    // A previous file's scripts would otherwise still answer for a marker this
    // one never armed.
    await script.reset();

    // A short label on purpose: this registration's name is half of every
    // delegation tool name, and the tool the model is handed is capped at 64
    // characters. See `stamp` in seed.ts.
    world = await federation(rest, created, { label: 'A2A8' });

    // ── Family 1: the partner's agents, and the peers they hand to ──────────
    // Terse names: each is the second half of a delegation tool name, which
    // must fit in 64 characters.
    pipelineStage = await createAgent(rest, created, {
      name: stamp('P8 stage'),
      persona: 'The mandatory second stage of the partner pipeline.',
    });
    transferPeer = await createAgent(rest, created, { name: stamp('P8 transfer peer') });
    consultPeer = await createAgent(rest, created, { name: stamp('P8 adviser') });

    plain = await createAgent(rest, created, {
      name: stamp('P8 plain'),
      persona: 'Answers work that arrives over A2A.',
    });
    pipelineEntry = await createAgent(rest, created, { name: stamp('P8 pipeline') });
    transferEntry = await createAgent(rest, created, { name: stamp('P8 router') });
    consultEntry = await createAgent(rest, created, { name: stamp('P8 consulter') });
    slowEntry = await createAgent(rest, created, {
      name: stamp('P8 slow'),
      persona: 'Takes its time, so the caller has to wait or stop it.',
    });

    await setHandover(rest, pipelineEntry.id, [
      { id: pipelineStage.id, type: 'mandatory_transfer' },
    ]);
    await setHandover(rest, transferEntry.id, [{ id: transferPeer.id, type: 'transfer' }]);
    await setHandover(rest, consultEntry.id, [{ id: consultPeer.id, type: 'consult' }]);

    // The binding IS the exposure. Peers are deliberately left unbound except
    // the transfer peer, which a follow-up has to be able to reach.
    for (const agent of [plain, pipelineEntry, transferEntry, transferPeer, consultEntry, slowEntry]) {
      await world.expose(agent.id);
    }

    // ── The registration that reaches them ──────────────────────────────────
    await world.link();
    // One partner client over the credential, for reading task state the way
    // the caller's own client does.
    partner = await PartnerClient.connect(world.cards[0], world.credentials);

    // ── Family 2: the callers, and the peers they hand to ───────────────────
    caller = await createAgent(rest, created, {
      name: stamp('E2E 08 caller'),
      persona: 'Sends work to the partner and reports what came back.',
    });
    backOffice = await createAgent(rest, created, { name: stamp('E2E 08 back office') });
    frontDesk = await createAgent(rest, created, { name: stamp('E2E 08 front desk') });
    router = await createAgent(rest, created, { name: stamp('E2E 08 caller router') });
    scribe = await createAgent(rest, created, { name: stamp('E2E 08 caller scribe') });
    advisor = await createAgent(rest, created, { name: stamp('E2E 08 caller adviser') });
    consulter = await createAgent(rest, created, { name: stamp('E2E 08 caller consulter') });
    dispatcher = await createAgent(rest, created, { name: stamp('E2E 08 caller dispatcher') });
    courier = await createAgent(rest, created, { name: stamp('E2E 08 caller courier') });
    backgroundWorker = await createAgent(rest, created, {
      name: stamp('E2E 08 caller background'),
      // `spawn_background_task` parks the parent on the child, and the `wait`
      // primitive is offered only to a run that already has a tool — so the
      // child names a built-in rather than leaving it implicit.
      extra: { builtin_tool_ids: ['inspect_my_traces'] },
    });
    backgroundCaller = await createAgent(rest, created, {
      name: stamp('E2E 08 caller spawning'),
      extra: { builtin_tool_ids: ['inspect_my_traces'] },
    });

    await setHandover(rest, frontDesk.id, [{ id: backOffice.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, router.id, [{ id: scribe.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, consulter.id, [{ id: advisor.id, type: 'consult' }]);
    await setHandover(rest, dispatcher.id, [{ id: courier.id, type: 'transfer' }]);
    await setHandover(rest, backgroundCaller.id, [{ id: backgroundWorker.id, type: 'delegate' }]);

    // Which agents are enrolled is what places a hand-off before or after the
    // crossing — see the file header. `frontDesk` and `dispatcher` are NOT
    // enrolled, so the hand-off has to happen before anything can reach the
    // partner; `router` IS, so its stage runs after the crossing.
    for (const agent of [
      caller,
      backOffice,
      router,
      consulter,
      courier,
      backgroundWorker,
      backgroundCaller,
    ]) {
      await world.enrol(agent.id);
    }
  });

  test.afterAll(async () => {
    if (available) {
      await partner?.dispose();
      await script.dispose();
      await cleanup(rest, created);
    }
    await rest?.dispose();
  });

  test.beforeEach(() => {
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  // -------------------------------------------------------------------------
  // The families themselves
  // -------------------------------------------------------------------------

  test('one registration faces each way, and an agent is in exactly one family', async () => {
    // The premise of the whole file, asserted rather than assumed: the two
    // registrations are separate rows facing opposite ways, and the outbound
    // one reaches this deployment's own agents.
    //
    // Read from the client surface rather than from a direction field, because
    // there is no direction field on the integration: the surface IS the
    // distinction. It answers for an outbound registration and is not-found
    // for an inbound one — the same not-found a non-A2A provider gets, so an
    // inbound registration is not even enumerable there.
    expect(
      (await rest.get(`/api/v1/a2a/client/${world.inbound.id}`)).status(),
      'the inbound registration must not appear on the client surface',
    ).toBe(404);
    expect((await rest.get(`/api/v1/a2a/client/${world.outbound.id}`)).status()).toBe(200);
    // And the mirror: only the inbound one mints a partner credential.
    expect(
      (await rest.post(`/api/v1/integrations/${world.outbound.id}/a2a-credential`)).ok(),
      'an outbound registration presents a credential rather than minting one',
    ).toBe(false);

    const state = await world.state();
    const targets = (state.targets as Record<string, unknown>[]) ?? [];
    expect(
      targets.map((t) => t.declared_name).sort(),
      'every exposed agent became a target of the registration that reaches them',
    ).toEqual(
      [plain, pipelineEntry, transferEntry, transferPeer, consultEntry, slowEntry]
        .map((a) => a.name)
        .sort(),
    );
    for (const target of targets) {
      expect(target.usability, `${target.declared_name} is not usable`).toBe('usable');
    }
  });

  test('family 2 is offered a tool per partner agent, and family 1 is offered none', async () => {
    const mark = marker('08-offered');
    await script.arm(mark, [{ text: 'Nothing to do.' }]);
    const conversation = await startConversation(caller.id, 'E2E 08 tools');
    await say(conversation, `Say nothing at all. ${mark}`);

    const offered = new Set(
      (await script.calls()).calls.filter((c) => c.matched === mark).flatMap((c) => c.tools_offered),
    );
    expect(offered, 'the caller reached the model at all').not.toEqual(new Set());
    expect([...offered]).toContain(await world.toolFor(plain.name));
    expect([...offered]).toContain(await world.toolFor(pipelineEntry.name));
    expect([...offered]).toContain(await world.cancelTool());

    // And the other way: an exposed agent is a partner's to task, not a
    // caller. Its binding confers no outbound tool at all.
    const partnerMark = marker('08-partner-offered');
    await script.arm(partnerMark, [{ text: 'Nothing to do.' }]);
    const partnerSide = await startConversation(plain.id, 'E2E 08 partner tools');
    await say(partnerSide, `Say nothing at all. ${partnerMark}`);
    const partnerOffered = new Set(
      (await script.calls()).calls
        .filter((c) => c.matched === partnerMark)
        .flatMap((c) => c.tools_offered),
    );
    expect(partnerOffered, 'the exposed agent reached the model at all').not.toEqual(new Set());
    expect([...partnerOffered].filter((t) => t.startsWith('delegate_to_'))).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // Handover within family 2 — the caller side, before anything crosses
  // -------------------------------------------------------------------------

  test('a mandatory stage is what reaches the partner, and the row names the stage', async () => {
    // Hand-off BEFORE the crossing. The front desk is not enrolled, so it
    // cannot reach the partner however it is asked to; its stage is, and the
    // delegation is recorded against the agent that actually made it.
    const mark = marker('08-before');
    const partnerMark = marker('08-before-partner');
    await script.arm(mark, [
      { text: 'Front desk: escalating this.' },
      {
        tool: await world.toolFor(plain.name),
        arguments: { message: `${partnerMark} handle the escalation` },
      },
      { text: 'Back office: the partner answered.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner answer for the escalation.' }]);

    const before = await rowIds();
    const conversation = await startConversation(frontDesk.id, 'E2E 08 before');
    await say(conversation, `${mark} please escalate this outward`);

    const row = await concludedRowSince(before, 'the escalated delegation');
    expect(row.outcome).toBe('completed');
    expect(
      row.agent_name,
      'the delegation belongs to the agent that made it, not to the one the user addressed',
    ).toBe(backOffice.name);
    expect(await saidBack(conversation)).toContain('Back office');
  });

  test('a chosen transfer moves the turn, and the peer it moved to reaches the partner', async () => {
    const mark = marker('08-transfer');
    const partnerMark = marker('08-transfer-partner');
    await script.arm(mark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: courier.id, reason: 'the courier owns this' } },
      {
        tool: await world.toolFor(plain.name),
        arguments: { message: `${partnerMark} carried by the courier` },
      },
      { text: 'Courier: delivered and answered.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner answer for the courier.' }]);

    const before = await rowIds();
    const conversation = await startConversation(dispatcher.id, 'E2E 08 transfer');
    await say(conversation, `${mark} route this to whoever can reach the partner`);

    const row = await concludedRowSince(before, 'the transferred delegation');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name).toBe(courier.name);
    expect(await saidBack(conversation)).toContain('Courier');
  });

  test('a consulted peer advises, and the agent that was asked still owns the crossing', async () => {
    const mark = marker('08-consult');
    const innerMark = marker('08-consult-inner');
    const partnerMark = marker('08-consult-partner');
    await script.arm(mark, [
      {
        tool: 'consult_agent',
        arguments: { agent_id: advisor.id, question: `${innerMark} is this the partner's subject?` },
      },
      {
        tool: await world.toolFor(plain.name),
        arguments: { message: `${partnerMark} as advised` },
      },
      { text: 'Consulter: answered, having taken advice.' },
    ]);
    // A consult sends its own prompt rather than forwarding the user's, so the
    // adviser's turn is keyed on a marker carried in the question.
    await script.arm(innerMark, [{ text: 'The advice: yes, ask the partner.' }]);
    await script.arm(partnerMark, [{ text: 'Partner answer after advice.' }]);

    const before = await rowIds();
    const conversation = await startConversation(consulter.id, 'E2E 08 consult');
    await say(conversation, `${mark} check with the adviser first`);

    const row = await concludedRowSince(before, 'the delegation made after a consult');
    expect(row.outcome).toBe('completed');
    expect(
      row.agent_name,
      'a consult advises; it does not move responsibility, so the crossing is still the asked agent\'s',
    ).toBe(consulter.name);
    expect(await saidBack(conversation)).toContain('Consulter');
  });

  test('a background child of a family-2 agent reaches the partner on its own', async () => {
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    // The fourth flavour, and the one that leaves the process: the delegating
    // agent's turn ends, the child runs under the reconciler, and the
    // crossing happens from there — which is why the worker carries the same
    // trusted-origin and callback settings the platform does.
    const mark = marker('08-background');
    const childMark = marker('08-background-child');
    const partnerMark = marker('08-background-partner');
    await script.arm(mark, [
      {
        tool: 'spawn_background_task',
        arguments: { agent_id: backgroundWorker.id, task: `${childMark} ask the partner about this` },
      },
      { text: 'Spawned the background worker.' },
      { text: 'Reporting after the background work.' },
    ]);
    await script.arm(childMark, [
      {
        tool: await world.toolFor(plain.name),
        arguments: { message: `${partnerMark} from the background child` },
      },
      { text: 'Background child: the partner answered.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner answer for the background child.' }]);

    const before = await rowIds();
    const conversation = await startConversation(backgroundCaller.id, 'E2E 08 background');
    await say(conversation, `${mark} do this in the background`);

    const row = await concludedRowSince(before, 'the delegation made from a background child');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name).toBe(backgroundWorker.name);
  });

  // -------------------------------------------------------------------------
  // The crossing, with the hand-off on the partner's side
  // -------------------------------------------------------------------------

  test('a crossing with no hand-off anywhere completes against the agent addressed', async () => {
    // The baseline every row below is read against: one row, one task, one
    // agent at each end.
    const mark = marker('08-plain');
    const partnerMark = marker('08-plain-partner');
    await script.arm(mark, [
      { tool: await world.toolFor(plain.name), arguments: { message: `${partnerMark} a plain question` } },
      { text: 'Caller: the partner answered.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner answer, plain.' }]);

    const knownTasks = await partnerTaskIds();
    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 plain');
    await say(conversation, `${mark} ask the partner`);

    const row = await concludedRowSince(before, 'the plain crossing');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name).toBe(caller.name);
    expect(row.continuation_count, 'nothing was continued').toBe(0);

    const taskId = await taskSubmittedSince(knownTasks, 'the task the plain crossing created');
    await eventually(async () => expect(await partnerTaskState(taskId)).toBe('completed'), {
      what: "the partner's task finishing",
      timeoutMs: 60_000,
    });
  });

  test("a partner's mandatory pipeline answers the caller, and the caller cannot tell", async () => {
    // Hand-off AFTER the crossing, at the far end. The contract the caller
    // validated is the entry agent's card; who actually produced the answer is
    // the partner's business, and nothing about the exchange changes.
    const mark = marker('08-far-pipeline');
    const partnerMark = marker('08-far-pipeline-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(pipelineEntry.name),
        arguments: { message: `${partnerMark} run your pipeline` },
      },
      { text: 'Caller: the partner pipeline answered.' },
    ]);
    // Two prose turns and no tool call: a mandatory transfer is wired by the
    // operator and fires after the loop, so the script supplies only words.
    await script.arm(partnerMark, [
      { text: 'Partner entry: first stage.' },
      { text: 'Partner stage: PIPELINE-ANSWER.' },
    ]);

    const knownTasks = await partnerTaskIds();
    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 far pipeline');
    await say(conversation, `${mark} ask the pipeline`);

    const row = await concludedRowSince(before, 'the crossing into a partner pipeline');
    expect(row.outcome).toBe('completed');
    expect(
      row.agent_name,
      'the row names the CALLING agent; who answered at the partner is not the caller\'s to know',
    ).toBe(caller.name);

    // Read from the partner's own task: the caller's reply is the caller's own
    // armed prose, which a partner that handed nothing to a stage would produce
    // just the same.
    const taskId = await taskSubmittedSince(knownTasks, 'the task the pipeline crossing created');
    const responding = await respondingAgentOf(taskId);
    expect(responding?.id, 'the stage, not the entry agent, produced the answer').toBe(
      pipelineStage.id,
    );
  });

  test("a partner's chosen transfer answers the caller in one exchange", async () => {
    const mark = marker('08-far-transfer');
    const partnerMark = marker('08-far-transfer-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(transferEntry.name),
        arguments: { message: `${partnerMark} route this at your end` },
      },
      { text: 'Caller: the partner transferred and answered.' },
    ]);
    await script.arm(partnerMark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: transferPeer.id, reason: 'the peer owns this' } },
      { text: 'Partner peer: TRANSFER-ANSWER.' },
    ]);

    const knownTasks = await partnerTaskIds();
    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 far transfer');
    await say(conversation, `${mark} ask the router`);

    const row = await concludedRowSince(before, 'the crossing into a partner transfer');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name).toBe(caller.name);

    // The row above is what a crossing with no hand-off produces too. These
    // two are the partner-side evidence: it reached for the handover tool, and
    // its task attributes the answer to the peer.
    expect(await toolsCalledFor(partnerMark)).toContain('transfer_to_agent');
    const taskId = await taskSubmittedSince(knownTasks, 'the task the transfer crossing created');
    expect((await respondingAgentOf(taskId))?.id).toBe(transferPeer.id);
  });

  test("a partner's consult leaves the addressed agent answering", async () => {
    const mark = marker('08-far-consult');
    const partnerMark = marker('08-far-consult-partner');
    const partnerInner = marker('08-far-consult-inner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(consultEntry.name),
        arguments: { message: `${partnerMark} take advice at your end` },
      },
      { text: 'Caller: the partner consulted and answered.' },
    ]);
    await script.arm(partnerMark, [
      {
        tool: 'consult_agent',
        arguments: { agent_id: consultPeer.id, question: `${partnerInner} which rule applies?` },
      },
      { text: 'Partner entry: CONSULT-ANSWER, informed by advice.' },
    ]);
    await script.arm(partnerInner, [{ text: 'Partner adviser: the advice.' }]);

    const knownTasks = await partnerTaskIds();
    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 far consult');
    await say(conversation, `${mark} ask the consulter`);

    const row = await concludedRowSince(before, 'the crossing into a partner consult');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name).toBe(caller.name);

    // A consult sends its own prompt, so the adviser answering at all is only
    // possible if the consult happened.
    expect(await toolsCalledFor(partnerMark)).toContain('consult_agent');
    expect(
      (await script.agentTurns(partnerInner)).length,
      'the adviser never took a turn, so nothing was consulted',
    ).toBeGreaterThan(0);

    // A consult attributes nothing: the agent the caller addressed is still the
    // one that answered.
    const taskId = await taskSubmittedSince(knownTasks, 'the task the consult crossing created');
    expect(
      await respondingAgentOf(taskId),
      'a consult must not move responsibility to the peer',
    ).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Hand-offs on both sides at once
  // -------------------------------------------------------------------------

  test('a stage reaches a pipeline: a hand-off at each end of one exchange', async () => {
    const mark = marker('08-both');
    const partnerMark = marker('08-both-partner');
    await script.arm(mark, [
      { text: 'Front desk: escalating this too.' },
      {
        tool: await world.toolFor(pipelineEntry.name),
        arguments: { message: `${partnerMark} both ends hand off` },
      },
      { text: 'Back office: the partner pipeline answered.' },
    ]);
    await script.arm(partnerMark, [
      { text: 'Partner entry: first stage.' },
      { text: 'Partner stage: BOTH-ENDS-ANSWER.' },
    ]);

    const before = await rowIds();
    const conversation = await startConversation(frontDesk.id, 'E2E 08 both ends');
    await say(conversation, `${mark} escalate this to the pipeline`);

    const row = await concludedRowSince(before, 'the crossing with a hand-off at each end');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name, 'the caller-side stage made the call').toBe(backOffice.name);
    expect(await saidBack(conversation)).toContain('Back office');
  });

  test('a hand-off AFTER the crossing writes the answer the user reads', async () => {
    // The mirror of the first test in this file: the entry agent is the
    // enrolled one, so the partner is reached first and the stage runs on what
    // came back. The delegation is the entry's; the last word is the stage's.
    const mark = marker('08-after');
    const partnerMark = marker('08-after-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(plain.name),
        arguments: { message: `${partnerMark} fetch the facts` },
      },
      { text: 'Router: fetched the facts.' },
      { text: 'Scribe: WRITTEN-UP for the reader.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner answer to be written up.' }]);

    const before = await rowIds();
    const conversation = await startConversation(router.id, 'E2E 08 after');
    await say(conversation, `${mark} fetch this and write it up`);

    const row = await concludedRowSince(before, 'the crossing that precedes a hand-off');
    expect(row.outcome).toBe('completed');
    expect(row.agent_name, 'the crossing was the entry agent\'s').toBe(router.name);

    const said = await saidBack(conversation);
    expect(said, 'the stage ran after the crossing').toContain('WRITTEN-UP');
    // A mandatory transfer answers AS its stage: the turn has one reply and the
    // stage wrote it. The answer is the stage's while the delegation is the
    // entry's, which only reading both shows.
    expect(said, 'a mandatory transfer replaces the reply rather than adding one').not.toContain(
      'Router: fetched the facts.',
    );
  });

  // -------------------------------------------------------------------------
  // Stopping a crossing, on either side of the hand-back
  // -------------------------------------------------------------------------

  test('work that outlives the turn hands back, with the partner still running', async () => {
    // The precondition for both cancels below, and worth stating on its own:
    // the caller is released at the grace period while the partner is still
    // working, so "cancel before the hand-back" and "cancel after it" are two
    // genuinely different moments rather than a race.
    const mark = marker('08-handback');
    const partnerMark = marker('08-handback-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(slowEntry.name),
        arguments: { message: `${partnerMark} this takes a while` },
      },
      { text: 'Caller: the partner is still working.' },
    ]);
    await script.arm(partnerMark, [
      { tool: 'wait', arguments: { duration: LONG_WORK, reason: 'hold the task open' } },
      { text: 'Partner: finished at last.' },
    ]);

    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 hand-back');
    const elapsed = await say(conversation, `${mark} ask the slow one`);

    // The grace bounds the WAIT for the partner, not the turn around it: a
    // first delegation also mints a token, runs the verification pre-flight
    // and makes a model round trip either side of the tool call. The bound
    // only has to separate the grace from the ceiling.
    const budget = CLOCK.graceSeconds * 1_000 + 12_000;
    expect(
      budget,
      'the hand-back budget must separate the grace from the ceiling',
    ).toBeLessThan(CLOCK.delegationCeilingSeconds * 1_000);
    expect(elapsed, 'the turn should have handed back at the grace period').toBeLessThan(budget);

    const outstanding = (await rowsSince(before)).find((row) => row.outcome === null);
    expect(outstanding, 'the exchange left no outstanding delegation').toBeTruthy();
  });

  test('abandoning before the hand-back stops a partner pipeline that is already running', async () => {
    // Cancel BEFORE the hand-back: the turn is still held when the user walks
    // away, and the work at the far end is a hand-off of its own — so the
    // cancel has to reach the stage, not merely the run that received it.
    const mark = marker('08-cancel-early');
    const partnerMark = marker('08-cancel-early-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(pipelineEntry.name),
        arguments: { message: `${partnerMark} start the long pipeline` },
      },
      { text: 'Caller: started.' },
    ]);
    await script.arm(partnerMark, [
      { text: 'Partner entry: handing to the stage.' },
      { tool: 'wait', arguments: { duration: WORK_TO_INTERRUPT, reason: 'hold the stage open' } },
      { text: 'Partner stage: should never be reached.' },
    ]);

    const knownTasks = await partnerTaskIds();
    const conversation = await startConversation(caller.id, 'E2E 08 cancel early');
    // Deliberately not awaited: the point is to cancel while the turn is still
    // held, which is the only moment "before the hand-back" exists.
    const turn = say(conversation, `${mark} start the long pipeline`).catch(() => 0);

    const taskId = await taskSubmittedSince(knownTasks, 'the task the cancelled crossing created');
    // A task id exists the moment the submission is accepted, so the cancel
    // waits for the partner to be WORKING — otherwise it proves only that a
    // cancel beats a run that has not started.
    await eventually(async () => expect(await partnerTaskState(taskId)).toBe('working'), {
      what: 'the partner pipeline actually running',
      timeoutMs: 60_000,
      intervalMs: 500,
    });
    await abandon(conversation);
    await turn;

    await eventually(
      async () => {
        // `canceled` exactly, not "canceled or failed": a task that failed
        // for its own reasons would satisfy the looser assertion and prove
        // nothing about the cancel.
        expect(await partnerTaskState(taskId)).toBe('canceled');
      },
      { what: 'the partner task stopping', timeoutMs: 90_000, intervalMs: 2_000 },
    );
  });

  test('abandoning after the hand-back still stops the partner, hand-off and all', async () => {
    // Cancel AFTER the hand-back. The turn is over and the conversation looks
    // idle, but a delegation is outstanding and the partner is still working —
    // which is exactly the state a user walking away leaves behind, and the
    // one where nothing would stop on its own until the reaper noticed.
    const mark = marker('08-cancel-late');
    const partnerMark = marker('08-cancel-late-partner');
    await script.arm(mark, [
      { text: 'Front desk: escalating the slow one.' },
      {
        tool: await world.toolFor(slowEntry.name),
        arguments: { message: `${partnerMark} this will outlive the turn` },
      },
      { text: 'Back office: still working, I will report back.' },
    ]);
    await script.arm(partnerMark, [
      { tool: 'wait', arguments: { duration: WORK_TO_INTERRUPT, reason: 'outlive the turn' } },
      { text: 'Partner: should never be reached.' },
    ]);

    const knownTasks = await partnerTaskIds();
    const before = await rowIds();
    const conversation = await startConversation(frontDesk.id, 'E2E 08 cancel late');
    await say(conversation, `${mark} escalate the slow one`);

    const taskId = await taskSubmittedSince(knownTasks, 'the task left running after the hand-back');
    const outstanding = (await rowsSince(before)).find((row) => row.outcome === null);
    expect(outstanding, 'nothing was left outstanding to cancel').toBeTruthy();
    expect(outstanding?.agent_name, 'the caller-side stage made the call').toBe(backOffice.name);

    await abandon(conversation);

    await eventually(
      async () => {
        // `canceled` exactly, not "canceled or failed": a task that failed
        // for its own reasons would satisfy the looser assertion and prove
        // nothing about the cancel.
        expect(await partnerTaskState(taskId)).toBe('canceled');
      },
      { what: 'the partner task stopping after the conversation was abandoned', timeoutMs: 90_000, intervalMs: 2_000 },
    );
  });

  test('the caller can stop the partner itself, without abandoning the conversation', async () => {
    // The other way work ends early, and the one a user chooses deliberately:
    // the registration confers one cancel tool, the hand-back handed the model
    // a task id, and the next turn uses it. The conversation carries on.
    const mark = marker('08-cancel-tool');
    const partnerMark = marker('08-cancel-tool-partner');
    await script.arm(mark, [
      {
        tool: await world.toolFor(slowEntry.name),
        arguments: { message: `${partnerMark} start something stoppable` },
      },
      { text: 'Caller: started, and it is still running.' },
    ]);
    await script.arm(partnerMark, [
      { tool: 'wait', arguments: { duration: WORK_TO_INTERRUPT, reason: 'wait to be stopped' } },
      { text: 'Partner: should never be reached.' },
    ]);

    const knownTasks = await partnerTaskIds();
    const conversation = await startConversation(caller.id, 'E2E 08 cancel tool');
    await say(conversation, `${mark} start something stoppable`);
    const taskId = await taskSubmittedSince(knownTasks, 'the task the caller will stop');

    const stopMark = marker('08-cancel-tool-stop');
    await script.arm(stopMark, [
      { tool: await world.cancelTool(), arguments: { task_id: taskId } },
      { text: 'Caller: stopped it.' },
    ]);
    // A turn that starts while a wake is in flight is folded into it rather
    // than run, so the conversation has to be idle first.
    await eventually(
      async () => expect((await conversationNow(conversation)).processing).toBe(false),
      { what: 'the conversation settling before the stop turn', timeoutMs: 60_000, intervalMs: 500 },
    );
    await say(conversation, `${stopMark} stop that, please`);

    await eventually(
      async () => {
        // `canceled` exactly, not "canceled or failed": a task that failed
        // for its own reasons would satisfy the looser assertion and prove
        // nothing about the cancel.
        expect(await partnerTaskState(taskId)).toBe('canceled');
      },
      { what: 'the partner task stopping on the cancel tool', timeoutMs: 90_000, intervalMs: 2_000 },
    );
    expect(await saidBack(conversation)).toContain('stopped it');
  });

  test('a stopped crossing leaves the conversation usable', async () => {
    // The reason the two cancels above are worth separating from a failure:
    // stopping work at the partner is not an error state for the caller. A
    // fresh exchange on a new conversation still completes afterwards, which
    // is what rules out a breaker left open or a registration left suspended.
    await sleep(CLOCK.breakerCooldownSeconds * 1_000);

    const mark = marker('08-after-cancel');
    const partnerMark = marker('08-after-cancel-partner');
    await script.arm(mark, [
      { tool: await world.toolFor(plain.name), arguments: { message: `${partnerMark} still there?` } },
      { text: 'Caller: still working.' },
    ]);
    await script.arm(partnerMark, [{ text: 'Partner: still here.' }]);

    const before = await rowIds();
    const conversation = await startConversation(caller.id, 'E2E 08 after cancel');
    await say(conversation, `${mark} ask again`);

    const row = await concludedRowSince(before, 'a crossing after a cancelled one');
    expect(row.outcome).toBe('completed');
  });
});
