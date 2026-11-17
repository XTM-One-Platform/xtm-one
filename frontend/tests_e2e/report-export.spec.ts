import { test, expect, type APIResponse } from '@playwright/test';
import { deleteAgent, ok, scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel, SCRIPTED_MODEL, SCRIPTED_PROVIDER } from './scripted-llm/client';

// ---------------------------------------------------------------------------
// Reports as stored files: the server-side PDF export.
//
// A report was printable only by the reader's browser, so it had no file. The
// platform now prints it itself, through the headless Chromium the decks export
// with, from the report's static render page. An agent writes a report on one
// turn and exports it on the next - the export is offered on every turn, and
// whether the user wants a file is the model's call - and the PDF lands
// as a file of the conversation. The REST route and the report viewer's Export
// menu produce the same document. Paper is A4 (the report's own `@page` rule).
// ---------------------------------------------------------------------------

/** Chromium writes an A4 page as this MediaBox (595 x 842 pt). */
const A4_MEDIA_BOX = /\/MediaBox\s*\[\s*0 0 594\.9\d* 841\.9\d*\s*\]/;

async function expectA4Pdf(body: Buffer, what: string): Promise<void> {
  expect(body.subarray(0, 5).toString('latin1'), `${what} is a PDF`).toBe('%PDF-');
  expect(A4_MEDIA_BOX.test(body.toString('latin1')), `${what} is printed on A4`).toBe(true);
}

interface Attachment {
  file_id?: string;
  filename: string;
  content_type?: string;
}

test.describe.serial('Report export', () => {
  let scripted: ScriptedModel | undefined;
  let skipWhy = '';
  let agentId = '';
  let conversationId = '';
  let reportId = '';
  const write = marker('reportwrite');
  const title = `E2E report export ${write}`;

  test.beforeAll(async () => {
    test.setTimeout(120_000);
    skipWhy = await scriptedSkipReason('chat_enabled', 'chat is not enabled on this deployment.');
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    await scripted.arm(write, [
      {
        tool: 'generate_report',
        arguments: {
          title,
          header: { title, eyebrow: 'E2E', tags: ['TLP:CLEAR'] },
          blocks: [
            { block: 'prose', title: 'Findings', lead: '<p>The platform prints this report itself.</p>' },
            { block: 'annex', title: 'Annex', summary: 'Method', items: ['A closed annex prints open.'] },
          ],
        },
      },
      { text: 'E2E report written.' },
    ]);
    await withAdmin(async (apiCtx, headers) => {
      const agent = await ok(
        await apiCtx.post('/api/v1/agents', {
          headers,
          data: {
            name: `E2E report exporter ${write}`,
            persona: 'Writes reports and exports them when asked.',
            llm_provider: SCRIPTED_PROVIDER,
            llm_model: SCRIPTED_MODEL,
            builtin_tool_ids: ['generate_report'],
            // The export under test, not an approval prompt for it: CI is a
            // fresh install, where tool approval is on by default.
            approve_tool_calls: false,
          },
        }),
        'create agent',
      );
      agentId = (await agent.json()).id;
      const conversation = await ok(
        await apiCtx.post('/api/v1/chat/conversations', { headers, data: { agent_id: agentId, title } }),
        'create conversation',
      );
      conversationId = (await conversation.json()).id;
      await ok(
        await apiCtx.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
          headers,
          data: { content: `${write} write the brief` },
        }),
        'the writing turn',
      );
      const reports = await ok(
        await apiCtx.get(`/api/v1/reports?conversation_id=${conversationId}`, { headers }),
        'list reports',
      );
      const found = ((await reports.json()) as { id: string; title: string }[]).find((r) => r.title === title);
      expect(found, 'the agent generated the report').toBeTruthy();
      reportId = found!.id;
    });
  });

  test.afterAll(async () => {
    if (conversationId) {
      await withAdmin((apiCtx, headers) =>
        apiCtx.delete(`/api/v1/chat/conversations/${conversationId}`, { headers }),
      ).catch(() => undefined);
    }
    if (agentId) await deleteAgent(agentId);
    await scripted?.dispose();
  });

  test('the agent exports the report as a file of the conversation', async () => {
    test.skip(!!skipWhy, skipWhy);
    test.setTimeout(180_000);
    const writing = await scripted!.agentTurns(write);
    expect(writing.length, 'the writing turn reached the model').toBeGreaterThan(0);
    expect(writing[0].tools_offered, 'the export is offered whatever the request says').toContain('export_report');

    const exporting = marker('reportexport');
    await scripted!.arm(exporting, [
      { tool: 'export_report', arguments: { report_id: reportId } },
      { text: 'E2E report exported.' },
    ]);
    await withAdmin(async (apiCtx, headers) => {
      const reply = await ok(
        await apiCtx.post(`/api/v1/chat/conversations/${conversationId}/messages`, {
          headers,
          data: { content: `${exporting} export the brief as a PDF file` },
          timeout: 150_000,
        }),
        'the export turn',
      );
      const turns = await scripted!.agentTurns(exporting);
      expect(turns[0].tools_offered).toContain('export_report');

      const { assistant_message: message } = (await reply.json()) as {
        assistant_message: { attachments?: Attachment[] | null };
      };
      const pdf = (message.attachments ?? []).find((a) => a.filename.endsWith('.pdf'));
      expect(pdf?.file_id, `the reply carries the PDF: ${JSON.stringify(message.attachments)}`).toBeTruthy();
      const file = await ok(
        await apiCtx.get(`/api/v1/chat/files/${pdf!.file_id}/download`, { headers }),
        'download the stored PDF',
      );
      await expectA4Pdf(await file.body(), 'the stored export');
    });
  });

  test('the REST route streams the same document', async () => {
    test.skip(!!skipWhy, skipWhy);
    test.setTimeout(180_000);
    await withAdmin(async (apiCtx, headers) => {
      const resp: APIResponse = await apiCtx.post(`/api/v1/reports/${reportId}/export`, {
        headers,
        timeout: 150_000,
      });
      expect(resp.ok(), `export failed: ${resp.status()} ${await resp.text()}`).toBe(true);
      expect(resp.headers()['content-type']).toContain('application/pdf');
      expect(resp.headers()['content-disposition']).toContain('.pdf');
      await expectA4Pdf(await resp.body(), 'the REST export');
    });
  });

  test("the viewer's Export menu downloads the server-side PDF", async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);
    test.setTimeout(180_000);
    await page.goto(`/reports/${reportId}`);
    await expect(page.getByRole('heading', { name: title }).first()).toBeVisible();
    await page.getByRole('button', { name: 'Export report' }).click();
    const download = page.waitForEvent('download', { timeout: 150_000 });
    await page.getByTestId('overflow-menu-item-pdf').click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.pdf$/);
  });
});
