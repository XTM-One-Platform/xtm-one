/**
 * A cancel stops the work, not just the run that received it.
 *
 * `tasks/cancel` reports `canceled` off `cancel_requested_at`, which is
 * stamped synchronously — so the reported state is true the moment it is
 * asked for, whatever the work is doing. That is the gap this spec exists to
 * close: dev-docs/a2a-review-scenarios.md §8 says to "check the child actually
 * stopped rather than merely being reported so", and every other cancel test
 * in this suite stops at the report.
 *
 * The shape is the one seen in a live trace: the exposed agent answers, a
 * mandatory transfer hands the turn to a CHILD RUN, and that child is where
 * the remaining work happens. A child run is neither a `SubAgentTask` nor an
 * `A2ADelegation`, so it is reached by neither of the cascade's other arms; it
 * runs inside its parent's turn, so it is stopped by the cancel event or by
 * nothing.
 *
 * The child's own clock is not accelerated. `_CANCEL_POLL_SECONDS` is a fixed
 * 15s on purpose — it bounds how long a user who says stop keeps waiting,
 * which is not something the lease measures — so the assertions below allow
 * for one poll plus the turn around it rather than for a scaled bound.
 */

import { request, type APIRequestContext } from '@playwright/test';
import { expect, test } from './fixtures';
import { ADMIN_EMAIL, ADMIN_PASSWORD, SELF } from './instance';
import {
  a2aAvailable,
  bind,
  createAgent,
  createInboundIntegration,
  exposeAgent,
  mintCredential,
  registerPartner,
  setHandover,
  stamp,
  tracker,
} from './seed';
import { PartnerClient, eventually, readState, sleep } from './wire';
import { ScriptedModel, marker } from '../scripted-llm/client';

/**
 * Long enough that the child is still working when the cancel lands AND for a
 * supervisor poll after it. Shorter than this and a passing test would only
 * prove the child finished on its own.
 */
const CHILD_WORK = '120s';

/** One cancel poll, plus the turn around it. */
const STOP_BUDGET_MS = 45_000;

interface AuditEntry {
  entity_id: string | null;
  details: Record<string, unknown> | null;
}

let rest: APIRequestContext;
let partner: PartnerClient;
let script: ScriptedModel;
let credentials: { clientId: string; clientSecret: string };
let caller: { id: string };
let available = false;

const created = tracker();

async function signIn() {
  const anonymous = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await anonymous.post('/api/v1/auth/login', {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    if (!login.ok()) {
      throw new Error(
        `Admin login failed: ${login.status()} ${await login.text()}`,
      );
    }
    const token = ((await login.json()) as { access_token: string }).access_token;
    return request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    });
  } finally {
    await anonymous.dispose();
  }
}

async function partnerTaskIds(): Promise<string[]> {
  const resp = await rest.get('/api/v1/audit-logs', {
    params: { entity_type: 'a2a_task', action: 'trigger', limit: 200 },
  });
  if (!resp.ok()) throw new Error(`audit read failed: ${resp.status()}`);
  const page = (await resp.json()) as { items: AuditEntry[] };
  return page.items
    .filter((item) => String((item.details ?? {}).client_id) === credentials.clientId)
    .map((item) => item.entity_id)
    .filter((id): id is string => Boolean(id));
}

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

async function partnerTaskState(taskId: string): Promise<string> {
  const { body } = await partner.getTask(taskId);
  return readState(body.result) ?? `no state — error ${body.error?.code}`;
}

/** Every run A has for this agent, newest first. */
async function runsFor(agentId: string): Promise<
  {
    id: string;
    status: string;
    parent_run_id: string | null;
    started_at: string;
    result_summary: string | null;
  }[]
> {
  // `parent_run_id=""` is the endpoint's "no filter" — its default of "none"
  // means top-level runs only, which is exactly the set a transfer child is
  // not in.
  const resp = await rest.get('/api/v1/assignments/runs/all', {
    params: { agent_id: agentId, parent_run_id: '', limit: 200 },
  });
  if (!resp.ok()) throw new Error(`runs read failed: ${resp.status()} ${await resp.text()}`);
  return (await resp.json()) as {
    id: string;
    status: string;
    parent_run_id: string | null;
    started_at: string;
    result_summary: string | null;
  }[];
}

test.describe.serial('a cancel reaches the run a transfer handed the turn to', () => {
  let peerId = '';
  let taskId = '';
  let cancelledAt = 0;
  let conversation = '';
  // Shared: the cancel test reads this queue to prove the child's wait was
  // cut short rather than trusting the row the cascade wrote.
  const childMark = marker('12-child-works');

  test.beforeAll(async () => {
    rest = await signIn();
    available = await a2aAvailable(rest);
    if (!available) return;

    script = await ScriptedModel.connect();
    await script.reset();

    const inbound = await createInboundIntegration(rest, created, stamp('E2E 12 inbound'));
    credentials = await mintCredential(rest, inbound.id);

    // The peer is where the work actually happens, and where a cancel has to
    // land. It is reached by a mandatory transfer so no model chooses it.
    const peer = await createAgent(rest, created, {
      name: stamp('E2E 12 peer that works on'),
      persona: 'Does the long half of the job.',
    });
    peerId = peer.id;
    const entry = await createAgent(rest, created, {
      name: stamp('E2E 12 entry that hands on'),
      persona: 'Answers, then hands the work to its peer.',
    });
    await setHandover(rest, entry.id, [{ id: peer.id, type: 'mandatory_transfer' }]);

    const cardUrl = await exposeAgent(rest, entry.id, inbound.id);
    partner = await PartnerClient.connect(cardUrl, credentials);

    const outbound = await registerPartner(rest, created, {
      cardUrls: cardUrl,
      tokenUrl: `${SELF.origin}/mcp/oauth/token`,
      clientId: credentials.clientId,
      clientSecret: credentials.clientSecret,
      name: stamp('E2E 12 partner'),
    });
    caller = await createAgent(rest, created, {
      name: stamp('E2E 12 caller'),
      persona: 'Sends work to the partner and stops it when asked.',
    });
    await bind(rest, caller.id, { oauth_a2a: [outbound.id] });
  });

  test.afterAll(async () => {
    if (available) {
      await partner.dispose();
      await script.dispose();
    }
    await rest?.dispose();
  });

  test.beforeEach(() => {
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  test('the transfer child is running while the task is', async () => {
    const callerMark = marker('12-caller');
    // One queue, served a step per model completion, so it spans both agents
    // at the partner: the entry agent answers and the mandatory transfer carries the
    // turn onward with no tool call, then the peer waits. Arming the wait
    // first would have the ENTRY agent do it and no transfer would happen.
    await script.arm(childMark, [
      { text: 'entry stage complete, handing on' },
      { tool: 'wait', arguments: { duration: CHILD_WORK, reason: 'e2e child work' } },
      { text: 'the child finished on its own' },
    ]);
    await script.arm(callerMark, [
      { tool: '^delegate_to_', arguments: { message: `hand this on ${childMark}` }, regex: true },
      { text: 'the partner has started' },
    ]);

    const known = await partnerTaskIds();
    conversation = await (async () => {
      const resp = await rest.post('/api/v1/chat/conversations', {
        data: { agent_id: caller.id, title: stamp('E2E 12 cancel cascade') },
      });
      return ((await resp.json()) as { id: string }).id;
    })();
    await rest.post(`/api/v1/chat/conversations/${conversation}/messages`, {
      data: { content: `delegate the two-stage job ${callerMark}` },
      timeout: 150_000,
    });
    taskId = await taskSubmittedSince(known);

    // The child run is the proof the transfer happened at all. Without one
    // there is no cascade to test and a later pass would mean nothing.
    const child = await eventually(
      async () => {
        const runs = await runsFor(peerId);
        const live = runs.filter((r) => r.status === 'running');
        expect(live, 'the mandatory transfer created no child run').not.toHaveLength(0);
        return live[0];
      },
      { timeoutMs: 60_000, what: "the partner's transfer child run" },
    );
    expect(child.parent_run_id, 'the child is not parented to the task run').toBeTruthy();
    expect(await partnerTaskState(taskId)).toBe('working');
  });

  test('asking the partner to stop it cancels the child, not just the parent', async () => {
    const stopMark = marker('12-stop');
    await script.arm(stopMark, [
      { tool: '^cancel_task_at_', arguments: {}, regex: true },
      { text: 'the partner has been told to stop' },
    ]);

    cancelledAt = Date.now();
    await rest.post(`/api/v1/chat/conversations/${conversation}/messages`, {
      data: { content: `stop that work please ${stopMark}` },
      timeout: 150_000,
    });

    // Reported at once, from the synchronous stamp.
    expect(await partnerTaskState(taskId)).toBe('canceled');

    // Leaving `running` proves nothing on its own — the cascade writes the
    // row synchronously, so it says "stopped" while the loop is still inside
    // its `wait`. What the row settles to, and whether the child's closing
    // prose was ever served, is the next test's business.
    await eventually(
      async () => {
        const runs = await runsFor(peerId);
        const live = runs.filter((r) => r.status === 'running');
        expect(live, 'the child never left running').toHaveLength(0);
      },
      { timeoutMs: STOP_BUDGET_MS, what: 'the transfer child leaving running' },
    );
  });

  test('the child stopped, and its run says so', async () => {
    // Read once the child has actually EXITED, not while it is stopping. The
    // child writes its own `result_summary` on the way out; the cascade never
    // does, so a non-empty one is the signal that its turn is over and every
    // write it will make has been made.
    const child = await eventually(
      async () => {
        const runs = await runsFor(peerId);
        const settled = runs.filter((r) => r.parent_run_id && r.result_summary);
        expect(settled, 'the child has not finished exiting').toHaveLength(1);
        return settled[0];
      },
      { timeoutMs: STOP_BUDGET_MS, what: 'the transfer child exiting' },
    );

    // The child finalises its own run on the way out. Writing "completed"
    // there erases what the cascade recorded and reports stopped work as
    // finished — a row that contradicts its own summary.
    expect(child.status, 'the child rewrote its cancellation as completion').toBe(
      'cancelled',
    );

    // And it really was cut short. The closing prose is the third step in the
    // queue and is served only if the `wait` ran to term, so a count below it
    // is the one fact the row cannot fake.
    expect(
      await script.turnsServed(childMark),
      'the child served its closing prose, so its wait ran to term',
    ).toBeLessThan(3);
  });

  test('and nothing new starts after the cancel', async () => {
    // The observed failure did not only leave a child running — the child
    // transferred again of its own accord, minutes after the cancel.
    await sleep(20_000);
    const runs = await runsFor(peerId);
    const late = runs.filter((r) => Date.parse(r.started_at) > cancelledAt + 2_000);
    expect(late, 'a run started after the cancel').toHaveLength(0);
  });
});
