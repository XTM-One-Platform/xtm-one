import { test, expect, type Page } from '@playwright/test';
import { SettingsLLMPage } from './model/settings-llm.page';
import { platformConfig, requireScriptedServer } from './helpers';
import { SCRIPTED_LLM_INTERNAL, SERVED_MODEL } from './scripted-llm/client';

// ---------------------------------------------------------------------------
// Settings — LLM provider add and remove.
//
// Smoke-tests the dialog UX only (open, provider selection, key field enables
// the Validate button, Escape closes). No providers are saved to keep the test
// isolated and avoid reliance on external credentials.
// ---------------------------------------------------------------------------

test.describe('Settings — AI Models tab', () => {
  test('should display the AI Models tab', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    // DetailTabs is the design system's Tabs now, so the strip exposes
    // role="tab" rather than the <Link> the hand-rolled version rendered.
    await expect(page.getByRole('tab', { name: 'AI and models' })).toBeVisible();
  });

  test('should show the Add Provider button for admins', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await expect(page.getByRole('button', { name: 'Add Provider' }).first()).toBeVisible({ timeout: 10_000 });
  });

  test('should open the Add Provider dialog', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog').getByText(/Add Provider|Configure/i).first()).toBeVisible();
  });

  test('should allow selecting a provider in the dialog', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();

    // Default provider is shown; we can change to Anthropic.
    await settingsPage.selectProvider('anthropic');
    await expect(page.getByRole('dialog').getByText(/Anthropic/i).first()).toBeVisible({ timeout: 5_000 });
  });

  test('should enable the Validate Provider button only when a key is entered', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();

    // Before typing — button should be disabled.
    const validateBtn = page.getByRole('button', { name: 'Validate Provider' });
    await expect(validateBtn).toBeDisabled();

    // After typing a fake key — button should become enabled.
    await settingsPage.fillApiKey('sk-ant-fake-key-for-e2e-test');
    await expect(validateBtn).toBeEnabled({ timeout: 5_000 });
  });

  // Every field of the dialog has an accessible name: a screen reader
  // announces what the field wants, clicking the label focuses the field, and
  // specs can reach it by label instead of by its placeholder copy.
  test('should label every field of the Add Provider dialog', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();
    const dialog = page.getByRole('dialog');

    await expect(dialog.getByRole('combobox', { name: 'Provider', exact: true })).toBeVisible();
    await expect(dialog.getByLabel('Display Name')).toBeVisible();
    await expect(dialog.getByLabel('API key', { exact: true })).toBeVisible();
    await expect(dialog.getByLabel('No API key required', { exact: true })).toBeVisible();

    // The Base URL field exists for the OpenAI-compatible provider.
    await settingsPage.selectProvider('Custom OpenAI-Compatible');
    const baseUrl = dialog.getByLabel('Base URL', { exact: true });
    await expect(baseUrl).toBeVisible({ timeout: 5_000 });
    await dialog.getByText('Base URL', { exact: true }).click();
    await expect(baseUrl).toBeFocused();
  });

  // The hint under each field is its accessible description, read by
  // a screen reader when the field gets focus.
  test('should describe the Add Provider fields by their hints', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();
    const dialog = page.getByRole('dialog');

    await expect(dialog.getByLabel('Display Name')).toHaveAccessibleDescription(
      'Custom name to distinguish this provider if you have multiple connections of the same type.',
    );

    await settingsPage.selectProvider('Custom OpenAI-Compatible');
    await expect(dialog.getByRole('combobox', { name: 'Provider', exact: true })).toHaveAccessibleDescription(
      'Works with vLLM, TGI, LocalAI, LM Studio, and any OpenAI-compatible endpoint.',
    );
    await expect(dialog.getByLabel('Base URL', { exact: true })).toHaveAccessibleDescription(
      /^Full URL to the OpenAI-compatible endpoint/,
    );
    await expect(dialog.getByLabel('API key', { exact: true })).toHaveAccessibleDescription(
      'Use none or dummy if authentication is not required.',
    );
  });

  test.describe('conditional fields are labelled too', () => {
    test.beforeEach(async ({ page }) => {
      // No agent default yet, so the dialog offers the Agent Default Model select.
      await page.route((url) => url.pathname === '/api/v1/settings/general-assistant', async (route) => {
        if (route.request().method() !== 'GET') return route.fallback();
        const response = await route.fetch();
        const json = await response.json();
        await route.fulfill({ response, json: { ...json, agent_default_model: null } });
      });
      // A validation that succeeds and discovers two models, without any real
      // provider behind it.
      await page.route((url) => url.pathname === '/api/v1/settings/llm-providers/validate', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            connection_ok: true,
            message: 'Connected.',
            models: ['e2e-model-a', 'e2e-model-b'],
            model_groups: null,
          }),
        }),
      );
    });

    // The proxied handlers above await a live route.fetch(). When a test ends
    // while the page still has one of those requests in flight, the page closes,
    // the fetched response is disposed and response.json() throws "Response has
    // been disposed" against the test. Drop the handlers first and let
    // any still running settle silently.
    test.afterEach(async ({ page }) => {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
    });

    test('the Gateway URL field', async ({ page }) => {
      const settingsPage = new SettingsLLMPage(page);
      await settingsPage.goto();
      await settingsPage.clickAddProvider();
      const dialog = page.getByRole('dialog');
      await settingsPage.selectProvider('Filigran AI Gateway');
      const gatewayUrl = dialog.getByLabel('Gateway URL', { exact: true });
      await expect(gatewayUrl).toBeVisible({ timeout: 5_000 });
      await dialog.getByText('Gateway URL', { exact: true }).click();
      await expect(gatewayUrl).toBeFocused();
      await expect(gatewayUrl).toHaveAccessibleDescription(/^Without a token, authenticates with this platform's XTM signing key/);
      const token = dialog.getByLabel('Token (optional)', { exact: true });
      await expect(token).toHaveAttribute('type', 'password');
      await expect(token).toHaveAccessibleDescription(/^When set, it is sent to the gateway instead of the XTM JWT/);
    });

    test('the discovered models and the Agent Default Model', async ({ page }) => {
      const settingsPage = new SettingsLLMPage(page);
      await settingsPage.goto();
      await settingsPage.clickAddProvider();
      const dialog = page.getByRole('dialog');
      await settingsPage.selectProvider('Custom OpenAI-Compatible');
      await dialog.getByLabel('Base URL', { exact: true }).fill('https://llm.example.com');
      await settingsPage.fillApiKey('sk-e2e-labels');
      await dialog.getByRole('button', { name: 'Validate Provider' }).click();

      const models = dialog.getByRole('group', { name: /Available Models/ });
      await expect(models).toBeVisible({ timeout: 5_000 });
      await expect(models.getByRole('switch', { name: 'e2e-model-a', exact: true })).toBeChecked();
      await expect(models.getByRole('switch', { name: 'e2e-model-b', exact: true })).toBeChecked();
      await expect(models).toHaveAccessibleDescription('Select which models to make available for agents and API proxy usage.');
      const defaultModel = dialog.getByRole('combobox', { name: 'Agent Default Model', exact: true });
      await expect(defaultModel).toBeVisible();
      await expect(defaultModel).toHaveAccessibleDescription(/^No agent default is set yet\./);
    });

    test('the Embedding Model field', async ({ page }) => {
      const settingsPage = new SettingsLLMPage(page);
      await settingsPage.goto();
      await settingsPage.clickAddProvider();
      const dialog = page.getByRole('dialog');
      // TEI (`/embed` servers) is embedding-only: once validated it asks for
      // the embedding model.
      await settingsPage.selectProvider('Embedding Server');
      await dialog.getByLabel('Base URL', { exact: true }).fill('https://embed.example.com');
      await settingsPage.fillApiKey('hf_e2e_labels');
      await dialog.getByRole('button', { name: 'Validate Provider' }).click();

      const embeddingModel = dialog.getByLabel(/^Embedding Model/);
      await expect(embeddingModel).toBeVisible({ timeout: 5_000 });
      // The label reads "Embedding Model (optional)" here (models were discovered).
      await dialog.getByText(/^Embedding Model/).click();
      await expect(embeddingModel).toBeFocused();
      await expect(embeddingModel).toHaveAttribute('aria-required', 'false');
      await expect(embeddingModel).toHaveAccessibleDescription(/^Used when provider model discovery is unavailable\./);
    });

    test('the Embedding Model field, when it is required', async ({ page }) => {
      // Nothing discovered: the typed model id is then the only source, so the
      // field is required - said by aria-required, not by the red asterisk.
      await page.route((url) => url.pathname === '/api/v1/settings/llm-providers/validate', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ connection_ok: true, message: 'Connected.', models: [], model_groups: null }),
        }),
      );
      const settingsPage = new SettingsLLMPage(page);
      await settingsPage.goto();
      await settingsPage.clickAddProvider();
      const dialog = page.getByRole('dialog');
      await settingsPage.selectProvider('Embedding Server');
      await dialog.getByLabel('Base URL', { exact: true }).fill('https://embed.example.com');
      await settingsPage.fillApiKey('hf_e2e_labels');
      await dialog.getByRole('button', { name: 'Validate Provider' }).click();

      // The asterisk is aria-hidden, so the name stays "Embedding Model".
      const embeddingModel = dialog.getByRole('textbox', { name: 'Embedding Model', exact: true });
      await expect(embeddingModel).toBeVisible({ timeout: 5_000 });
      await expect(embeddingModel).toHaveAttribute('aria-required', 'true');
      await expect(embeddingModel).toHaveAccessibleDescription(/^This provider exposes no model discovery, so a model id is required/);
    });

    test('the gateway models, grouped by upstream provider', async ({ page }) => {
      await page.route((url) => url.pathname === '/api/v1/settings/llm-providers/validate', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            connection_ok: true,
            message: 'Connected.',
            models: ['e2e-gateway-model-a', 'e2e-gateway-model-b'],
            model_groups: { openai: ['e2e-gateway-model-a'], anthropic: ['e2e-gateway-model-b'] },
          }),
        }),
      );
      const settingsPage = new SettingsLLMPage(page);
      await settingsPage.goto();
      await settingsPage.clickAddProvider();
      const dialog = page.getByRole('dialog');
      await settingsPage.selectProvider('Filigran AI Gateway');
      await dialog.getByLabel('Gateway URL', { exact: true }).fill('https://gateway.example.com');
      await dialog.getByRole('button', { name: 'Validate Provider' }).click();

      // Each upstream provider's heading names the group of its models.
      const models = dialog.getByRole('group', { name: /Available Models/ });
      await expect(models).toBeVisible({ timeout: 5_000 });
      await expect(
        models.getByRole('group', { name: 'OpenAI', exact: true }).getByRole('switch', { name: 'e2e-gateway-model-a', exact: true }),
      ).toBeChecked();
      await expect(
        models.getByRole('group', { name: 'Anthropic', exact: true }).getByRole('switch', { name: 'e2e-gateway-model-b', exact: true }),
      ).toBeChecked();
    });
  });

  test('should close the dialog on Escape', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();
    await expect(page.getByRole('dialog')).toBeVisible();

    await settingsPage.closeDialog();
    await expect(page.getByRole('dialog')).not.toBeVisible({ timeout: 5_000 });
  });

  test('should show the All Providers section', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await expect(page.getByText('All Providers').first()).toBeVisible({ timeout: 10_000 });
  });
});

// ---------------------------------------------------------------------------
// Settings — a provider card's own fields are labelled too
//
// The rename field and the model editor of a configured provider had the same
// gaps as the Add Provider dialog. Route mocking: one provider and the catalog
// its model editor lists.
// ---------------------------------------------------------------------------
test.describe('Settings — provider card fields', () => {
  const CARD_PROVIDER_ID = 'cccccccc-0000-0000-0000-000000000001';

  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/settings/llm-providers', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          id: CARD_PROVIDER_ID,
          provider: 'anthropic',
          display_name: 'Anthropic (labels)',
          default_model: null,
          available_models: ['e2e-card-model-a', 'e2e-card-model-b'],
          context_windows: {},
          has_key: true,
          is_from_env: false,
          models_env_locked: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }]),
      }),
    );
    await page.route(`**/api/v1/settings/llm-providers/${CARD_PROVIDER_ID}/models`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          provider: 'anthropic',
          models: ['e2e-card-model-a', 'e2e-card-model-b', 'e2e-card-model-c'],
        }),
      }),
    );
  });

  test('the rename field is reached by its label', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.gotoModels();
    await page.getByRole('button', { name: 'Rename', exact: true }).click();

    const displayName = page.getByLabel('Provider Display Name', { exact: true });
    await expect(displayName).toBeVisible();
    await page.getByText('Provider Display Name', { exact: true }).click();
    await expect(displayName).toBeFocused();
    await expect(displayName).toHaveAccessibleDescription('Leave empty to fallback to the default provider label.');
  });

  test('the model editor names its switches and context windows', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.gotoModels();
    // The provider card's Models button, not the "Models" tab of the AI and
    // models section row (#9173), which carries the same name.
    await page
      .getByRole('button', { name: 'Models', exact: true })
      .and(page.locator(':not([data-section])'))
      .click();

    const models = page.getByRole('group', { name: 'Manage Available Models', exact: true });
    await expect(models.getByRole('switch', { name: 'e2e-card-model-a', exact: true })).toBeChecked();
    await expect(models.getByRole('switch', { name: 'e2e-card-model-c', exact: true })).not.toBeChecked();
    // Each context-window field says which model it overrides.
    await expect(
      models.getByRole('textbox', { name: 'Context window for e2e-card-model-a', exact: true }),
    ).toBeVisible();
    await expect(
      models.getByRole('textbox', { name: 'Context window for e2e-card-model-b', exact: true }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Settings — the platform default pickers are labelled and described
//
// Route mocking: one provider offering a chat, an embedding and an image model,
// so every section shows its Configure button (the 7 model pickers plus the
// Agent runs block); each editor is then opened and its field is reached by the
// visible label, its hint read as the description.
// ---------------------------------------------------------------------------
test.describe('Settings — platform default pickers', () => {
  const PICKER_PROVIDER_ID = 'dddddddd-0000-0000-0000-000000000001';
  const model = (name: string) => ({ model: name, provider: 'openai', provider_id: PICKER_PROVIDER_ID });

  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/settings/llm-providers', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          id: PICKER_PROVIDER_ID,
          provider: 'openai',
          display_name: 'OpenAI (pickers)',
          default_model: 'e2e-chat-model',
          available_models: ['e2e-chat-model', 'e2e-embedding-model', 'e2e-image-model'],
          context_windows: {},
          has_key: true,
          is_from_env: false,
          models_env_locked: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }]),
      }),
    );
    await page.route('**/api/v1/settings/general-assistant', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agent_default_model: 'e2e-chat-model',
          agent_default_provider: 'openai',
          agent_default_provider_id: PICKER_PROVIDER_ID,
          env_managed_fields: [],
        }),
      });
    });
    await page.route('**/api/v1/settings/available-models', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([model('e2e-chat-model')]) }),
    );
    await page.route('**/api/v1/settings/available-embedding-models', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([model('e2e-embedding-model')]) }),
    );
    await page.route('**/api/v1/settings/available-image-models', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([model('e2e-image-model')]) }),
    );
    await page.route('**/api/v1/settings/available-decision-models', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ models: [model('e2e-decision-model')], listings: [] }) }),
    );
  });

  test('every picker is reached by its label and described by its hint', async ({ page }) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.gotoModels();

    // One Configure button per model picker of the Models sub-section (6: the
    // image picker lives under Media and the Agent runs block under Agents and
    // automations since #9173); opening one turns it into Save / Cancel.
    const configure = page.getByRole('button', { name: 'Configure', exact: true });
    for (let remaining = 6; remaining > 0; remaining--) {
      await expect(configure).toHaveCount(remaining, { timeout: 10_000 });
      await configure.first().click();
    }
    await expect(configure).toHaveCount(0);

    const pickers: [string, 'combobox' | 'spinbutton', string | RegExp][] = [
      ['Agent LLM Model', 'combobox', 'The primary reasoning model used by agents.'],
      ['Reasoning Effort Level', 'combobox', /^Controls how much compute budget is allocated to thinking\/reasoning\./],
      ['Routing Model', 'combobox', /routing is a simple classification task/],
      ['Decision Model', 'combobox', 'A decision model is given a list of choices, such as the elements on a page, and returns one of them instead of writing a reply.'],
      ['Fallback Model', 'combobox', 'Choose a model that stays available when your primary model is rate-limited or overloaded.'],
      // The embedding picker has no label of its own on screen; it is named like
      // the Add Provider field.
      ['Embedding Model', 'combobox', 'Model used for knowledge base embeddings and semantic search.'],
      ['Vector Dimensions', 'spinbutton', /^Number of dimensions used for KB vectors\./],
    ];
    for (const [name, role, description] of pickers) {
      const field = page.getByRole(role, { name, exact: true });
      await expect(field).toBeVisible();
      await expect(field).toHaveAccessibleDescription(description);
    }

    // A label click reaches its field.
    await page.getByText('Vector Dimensions', { exact: true }).click();
    await expect(page.getByRole('spinbutton', { name: 'Vector Dimensions', exact: true })).toBeFocused();

    // The image picker is the one Configure block of the Media sub-section.
    await page.goto('/settings/ai/media');
    await expect(configure).toHaveCount(1, { timeout: 10_000 });
    await configure.first().click();
    await expect(configure).toHaveCount(0);
    const image = page.getByRole('combobox', { name: 'Image Generation Model', exact: true });
    await expect(image).toBeVisible();
    await expect(image).toHaveAccessibleDescription('Model used for generating images from text prompts.');
  });
});

// ---------------------------------------------------------------------------
// Settings — All Providers "set as platform default" model tile
//
// Uses route mocking so no real LLM provider credentials are required.
// Verifies that clicking a non-default model tile in the All Providers card
// fires PUT /settings/general-assistant with the correct payload.
// ---------------------------------------------------------------------------
test.describe('Settings — All Providers set-platform-default tile', () => {
  const FAKE_PROVIDER_ID = 'aaaaaaaa-0000-0000-0000-000000000001';
  const FAKE_MODEL = 'claude-test-model';
  const CURRENT_DEFAULT = 'current-default-model';

  async function mockDeps(page: import('@playwright/test').Page) {
    // Mock LLM providers list — one provider with two models.
    await page.route('**/api/v1/settings/llm-providers', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          id: FAKE_PROVIDER_ID,
          provider: 'anthropic',
          display_name: 'Anthropic (test)',
          default_model: null,
          available_models: [CURRENT_DEFAULT, FAKE_MODEL],
          context_windows: {},
          has_key: true,
          is_from_env: false,
          models_env_locked: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }]),
      }),
    );

    // Mock GA config — current default is CURRENT_DEFAULT.
    await page.route('**/api/v1/settings/general-assistant', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            agent_default_model: CURRENT_DEFAULT,
            agent_default_provider: 'anthropic',
            agent_default_provider_id: FAKE_PROVIDER_ID,
            env_managed_fields: [],
          }),
        });
      } else {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      }
    });

    // Mock available models.
    await page.route('**/api/v1/settings/available-models', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { model: CURRENT_DEFAULT, provider: 'anthropic', provider_id: FAKE_PROVIDER_ID },
          { model: FAKE_MODEL, provider: 'anthropic', provider_id: FAKE_PROVIDER_ID },
        ]),
      }),
    );
  }

  test('non-default model tile shows "Click to set as platform default" tooltip text', async ({ page }) => {
    await mockDeps(page);
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();

    // The non-default tile button should be enabled.
    const nonDefaultTile = page.getByRole('button', { name: new RegExp(FAKE_MODEL) }).first();
    await expect(nonDefaultTile).toBeVisible({ timeout: 10_000 });
    await expect(nonDefaultTile).toBeEnabled();

    // Hovering it must actually surface the tooltip with the expected text —
    // asserting only "enabled" above would still pass if the tooltip content
    // regressed or was removed entirely.
    await nonDefaultTile.hover();
    await expect(page.getByRole('tooltip', { name: 'Click to set as platform default' })).toBeVisible({
      timeout: 2_000,
    });
  });

  test('current default model tile is disabled (already default)', async ({ page }) => {
    await mockDeps(page);
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();

    const defaultTile = page.getByRole('button', { name: new RegExp(CURRENT_DEFAULT) }).first();
    await expect(defaultTile).toBeVisible({ timeout: 10_000 });
    await expect(defaultTile).toBeDisabled();
  });

  test('clicking a non-default tile fires PUT /settings/general-assistant with correct payload', async ({ page }) => {
    await mockDeps(page);

    // Capture the PUT request body. Non-PUT requests fall back to the
    // GET mock registered by mockDeps (route.continue() would bypass it
    // and hit the real network).
    let capturedBody: Record<string, unknown> | null = null;
    await page.route('**/api/v1/settings/general-assistant', async (route) => {
      if (route.request().method() === 'PUT') {
        capturedBody = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      } else {
        await route.fallback();
      }
    });

    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();

    const nonDefaultTile = page.getByRole('button', { name: new RegExp(FAKE_MODEL) }).first();
    await expect(nonDefaultTile).toBeVisible({ timeout: 10_000 });
    await nonDefaultTile.click();

    await expect.poll(() => capturedBody, { timeout: 5_000 }).not.toBeNull();
    expect(capturedBody!['agent_default_model']).toBe(FAKE_MODEL);
    expect(capturedBody!['agent_default_provider']).toBe('anthropic');
    expect(capturedBody!['agent_default_provider_id']).toBe(FAKE_PROVIDER_ID);
  });
});

// ---------------------------------------------------------------------------
// Settings — deleting a provider names what replaces it
//
// Route mocking again: the confirmation reads the deletion impact, re-reads it
// for the replacement the admin picks, and the delete carries that same
// replacement so what is applied is what was shown.
// ---------------------------------------------------------------------------
test.describe('Settings — provider deletion confirmation', () => {
  const CLAUDE_ID = 'bbbbbbbb-0000-0000-0000-000000000001';
  const BACKUP_ID = 'bbbbbbbb-0000-0000-0000-000000000002';
  const DOOMED_ID = 'bbbbbbbb-0000-0000-0000-000000000003';

  const provider = (id: string, kind: string, name: string, models: string[]) => ({
    id,
    provider: kind,
    display_name: name,
    default_model: models[0],
    available_models: models,
    context_windows: {},
    has_key: true,
    is_from_env: false,
    models_env_locked: false,
  });
  const target = (id: string, kind: string, name: string, model: string) => ({
    provider_id: id,
    provider: kind,
    display_name: name,
    model,
  });
  const claude = target(CLAUDE_ID, 'anthropic', 'Claude', 'claude-sonnet-5');
  const backup = target(BACKUP_ID, 'openai', 'Backup', 'gpt-4o');
  const doomed = target(DOOMED_ID, 'openai', 'Production', 'gpt-5.1');
  const unchanged = { affected: false, action: 'unchanged', before: null, after: null, env_managed: false };

  const impactFor = (replacementId: string | null) => {
    const replacement = replacementId === BACKUP_ID ? backup : claude;
    return {
      provider_id: DOOMED_ID,
      provider: 'openai',
      display_name: 'Production',
      replacement,
      replacement_explicit: replacementId !== null,
      default_replacement_id: CLAUDE_ID,
      candidates: [claude, backup],
      chat_available: true,
      general_assistant: unchanged,
      // Backup also offers gpt-5.1, so the platform default keeps it there;
      // Claude does not, so it moves to Claude's default chat model.
      agent_default:
        replacementId === BACKUP_ID
          ? { affected: true, action: 'reseeded', before: doomed, after: { ...backup, model: 'gpt-5.1' }, env_managed: false, model_kept: true }
          : { affected: true, action: 'reseeded', before: doomed, after: claude, env_managed: false, model_kept: false },
      image_default: unchanged,
      embedding_default: { ...unchanged, fallback: null, fallback_same_model: false, knowledge_base_count: 0 },
      routing_model: unchanged,
      fallback_model: unchanged,
      decision_model: unchanged,
      agents: { count: 2, names: ['Analyst', 'Triage'] },
      users: { count: 0, names: [] },
      impact_fingerprint: replacementId === BACKUP_ID ? 'fp-backup' : 'fp-claude',
    };
  };

  const mockProviderSettings = async (page: Page) => {
    await page.route('**/api/v1/settings/llm-providers', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          provider(CLAUDE_ID, 'anthropic', 'Claude', ['claude-sonnet-5']),
          provider(BACKUP_ID, 'openai', 'Backup', ['gpt-4o', 'gpt-5.1']),
          provider(DOOMED_ID, 'openai', 'Production', ['gpt-5.1']),
        ]),
      }),
    );
    await page.route('**/api/v1/settings/general-assistant', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agent_default_model: 'gpt-5.1',
          agent_default_provider: 'openai',
          agent_default_provider_id: DOOMED_ID,
          env_managed_fields: [],
        }),
      }),
    );
    await page.route('**/api/v1/settings/available-models', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { model: 'claude-sonnet-5', provider: 'anthropic', provider_id: CLAUDE_ID },
          { model: 'gpt-4o', provider: 'openai', provider_id: BACKUP_ID },
          { model: 'gpt-5.1', provider: 'openai', provider_id: DOOMED_ID },
        ]),
      }),
    );
  };

  const openDeletion = async (page: Page) => {
    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.gotoModels();
    // Listed oldest first: Claude, Backup, Production.
    await page.getByRole('button', { name: 'Delete provider' }).nth(2).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Delete Production?')).toBeVisible();
    return dialog;
  };

  test('shows what each route becomes and deletes with the chosen replacement', async ({ page }) => {
    const impactRequests: (string | null)[] = [];
    let deleteUrl: URL | null = null;

    await mockProviderSettings(page);
    await page.route(
      (url) => url.pathname.endsWith(`/settings/llm-providers/${DOOMED_ID}/deletion-impact`),
      (route) => {
        const replacementId = new URL(route.request().url()).searchParams.get('replacement_provider_id');
        impactRequests.push(replacementId);
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(impactFor(replacementId)),
        });
      },
    );
    await page.route(
      (url) => url.pathname.endsWith(`/settings/llm-providers/${DOOMED_ID}`),
      (route) => {
        if (route.request().method() !== 'DELETE') return route.fallback();
        deleteUrl = new URL(route.request().url());
        return route.fulfill({ status: 204 });
      },
    );

    const dialog = await openDeletion(page);
    await expect(dialog.getByText('The platform default model becomes claude-sonnet-5, the default model of Claude, which does not list gpt-5.1.')).toBeVisible();
    await expect(
      dialog.getByText('2 agents use this provider directly and will follow the platform default model instead.'),
    ).toBeVisible();
    await expect(dialog.getByText('Analyst, Triage')).toBeVisible();
    expect(impactRequests[0]).toBeNull();
    // The rule's pick is proposed, under the label the searchable field is announced by.
    await expect(dialog.getByRole('combobox', { name: 'Replacement provider' })).toHaveValue('Claude (claude-sonnet-5)');

    await dialog.getByRole('combobox').click();
    await page.getByRole('option', { name: 'Backup (gpt-4o)' }).click();
    await expect(dialog.getByText('The platform default model keeps gpt-5.1, now on Backup.')).toBeVisible();
    expect(impactRequests).toContain(BACKUP_ID);

    await dialog.getByRole('button', { name: 'Delete provider' }).click();
    await expect.poll(() => deleteUrl?.searchParams.get('replacement_provider_id') ?? null).toBe(BACKUP_ID);
    // And the fingerprint of the preview it showed, so nothing else is applied.
    await expect.poll(() => deleteUrl?.searchParams.get('expected_impact') ?? null).toBe('fp-backup');
    await expect(dialog).not.toBeVisible();
  });

  test('reads the impact again when another change overtook it, and deletes once reconfirmed', async ({ page }) => {
    // Meanwhile Claude started offering gpt-5.1: the platform default now keeps it.
    const overtakenImpact = {
      ...impactFor(null),
      agent_default: { affected: true, action: 'reseeded', before: doomed, after: { ...claude, model: 'gpt-5.1' }, env_managed: false, model_kept: true },
      impact_fingerprint: 'fp-claude-kept',
    };
    let overtaken = false;
    const expectedImpacts: (string | null)[] = [];

    await mockProviderSettings(page);
    await page.route(
      (url) => url.pathname.endsWith(`/settings/llm-providers/${DOOMED_ID}/deletion-impact`),
      (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(overtaken ? overtakenImpact : impactFor(null)),
        }),
    );
    await page.route(
      (url) => url.pathname.endsWith(`/settings/llm-providers/${DOOMED_ID}`),
      (route) => {
        if (route.request().method() !== 'DELETE') return route.fallback();
        const expected = new URL(route.request().url()).searchParams.get('expected_impact');
        expectedImpacts.push(expected);
        if (expected !== overtakenImpact.impact_fingerprint) {
          overtaken = true;
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({
              detail: { code: 'deletion_impact_changed', message: 'What this deletion changes is no longer what was reviewed.' },
            }),
          });
        }
        return route.fulfill({ status: 204 });
      },
    );

    const dialog = await openDeletion(page);
    await expect(dialog.getByText('The platform default model becomes claude-sonnet-5, the default model of Claude, which does not list gpt-5.1.')).toBeVisible();

    await dialog.getByRole('button', { name: 'Delete provider' }).click();
    await expect(
      dialog.getByText('The provider was not deleted: another change was saved in the meantime, so what this deletion does has been read again. Review it, then confirm.'),
    ).toBeVisible();
    await expect(dialog.getByText('The platform default model keeps gpt-5.1, now on Claude.')).toBeVisible();

    await dialog.getByRole('button', { name: 'Delete provider' }).click();
    await expect(dialog).not.toBeVisible();
    expect(expectedImpacts).toEqual(['fp-claude', 'fp-claude-kept']);
  });
});

// ---------------------------------------------------------------------------
// Validate Provider, pressed: the dialog composes the `key||base_url`
// credential and the backend dials the endpoint for /v1/models.
// `validate_provider_with_key` takes no database session, so nothing here
// reaches the provider list.
// ---------------------------------------------------------------------------

test.describe('Settings — Validate Provider against the scripted model', () => {
  let skipWhy = '';

  test.beforeAll(async () => {
    // Only reachability: this dials the endpoint itself, so whether the
    // platform is pointed at it or seeds it as the default is beside the point.
    skipWhy = await requireScriptedServer();
    if (skipWhy) return;
    // An outbound call, so `require_ai_enabled` refuses it with a 403 while
    // AI is off. That gate publishes as `ai_enabled`, not the licence flag.
    if ((await platformConfig()).ai_enabled === false) {
      skipWhy = 'AI is off on this deployment; validation is refused rather than attempted.';
    }
  });

  test('validating discovers the model the endpoint serves', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const settingsPage = new SettingsLLMPage(page);
    await settingsPage.goto();
    await settingsPage.clickAddProvider();

    const dialog = page.getByRole('dialog');
    // `selectProvider` matches option text. The label is PROVIDER_DISPLAY_NAMES
    // in src/lib/constants.ts, written out because that module reaches
    // `import.meta.env` and the tests_e2e tsconfig cannot compile it.
    await settingsPage.selectProvider('Custom OpenAI-Compatible');

    // By placeholder: the Base URL label carries no `htmlFor` and the input no
    // id, so there is no accessible name to match. The endpoint as the
    // platform reaches it — the published address is refused from a container.
    await dialog.getByPlaceholder('https://my-server.example.com').fill(SCRIPTED_LLM_INTERNAL);
    // The scripted server authenticates nobody.
    await settingsPage.fillApiKey('none');

    const validate = page.getByRole('button', { name: 'Validate Provider' });
    await expect(validate).toBeEnabled();
    await validate.click();

    // The served id, not the catalogue's: discovery lists what /v1/models
    // answers, which is the server's own `MODEL_ID` whatever the deployment
    // named the model. Neither typed here nor known to the frontend.
    await expect(dialog.getByText(SERVED_MODEL, { exact: false }).first()).toBeVisible({
      timeout: 30_000,
    });

    // Abandoned, not saved: a provider row is deployment-global.
    await settingsPage.closeDialog();
    await expect(dialog).not.toBeVisible({ timeout: 5_000 });
  });
});
