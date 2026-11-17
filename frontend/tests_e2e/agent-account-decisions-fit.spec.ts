import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Agent account, Decisions tab at 1024 px.
//
// A "left alone" row whose outcome is application_author carries the longest
// outcome label of the catalog: fr "Une application ou un bot l'a écrit",
// es "Lo escribió una aplicación o un bot", ja about 264 px. Drawn on one
// line, it pushed the table into a horizontal scroll inside its card in fr
// (18 px), es (23 px) and ja (70 px). The outcome badge now wraps.
//
// The account and its decisions are mocked so the page holds exactly the row
// the issue names; the signed-in user's language is overridden per locale.
// ---------------------------------------------------------------------------

const ACCOUNT_ID = '00000000-0000-4000-8000-000000007794';
const NOW = new Date().toISOString();

const ACCOUNT = {
  id: ACCOUNT_ID,
  user_id: '00000000-0000-4000-8000-0000000077ff',
  owner_name: null,
  name: 'Decisions fit',
  description: null,
  provider: 'microsoft_teams',
  provider_label: 'Microsoft Teams',
  integration_id: null,
  integration_name: null,
  agent_id: null,
  agent_name: null,
  mode: 'ai_user',
  enabled: true,
  response_mode: 'approval',
  ai_label_enabled: false,
  ai_label_text: null,
  ai_label_preview: '',
  ai_label_position: 'suffix',
  grace_period_seconds: 0,
  owner_takeover_minutes: 0,
  max_replies_per_hour: 10,
  context_messages: 10,
  active_hours: null,
  instructions: null,
  rules: [],
  reaction_feedback: {},
  notify_on_send: false,
  mailbox_actions: {},
  signature_source: 'none',
  signature_integration_id: null,
  signature_integration_name: null,
  account_external_id: null,
  account_display_name: null,
  account_email: null,
  transport: 'none',
  status: 'active',
  last_error: null,
  sync_report: null,
  last_event_at: null,
  last_reply_at: null,
  pending_count: 0,
  subscription_count: 0,
  can_manage: true,
  created_at: NOW,
  updated_at: NOW,
};

const ROW = {
  at: NOW,
  updated_at: NOW,
  message_at: NOW,
  respond: false,
  waited: false,
  conversation_type: 'channel',
  conversation_id: '19:ops',
  conversation_name: 'Engineering > Alerts',
  sender_id: 'app',
  sender_name: 'Workflows',
  preview: '[FIRING:1] Disk full on the build runner of the platform team',
  mentions_account: false,
  addressed: false,
  thread_continuation: false,
  rule_id: null,
  rule_name: null,
  sender: null,
  note: null,
  reply_id: null,
  reply_status: null,
  reply_text: null,
  reply_error: null,
};

const DECISIONS = {
  total: 3,
  items: [
    { ...ROW, id: 'd1', outcome: 'application_author', group: 'left', reason: 'application_author' },
    { ...ROW, id: 'd2', outcome: 'not_addressed', group: 'left', reason: 'not_addressed', sender_name: 'Alice' },
    { ...ROW, id: 'd3', outcome: 'queued', group: 'waiting', reason: null, sender_name: 'Bob' },
  ],
};

async function openDecisions(page: Page, language: string) {
  await page.route('**/api/v1/auth/me', async (route: Route) => {
    const response = await route.fetch();
    const me = await response.json();
    await route.fulfill({ response, json: { ...me, language } });
  });
  await page.route(
    (url) => url.pathname === `/api/v1/agent-accounts/${ACCOUNT_ID}`,
    (route: Route) => (route.request().method() === 'GET' ? route.fulfill({ json: ACCOUNT }) : route.fallback()),
  );
  await page.route(
    (url) => url.pathname === `/api/v1/agent-accounts/${ACCOUNT_ID}/decisions`,
    (route: Route) => route.fulfill({ json: DECISIONS }),
  );
  await page.goto(`/profile/agent-accounts/${ACCOUNT_ID}/decisions`);
}

test.describe('Agent account Decisions table at 1024 px', () => {
  for (const language of ['en-us', 'fr-fr', 'es-es', 'ja-jp']) {
    test(`fits its card with an application_author row in ${language}`, async ({ page }) => {
      await page.setViewportSize({ width: 1024, height: 900 });
      await openDecisions(page, language);

      const table = page.locator('table');
      await expect(table).toBeVisible({ timeout: 15_000 });
      await expect(table.locator('tbody tr')).toHaveCount(3);

      // The table fits its card: no horizontal scroll inside it.
      const scroller = table.locator('xpath=..');
      const overflow = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
