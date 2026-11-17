import { test, expect } from '@playwright/test';
import { createAdminApiContext } from '../helpers';

// ---------------------------------------------------------------------------
// Output extractors — the authoring flow.
//
// `evaluation-extractor.spec.ts` checks catalog parity without a stack; this one
// drives the flow an author performs, so it needs the app and a seeded admin.
//
// Every fixture (agent, evaluation, binding) is created here through the API,
// so a test can never pass because data was missing.
// ---------------------------------------------------------------------------

test.describe.serial('Output extractors — authoring flow', () => {
  const AGENT_NAME = `E2E Extractor Agent ${Date.now()}`;
  let evaluationId = '';
  let agentId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    const auth = { Authorization: `Bearer ${token}` };
    try {
      const agentResp = await apiCtx.post('/api/v1/agents', {
        headers: auth,
        data: { name: AGENT_NAME, persona: 'Temp agent for extractor tests.' },
      });
      if (!agentResp.ok()) {
        throw new Error(
          `agent create failed: ${agentResp.status()} ${await agentResp.text()}`,
        );
      }
      agentId = (await agentResp.json()).id;

      const evalResp = await apiCtx.post('/api/v1/evaluations', {
        headers: auth,
        data: {
          name: `E2E Extractor ${Date.now()}`,
          family: 'CLASSIFICATION',
          agent_ids: [agentId],
        },
      });
      if (!evalResp.ok()) {
        throw new Error(
          `evaluation create failed: ${evalResp.status()} ${await evalResp.text()}`,
        );
      }
      evaluationId = (await evalResp.json()).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  /** Create an agent, bind it to the evaluation under test, and return its id. */
  const bindSpareAgent = async (name: string): Promise<string> => {
    const { apiCtx, token } = await createAdminApiContext();
    const auth = { Authorization: `Bearer ${token}` };
    try {
      const created = await apiCtx.post('/api/v1/agents', {
        headers: auth,
        data: {
          name,
          persona: 'Second agent, removed mid-edit by the swap regression.',
        },
      });
      if (!created.ok()) {
        throw new Error(
          `spare agent create failed: ${created.status()} ${await created.text()}`,
        );
      }
      const id = (await created.json()).id as string;
      const bound = await apiCtx.post(`/api/v1/evaluations/${evaluationId}/agents`, {
        headers: auth,
        data: { agent_ids: [id] },
      });
      if (!bound.ok()) {
        throw new Error(
          `spare agent bind failed: ${bound.status()} ${await bound.text()}`,
        );
      }
      return id;
    } finally {
      await apiCtx.dispose();
    }
  };

  const deleteAgent = async (id: string): Promise<void> => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/agents/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    } finally {
      await apiCtx.dispose();
    }
  };

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    const auth = { Authorization: `Bearer ${token}` };
    try {
      if (evaluationId) {
        await apiCtx.delete(`/api/v1/evaluations/${evaluationId}`, { headers: auth });
      }
      if (agentId) {
        await apiCtx.delete(`/api/v1/agents/${agentId}`, { headers: auth });
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('offers extraction for the bound agent, defaulting to the raw output', async ({
    page,
  }) => {
    await page.goto(`/evaluations/${evaluationId}/config`);
    await expect(page.getByText('Output extractors')).toBeVisible();

    // By default an agent is scored on its raw output.
    const toggle = page.getByRole('switch', {
      name: `Extract before scoring for ${AGENT_NAME}`,
    });
    await expect(toggle).toBeVisible();
    await expect(toggle).not.toBeChecked();
  });

  test('an author can add, reorder and remove operations', async ({ page }) => {
    await page.goto(`/evaluations/${evaluationId}/config`);
    await page.getByRole('button', { name: 'Edit' }).first().click();
    await page
      .getByRole('switch', { name: `Extract before scoring for ${AGENT_NAME}` })
      .click();

    await page.getByRole('button', { name: 'Add operation' }).click();
    const first = page.getByRole('combobox', { name: `Operation 1 for ${AGENT_NAME}` });
    const second = page.getByRole('combobox', { name: `Operation 2 for ${AGENT_NAME}` });
    await expect(first).toBeVisible();
    await expect(second).toBeVisible();

    // Order is load-bearing for a typed fold, so a mis-ordered pair must be
    // fixable without retyping the rest.
    await expect(
      page.getByRole('button', { name: `Move operation 1 of ${AGENT_NAME} up` }),
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: `Move operation 2 of ${AGENT_NAME} down` }),
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: `Move operation 1 of ${AGENT_NAME} down` }),
    ).toBeEnabled();

    await page
      .getByRole('button', { name: `Remove operation 2 of ${AGENT_NAME}` })
      .click();
    await expect(second).toHaveCount(0);
    await expect(first).toBeVisible();
  });

  test('preview accepts both a pasted sample and a stored result', async ({ page }) => {
    await page.goto(`/evaluations/${evaluationId}/config`);
    await page.getByRole('button', { name: 'Edit' }).first().click();
    await page
      .getByRole('switch', { name: `Extract before scoring for ${AGENT_NAME}` })
      .click();

    // Both preview sources are reachable from the UI.
    const source = page.getByRole('combobox', { name: `Preview source for ${AGENT_NAME}` });
    await expect(source).toBeVisible();
    await expect(
      page.getByRole('textbox', { name: `Sample output to preview for ${AGENT_NAME}` }),
    ).toBeVisible();

    await source.click();
    await page.getByRole('option', { name: 'A stored result' }).click();
    await expect(
      page.getByRole('combobox', { name: `Stored result to preview for ${AGENT_NAME}` }),
    ).toBeVisible();
    // This agent has never run, so the picker says so rather than offering none.
    await expect(
      page.getByText('This agent has no stored results on this evaluation yet.'),
    ).toBeVisible();
  });

  // Agents are added and removed by immediate mutations, outside the config form's
  // save. A draft extractor that outlived its agent would get the next save refused
  // with "agent_extractors names agent '…', which is not on this evaluation".
  test('removing an agent drops its draft extractor, so the next save is accepted', async ({
    page,
  }) => {
    const spareName = `${AGENT_NAME} spare`;
    const spareId = await bindSpareAgent(spareName);

    try {
      await page.goto(`/evaluations/${evaluationId}/config`);
      await page.getByRole('button', { name: 'Edit' }).first().click();

      // Draft an extractor for the spare, then take the spare off the evaluation.
      await page
        .getByRole('switch', { name: `Extract before scoring for ${spareName}` })
        .click();
      await page.getByRole('button', { name: `Remove agent ${spareName}` }).click();
      await expect(
        page.getByRole('switch', { name: `Extract before scoring for ${spareName}` }),
      ).toHaveCount(0);

      await page.getByRole('button', { name: 'Save' }).click();
      // Exact: the toast's screen-reader announcement reads "Notification Evaluation
      // updated" while it is live, and a substring match would resolve to both.
      await expect(page.getByText('Evaluation updated', { exact: true })).toBeVisible();
      await expect(page.getByText('which is not on this evaluation')).toHaveCount(0);
    } finally {
      await deleteAgent(spareId);
    }
  });
});
