/**
 * Waiting, handing back, and stopping work that outlived the turn that started it.
 *
 * These branches belong together because they are all decided by a clock
 * rather than by a value. A delegation ends its attempt at the grace period
 * when a callback is registered and at the ceiling when none is; a handed-back
 * task keeps running past the turn that started it; the polling floor
 * concludes what no callback ever reported; and a cancel is the one thing that
 * ends the work early. Each is only true relative to the others, so a spec
 * that pinned one in isolation would pin a coincidence.
 *
 * The deployment runs on an accelerated clock (scripts/a2a-e2e.sh), and every
 * timing assertion below is written against `CLOCK` rather than against a
 * number: at the shipped defaults — a five-minute ceiling, a fifteen-second
 * grace, a fifteen-minute floor — none of these branches is observable inside
 * a test at all, and a bare `10_000` says nothing about which bound it is
 * testing or how much slack it is allowing.
 *
 * One clock is deliberately NOT accelerated. The re-verification sweep is
 * parked at an hour (see instance.ts), and `collect_overdue` shares its loop
 * — main.py::_a2a_client_sweep_loop calls both — so the polling floor ticks on
 * the SWEEP's cadence however short its own overdue threshold
 * (`CLOCK.floorIntervalSeconds`) is. The one branch here whose conclusion can
 * only come from the floor therefore skips itself unless the deployment was started
 * with a short `A2A_CLIENT_SWEEP_INTERVAL_SECONDS`.
 *
 * TWO HARNESS FACTS SHAPE EVERY ASSERTION HERE
 *
 * The REST chat endpoint records no tool trace. `tool_call_trace` is written
 * only by the web chat's streamed send (chat.py), so the JSON a delegation tool returned
 * to the model is simply not readable from a REST-driven turn. Everything is
 * therefore asserted from state the two halves agree on instead: the outbound
 * delegation rows, the partner's task read through the credential, and the
 * audit trail — which is also the only surface that names a task id the caller
 * created.
 *
 * The fixtures are built with contexts this file owns rather than through the
 * `one` fixture. `one` is test-scoped and Playwright allows only worker-scoped
 * fixtures in `beforeAll`, so a spec that has to stand its whole story up once
 * cannot use it there; the one genuinely visual test takes `one` and nothing
 * else does.
 *
 * A note on what makes the partner slow. A2A submissions answer before the
 * work is done, so a hand-back needs an exposed agent that takes longer than
 * the grace. The scripted model calls the platform's own `wait` primitive,
 * which is offered to any run that has tools — and an external task always
 * has `ask_the_caller`. If that ever stops being offered the scripted server
 * answers `SCRIPT-ERROR: no offered tool matches 'wait'`, the partner
 * finishes at once, and the hand-back assertions fail naming the cause.
 */

import { request, type APIRequestContext } from '@playwright/test';
import { expect, test } from './fixtures';
import { ChatPage } from '../model/chat.page';
import {
  ADMIN_EMAIL,
  ADMIN_PASSWORD,
  CLOCK,
  SELF,
  SWEEP_IS_FAST,
  WIRE_HEADERS,
} from './instance';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  delegations,
  exposeAgent,
  mintCredential,
  registerPartner,
  registrationState,
  stamp,
  tracker,
} from './seed';
import { METHOD, PartnerClient, eventually, readState, sleep } from './wire';
import { ScriptedModel, marker } from '../scripted-llm/client';

/** Far longer than the grace, so the attempt hands back rather than answering. */
const LONG_WORK = '20s';
/** Long enough that a cancel arriving several seconds in still interrupts it. */
const WORK_TO_INTERRUPT = '25s';

/**
 * What a hand-back may cost beyond the grace itself.
 *
 * The grace bounds the WAIT for the partner, not the turn around it. A run's
 * first delegation also mints an OAuth token, runs the verification pre-flight
 * over the partner's card, and makes a model round trip either side of the
 * tool call — none of which the grace covers, and all of which a cold run pays
 * at once. Twelve seconds is the budget for that; the bound only has to
 * separate the grace from the ceiling, and it stays well clear of it.
 */
const HANDBACK_BUDGET_MS = CLOCK.graceSeconds * 1_000 + 12_000;

/** One delegation as the outbound half reports it. The endpoint's shape, narrowed once here. */
interface DelegationRow {
  id: string;
  agent_name: string | null;
  outcome: string | null;
  partner_reason: string | null;
  protocol_version: string;
  signature_state: string;
  continuation_count: number;
  mechanism: string | null;
  submitted_at: string;
}

interface AuditEntry {
  entity_id: string | null;
  details: Record<string, unknown> | null;
}

interface RegisteredCallback {
  id: string;
  url: string;
}

interface ChatMessage {
  role: string;
  content: string | null;
}

const created = tracker();

let available = false;
let script: ScriptedModel;
let rest: APIRequestContext;
let wire: APIRequestContext;
let partner: PartnerClient;
let credentials = { clientId: '', clientSecret: '' };
let inboundId = '';
let outboundId = '';
let caller = { id: '', name: '' };

// ---------------------------------------------------------------------------
// Local helpers. Each one exists because the harness has no equivalent, and
// each is confined to this file deliberately: they are all readings of state
// that only the timing branches care about.
// ---------------------------------------------------------------------------

/** An admin context. */
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
    const token = ((await login.json()) as { access_token: string }).access_token;
    return await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    });
  } finally {
    await anonymous.dispose();
  }
}

/**
 * Every task id the inbound half has recorded for this partner, newest first.
 *
 * A2A task ids are minted by the inbound half and never travel to the outbound
 * half's control plane — the delegations endpoint deliberately does not
 * publish `partner_task_id` — so the audit trail is the only API that names
 * one. The submission entry carries
 * the task in `entity_id` and the calling registration in `details.client_id`,
 * which is exactly the pair a spec needs to say "the task the caller just
 * created".
 */
async function partnerTaskIds(): Promise<string[]> {
  const resp = await rest.get('/api/v1/audit-logs', {
    params: { entity_type: 'a2a_task', action: 'trigger', limit: 200 },
  });
  if (!resp.ok()) {
    throw new Error(`audit read failed: ${resp.status()} ${await resp.text()}`);
  }
  const page = (await resp.json()) as { items: AuditEntry[] };
  return page.items
    .filter((item) => String((item.details ?? {}).client_id) === credentials.clientId)
    .map((item) => item.entity_id)
    .filter((id): id is string => Boolean(id));
}

/**
 * The one task id that appeared since `known`.
 *
 * Polled rather than read once: the audit entry is committed in the same
 * transaction as the task, but the caller's turn returns as soon as the
 * delegation hands back, which can be the same instant.
 */
async function taskSubmittedSince(known: readonly string[]): Promise<string> {
  return eventually(
    async () => {
      const fresh = (await partnerTaskIds()).filter((id) => !known.includes(id));
      expect(fresh, 'no new task was recorded for this delegation').toHaveLength(1);
      return fresh[0];
    },
    { timeoutMs: 20_000, what: 'the record of the task just submitted' },
  );
}

/** What the partner says about one of its tasks, read with the caller's own credential. */
async function partnerTaskState(taskId: string): Promise<string> {
  const { body } = await partner.getTask(taskId);
  return (
    readState(body.result) ??
    `no state — error ${body.error?.code}: ${body.error?.message}`
  );
}

/** The callbacks held for one task, as the partner may read them back. */
async function registeredCallbacks(taskId: string): Promise<RegisteredCallback[]> {
  const { body } = await partner.rpc(METHOD.pushList, { taskId });
  const listed = (body.result ?? []) as {
    pushNotificationConfig?: RegisteredCallback;
  }[];
  return listed.flatMap((entry) =>
    entry.pushNotificationConfig ? [entry.pushNotificationConfig] : [],
  );
}

/** When this partner last submitted anything, derived from its tasks. */
async function lastActivityOnA(): Promise<number> {
  const resp = await rest.get(`/api/v1/integrations/${inboundId}/a2a-activity`);
  if (!resp.ok()) {
    throw new Error(`activity read failed: ${resp.status()} ${await resp.text()}`);
  }
  const body = (await resp.json()) as { last_activity_at: string | null };
  return body.last_activity_at ? Date.parse(body.last_activity_at) : 0;
}

/** The outbound delegation rows, newest first. One narrowing cast, in one place. */
async function delegationRows(): Promise<DelegationRow[]> {
  return (await delegations(rest, outboundId)) as unknown as DelegationRow[];
}

async function rowById(id: string): Promise<DelegationRow> {
  const found = (await delegationRows()).find((row) => row.id === id);
  if (!found) throw new Error(`no delegation row ${id}.`);
  return found;
}

async function rowIds(): Promise<Set<string>> {
  return new Set((await delegationRows()).map((row) => row.id));
}

async function rowsSince(known: Set<string>): Promise<DelegationRow[]> {
  return (await delegationRows()).filter((row) => !known.has(row.id));
}

/**
 * The row a delegation just left behind.
 *
 * Rows come back newest first and outstanding means "no outcome", so the
 * first unconcluded one is the exchange that has only just handed back. Found
 * this way rather than by diffing ids, because a previous block's floor sweep
 * can still be writing rows of its own while this one submits.
 */
async function newestOutstandingRow(): Promise<DelegationRow> {
  const found = (await delegationRows()).find((row) => row.outcome === null);
  if (!found) throw new Error('no outstanding delegation was recorded for this exchange.');
  return found;
}

/** Hold until one of the conversation's replies carries `text`. */
async function replyCarrying(conversationId: string, text: string, what: string) {
  await eventually(
    async () => {
      const state = await conversationNow(conversationId);
      expect(state.assistant.some((reply) => reply.includes(text))).toBe(true);
    },
    { timeoutMs: 60_000, intervalMs: 1_000, what },
  );
}

async function startConversation(title: string): Promise<string> {
  const resp = await rest.post('/api/v1/chat/conversations', {
    data: { agent_id: caller.id, title },
  });
  if (!resp.ok()) {
    throw new Error(`conversation create failed: ${resp.status()} ${await resp.text()}`);
  }
  return ((await resp.json()) as { id: string }).id;
}

/** One chat turn, returning how long the caller was held. */
async function say(conversationId: string, content: string): Promise<number> {
  const started = Date.now();
  const resp = await rest.post(
    `/api/v1/chat/conversations/${conversationId}/messages`,
    // Generous, because a collect against a task that is still running polls
    // to the ceiling before it hands back — 20s here, five minutes shipped.
    { data: { content }, timeout: 150_000 },
  );
  const elapsedMs = Date.now() - started;
  if (!resp.ok()) {
    throw new Error(`chat turn failed: ${resp.status()} ${await resp.text()}`);
  }
  return elapsedMs;
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
  const body = (await resp.json()) as {
    is_processing: boolean;
    messages: ChatMessage[];
  };
  return {
    processing: body.is_processing,
    assistant: body.messages
      .filter((message) => message.role === 'assistant')
      .map((message) => message.content ?? ''),
  };
}

/**
 * Hold until nothing is running in the conversation.
 *
 * A turn that starts while a wake is in flight is FOLDED into it rather than
 * run, so a spec that sends without waiting silently loses its own turn.
 */
async function settled(conversationId: string): Promise<void> {
  await eventually(
    async () => {
      expect((await conversationNow(conversationId)).processing).toBe(false);
    },
    {
      timeoutMs: 60_000,
      intervalMs: 500,
      what: `conversation ${conversationId} finishing whatever it was doing`,
    },
  );
}

/**
 * Abandon a conversation, the only way a user can: Stop.
 *
 * The cascade that cancels outstanding partner tasks hangs off the chat's
 * Stop (`POST /web/chat/messages/cancel`), and the composer renders no Stop
 * control outside a turn (pinned below). So the route is called directly. Its answer means the Stop was taken; the cascade
 * itself runs in the background afterwards.
 */
async function abandonConversation(conversationId: string): Promise<void> {
  const resp = await rest.post('/api/v1/web/chat/messages/cancel', {
    data: { conversation_id: conversationId },
  });
  expect(resp.ok(), `cancel: ${resp.status()} ${await resp.text()}`).toBe(true);
  expect(((await resp.json()) as { status: string }).status).toBe('cancelled');
}

// ---------------------------------------------------------------------------

test.describe.serial('A2A waiting, hand-back and cancellation', () => {
  test.beforeAll(async () => {
    rest = await signIn();
    wire = await request.newContext({ baseURL: SELF.origin, extraHTTPHeaders: { ...WIRE_HEADERS } });

    available = await a2aAvailable(rest);
    if (!available) return;

    script = await ScriptedModel.connect();
    await script.reset();

    const inbound = await createInboundIntegration(rest, created, stamp('E2E 06 inbound'));
    inboundId = inbound.id;
    credentials = await mintCredential(rest, inbound.id);
    const responder = await createAgent(rest, created, {
      name: stamp('E2E 06 responder'),
      persona: 'Answers work that arrives over A2A, slowly when asked to.',
    });
    const cardUrl = await exposeAgent(rest, responder.id, inbound.id);
    partner = await PartnerClient.connect(cardUrl, credentials);

    const outbound = await registerPartner(rest, created, {
      cardUrls: cardUrl,
      tokenUrl: `${SELF.origin}/mcp/oauth/token`,
      clientId: credentials.clientId,
      clientSecret: credentials.clientSecret,
      name: stamp('E2E 06 partner'),
    });
    outboundId = outbound.id;

    caller = await createAgent(rest, created, {
      name: stamp('E2E 06 caller'),
      persona: 'Sends work to the partner and reports what came back.',
    });
    await bind(rest, caller.id, { oauth_a2a: [outbound.id] });
  });

  test.afterAll(async () => {
    if (available) {
      await cleanup(rest, created);
      await partner.dispose();
      await script.dispose();
    }
    await Promise.all([rest.dispose(), wire.dispose()]);
  });

  test.beforeEach(() => {
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  test('the partner declares callbacks and this deployment says it can be reached', async () => {
    // Everything below chooses its mechanism from these two facts, and neither
    // is inferred: the first comes off the partner's card at verification, the
    // second is the operator's own setting. Both halves are the same loopback
    // deployment, so the reachability derivation correctly reads
    // "unreachable" — and then no callback branch could run at all. The
    // explicit setting is what overrules it.
    const state = await registrationState(rest, outboundId);
    const targets = state.targets as Record<string, unknown>[];
    expect(targets).toHaveLength(1);
    expect(targets[0].declares_callbacks).toBe(true);
    expect(state.callbacks_reachable).toBe(true);
  });

  // -------------------------------------------------------------------------
  test.describe('work that outlives the turn that started it', () => {
    let conversation = '';
    let taskId = '';
    let known = new Set<string>();
    let rowAtHandback: DelegationRow;
    let handbackMs = 0;
    let wakeMark = '';

    test.beforeAll(async () => {
      if (!available) return;
      conversation = await startConversation(stamp('E2E 06 long work'));
      const partnerMark = marker('06-long-partner');
      const callerMark = marker('06-long-caller');
      wakeMark = marker('06-long-wake');

      // Armed in this order because the scripted model matches the first
      // script whose marker is in the prompt and still has steps. The woken
      // turn carries its own history, so the caller's script must be spent by
      // then — two steps, two turns, and no more.
      await script.arm(partnerMark, [
        { tool: 'wait', arguments: { duration: LONG_WORK, reason: 'e2e slow partner' } },
        { text: `the slow job is finished ${wakeMark}` },
      ]);
      await script.arm(callerMark, [
        { tool: '^delegate_to_', arguments: { message: `take your time ${partnerMark}` }, regex: true },
        { text: 'the partner has started' },
      ]);
      // The wake prompt carries the partner's own answer, so the marker the
      // partner wrote is what identifies the woken turn.
      await script.arm(wakeMark, [{ text: `woken by the partner ${wakeMark}` }]);

      const before = await partnerTaskIds();
      // Counted as a DELTA, not against an empty table. What this block
      // asserts is that one delegation produced one row; asserting the table
      // holds exactly one altogether would be a claim about the whole
      // deployment, and an earlier spec that delegated would break it silently
      // rather than fail on its own subject.
      known = await rowIds();
      handbackMs = await say(conversation, `delegate the slow job ${callerMark}`);
      // Read before anything else: the polling floor becomes eligible for this
      // row a floor interval after submission and restamps its mechanism.
      const fresh = await rowsSince(known);
      expect(fresh, 'the delegation left no row at all').toHaveLength(1);
      [rowAtHandback] = fresh;
      taskId = await taskSubmittedSince(before);
    });

    test('the attempt ends at the grace period, not at the ceiling', async () => {
      // The claim is which of two clocks ended the attempt, so the bound has
      // to sit between them: above the grace plus a cold pair's overhead, and
      // below the ceiling an unanswered attempt would have run to. Checked
      // rather than assumed, because a pair started with a shorter ceiling
      // would make the assertion below prove nothing at all.
      expect(
        HANDBACK_BUDGET_MS,
        'the hand-back budget must separate the grace from the ceiling',
      ).toBeLessThan(CLOCK.delegationCeilingSeconds * 1_000);
      expect(
        handbackMs,
        'the caller was held far longer than the grace period allows',
      ).toBeLessThan(HANDBACK_BUDGET_MS);
    });

    test('the hand-back leaves an unconcluded row naming the agent that sent it', async () => {
      // Outstanding is the ABSENCE of a conclusion — there is no state column
      // — and `mechanism` is stamped when the callback is registered, not when
      // the exchange finally concludes.
      expect(rowAtHandback.outcome).toBeNull();
      expect(rowAtHandback.mechanism).toBe('called_back');
      expect(rowAtHandback.continuation_count).toBe(0);
      expect(rowAtHandback.agent_name).toBe(caller.name);
    });

    test('the partner already holds the address of that row', async () => {
      // The callback rides on the submission itself, and the row it names is
      // committed in its own session before polling starts — otherwise a
      // partner quick enough to call back would address a row nothing can find.
      const callbacks = await registeredCallbacks(taskId);
      expect(callbacks).toHaveLength(1);
      expect(callbacks[0].url).toBe(`${SELF.origin}/a2a/callbacks/${rowAtHandback.id}`);
    });

    test('the turn ending leaves the partner still working', async () => {
      // The load-bearing negative of the whole feature. Chat teardown cancels
      // only tasks the executor holds for an input-required answer; a handed
      // back task is not one of them, and cancelling here would kill every
      // delegation seconds after creating it.
      expect(await partnerTaskState(taskId)).toBe('working');
      // Long enough that a teardown cancel would have reached the partner by now.
      await sleep(5_000);
      expect(await partnerTaskState(taskId)).toBe('working');
      expect((await rowById(rowAtHandback.id)).outcome).toBeNull();
    });

    test('a delegation whose callback can never arrive still concludes, on the floor', async () => {
      // `collect_overdue` runs inside the sweep's own loop, so the floor ticks
      // at `CLOCK.sweepIntervalSeconds` whatever its own overdue threshold
      // says — and the suite parks the sweep at an hour on purpose, because a
      // live one verifies every registration a spec creates within seconds
      // and makes "nothing verifies a registration on save" unobservable. So
      // this branch needs the other deployment rather than a longer wait.
      test.skip(
        !SWEEP_IS_FAST,
        'the floor ticks on the parked sweep loop',
      );
      // Skipped, the callback below is never deleted and the exchange
      // concludes by callback instead — which the two tests after this one
      // still hold under, neither of them asserting the mechanism.

      // Delivery is broken deliberately, by deleting the callback the partner
      // holds, through the same credential the caller registered it with.
      // Nothing will ever POST back for this exchange, so whatever concludes
      // it is the floor.
      const [registered] = await registeredCallbacks(taskId);
      const deleted = await partner.rpc(METHOD.pushDelete, {
        taskId,
        pushNotificationConfigId: registered.id,
      });
      expect(deleted.body.error).toBeUndefined();
      expect(await registeredCallbacks(taskId)).toHaveLength(0);

      const concluded = await eventually(
        async () => {
          const row = await rowById(rowAtHandback.id);
          expect(row.outcome).toBe('completed');
          return row;
        },
        {
          timeoutMs: 90_000,
          intervalMs: 1_000,
          // Two clocks in series: the row goes overdue after
          // `CLOCK.floorIntervalSeconds`, and the loop that notices runs
          // every `CLOCK.sweepIntervalSeconds` — five and five under
          // a shortened sweep; fifteen minutes and an hour as shipped.
          what: 'the polling floor collecting a partner task nobody asked about',
        },
      );
      // The floor asked, whatever the row once hoped for.
      expect(concluded.mechanism).toBe('asked');
    });

    test('the conversation is woken with the answer, and nobody asked', async () => {
      const woken = await eventually(
        async () => {
          const state = await conversationNow(conversation);
          expect(state.assistant.some((reply) => reply.includes(wakeMark))).toBe(true);
          return state;
        },
        {
          timeoutMs: 60_000,
          intervalMs: 1_000,
          what: "a hidden wake turn carrying the partner's answer",
        },
      );
      // One reply for the turn the user took, one for the wake they did not.
      expect(woken.assistant).toHaveLength(2);
    });

    test('one exchange is one row, concluded in place', async () => {
      // The same delta the hand-back was counted against: one submission, one
      // row, still the row it started as.
      const rows = await rowsSince(known);
      expect(rows).toHaveLength(1);
      expect(rows[0].id).toBe(rowAtHandback.id);
      // The conclusion came from a collect, which is what makes it a
      // continuation of the same exchange rather than a second one.
      expect(rows[0].continuation_count).toBe(1);
    });
  });

  // -------------------------------------------------------------------------
  test.describe('work that finishes while the caller is still waiting', () => {
    let conversation = '';
    let known = new Set<string>();
    let atTurnEnd: DelegationRow[] = [];
    let quickMs = 0;

    test.beforeAll(async () => {
      if (!available) return;
      conversation = await startConversation(stamp('E2E 06 quick work'));
      const partnerMark = marker('06-quick-partner');
      const callerMark = marker('06-quick-caller');
      await script.arm(partnerMark, [{ text: 'answered without pausing' }]);
      await script.arm(callerMark, [
        { tool: '^delegate_to_', arguments: { message: `answer at once ${partnerMark}` }, regex: true },
        { text: 'the partner answered' },
      ]);

      known = await rowIds();
      quickMs = await say(conversation, `delegate the quick job ${callerMark}`);
      // The instant the turn ended, before any later mechanism can touch it.
      atTurnEnd = await rowsSince(known);
    });

    test('work that finishes inside the grace is reported in the calling turn', async () => {
      // SC-008's regression guard. A2A answers before the work is done, so
      // without a grace every delegation would cost the user two turns: the
      // discriminator is whether anything was still unconcluded when the
      // caller's own turn ended.
      expect(
        quickMs,
        'the quick turn cost more than a hand-back would have',
      ).toBeLessThan(HANDBACK_BUDGET_MS);
      expect(atTurnEnd.length).toBeGreaterThan(0);
      expect(
        atTurnEnd.every((row) => row.outcome === 'completed'),
        'the quick delegation was handed back instead of answered in the turn',
      ).toBe(true);
    });

    test('one exchange is one row, however fast the partner is', async () => {
      // FR-019 and SC-005: a delegation concludes exactly once. The attempt
      // holds the claim for as long as it is polling — the row is published
      // claimed and gives the claim back the moment the attempt stops — so a
      // callback arriving mid-poll loses, the inline poll concludes the one
      // row, and the answer reaches the caller once rather than inline AND as
      // a wake turn. A quick partner is what makes the race observable.
      const rows = await eventually(
        async () => {
          const found = await rowsSince(known);
          expect(
            found,
            `${found.length} rows for one quick exchange — the attempt holds ` +
              'the callback claim while it polls, so there is exactly one',
          ).toHaveLength(1);
          return found;
        },
        {
          timeoutMs: 30_000,
          intervalMs: 500,
          what: 'the single row one quick exchange produces',
        },
      );

      expect(rows[0].outcome).toBe('completed');
    });
  });

  // -------------------------------------------------------------------------
  test.describe('collecting a task on a later turn', () => {
    let conversation = '';
    let taskId = '';
    let rowId = '';
    let wakeMark = '';

    test.beforeAll(async () => {
      if (!available) return;
      conversation = await startConversation(stamp('E2E 06 collect'));
      const partnerMark = marker('06-collect-partner');
      const callerMark = marker('06-collect-caller');
      wakeMark = marker('06-collect-wake');
      await script.arm(partnerMark, [
        { tool: 'wait', arguments: { duration: LONG_WORK, reason: 'e2e collectable work' } },
        { text: `the collectable answer ${wakeMark}` },
      ]);
      await script.arm(callerMark, [
        { tool: '^delegate_to_', arguments: { message: `take your time ${partnerMark}` }, regex: true },
        { text: 'the partner has started' },
      ]);
      await script.arm(wakeMark, [{ text: `woken before collecting ${wakeMark}` }]);

      const knownTasks = await partnerTaskIds();
      await say(conversation, `delegate the collectable job ${callerMark}`);
      rowId = (await newestOutstandingRow()).id;
      taskId = await taskSubmittedSince(knownTasks);
    });

    test('a task this caller never started is refused before anything is sent', async () => {
      // The task id is the one field of a delegation the model supplies
      // verbatim, so the ownership check is what stops a run resuming — and
      // reading — work it never began.
      const strangerMark = marker('06-collect-stranger');
      const fabricated = crypto.randomUUID();
      await script.arm(strangerMark, [
        { tool: '^delegate_to_', arguments: { message: '', continue_task_id: fabricated }, regex: true },
        { text: 'there was nothing to collect' },
      ]);

      const tasksBefore = await partnerTaskIds();
      // Identified by diffing ids, not by taking the newest row: the block's
      // own outstanding delegation is still there, so a refusal not yet
      // written reads as that row and its null outcome.
      const known = await rowIds();
      await say(conversation, `collect task ${fabricated} now ${strangerMark}`);

      await eventually(
        async () => {
          const fresh = await rowsSince(known);
          expect(fresh.length).toBeGreaterThan(0);
          expect(fresh[0].outcome).toBe('refused_preflight');
          expect(fresh[0].partner_reason).toBe('continuation_not_owned');
        },
        {
          timeoutMs: 60_000,
          intervalMs: 1_000,
          what: 'the preflight refusal being recorded',
        },
      );
      // Refused HERE: the partner was never asked, so it has no new task.
      expect(await partnerTaskIds()).toEqual(tasksBefore);
    });

    test('a collect with an empty message reaches the partner and returns its result', async () => {
      // Collecting is the same tool with the id and nothing to say — an empty
      // message is what the tool boundary above will actually carry. Waiting
      // for the exchange to conclude first is deliberate: it is the only state
      // in which the partner has a result to hand over.
      await eventually(
        async () => {
          expect((await rowById(rowId)).outcome).toBe('completed');
        },
        {
          timeoutMs: 90_000,
          intervalMs: 1_000,
          what: 'the outstanding delegation reaching an outcome',
        },
      );
      // Concluding wakes the conversation, and a turn sent while that wake is
      // in flight is folded into it rather than run.
      await replyCarrying(conversation, wakeMark, 'the wake that follows the conclusion');
      await settled(conversation);

      const collectMark = marker('06-collect-later');
      await script.arm(collectMark, [
        { tool: '^delegate_to_', arguments: { message: '', continue_task_id: taskId }, regex: true },
        { text: 'collected' },
      ]);

      const known = await rowIds();
      await say(conversation, `collect task ${taskId} now ${collectMark}`);

      // Recorded as a fresh row rather than as a continuation of the first,
      // because the row the exchange started with is already concluded — the
      // same duplication the quick-work case produces, by a second route.
      const fresh = await rowsSince(known);
      expect(fresh).toHaveLength(1);
      expect(fresh[0].outcome).toBe('completed');
      expect(fresh[0].continuation_count).toBe(1);
      expect(fresh[0].mechanism).toBe('asked');
    });

    test('a collect carrying no message at all reaches the delegation', async () => {
      // FR-002 through the only surface a calling agent has. The tool's
      // description tells the model to call it "with the id and no message to
      // collect the result", and its schema requires nothing — so an argument
      // set of exactly {continue_task_id} is the advertised recovery path and
      // has to reach `delegate`. The contrast with the refusal above holds
      // either way: that one was refused before the partner was asked, this
      // one continues a task this caller owns.
      const collectMark = marker('06-collect-no-message');
      await script.arm(collectMark, [
        { tool: '^delegate_to_', arguments: { continue_task_id: taskId }, regex: true },
        { text: 'the collect was made' },
      ]);

      const known = await rowIds();
      await say(conversation, `collect the running task ${collectMark}`);

      // Continued in place while the first row is still outstanding, recorded
      // fresh once it has concluded — which of the two depends only on whether
      // the partner has finished, and neither is what this test is about. The
      // claim is that `delegate` was ENTERED: the defect recorded nothing at
      // all, where the refusal above recorded a row precisely because it got
      // that far.
      await eventually(
        async () => {
          const fresh = await rowsSince(known);
          const continued = fresh.length ? fresh[0] : await rowById(rowId);
          expect(continued.continuation_count).toBeGreaterThan(0);
        },
        {
          timeoutMs: 60_000,
          intervalMs: 1_000,
          what: 'the collect reaching the delegation',
        },
      );
    });
  });

  // -------------------------------------------------------------------------
  test.describe('stopping work at the partner', () => {
    let conversation = '';
    let elsewhere = '';
    let taskId = '';
    let rowId = '';

    test.beforeAll(async () => {
      if (!available) return;
      conversation = await startConversation(stamp('E2E 06 stop'));
      elsewhere = await startConversation(stamp('E2E 06 stop from elsewhere'));
      const partnerMark = marker('06-stop-partner');
      const callerMark = marker('06-stop-caller');
      await script.arm(partnerMark, [
        { tool: 'wait', arguments: { duration: WORK_TO_INTERRUPT, reason: 'e2e interruptible work' } },
        { text: 'the interruptible job is finished' },
      ]);
      await script.arm(callerMark, [
        { tool: '^delegate_to_', arguments: { message: `take your time ${partnerMark}` }, regex: true },
        { text: 'the partner has started' },
      ]);

      const knownTasks = await partnerTaskIds();
      await say(conversation, `delegate the interruptible job ${callerMark}`);
      rowId = (await newestOutstandingRow()).id;
      taskId = await taskSubmittedSince(knownTasks);
    });

    test('naming the task from another conversation stops nothing', async () => {
      // A task id is not a capability. `partner_task_id` narrows the
      // conversation's own outstanding work rather than selecting on its own,
      // so a second conversation holding the id reaches nothing — and is told
      // exactly what a finished task would tell it.
      const strangerMark = marker('06-stop-stranger');
      await script.arm(strangerMark, [
        { tool: '^cancel_task_at_', arguments: { task_id: taskId }, regex: true },
        { text: 'there was nothing to stop' },
      ]);

      await say(elsewhere, `stop task ${taskId} ${strangerMark}`);

      expect(await partnerTaskState(taskId)).toBe('working');
      expect((await rowById(rowId)).outcome).toBeNull();
    });

    test('asking the agent to stop reaches the partner', async () => {
      // The cancel is a tool the user asks for, not a control on screen: the
      // work outlives the turn, so the affordance has to outlive it too.
      const stopMark = marker('06-stop-asked');
      await script.arm(stopMark, [
        { tool: '^cancel_task_at_', arguments: { task_id: taskId }, regex: true },
        { text: 'the partner has been told to stop' },
      ]);

      await say(conversation, `please stop task ${taskId} ${stopMark}`);

      // A2A reports the cancel from `cancel_requested_at` the moment it is
      // stamped; the partner's run notices on its supervisor's next poll.
      expect(await partnerTaskState(taskId)).toBe('canceled');
    });

    test('the delegation that was stopped is recorded as cancelled', async () => {
      expect((await rowById(rowId)).outcome).toBe('cancelled');
    });
  });

  // -------------------------------------------------------------------------
  test.describe('abandoning a conversation that left work running', () => {
    let conversation = '';
    let taskId = '';
    let rowId = '';

    test.beforeAll(async () => {
      if (!available) return;
      conversation = await startConversation(stamp('E2E 06 abandon'));
      const partnerMark = marker('06-abandon-partner');
      const callerMark = marker('06-abandon-caller');
      await script.arm(partnerMark, [
        { tool: 'wait', arguments: { duration: WORK_TO_INTERRUPT, reason: 'e2e abandoned work' } },
        { text: 'the abandoned job is finished' },
      ]);
      await script.arm(callerMark, [
        { tool: '^delegate_to_', arguments: { message: `take your time ${partnerMark}` }, regex: true },
        { text: 'the partner has started' },
      ]);

      const knownTasks = await partnerTaskIds();
      await say(conversation, `delegate the job to abandon ${callerMark}`);
      rowId = (await newestOutstandingRow()).id;
      taskId = await taskSubmittedSince(knownTasks);
    });

    test('abandoning the conversation cancels the work it left at the partner', async () => {
      // The turn ending is not the boundary; abandoning the conversation is.
      // The cascade reaches partner tasks before its own early exit for
      // conversations with no local sub-agent work — which an ordinary A2A
      // conversation never has.
      await abandonConversation(conversation);

      // The cascade tells the partner first and records the local outcome
      // after (`cancel_outstanding_for`), committing once the loop ends: the
      // partner can read `canceled` while the row is still unconcluded. Both
      // states are the cascade's end, so both are waited on.
      await eventually(
        async () => {
          expect(await partnerTaskState(taskId)).toBe('canceled');
          expect((await rowById(rowId)).outcome).toBe('cancelled');
        },
        {
          timeoutMs: 30_000,
          intervalMs: 500,
          what: 'the cancel cascade reaching the partner and concluding the delegation',
        },
      );
    });
  });

  // -------------------------------------------------------------------------
  test.describe('the surfaces the callback mechanism exposes', () => {
    test('every refusal on the callback route looks the same', async () => {
      // An id nobody issued, an unparseable one and one already concluded are
      // three different facts and one answer, so a partner cannot learn which
      // delegations exist by asking. The concluded id is a real one this file
      // created, presented with a secret that was never its own.
      const concluded = (await delegationRows()).find((row) => row.outcome !== null);
      expect(concluded, 'no concluded delegation to address').toBeTruthy();

      for (const addressed of [crypto.randomUUID(), 'not-a-uuid', concluded?.id ?? '']) {
        const resp = await wire.post(`/a2a/callbacks/${addressed}`, {
          data: { taskId: 'whatever', state: 'completed', token: 'not the secret' },
        });
        expect(resp.status(), `callback to ${addressed}`).toBe(404);
        expect(await resp.json()).toEqual({ error: 'not found' });
      }
    });

    test("a callback address inside the deployment's own network is refused, task and all", async () => {
      // The destination guard runs BEFORE the task is written, and the send
      // handler turns its refusal into an invalid-params error. A mis-set
      // callback base URL does not degrade a partner to polling, it fails
      // every submission outright — and leaves nothing behind to retry past.
      const before = await lastActivityOnA();

      const metadata = await partner.send('work with a metadata callback', {
        pushNotificationConfig: { url: 'https://169.254.169.254/x' },
      });
      expect(metadata.body.error?.code).toBe(-32602);
      expect(metadata.body.error?.message).toContain('cloud metadata endpoint');

      // An unlisted loopback origin is refused on transport first — only the
      // sibling instance's exact origin is trusted here.
      const plain = await partner.send('work with a plain-http callback', {
        pushNotificationConfig: { url: 'http://localhost:9999/x' },
      });
      expect(plain.body.error?.code).toBe(-32602);
      expect(plain.body.error?.message).toContain('must use https');

      // Nothing was written. A task created and then refused would never run
      // — the spawn sits behind the same guard the refusal returns past — and
      // its message id would be claimed, so the corrected retry would dedupe
      // to the dead task and the partner could never submit that message.
      expect(
        await lastActivityOnA(),
        'a refused callback must leave no task behind',
      ).toBe(before);
    });

    test('the composer offers no way to stop work the turn left running', async ({ one }) => {
      // The composer has no Stop beside Send: the stop a hand-back tells the
      // agent to offer exists only while a turn is generating.
      const conversation = await startConversation(stamp('E2E 06 composer'));
      await one.page.goto(`/chat?c=${conversation}`);
      await expect(new ChatPage(one.page).sendButton()).toBeVisible();
      await expect(one.page.getByRole('button', { name: 'Stop generating' })).toHaveCount(0);
    });
  });
});
