import { test, expect } from '@playwright/test';
import { createAdminApiContext } from '../helpers';

// ---------------------------------------------------------------------------
// The label vocabulary editor keeps the case it was given.
//
// `TagInput` lowercases tags for the free-form tag fields, but a vocabulary
// shows the author what they declared: `T1003.001` must not read back as
// `t1003.001`. Scoring folds case, so only the display is at stake.
//
// The API stores what it is sent, so only a browser catches this.
//
// The field also offers no platform tags as autocomplete: they are lowercase
// and never members of the closed set the dataset declares.
// ---------------------------------------------------------------------------

const TECHNIQUE = 'T1003.001';

test.describe.serial('Dataset label vocabulary — case', () => {
  // ONE DATASET PER TEST. `TagInput` renders its placeholder only while the
  // field is empty, and this spec locates the field by that placeholder, so a
  // shared dataset would make the second test depend on the first saving nothing.
  let datasetId = '';
  let emptyDatasetId = '';

  const createDataset = async (name: string, withEntry: boolean) => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/datasets', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: `${name} ${Date.now()}`,
          family: 'CLASSIFICATION',
          // No vocabulary yet: it is what the browser is about to type.
          entries: withEntry
            ? [{ input_prompt: 'Which technique?', expected_answer: `["${TECHNIQUE}"]` }]
            : [],
        },
      });
      if (!resp.ok()) {
        throw new Error(`dataset create failed: ${resp.status()} ${await resp.text()}`);
      }
      return (await resp.json()).id as string;
    } finally {
      await apiCtx.dispose();
    }
  };

  test.beforeAll(async () => {
    datasetId = await createDataset('E2E Vocabulary Case', true);
    emptyDatasetId = await createDataset('E2E Vocabulary Suggestions', false);
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      for (const id of [datasetId, emptyDatasetId]) {
        if (id) {
          await apiCtx.delete(`/api/v1/datasets/${id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('a typed label keeps its case, and the dataset accepts it', async ({ page }) => {
    await page.goto(`/evaluations/datasets/${datasetId}/settings`);

    const field = page.getByPlaceholder('Add label…');
    await expect(field).toBeVisible();
    await field.fill(TECHNIQUE);
    await field.press('Enter');

    // The badge is what the author reads back.
    await expect(page.getByText(TECHNIQUE, { exact: true })).toBeVisible();

    // The family card is the last one on the tab, and the only one dirty here.
    await page.getByRole('button', { name: 'Save changes' }).last().click();

    // The save proves it: lowercased, the server would store `t1003.001` and the
    // poll would time out. Asserting the error text is absent would pass before the
    // round trip.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await expect
        .poll(async () => {
          const resp = await apiCtx.get(`/api/v1/datasets/${datasetId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          return (await resp.json()).label_vocabulary;
        })
        .toEqual([TECHNIQUE]);
    } finally {
      await apiCtx.dispose();
    }
  });

  test('the field offers no platform tags — the vocabulary is the dataset\'s own', async ({
    page,
  }) => {
    await page.goto(`/evaluations/datasets/${emptyDatasetId}/settings`);

    const field = page.getByPlaceholder('Add label…');
    await field.click();
    // Neither focus on the empty field nor a typed prefix opens the dropdown.
    await expect(page.getByTestId('tag-input.suggestions')).toHaveCount(0);
    await field.fill('T10');
    await expect(page.getByTestId('tag-input.suggestions')).toHaveCount(0);
  });

  test('the test id is real — a free-form tag field does show the dropdown', async ({
    page,
  }) => {
    // `toHaveCount(0)` above is also satisfied by a renamed `data-testid`. This pins
    // the selector on a plain Tags field that needs no fixture.
    await page.goto('/knowledge/new');

    const tags = page.getByPlaceholder('Add tags…');
    await tags.click();
    await tags.fill('a-tag-that-does-not-exist-yet');
    await expect(page.getByTestId('tag-input.suggestions')).toBeVisible();
  });
});
