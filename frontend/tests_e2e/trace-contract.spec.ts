import { test, expect } from '@playwright/test';
import { createScriptedAgent, deleteAgent, ok, scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';
import { traceFixture, spansFixture } from './fixtures/trace-shapes';

/**
 * The trace and span shape the backend emits, against what the Explorer reads.
 *
 * The Explorer specs mock their payloads, pinning the renderer against a shape
 * the frontend authored; this asserts the wire carries every key they read.
 *
 * Shape only: the scripted server reports fixed usage and `custom_openai` has
 * no `model_prices.json` row, so tokens and cost would assert nothing.
 */

/** Derived from the rendered fixtures, so the two cannot drift. */
const TRACE_KEYS = Object.keys(traceFixture);
const SPAN_KEYS = Object.keys(spansFixture.items[0]);

/** Types `langfuse_tracing.py` writes. Membership, so an unknown one is caught. */
const SPAN_TYPES = ['GENERATION', 'TOOL', 'ITERATION', 'MEMORY', 'SPAN', 'AGENT', 'EVENT'];

interface TraceRow { id: string; conversation_id: string | null }

test.describe.serial('Trace Explorer — the backend\'s half of the contract', () => {
  let scripted: ScriptedModel;
  let skipWhy = '';
  let conversationId = '';
  let agentId = '';
  let traceId = '';

  test.beforeAll(async () => {
    // The hook's own budget, which otherwise matches the 60s test timeout and
    // expires during the 60s poll below — reporting a hook timeout instead of
    // the poll's diagnostic.
    test.setTimeout(120_000);

    skipWhy = await scriptedSkipReason(
      'chat_enabled',
      'chat is not enabled on this deployment; there is no turn to trace.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();

    const mark = marker('tracecontract');
    await scripted.arm(mark, [{ text: `traced answer ${mark}` }]);

    agentId = await createScriptedAgent(
      `E2E trace contract ${Date.now().toString(36)}`,
      'An agent that exists to produce one traced turn.',
    );

    await withAdmin(async (apiCtx, headers) => {
      // Over REST: the subject is the row the backend writes. chat-turn.spec.ts
      // covers the rendering.
      const conv = await ok(
        await apiCtx.post('/api/v1/chat/conversations', {
          headers,
          data: { agent_id: agentId, title: `E2E trace contract ${mark}` },
        }),
        'create conversation',
      );
      conversationId = (await conv.json()).id;

      await ok(
        await apiCtx.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
          headers,
          data: { content: `${mark} answer me so a trace is written` },
        }),
        'send message',
      );

      // Filtered, so another spec's traces cannot push this one off the page.
      // The dispatcher does not block the reply, so the row can trail it.
      await expect
        .poll(
          async () => {
            const resp = await apiCtx.get(
              `/api/v1/traces?conversation_id=${conversationId}`,
              { headers },
            );
            if (!resp.ok()) return false;
            const { items }: { items: TraceRow[] } = await resp.json();
            const [mine] = items;
            if (mine) traceId = mine.id;
            return !!mine;
          },
          {
            timeout: 60_000,
            message:
              `no trace was written for conversation ${conversationId}. The turn succeeded, so the ` +
              'cause is tracing rather than chat.',
          },
        )
        .toBe(true);
    });
  });

  test.afterAll(async () => {
    // Per id: the collection-level DELETE empties the whole account.
    if (conversationId) {
      await withAdmin((apiCtx, headers) =>
        apiCtx.delete(`/api/v1/chat/conversations/${conversationId}`, { headers }),
      ).catch(() => undefined);
    }
    if (agentId) await deleteAgent(agentId);
    await scripted?.dispose();
  });

  async function read(path: string): Promise<Record<string, unknown>> {
    return withAdmin(async (apiCtx, headers) => {
      const resp = await apiCtx.get(path, { headers });
      expect(resp.ok(), `${path} answered ${resp.status()}`).toBe(true);
      return await resp.json();
    });
  }

  test('a real trace carries every key the Explorer reads', async () => {
    test.skip(!!skipWhy, skipWhy);

    const trace = await read(`/api/v1/traces/${traceId}`);
    // Per key, not a set comparison: the backend may send more, and does.
    for (const key of TRACE_KEYS) {
      expect(trace, `the trace payload is missing "${key}", which traces.spec.ts renders`).toHaveProperty(key);
    }
  });

  test('a real span list carries every key the Explorer reads, and only known types', async () => {
    test.skip(!!skipWhy, skipWhy);

    const spans = (await read(`/api/v1/traces/${traceId}/spans`)) as unknown as {
      items: Record<string, unknown>[];
      total: number;
    };
    expect(spans).toHaveProperty('total');
    expect(spans.items.length, 'a scripted turn should have produced at least one span').toBeGreaterThan(0);

    for (const span of spans.items) {
      for (const key of SPAN_KEYS) {
        expect(span, `a ${span.type} span is missing "${key}", which traces.spec.ts renders`).toHaveProperty(key);
      }
      expect(SPAN_TYPES, `the backend emitted an unknown span type "${span.type}"`).toContain(span.type);
    }

    // The Explorer's model, token and latency columns all read this span.
    expect(
      spans.items.some((span) => span.type === 'GENERATION'),
      `the turn produced no GENERATION span; got ${spans.items.map((s) => s.type).join(', ') || 'none'}`,
    ).toBe(true);
  });
});
