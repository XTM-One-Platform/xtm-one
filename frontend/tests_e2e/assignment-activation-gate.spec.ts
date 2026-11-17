import { test, expect } from '@playwright/test';
import { createScriptedAgent, deleteAgent, ok, scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';

/**
 * The activation gate decides whether an assignment runs, so the subject is
 * what the product does with the answer, not the answer. Gate and body carry
 * separate markers because they are separate USER messages. Needs a worker.
 */

/**
 * How many calls the server matched to `mark`. The gate is a plain `llm.chat`
 * — no stream, no tools — which the server counts as skipped rather than
 * served, so `turnsServed` cannot see it. The agent loop does stream, so its
 * marker is served as well as matched.
 */
async function callsMatching(scripted: ScriptedModel, mark: string): Promise<number> {
  const { calls } = await scripted.calls();
  return calls.filter((call) => call.matched === mark).length;
}

/** `execute_assignment_run`'s terminal statuses. */
const TERMINAL = ['completed', 'failed', 'cancelled', 'skipped'];

interface Run {
  id: string;
  status: string;
  result_summary: string | null;
}

test.describe.serial('Assignment activation gate', () => {
  // The core budget is 60s; the wait below covers worker queueing plus two
  // model calls. Scoped here so no other core spec gets a longer leash.
  test.describe.configure({ timeout: 180_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';
  let agentId = '';
  const created: string[] = [];

  async function makeAssignment(gateMark: string, bodyMark: string): Promise<string> {
    return createAssignment({
      name: `E2E gate ${gateMark}`,
      activation_prompt: `Should this run? ${gateMark}`,
      prompt: `Do the work. ${bodyMark}`,
    });
  }

  async function readAssignment(id: string): Promise<{ activation_prompt: string | null }> {
    return withAdmin(async (apiCtx, headers) => {
      const resp = await ok(await apiCtx.get(`/api/v1/assignments/${id}`, { headers }), 'read assignment');
      return await resp.json();
    });
  }

  /** `trigger_type: 'manual'` so only this test fires it and no cron must parse. */
  async function createAssignment(fields: Record<string, unknown>): Promise<string> {
    return withAdmin(async (apiCtx, headers) => {
      const resp = await ok(
        await apiCtx.post('/api/v1/assignments', {
          headers,
          data: { agent_id: agentId, trigger_type: 'manual', trigger_config: {}, ...fields },
        }),
        'create assignment',
      );
      const id = (await resp.json()).id;
      created.push(id);
      return id;
    });
  }

  async function runToCompletion(assignmentId: string): Promise<Run> {
    return withAdmin(async (apiCtx, headers) => {
      await ok(await apiCtx.post(`/api/v1/assignments/${assignmentId}/trigger`, { headers }), 'trigger');

      let last: Run | undefined;
      await expect
        .poll(
          async () => {
            const resp = await apiCtx.get(`/api/v1/assignments/${assignmentId}/runs`, { headers });
            if (!resp.ok()) return false;
            const runs: Run[] = await resp.json();
            last = runs[0];
            return TERMINAL.includes(last?.status ?? '');
          },
          {
            timeout: 120_000,
            message:
              'the run never reached a terminal status. A trigger only writes it at "pending" and ' +
              'hands it to the worker, so the usual cause is that no worker is running.',
          },
        )
        .toBe(true);
      return last as Run;
    });
  }

  test.beforeAll(async () => {
    // `require_ai_enabled` -> `ai_enabled`. The licence half,
    // `require_agentic_ai`, publishes no flag, so an unlicensed deployment
    // fails here instead of skipping.
    skipWhy = await scriptedSkipReason(
      'ai_enabled',
      'AI is off on this deployment; the trigger would answer 403.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    agentId = await createScriptedAgent(
      `E2E gate agent ${Date.now().toString(36)}`,
      'An agent that exists to be triggered by the activation-gate spec.',
    );
  });

  test.afterAll(async () => {
    if (created.length) {
      await withAdmin(async (apiCtx, headers) => {
        for (const id of created) await apiCtx.delete(`/api/v1/assignments/${id}`, { headers });
      });
    }
    if (agentId) await deleteAgent(agentId);
    await scripted?.dispose();
  });

  test('a gate that answers NO stops the run before the agent loop', async () => {
    test.skip(!!skipWhy, skipWhy);

    const gate = marker('gateno');
    const body = marker('bodyno');
    await scripted.arm(gate, [{ text: 'NO' }]);
    // Armed but never expected to serve. Unarmed, a body turn would fall through
    // to the default reply and the run would complete looking like a success.
    await scripted.arm(body, [{ text: `the loop should never have run ${body}` }]);

    const run = await runToCompletion(await makeAssignment(gate, body));

    // A refused run completes: the gate decided correctly. The summary is where
    // the decision shows (`_format_skip_summary`).
    expect(run.status).toBe('completed');
    expect(run.result_summary).toBe("Skipped: activation check returned 'NO'");

    // One snapshot, so the two counts cannot disagree about when they were
    // read, and the message carries what the server actually recorded — a
    // count of zero for both means it saw neither marker, which is a different
    // fault from the loop having run.
    // One snapshot, so the counts cannot disagree about when they were read.
    const { served, calls } = await scripted.calls();
    const matched = (mark: string) => calls.filter((call) => call.matched === mark).length;

    // A build that runs the assignment anyway and writes the summary over it
    // passes everything above. The loop streams, so it is served as well as
    // matched; either being non-zero means it ran.
    expect(served[body] ?? 0, 'the agent loop ran').toBe(0);
    expect(matched(body), 'the agent loop was asked something').toBe(0);
    expect(matched(gate), 'the gate answered NO but the model was never asked').toBeGreaterThan(0);
  });

  test('a gate that answers YES admits the run to the agent loop', async () => {
    test.skip(!!skipWhy, skipWhy);

    const gate = marker('gateyes');
    const body = marker('bodyyes');
    await scripted.arm(gate, [{ text: 'YES' }]);
    await scripted.arm(body, [{ text: `the loop ran ${body}` }]);

    const run = await runToCompletion(await makeAssignment(gate, body));

    // Same status as a refused run, so the summary is what separates them.
    expect(run.status).toBe('completed');
    expect(run.result_summary ?? '').not.toContain('activation check returned');

    // A build that dropped the gate also reaches the body; only the first catches it.
    expect(await callsMatching(scripted, gate), 'the gate was never asked').toBeGreaterThan(0);
    expect(await scripted.turnsServed(body), 'the agent loop never ran').toBeGreaterThan(0);
  });

  // Named for what it can see. A blank gate call would carry no marker, so
  // the scripted server cannot tell one apart from any other spec's traffic;
  // what is observable is the stored row and that the body still ran.
  test('a whitespace-only gate is stored as no gate, and the run proceeds', async () => {
    test.skip(!!skipWhy, skipWhy);

    const body = marker('nogate');
    await scripted.arm(body, [{ text: `ungated run ${body}` }]);

    const assignmentId = await createAssignment({
      name: `E2E ungated ${body}`,
      // Whitespace, not absence: `_normalize_blank_activation_prompt` treats it
      // as no gate rather than a gate with an empty question.
      activation_prompt: '   ',
      prompt: `Do the work. ${body}`,
    });

    // The row, not the run: a blank gate would answer with the server's
    // default, which is not NO, so the run completes either way.
    expect((await readAssignment(assignmentId)).activation_prompt).toBeNull();

    const run = await runToCompletion(assignmentId);
    expect(run.status).toBe('completed');
    expect(run.result_summary ?? '').not.toContain('activation check returned');
    expect(await scripted.turnsServed(body)).toBeGreaterThan(0);
  });
});
