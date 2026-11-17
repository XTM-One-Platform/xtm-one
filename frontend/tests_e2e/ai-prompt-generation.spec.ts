import { test, expect, type Page } from '@playwright/test';
import { createScriptedAgent, deleteAgent, scriptedSkipReason } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';

/**
 * "Generate with AI" puts a model's answer into the field it is mounted against.
 *
 * `/ai/generate-prompt` calls `llm.chat` — no stream, no tools — and
 * `_build_messages` carries the typed description in a USER message, so a
 * marker typed into the box reaches the script.
 */

/** The persona block; both ids exist for this spec. */
const PERSONA = 'agent-persona';
const PERSONA_TOOLS = 'persona-tools';
/** The General card's Edit button. The persona block renders only in edit mode. */
const EDIT_GENERAL = 'edit-general';
/** The generator's input once expanded. `PLACEHOLDERS.persona`. */
const BRIEF_BOX = "Describe the agent's role and behavior...";

test.describe.serial('Generate with AI', () => {
  let scripted: ScriptedModel;
  let skipWhy = '';
  let agentId = '';

  test.beforeAll(async () => {
    // `require_ai_enabled` reads `is_ai_enabled`, published as `ai_enabled`.
    skipWhy = await scriptedSkipReason(
      'ai_enabled',
      'AI is off on this deployment; /ai/generate-prompt would answer 403.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    agentId = await createScriptedAgent(`E2E prompt generation ${Date.now().toString(36)}`, '');
  });

  test.afterAll(async () => {
    if (agentId) await deleteAgent(agentId);
    await scripted?.dispose();
  });

  async function openPersonaEditor(page: Page): Promise<void> {
    await page.goto(`/agents/${agentId}/config`);
    const edit = page.getByTestId(EDIT_GENERAL);
    await expect(edit, 'the General card offered no Edit button').toBeVisible({ timeout: 15_000 });
    await edit.click();
    await expect(page.getByTestId(PERSONA)).toBeVisible();
  }

  async function generateInto(page: Page, brief: string): Promise<void> {
    // The label tracks whether the field holds text; either spelling is this control's.
    await page.getByTestId(PERSONA_TOOLS).getByRole('button', { name: /(Generate|Improve) with AI/ }).click();
    const box = page.getByPlaceholder(BRIEF_BOX);
    await expect(box).toBeVisible();
    await box.fill(brief);
    // The generator's own submit; the Send button beside it is an unlabelled icon.
    await box.press('Enter');
  }

  test('the generated prompt lands in the persona field', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('genprompt');
    // Present in no frontend file, so on screen it came from the model.
    const generated = `You are a scripted persona. ${mark}`;
    await scripted.arm(mark, [{ text: generated }]);

    await openPersonaEditor(page);
    const persona = page.getByTestId(PERSONA);
    await expect(persona).toBeEmpty();

    await generateInto(page, `${mark} write a persona for a test agent`);

    await expect(persona).toHaveValue(generated, { timeout: 30_000 });
    await expect(page.getByPlaceholder(BRIEF_BOX)).toHaveCount(0);
  });

  test('the model is asked once, as a plain completion', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('genonce');
    await scripted.arm(mark, [{ text: `Scripted persona for ${mark}` }]);

    await openPersonaEditor(page);
    await generateInto(page, `${mark} write a persona`);
    await expect(page.getByTestId(PERSONA)).toHaveValue(`Scripted persona for ${mark}`, {
      timeout: 30_000,
    });

    // A double submit is invisible in the field and doubles model traffic.
    const { calls } = await scripted.calls();
    const mine = calls.filter((call) => call.matched === mark);
    expect(mine).toHaveLength(1);
    // Not an agent turn: resolving to the loop would fill the field just the same.
    expect(mine[0].streamed).toBe(false);
    expect(mine[0].tools_offered).toEqual([]);
  });
});
