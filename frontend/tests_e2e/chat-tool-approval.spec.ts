import { test, expect, type Page, type Request } from '@playwright/test';
import { ok, platformConfig, requireScriptedServer, withAdmin } from './helpers';
import { marker, ScriptedModel, SCRIPTED_MODEL, SCRIPTED_PROVIDER } from './scripted-llm/client';
import { ChatPage } from './model/chat.page';

/**
 * Tool approval on the /chat page's shared panel (#173, specs/006-per-tool-approval),
 * each verdict end to end on a real stack:
 *
 * - "Yes" runs the call and the turn answers; it holds for that call only, so
 *   the tool asks again on the next one;
 * - "No" with a reason does not run the call: the agent is handed the
 *   structured refusal carrying the reason (FR-010) and answers;
 * - "Yes, always" runs the call and records a standing approval for the user
 *   and tool (Profile > Tool approvals, `GET /tool-approvals`), so the agent's
 *   next call of that tool runs without asking.
 *
 * A reload while the prompt is up is `chat-resume.spec.ts`'s. The agent is
 * pinned to the scripted model and asks before its tools
 * (`approve_tool_calls`); the gated tool is `generate_file`, whose file shows
 * whether the call ran.
 */

const WEB_SESSIONS = '/api/v1/web/chat/sessions';
const APPROVE = '/api/v1/web/chat/messages/approve';
const TOOL = 'generate_file';

const conversations: string[] = [];
const agents: string[] = [];
/** The name each agent was created with, by its id. */
const agentNames = new Map<string, string>();
/** Standing approvals this spec granted, revoked in `afterAll`. */
const granted = new Set<string>();

interface Preference {
  id: string;
  tool_key: string;
  tool_source: string | null;
}

interface ApprovalBody {
  conversation_id: string;
  decisions: { tool_call_id: string; decision: string; rejection_reason?: string | null }[];
}

async function myApprovals(): Promise<{ tool_approvals_enabled: boolean; approve_all_tools: boolean; preferences: Preference[] }> {
  return withAdmin(async (apiCtx, headers) =>
    (await ok(await apiCtx.get('/api/v1/tool-approvals', { headers }), 'list tool approvals')).json(),
  );
}

/** The caller's standing approvals of the gated tool. */
async function approvalsOfTool(): Promise<Preference[]> {
  return (await myApprovals()).preferences.filter((p) => p.tool_key === TOOL || p.tool_key.endsWith(`:${TOOL}`));
}

/** Names of the files the conversation holds. */
async function conversationFiles(conversationId: string): Promise<string[]> {
  return withAdmin(async (apiCtx, headers) => {
    const resp = await ok(
      await apiCtx.get(`/api/v1/chat/conversations/${conversationId}/files`, { headers }),
      'list conversation files',
    );
    return ((await resp.json()) as { files: { filename: string }[] }).files.map((f) => f.filename);
  });
}

/** Every tool result recorded on the conversation's replies. */
async function toolResults(conversationId: string): Promise<{ name: string; output: string; success: boolean }[]> {
  return withAdmin(async (apiCtx, headers) => {
    const resp = await ok(
      await apiCtx.get(`/api/v1/chat/conversations/${conversationId}/messages`, { headers }),
      'list messages',
    );
    const body = (await resp.json()) as {
      messages: { tool_call_trace?: { name: string; output: string; success: boolean }[] | null }[];
    };
    return body.messages.flatMap((m) => m.tool_call_trace ?? []);
  });
}

async function createAgent(name: string, data: Record<string, unknown>): Promise<string> {
  return withAdmin(async (apiCtx, headers) => {
    const resp = await ok(
      await apiCtx.post('/api/v1/agents', {
        headers,
        data: { name, persona: 'You follow the script.', llm_provider: SCRIPTED_PROVIDER, llm_model: SCRIPTED_MODEL, ...data },
      }),
      'create agent',
    );
    const id = (await resp.json()).id as string;
    agents.push(id);
    agentNames.set(id, name);
    return id;
  });
}

/** Open a new chat with *agentId* and send *text*: the conversation the send created. */
async function startConversation(page: Page, agentId: string, text: string): Promise<string> {
  const chat = new ChatPage(page);
  const created = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === WEB_SESSIONS,
  );
  await page.goto(`/chat?agent=${agentId}`);
  await chat.waitForComposer();
  await chat.waitForAgent(agentNames.get(agentId)!);
  await chat.send(text);
  const id = ((await (await created).json()) as { conversation_id: string }).conversation_id;
  conversations.push(id);
  await expect(page).toHaveURL(new RegExp(`[?&]c=${id}`));
  return id;
}

/** The next decision set the panel posts, and the platform's answer to it. */
function nextDecision(page: Page): Promise<Request> {
  return page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === APPROVE);
}

/** A card still waiting on a verdict: a decided one shows a badge, not buttons. */
const undecided = (page: Page) =>
  new ChatPage(page).approval().filter({ has: page.getByRole('button', { name: 'Yes', exact: true }) });

test.describe.serial('Chat - tool approval on the shared panel', () => {
  test.describe.configure({ timeout: 180_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';
  let askingAgent = '';

  test.beforeAll(async () => {
    skipWhy = await requireScriptedServer();
    if (!skipWhy && (await platformConfig()).chat_enabled === false) {
      skipWhy = 'chat is not enabled on this deployment; the send would answer 403.';
    }
    if (!skipWhy) {
      const mine = await myApprovals();
      if (!mine.tool_approvals_enabled) skipWhy = 'tool approvals are switched off on this platform.';
      else if (mine.approve_all_tools) skipWhy = 'the e2e user approves every tool, so nothing would ask.';
      else if ((await approvalsOfTool()).length > 0) {
        skipWhy = `the e2e user already holds a standing approval of ${TOOL}, so it would never ask.`;
      }
    }
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    askingAgent = await createAgent(`E2E tool approval ${Date.now()}`, {
      builtin_tool_ids: [TOOL],
      approve_tool_calls: true,
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of granted) await apiCtx.delete(`/api/v1/tool-approvals/${id}`, { headers });
      for (const id of conversations) await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      for (const id of agents) await apiCtx.delete(`/api/v1/agents/${id}`, { headers });
    }).catch(() => undefined);
    await scripted?.dispose();
  });

  test('"Yes" runs the call and the turn answers, and the next call asks again', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('approve-once');
    await scripted.arm(mark, [
      { tool: TOOL, arguments: { filename: `once-${mark}.txt`, content: 'approved once' } },
      { text: `Written once ${mark}` },
      { tool: TOOL, arguments: { filename: `again-${mark}.txt`, content: 'asked again' } },
      { text: `Written again ${mark}` },
    ]);

    const chat = new ChatPage(page);
    const conversationId = await startConversation(page, askingAgent, `${mark} write the note`);
    await expect(undecided(page)).toHaveCount(1, { timeout: 60_000 });
    await expect(chat.approval()).toContainText(TOOL);
    await expect(chat.approval()).toContainText(`once-${mark}.txt`);

    const sent = nextDecision(page);
    await chat.approval().getByRole('button', { name: 'Yes', exact: true }).click();
    const body = (await sent).postDataJSON() as ApprovalBody;
    expect(body.conversation_id).toBe(conversationId);
    expect(body.decisions).toHaveLength(1);
    expect(body.decisions[0].decision).toBe('approve');

    await expect(chat.transcript().getByText(`Written once ${mark}`)).toBeVisible({ timeout: 60_000 });
    await expect(undecided(page)).toHaveCount(0);
    expect(await conversationFiles(conversationId)).toContain(`once-${mark}.txt`);

    // A plain "Yes" left nothing standing: the next call asks again.
    expect(await approvalsOfTool()).toHaveLength(0);
    // The send must not land while the previous turn still streams.
    await chat.waitForTurnSettled();
    await chat.send(`${mark} and another`);
    await expect(undecided(page)).toHaveCount(1, { timeout: 60_000 });
    await expect(undecided(page)).toContainText(`again-${mark}.txt`);
    await undecided(page).getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(chat.transcript().getByText(`Written again ${mark}`)).toBeVisible({ timeout: 60_000 });
    expect(await conversationFiles(conversationId)).toContain(`again-${mark}.txt`);
  });

  test('"No" with a reason does not run the call, and the agent answers after the refusal', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('decline');
    const reason = `wrong environment, use staging instead ${mark}`;
    await scripted.arm(mark, [
      { tool: TOOL, arguments: { filename: `declined-${mark}.txt`, content: 'never written' } },
      { text: `Understood, nothing was written ${mark}` },
    ]);

    const chat = new ChatPage(page);
    const conversationId = await startConversation(page, askingAgent, `${mark} write the note`);
    await expect(undecided(page)).toHaveCount(1, { timeout: 60_000 });

    // "No" asks why before it sends.
    await chat.approval().getByRole('button', { name: 'No', exact: true }).click();
    const why = chat.approval().getByRole('textbox');
    await expect(why).toBeVisible();
    await why.fill(reason);
    const sent = nextDecision(page);
    await chat.approval().getByRole('button', { name: 'Decline this call', exact: true }).click();
    const body = (await sent).postDataJSON() as ApprovalBody;
    expect(body.conversation_id).toBe(conversationId);
    expect(body.decisions).toEqual([expect.objectContaining({ decision: 'reject', rejection_reason: reason })]);

    await expect(chat.transcript().getByText(`Understood, nothing was written ${mark}`)).toBeVisible({ timeout: 60_000 });
    await expect(undecided(page)).toHaveCount(0);

    // Not run: no file, nothing in the stored trace (it holds executed calls);
    // the agent was called again after the refusal, which reaches it as the
    // call's result with the reviewer's reason (backend tests/test_chat_approval_review_fixes.py).
    expect(await conversationFiles(conversationId)).not.toContain(`declined-${mark}.txt`);
    expect((await toolResults(conversationId)).filter((r) => r.name === TOOL)).toEqual([]);
    expect(await scripted.turnsServed(mark)).toBe(2);
    expect(await approvalsOfTool()).toHaveLength(0);
  });

  test('"Yes, always" runs the call, records a standing approval, and the next call does not ask', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('approve-always');
    await scripted.arm(mark, [
      { tool: TOOL, arguments: { filename: `always-${mark}.txt`, content: 'approved always' } },
      { text: `Written and remembered ${mark}` },
      { tool: TOOL, arguments: { filename: `unasked-${mark}.txt`, content: 'not asked' } },
      { text: `Written without asking ${mark}` },
    ]);

    const chat = new ChatPage(page);
    const conversationId = await startConversation(page, askingAgent, `${mark} write the note`);
    await expect(undecided(page)).toHaveCount(1, { timeout: 60_000 });

    // The one verdict that outlives the turn: it is confirmed, after a warning.
    await chat.approval().getByRole('button', { name: 'Yes, always', exact: true }).click();
    await expect(chat.root().getByText('“Yes, always” saves a preference for you.', { exact: false })).toBeVisible();
    const sent = nextDecision(page);
    await chat.root().getByRole('button', { name: 'Confirm', exact: true }).click();
    const body = (await sent).postDataJSON() as ApprovalBody;
    expect(body.conversation_id).toBe(conversationId);
    expect(body.decisions).toEqual([expect.objectContaining({ decision: 'approve_always' })]);

    await expect(chat.transcript().getByText(`Written and remembered ${mark}`)).toBeVisible({ timeout: 60_000 });
    expect(await conversationFiles(conversationId)).toContain(`always-${mark}.txt`);

    const standing = await approvalsOfTool();
    expect(standing).toHaveLength(1);
    for (const p of standing) granted.add(p.id);

    // The next call of the tool runs without a prompt: had it asked, the turn
    // would wait on the decision and never answer.
    let prompted = 0;
    page.on('request', (r) => {
      if (r.method() === 'POST' && new URL(r.url()).pathname === APPROVE) prompted += 1;
    });
    await chat.waitForTurnSettled();
    await chat.send(`${mark} and another`);
    await expect(chat.transcript().getByText(`Written without asking ${mark}`)).toBeVisible({ timeout: 60_000 });
    await expect(undecided(page)).toHaveCount(0);
    expect(prompted).toBe(0);
    expect(await conversationFiles(conversationId)).toContain(`unasked-${mark}.txt`);

    // Profile > Tool approvals lists it, where it can be taken back.
    await page.goto('/profile/approvals');
    await expect(page.getByText(TOOL, { exact: false }).first()).toBeVisible({ timeout: 15_000 });
  });
});
