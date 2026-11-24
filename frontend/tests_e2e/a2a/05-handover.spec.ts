/**
 * What happens when a tasked agent hands the work on.
 *
 * A partner addresses one card and validates one contract, and then the agent
 * behind that card gives the work to somebody else. The four ways it can do
 * that are not four spellings of one thing — each answers a different set of
 * questions, and the questions only have answers end to end:
 *
 *   consult            the peer advises, the addressed agent still answers,
 *                      and the artifact stays unattributed;
 *   transfer           the peer answers, the artifact says so, and the peer
 *                      inherits the conversation;
 *   mandatory_transfer the operator wired a pipeline stage, no model chose
 *                      anything, and nobody inherits the conversation;
 *   delegate           the work leaves the process entirely, the task stays
 *                      open on the child's behalf, and the child comes back
 *                      as an artifact of its own. This is the one group that
 *                      needs the worker, and it says so per test.
 *
 * Two facts hold across all four and are the reason they belong in one file.
 * A peer needs no inbound binding of its own — handover deliberately leaves
 * the partner's scope — which is a capability for a pipeline stage and a trap
 * for a chosen transfer, because responsibility recorded on an unreachable
 * agent refuses every follow-up afterwards. And every hop, in-process or over
 * the worker, runs as the exposure's run-as user rather than as the peer's
 * owner.
 *
 * Everything is asserted over raw JSON-RPC. No frontend file reads
 * `respondingAgent`, and neither does the outbound client, so driving a UI
 * could never prove attribution.
 *
 * mandatory_transfer is used wherever a branch only needs a hand-off to
 * happen, because it is the one type that fires with no tool call and no
 * model opinion. The scripted model is used where the branch is precisely
 * that the agent CHOSE — a consult, a transfer, a spawn.
 *
 * The file takes only the `model` fixture and keeps one admin context of its
 * own. Nothing here opens a page, and asking for `one` would build a browser
 * context per test for nothing.
 */

import { request, type APIRequestContext } from '@playwright/test';
import { test, expect } from './fixtures';
import { ADMIN_EMAIL, ADMIN_PASSWORD, SELF, WORKER_UP } from './instance';
import { ScriptedModel, marker, type Call } from '../scripted-llm/client';
import {
  PartnerClient,
  artifactsNamed,
  eventually,
  readState,
  type JsonRpcEnvelope,
} from './wire';
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

type Json = Record<string, unknown>;

/** The seeded pipeline the review script points at, and the name it gets wrong. */
const CHAIN_ENTRY_NAME = 'AEV Detection Rules Generator';
const CHAIN_ENTRY_SLUG = 'aev-detection-rules-generator';
const REVIEW_DOC_NAME = 'Remediation Malware Behavior Analyzer';

// ---------------------------------------------------------------------------
// Local helpers. The harness has no equivalents: this file never takes `one`,
// so it signs in to A itself, and the control-plane reads below are all one
// GET with a query string that carries the meaning.
// ---------------------------------------------------------------------------

/** An admin context, for a file that asks for no `one` fixture. */
async function signIn(): Promise<APIRequestContext> {
  if (!ADMIN_EMAIL || !ADMIN_PASSWORD) {
    throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set — the fixtures sign in with them.');
  }
  const ctx = await request.newContext({ baseURL: SELF.origin });
  try {
    const login = await ctx.post('/api/v1/auth/login', {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    if (!login.ok()) {
      throw new Error(`admin login failed: ${login.status()} ${await login.text()}`);
    }
    const token = (await login.json()).access_token as string;
    return await request.newContext({
      baseURL: SELF.origin,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    });
  } finally {
    await ctx.dispose();
  }
}

/** A list-returning control-plane read, with the path in the failure. */
async function getJson(rest: APIRequestContext, path: string): Promise<Json[]> {
  const resp = await rest.get(path);
  if (!resp.ok()) throw new Error(`GET ${path} failed: ${resp.status()} ${await resp.text()}`);
  return (await resp.json()) as Json[];
}

/** The JSON-RPC result, or an error naming the envelope a spec did not expect. */
function taskOf(sent: { body: JsonRpcEnvelope }, what: string): Json {
  if (sent.body.error) {
    throw new Error(`${what} was refused: ${sent.body.error.code} ${sent.body.error.message}`);
  }
  return sent.body.result as Json;
}

/**
 * The external run a submission created, found by the message id it carries.
 *
 * `create_task` stamps `trigger_payload.a2a_message_id`, which is the only
 * identifier shared between what the partner sent and what the control plane
 * lists — matching on "the newest run" instead would silently follow another
 * test's work the moment one of these files stops being serial. Polled
 * because the run is written by the request being answered, not before it.
 */
async function runForMessage(
  rest: APIRequestContext,
  integrationId: string,
  messageId: string,
): Promise<Json> {
  return eventually(
    async () => {
      const runs = await getJson(
        rest,
        `/api/v1/assignments/runs/all?a2a_integration_id=${integrationId}&limit=200`,
      );
      const run = runs.find(
        (r) => (r.trigger_payload as Json | null)?.a2a_message_id === messageId,
      );
      if (!run) throw new Error(`no external run yet carries message id ${messageId}`);
      return run;
    },
    { what: `the external run for ${messageId}`, timeoutMs: 30_000 },
  );
}

/** Every AGENT turn the scripted model served for one marker, in order. */
async function turnsFor(model: ScriptedModel, mark: string): Promise<Call[]> {
  return model.agentTurns(mark);
}

test.describe.serial('A2A handover from a tasked agent', () => {
  let available = false;
  let rest: APIRequestContext;
  const created: Created = tracker();

  let inboundId = '';
  let builtinInboundId = '';
  let runAsUserId = '';
  let adminUserId = '';

  // One credential for the lot: several exposed agents on one inbound
  // registration is the ordinary shape, and it keeps the token exchanges down.
  let credentials = { clientId: '', clientSecret: '' };

  let mtEntry = { id: '', name: '' };
  let mtPeer = { id: '', name: '' };
  let offEntry = { id: '', name: '' };
  let offPeer = { id: '', name: '' };
  let strandEntry = { id: '', name: '' };
  let strandPeer = { id: '', name: '' };
  let chainEntry = { id: '', name: '' };
  let chainMid = { id: '', name: '' };
  let chainTail = { id: '', name: '' };
  let consultEntry = { id: '', name: '' };
  let consultPeer = { id: '', name: '' };
  let consultStage = { id: '', name: '' };
  let delegEntry = { id: '', name: '' };
  let delegPeer = { id: '', name: '' };

  let mtClient: PartnerClient;
  let offClient: PartnerClient;
  let strandClient: PartnerClient;
  let chainClient: PartnerClient;
  let consultClient: PartnerClient;
  let delegClient: PartnerClient;

  /** The built-in agent bound in the exposure tests, so `afterAll` can let it go. */
  let boundBuiltinId = '';

  test.beforeAll(async () => {
    rest = await signIn();
    available = await a2aAvailable(rest);
    if (!available) return;

    // A previous file's scripts would otherwise still answer for a marker
    // this one never armed.
    const model = await ScriptedModel.connect();
    await model.reset();
    await model.dispose();

    adminUserId = (await (await rest.get('/api/v1/auth/me')).json()).id as string;

    const inbound = await createInboundIntegration(rest, created, stamp('E2E handover inbound'));
    inboundId = inbound.id;
    credentials = await mintCredential(rest, inboundId);

    // A second registration for the built-in exposure branch, so binding and
    // unbinding a product agent cannot disturb the rest of the file.
    const builtinInbound = await createInboundIntegration(
      rest,
      created,
      stamp('E2E handover built-in inbound'),
    );
    builtinInboundId = builtinInbound.id;

    // Peers first: a handover configuration names ids that must already exist.
    mtPeer = await createAgent(rest, created, {
      name: stamp('E2E pipeline stage'),
      persona: 'The single mandatory stage of a two-step pipeline.',
    });
    offPeer = await createAgent(rest, created, { name: stamp('E2E stage to disable') });
    strandPeer = await createAgent(rest, created, { name: stamp('E2E unbound transfer peer') });
    chainTail = await createAgent(rest, created, { name: stamp('E2E chain terminal stage') });
    chainMid = await createAgent(rest, created, { name: stamp('E2E chain middle agent') });
    consultStage = await createAgent(rest, created, { name: stamp('E2E consulted stage') });
    consultPeer = await createAgent(rest, created, { name: stamp('E2E consulted specialist') });
    // The delegate peer's script pauses with the `wait` primitive to keep its
    // child alive across several assertions. That primitive is offered only to
    // a run that already has at least one tool, so the default built-in is
    // named rather than left implicit.
    delegPeer = await createAgent(rest, created, {
      name: stamp('E2E background specialist'),
      extra: { builtin_tool_ids: ['inspect_my_traces'] },
    });

    mtEntry = await createAgent(rest, created, { name: stamp('E2E pipeline entry') });
    offEntry = await createAgent(rest, created, { name: stamp('E2E entry with a stage to disable') });
    strandEntry = await createAgent(rest, created, { name: stamp('E2E entry transferring outward') });
    chainEntry = await createAgent(rest, created, { name: stamp('E2E entry starting a chain') });
    consultEntry = await createAgent(rest, created, { name: stamp('E2E entry consulting a peer') });
    delegEntry = await createAgent(rest, created, {
      name: stamp('E2E entry delegating in background'),
      extra: { builtin_tool_ids: ['inspect_my_traces'] },
    });

    await setHandover(rest, mtEntry.id, [{ id: mtPeer.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, offEntry.id, [{ id: offPeer.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, strandEntry.id, [{ id: strandPeer.id, type: 'transfer' }]);
    await setHandover(rest, chainEntry.id, [{ id: chainMid.id, type: 'transfer' }]);
    await setHandover(rest, chainMid.id, [{ id: chainTail.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, consultEntry.id, [{ id: consultPeer.id, type: 'consult' }]);
    await setHandover(rest, consultPeer.id, [{ id: consultStage.id, type: 'mandatory_transfer' }]);
    await setHandover(rest, delegEntry.id, [{ id: delegPeer.id, type: 'delegate' }]);

    // Only the addressed agents are bound, plus the middle of the chain — the
    // one peer a follow-up has to reach. Every other peer stays unbound, which
    // is what several of these branches are about.
    const cards = await Promise.all([
      exposeAgent(rest, mtEntry.id, inboundId),
      exposeAgent(rest, offEntry.id, inboundId),
      exposeAgent(rest, strandEntry.id, inboundId),
      exposeAgent(rest, chainEntry.id, inboundId),
      exposeAgent(rest, consultEntry.id, inboundId),
      exposeAgent(rest, delegEntry.id, inboundId),
    ]);
    await bind(rest, chainMid.id, { oauth_a2a: [inboundId] });

    [mtClient, offClient, strandClient, chainClient, consultClient, delegClient] =
      await Promise.all(cards.map((card) => PartnerClient.connect(card, credentials)));

    runAsUserId = (await exposureOf(rest, mtEntry.id)).run_as_user_id ?? '';
  });

  test.afterAll(async () => {
    if (!rest) return;
    // A built-in is not in `created`, so the cleanup below never takes it
    // away; its binding has to go first or the registration it points at
    // cannot be deleted.
    if (boundBuiltinId) {
      await rest
        .put(`/api/v1/agents/${boundBuiltinId}`, { data: { integration_bindings: {} } })
        .catch(() => undefined);
    }
    await Promise.all(
      [mtClient, offClient, strandClient, chainClient, consultClient, delegClient]
        .filter(Boolean)
        .map((client) => client.dispose().catch(() => undefined)),
    );
    await cleanup(rest, created);
    await rest.dispose();
  });

  // -------------------------------------------------------------------------
  // mandatory_transfer — the hand-off nobody chooses
  // -------------------------------------------------------------------------

  const mtMark = marker('mandatory');
  let mtContextId = '';

  test('a mandatory transfer answers as its stage, with no tool call', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Two prose turns, no tool step anywhere: the hand-off is wired by the
    // operator and fires after the loop, so the script only supplies the words
    // each stage says. The marker is here so the turns can be told apart in
    // the next test, not to make the transfer happen.
    await model.arm(mtMark, [{ text: 'Entry stage complete.' }, { text: 'Stage answer.' }]);

    const messageId = `e2e-mt-${crypto.randomUUID()}`;
    const sent = await mtClient.send(`${mtMark} draft a detection rule`, { messageId });
    const submitted = taskOf(sent, 'the mandatory-transfer submission');
    mtContextId = submitted.contextId as string;

    const mtTask = await mtClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(mtTask)).toBe('completed');

    const artifacts = mtTask.artifacts as Json[];
    expect(artifacts.map((a) => a.name)).toEqual(['result']);
    const responding = (artifacts[0].metadata as Json).respondingAgent as Json;
    expect(responding.id).toBe(mtPeer.id);
    expect(responding.name).toBe(mtPeer.name);
  });

  test('a mandatory peer is never offered to the model', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const turns = await turnsFor(model, mtMark);
    expect(turns, 'the entry agent and its stage should have taken one turn each').toHaveLength(2);
    // Mandatory targets are removed from the peer list before tools and the
    // system prompt are built, so no peer-facing handover tool is generated at
    // all. `spawn_background_task` is deliberately not asserted on: self-
    // fan-out offers it to any agent for parallel copies of ITSELF, which says
    // nothing about this peer.
    expect(turns[0].tools_offered).not.toContain('transfer_to_agent');
    expect(turns[0].tools_offered).not.toContain('consult_agent');
  });

  test('the stage that answered is bound to no A2A registration', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Handover leaves the partner's scope deliberately: reaching this agent
    // was never something the partner was granted, and the operator's own
    // wiring is what got the work to it.
    const exposure = await exposureOf(rest, mtPeer.id);
    expect(exposure.exposed).toBe(false);
    expect(exposure.card_url).toBeFalsy();
  });

  test('a mandatory transfer leaves the entry agent answering the context', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const followMark = marker('mandatory-follow');
    await model.arm(followMark, [{ text: 'Entry again.' }, { text: 'Stage again.' }]);

    const messageId = `e2e-mt2-${crypto.randomUUID()}`;
    const sent = await mtClient.send(`${followMark} and now the second question`, {
      messageId,
      contextId: mtContextId,
    });
    // A pipeline stage is how one message gets answered, not a change of who
    // the partner is talking to — so the follow-up is answered rather than
    // refused with the unreachable-agent error a recorded stage would earn.
    const submitted = taskOf(sent, 'the follow-up under the mandatory context');
    const task = await mtClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(task)).toBe('completed');

    const run = await runForMessage(rest, inboundId, messageId);
    expect(run.agent_id, 'the entry agent, not its stage, should still be answering').toBe(
      mtEntry.id,
    );
  });

  // -------------------------------------------------------------------------
  // What a peer is NOT given
  // -------------------------------------------------------------------------

  const askMark = marker('askcaller');

  test('a peer that tries to ask the caller finishes the task instead', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Turn one is the addressed agent's and simply hands on; turn two is the
    // stage's, and it reaches for the tool that would park the task on its
    // caller.
    await model.arm(askMark, [
      { text: 'Entry hands on.' },
      { tool: 'ask_the_caller', arguments: { question: 'Which environment?' } },
    ]);

    const messageId = `e2e-ask-${crypto.randomUUID()}`;
    const sent = await mtClient.send(`${askMark} something under-specified`, { messageId });
    const submitted = taskOf(sent, 'the clarification submission');
    const task = await mtClient.waitForState(
      submitted.id as string,
      ['completed', 'failed', 'input-required'],
      { timeoutMs: 90_000 },
    );
    // Nowhere for the stage to put its question, so it answers with what it
    // has and the partner is never asked anything.
    expect(readState(task)).toBe('completed');
  });

  test('only the addressed agent is offered the caller-question tool', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const turns = await turnsFor(model, askMark);
    expect(turns).toHaveLength(2);
    // The tool is threaded in from exactly one call site, the external-task
    // executor's own top-level run. The recursive call that runs a handover
    // peer passes nowhere to put a question, so one hop down the feature is
    // simply absent.
    expect(turns[0].tools_offered).toContain('ask_the_caller');
    expect(turns[1].tools_offered).not.toContain('ask_the_caller');
  });

  test('a disabled peer disappears and the addressed agent answers alone', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const disabled = await rest.put(`/api/v1/agents/${offPeer.id}`, { data: { enabled: false } });
    expect(disabled.ok(), `disabling the peer: ${disabled.status()}`).toBeTruthy();

    const messageId = `e2e-off-${crypto.randomUUID()}`;
    const sent = await offClient.send('anything at all', { messageId });
    const submitted = taskOf(sent, 'the submission to the entry with a disabled stage');
    const task = await offClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(task)).toBe('completed');

    // The peer is filtered out before the mandatory targets are extracted, so
    // there is no hand-off to attribute and no delegation to record — the
    // partner simply gets the addressed agent's own answer.
    const artifacts = task.artifacts as Json[];
    expect(artifacts[0].metadata).toBeUndefined();
    const rows = await getJson(rest, `/api/v1/chat/delegations?target_agent_id=${offPeer.id}`);
    expect(rows).toHaveLength(0);
  });

  // -------------------------------------------------------------------------
  // transfer — the hand-off a model chooses
  // -------------------------------------------------------------------------

  let strandContextId = '';

  test('a chosen transfer attributes the result to the peer that answered', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const strandMark = marker('strand');
    await model.arm(strandMark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: strandPeer.id, reason: 'the peer owns this' } },
      { text: 'Answer from the transferred-to peer.' },
    ]);

    const messageId = `e2e-strand-${crypto.randomUUID()}`;
    const sent = await strandClient.send(`${strandMark} hand this to the specialist`, { messageId });
    const submitted = taskOf(sent, 'the transfer submission');
    strandContextId = submitted.contextId as string;

    const task = await strandClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(task)).toBe('completed');

    const artifacts = task.artifacts as Json[];
    expect(artifacts[0].name).toBe('result');
    const responding = (artifacts[0].metadata as Json).respondingAgent as Json;
    expect(responding.id).toBe(strandPeer.id);
    expect(responding.name).toBe(strandPeer.name);
  });

  test('a transfer to a peer this partner cannot reach leaves the addressed agent in charge', async ({
    model,
  }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The sharp edge of "a peer needs no binding". The transfer happened and
    // was attributed to the peer, but handover spans every configured peer
    // while responsibility may only move to one this registration binds.
    // Recorded otherwise, an ordinary model-chosen transfer ends the
    // conversation: every follow-up refused forever as "no longer available",
    // for a hand-off the partner was never able to see.
    const followMark = marker('strand-follow');
    await model.arm(followMark, [{ text: 'The addressed agent answers the follow-up.' }]);

    const sent = await strandClient.send(`${followMark} and the follow-up`, {
      contextId: strandContextId,
    });
    expect(sent.body.error, 'the conversation must survive the transfer').toBeUndefined();
    const submitted = taskOf(sent, 'the follow-up under the unbound-transfer context');
    const task = await strandClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(task)).toBe('completed');
    // Answered by the agent the partner addressed, so there is no hand-off on
    // this run to attribute.
    expect((task.artifacts as Json[])[0].metadata).toBeUndefined();
  });

  let chainContextId = '';

  test('a chained transfer attributes the innermost agent that ran', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const chainMark = marker('chain');
    // Entry chooses to transfer to the middle agent; the middle agent's own
    // pipeline then fires on its own. Three turns, one per agent.
    await model.arm(chainMark, [
      { tool: 'transfer_to_agent', arguments: { agent_id: chainMid.id, reason: 'routing onward' } },
      { text: 'Middle agent output.' },
      { text: 'Terminal stage output.' },
    ]);

    const messageId = `e2e-chain-${crypto.randomUUID()}`;
    const sent = await chainClient.send(`${chainMark} run the whole chain`, { messageId });
    const submitted = taskOf(sent, 'the chained-transfer submission');
    chainContextId = submitted.contextId as string;

    const task = await chainClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 120_000,
    });
    expect(readState(task)).toBe('completed');

    const responding = ((task.artifacts as Json[])[0].metadata as Json).respondingAgent as Json;
    expect(responding.id, 'the last agent to run wins, not the first hop').toBe(chainTail.id);
    expect(responding.name).toBe(chainTail.name);
  });

  test('a chained transfer leaves the chosen hop, not its pipeline, in charge', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const followMark = marker('chain-follow');
    await model.arm(followMark, [{ text: 'Middle again.' }, { text: 'Terminal again.' }]);

    const messageId = `e2e-chain2-${crypto.randomUUID()}`;
    const sent = await chainClient.send(`${followMark} the next question`, {
      messageId,
      contextId: chainContextId,
    });
    const submitted = taskOf(sent, 'the follow-up under the chained context');
    await chainClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 120_000,
    });

    // "Who answered" and "who answers next" genuinely diverge here: the
    // artifact named the terminal stage, and the conversation stayed with the
    // agent the model chose to transfer to.
    const run = await runForMessage(rest, inboundId, messageId);
    expect(run.agent_id).toBe(chainMid.id);
  });

  // -------------------------------------------------------------------------
  // consult — advice, not attribution
  // -------------------------------------------------------------------------

  let consultRunId = '';

  test('a consulted answer stays the answer of the agent that was addressed', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const consultMark = marker('consult');
    const innerMark = marker('consult-inner');
    await model.arm(consultMark, [
      {
        tool: 'consult_agent',
        arguments: { agent_id: consultPeer.id, question: `${innerMark} which rule type applies?` },
      },
      { text: 'Answer written by the addressed agent, informed by the advice.' },
    ]);
    // The peer's turn is keyed on a marker carried in the question, because a
    // consult sends its own prompt rather than forwarding the partner's.
    await model.arm(innerMark, [{ text: 'The advice.' }]);

    const messageId = `e2e-consult-${crypto.randomUUID()}`;
    const sent = await consultClient.send(`${consultMark} ask the specialist first`, { messageId });
    const submitted = taskOf(sent, 'the consult submission');
    const task = await consultClient.waitForState(submitted.id as string, ['completed', 'failed'], {
      timeoutMs: 90_000,
    });
    expect(readState(task)).toBe('completed');

    // A consult never sets the responding agent, so the artifact carries no
    // metadata at all — the partner reads the answer as the agent's whose card
    // it validated against, which is exactly what happened.
    const artifacts = task.artifacts as Json[];
    expect(artifacts.map((a) => a.name)).toEqual(['result']);
    expect(artifacts[0].metadata).toBeUndefined();

    consultRunId = (await runForMessage(rest, inboundId, messageId)).id as string;
  });

  test('a consult is recorded against the addressed agent', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const rows = await getJson(
      rest,
      `/api/v1/chat/delegations?parent_agent_id=${consultEntry.id}&type=consult`,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].target_agent_id).toBe(consultPeer.id);
  });

  test('a consulted peer cannot hand on to its own pipeline', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // The consult call site passes handover off, and the mandatory extraction
    // reads the same peer list — so a consulted agent that is itself a
    // pipeline entry answers with its own output and its stage never runs.
    const rows = await getJson(
      rest,
      `/api/v1/chat/delegations?parent_agent_id=${consultPeer.id}`,
    );
    expect(rows).toHaveLength(0);
    // The control, so the zero above cannot be a delegation trail that never
    // records anything: the middle of the chain was reached by transfer, which
    // does pass handover on, and its own mandatory stage did fire.
    const transferred = await getJson(
      rest,
      `/api/v1/chat/delegations?parent_agent_id=${chainMid.id}&type=transfer`,
    );
    expect(transferred.length).toBeGreaterThan(0);
  });

  test('every hop of a handover runs as the exposure run-as user', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    expect(runAsUserId, 'the exposure should record a run-as user').toBe(adminUserId);

    // `ownership=mine` filters on the run's own user id, so the external run
    // and the consult it nested both appearing under it is the assertion: the
    // identity is the exposure's, never the peer's owner, and it does not
    // change on the way down.
    const external = await getJson(
      rest,
      `/api/v1/assignments/runs/all?a2a_integration_id=${inboundId}&ownership=mine&limit=200`,
    );
    expect(external.some((r) => r.id === consultRunId)).toBe(true);

    const children = await getJson(
      rest,
      `/api/v1/assignments/runs/all?parent_run_id=${consultRunId}&ownership=mine&limit=200`,
    );
    const consultChild = children.find(
      (c) => (c.trigger_payload as Json | null)?.delegation_type === 'consult',
    );
    expect(consultChild, 'the consult should have left a child run of the external run').toBeTruthy();
    expect(consultChild?.agent_id).toBe(consultPeer.id);
  });

  // -------------------------------------------------------------------------
  // delegate — work that outlives the reply, and needs the worker
  //
  // An inbound submission runs in the API process, but a delegate hop writes a
  // `sub_agent` orchestration execution and hands it to the reconciler, which
  // lives in the worker. Without one, every branch below sits
  // at `pending` and times out on a state that never arrives — so each one
  // gates itself rather than the group, because a serial group whose first
  // test is skipped still runs the rest.
  // -------------------------------------------------------------------------

  let delegTaskId = '';
  let delegRunId = '';

  test('a delegate hop leaves a child for the worker to pick up', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    const delegMark = marker('delegate');
    const childMark = marker('delegate-child');
    await model.arm(delegMark, [
      {
        tool: 'spawn_background_task',
        arguments: { agent_id: delegPeer.id, task: `${childMark} produce the long answer` },
      },
      { text: 'Handed the work to a background specialist.' },
      // A third turn, because the agent loop does not end the turn on that
      // answer: it parks the run on the live child and asks again once the
      // child is terminal.
      { text: 'Reporting after the background work.' },
    ]);
    // The child pauses deliberately, so the assertion below reads a child that
    // has not finished rather than racing one that has. If the wait primitive
    // were ever not offered, the scripted model answers `SCRIPT-ERROR: no
    // offered tool matches 'wait'` and it surfaces in the delegated artifact.
    await model.arm(childMark, [
      { tool: 'wait', arguments: { duration: '20s', reason: 'hold the delegated work open' } },
      { text: 'Delegated child answer.' },
    ]);

    const messageId = `e2e-deleg-${crypto.randomUUID()}`;
    const sent = await delegClient.send(`${delegMark} this will take a while`, { messageId });
    delegTaskId = taskOf(sent, 'the delegate submission').id as string;
    delegRunId = (await runForMessage(rest, inboundId, messageId)).id as string;

    const child = await eventually(
      async () => {
        const rows = await getJson(rest, `/api/v1/chat/sub-agent-tasks?agent_id=${delegPeer.id}&limit=200`);
        const row = rows.find((r) => r.parent_run_id === delegRunId);
        if (!row) throw new Error('no background task is linked to the external run yet');
        return row;
      },
      { what: 'the delegated child row', timeoutMs: 60_000 },
    );
    expect(['pending', 'running']).toContain(child.status);
    // An external run carries no conversation, so nothing parks or wakes a
    // chat on the child's behalf — polling the task is the partner's only
    // channel, which is what the rest of this group is about.
    expect(child.conversation_id).toBeNull();
  });

  test('a delegating run stays open until its child finishes', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    // Worth stating, because the projection is built for the opposite case:
    // `resolve_state` folds live children in so a FINISHED run with a running
    // child still reports `working`. On this path the run does not finish
    // first — the agent loop parks it on the live child and only then asks the
    // model again — so the task is `working` because its run is, and the
    // partner cannot tell the two apart. The projection's own branch needs an
    // exhausted wait budget, which no submission can reach from outside.
    const { body } = await delegClient.getTask(delegTaskId);
    expect(readState(body.result as Json)).toBe('working');
    expect((body.result as Json).artifacts).toEqual([]);

    const run = (await getJson(
      rest,
      `/api/v1/assignments/runs/all?a2a_integration_id=${inboundId}&limit=200`,
    )).find((r) => r.id === delegRunId);
    expect(['pending', 'running'], 'the run should not have finished ahead of its child').toContain(
      run?.status,
    );
  });

  test('a finished child arrives as its own delegated-result artifact', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    const task = await delegClient.waitForState(delegTaskId, ['completed', 'failed'], {
      timeoutMs: 120_000,
    });
    expect(readState(task)).toBe('completed');

    // Order matters: the run's own answer first, then one artifact per
    // finished child, so a reader can tell the pipeline's stages apart.
    const artifacts = task.artifacts as Json[];
    expect(artifacts.map((a) => a.name)).toEqual(['result', 'delegated-result']);

    const delegated = artifactsNamed(task, 'delegated-result')[0];
    const part = (delegated.parts as Json[])[0];
    // Always text, whatever the child agent's output format.
    expect(part.kind).toBe('text');
    expect(part.text).toContain('Delegated child answer.');
    const responding = (delegated.metadata as Json).respondingAgent as Json;
    expect(responding.id).toBe(delegPeer.id);
    expect(responding.name).toBe(delegPeer.name);
  });

  test('the delegated child carries the run-as identity across the process boundary', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    // The consult and transfer hops keep the identity in one process; a
    // delegate hands it to the worker in the execution payload, which is the
    // only hop where it could be lost.
    const rows = await getJson(rest, `/api/v1/chat/sub-agent-tasks?agent_id=${delegPeer.id}&limit=200`);
    const child = rows.find((r) => r.parent_run_id === delegRunId);
    expect(child?.status).toBe('completed');
    expect(child?.user_id).toBe(runAsUserId);
  });

  test('cancelling a running task cancels the work it delegated', async ({ model }) => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    // Gated even though the cascade itself runs in the API process: with no
    // worker the child never leaves `pending`, so cancelling it would say
    // nothing about cancelling work that had actually started.
    test.skip(!WORKER_UP, 'a delegated child needs the worker to run it');
    const cancelMark = marker('delegate-cancel');
    const cancelChildMark = marker('delegate-cancel-child');
    // The entry pauses after spawning, so the run is still active when the
    // cancel lands — which is the case where the cascade actually runs.
    await model.arm(cancelMark, [
      {
        tool: 'spawn_background_task',
        arguments: { agent_id: delegPeer.id, task: `${cancelChildMark} slow work` },
      },
      { tool: 'wait', arguments: { duration: '40s', reason: 'stay live while the child starts' } },
      { text: 'Never reached.' },
    ]);
    await model.arm(cancelChildMark, [
      { tool: 'wait', arguments: { duration: '60s', reason: 'still going when the cancel lands' } },
      { text: 'Never delivered.' },
    ]);

    const messageId = `e2e-deleg-cancel-${crypto.randomUUID()}`;
    const sent = await delegClient.send(`${cancelMark} start something long`, { messageId });
    const taskId = taskOf(sent, 'the cancellable delegate submission').id as string;
    const runId = (await runForMessage(rest, inboundId, messageId)).id as string;

    await eventually(
      async () => {
        const rows = await getJson(rest, `/api/v1/chat/sub-agent-tasks?agent_id=${delegPeer.id}&limit=200`);
        const row = rows.find((r) => r.parent_run_id === runId);
        if (!row) throw new Error('the child has not been spawned yet');
        return row;
      },
      { what: 'the child of the cancellable task', timeoutMs: 60_000 },
    );

    const { body } = await delegClient.cancelTask(taskId);
    expect(body.error, `cancel refused: ${JSON.stringify(body.error)}`).toBeUndefined();
    expect(readState(body.result)).toBe('canceled');

    // The platform spells a cancelled background task with two Ls; the wire
    // spells the task's state with one. Both appear here on purpose.
    const child = await eventually(
      async () => {
        const rows = await getJson(rest, `/api/v1/chat/sub-agent-tasks?agent_id=${delegPeer.id}&limit=200`);
        const row = rows.find((r) => r.parent_run_id === runId);
        if (row?.status !== 'cancelled') {
          throw new Error(`the child is ${String(row?.status)}, not cancelled`);
        }
        return row;
      },
      { what: 'the cancellation reaching the child', timeoutMs: 30_000 },
    );
    // The background task says who stopped it: the partner, not a deletion
    // (`tasks.PARTNER_CANCEL_TASK_REASON`).
    expect(child.error).toBe(
      'Cancelled because the A2A partner that submitted its run cancelled the task.',
    );
  });

  // -------------------------------------------------------------------------
  // The seeded pipeline an operator is told to expose
  // -------------------------------------------------------------------------

  let chainBuiltin: Json | undefined;

  test('the seeded chain entry is the agent the product ships, not the one the doc names', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const agents = await getJson(rest, '/api/v1/agents');
    chainBuiltin = agents.find((a) => a.slug === CHAIN_ENTRY_SLUG);
    expect(chainBuiltin, `no agent with slug ${CHAIN_ENTRY_SLUG}`).toBeTruthy();
    expect(chainBuiltin?.name).toBe(CHAIN_ENTRY_NAME);
    // a2a-review-scenarios.md §4 tells a reviewer to expose this one; no such
    // agent exists anywhere in the product.
    expect(agents.some((a) => a.name === REVIEW_DOC_NAME)).toBe(false);
  });

  test('the handover wiring of a built-in cannot be retyped', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const agentId = chainBuiltin?.id as string;
    const resp = await rest.put(`/api/v1/agents/${agentId}`, {
      data: { handover_types: {} },
    });
    // Handover wiring is mechanics: rewritten from seed data on every startup,
    // which is what makes the chain something an e2e may rely on (#914).
    expect(resp.status()).toBe(403);
    expect((await resp.json()).detail).toMatch(/^Built-in agents cannot be modified/);
  });

  test('a built-in can still be exposed, but only by a company-managed registration', async () => {
    test.skip(!available, 'A2A is not licensed on this deployment');
    const agentId = chainBuiltin?.id as string;

    // Every built-in is company-managed, and a company-managed agent may only
    // bind a company-managed integration — so the ordinary registration the
    // rest of this file uses is refused, by name, before anything is exposed.
    const refused = await rest.put(`/api/v1/agents/${agentId}`, {
      data: { integration_bindings: { oauth_a2a: [builtinInboundId] } },
    });
    expect(refused.status()).toBe(400);
    expect((await refused.json()).detail).toContain(
      'Company-managed agents can only use company-managed integrations.',
    );

    // The narrow carve-out the refusal leaves open: an integration is the
    // deployment's own account rather than part of the seeded definition, so a
    // built-in whose every other field is rewritten from seed data on each
    // startup can still be exposed — and for A2A the binding IS the opt-in.
    const companyManaged = await createInboundIntegration(
      rest,
      created,
      stamp('E2E handover built-in company inbound'),
      { companyManaged: true },
    );
    boundBuiltinId = agentId;
    const cardUrl = await exposeAgent(rest, agentId, companyManaged.id);
    expect(cardUrl).toContain('/.well-known/agent-card.json');
  });
});
