/**
 * Inbound and outbound, as an operator sees them.
 *
 * Direction is the most consequential thing about a partner registration:
 * binding an inbound one to an agent EXPOSES that agent, binding an outbound
 * one gives it a tool. One action, opposite meanings — so the only defence
 * against configuring the wrong one is that the screens say which is which.
 * That is a claim about rendering, and it is the reason these branches belong
 * in one file rather than spread across the control-plane specs: every
 * assertion here is on something an operator reads, and every one of them has
 * a mirror on a registration facing the other way that must NOT say the same
 * thing.
 *
 * Both registrations live on one deployment, which is what makes the contrast
 * cheap to assert: the two Config tabs, the two Overview lines, the two Setup
 * Guides and the two agent Tooling sections are compared side by side in the
 * same test rather than asserted separately and hoped to differ.
 *
 * Everything else is built over the API. A UI test of its own setup is a slow
 * test of the API, and the fixtures here — three inbound registrations, two
 * outbound ones, an ordinary integration to contrast against — exist only so
 * a page has something to render.
 *
 * Three notes on how the assertions are written:
 *
 *   - Strings come from three sources and each is quoted as that source has
 *     it. `t_i18n` message ids ARE the English text and en.json is
 *     identity-mapped, so a key missing from the catalogue still renders in
 *     English; the Setup Guide's prose and every extra-field LABEL come raw
 *     from the backend catalogue with no translation at all.
 *   - Absence is asserted only after a positive anchor on the same page. Each
 *     A2A card gates itself on a direction lookup that resolves in its own
 *     request, so `toHaveCount(0)` taken too early passes for the wrong
 *     reason.
 *   - The order of the tests inside a describe.serial is load-bearing around
 *     the one test that presses Verify: the outbound panels are read
 *     unverified before it and against its verdict after it. Both say so
 *     where they rely on it.
 */

import type { APIRequestContext, Locator, Page } from '@playwright/test';
import { test, expect } from './fixtures';
import {
  a2aAvailable,
  bind,
  cleanup,
  createAgent,
  createInboundIntegration,
  createOutboundIntegration,
  exposureOf,
  mintCredential,
  registrationState,
  stamp,
  tracker,
  type Created,
} from './seed';
import { IntegrationPage } from '../model/integration.page';

// ---------------------------------------------------------------------------
// Local helpers. The harness has no UI vocabulary and this is the only spec
// that needs one, so these stay here rather than growing seed.ts a page
// object the other lanes would never call.
// ---------------------------------------------------------------------------

/**
 * The Card whose title begins with `title`.
 *
 * A Card is a plain `div` with no role, so it is addressed by `data-slot`
 * rather than by the class that paints it — the surface class is a design
 * decision and changed under this locator once already. Scoping matters more
 * here than anywhere else in the suite: "1 tools" and "Verify now" are
 * meaningful only inside the card that owns them, and an unscoped `getByText`
 * would happily read them off another.
 */
function cardTitled(page: Page, title: string): Locator {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: new RegExp(`^${title}`) }) })
    .first();
}

/**
 * One integration's row in an agent's binding list — the Overview list, the
 * Tooling read list and the Tooling edit grid all share the shape.
 *
 * The rows are plain divs, so they are addressed by `data-slot` rather than by
 * the corner radius they happen to share with their Card — that class is a
 * design decision, and it moved. All four surfaces this helper is used on carry
 * the same slot — the Overview list, the Tooling read list, the Tooling edit
 * grid and its template variant — while the MCP and built-in tiles beside them
 * deliberately do not.
 */
function bindingTile(page: Page, integrationName: string): Locator {
  return page
    .locator('[data-slot="integration-binding"]')
    .filter({ hasText: integrationName })
    .last();
}

/** An ordinary, non-A2A provider to put beside an A2A binding. */
async function ordinaryProvider(rest: APIRequestContext): Promise<{ provider: string; name: string }> {
  const resp = await rest.get('/api/v1/integrations/catalog?category=integration');
  if (!resp.ok()) throw new Error(`catalog: ${resp.status()} ${await resp.text()}`);
  const catalog = (await resp.json()) as {
    provider: string;
    name: string;
    auth_type: string;
    fields?: string[];
    is_disabled?: boolean;
    is_registration_only?: boolean;
    a2a_directions?: string[];
  }[];
  const item = catalog.find(
    (c) =>
      c.auth_type === 'api_key' &&
      (c.fields ?? []).includes('api_key') &&
      !c.is_disabled &&
      !c.is_registration_only &&
      !c.a2a_directions,
  );
  if (!item) {
    throw new Error('No ordinary API-key provider in the catalog to contrast an A2A binding against.');
  }
  return { provider: item.provider, name: item.name };
}

async function createOrdinaryIntegration(
  rest: APIRequestContext,
  created: Created,
  provider: string,
  name: string,
): Promise<string> {
  const resp = await rest.post('/api/v1/integrations', {
    data: { provider, name, auth_type: 'api_key', api_key: 'e2e-not-a-real-key' },
  });
  if (!resp.ok()) throw new Error(`create ordinary integration: ${resp.status()} ${await resp.text()}`);
  const body = await resp.json();
  created.integrations.push(body.id);
  return body.id as string;
}

// ---------------------------------------------------------------------------

test.describe.serial('A2A in the UI: inbound versus outbound', () => {
  const onA = tracker();
  const onB = tracker();

  let available = false;

  /** Inbound: minted, bound to an exposed agent. The working one. */
  let exposedReg = { id: '', name: '' };
  /** Inbound: nothing minted, nothing bound. The registration an operator just made. */
  let idleReg = { id: '', name: '' };
  /** Inbound: the same, walked from blocker to blocker without touching the UI. */
  let blockedReg = { id: '', name: '' };
  /**
   * A second inbound registration on the same exposed agent: one agent, two
   * partners, still one exposure. The Agent card URLs card counts the agents
   * bound to a registration, so `exposedReg` still counts 1.
   */
  let secondReg = { id: '', name: '' };
  let exposedAgent = { id: '', name: '' };
  let cardUrl = '';
  let clientId = '';
  /** `name` is the provider's catalog label; `instance` is the registration's own name. */
  let ordinaryInbound = { provider: '', name: '', id: '', instance: '' };

  /**
   * Outbound: pointed at the exposed agent's own card, and unverified until
   * the test that presses Verify. The suite parks the re-verification sweep
   * (SWEEP_IS_FAST in instance.ts says why), and the only other writers of a
   * verdict are that button and the delegation pre-flight, which nothing here
   * reaches — so one fixture serves both states, read in the order this file
   * declares them.
   */
  let outboundReg = { id: '', name: '' };
  /** An outbound registration nobody gave a card URL. */
  let emptyReg = { id: '', name: '' };
  let delegatingAgent = { id: '', name: '' };
  let ordinaryOutbound = { provider: '', name: '', id: '', instance: '' };

  test.beforeAll(async ({ one }) => {
    available = (await a2aAvailable(one.rest)) && (await a2aAvailable(one.rest));
    if (!available) return;

    // ---- A, the side that exposes -----------------------------------------
    exposedReg = await createInboundIntegration(one.rest, onA, stamp('E2E ui inbound exposed'));
    idleReg = await createInboundIntegration(one.rest, onA, stamp('E2E ui inbound idle'));
    blockedReg = await createInboundIntegration(one.rest, onA, stamp('E2E ui inbound blocked'));
    secondReg = await createInboundIntegration(one.rest, onA, stamp('E2E ui inbound second'));

    const credential = await mintCredential(one.rest, exposedReg.id);
    clientId = credential.clientId;

    ordinaryInbound = { ...(await ordinaryProvider(one.rest)), id: '', instance: stamp('E2E ui ordinary A') };
    ordinaryInbound.id = await createOrdinaryIntegration(
      one.rest,
      onA,
      ordinaryInbound.provider,
      ordinaryInbound.instance,
    );

    exposedAgent = await createAgent(one.rest, onA, { name: stamp('E2E ui exposed agent') });
    await bind(one.rest, exposedAgent.id, {
      oauth_a2a: [exposedReg.id, secondReg.id],
      [ordinaryInbound.provider]: [ordinaryInbound.id],
    });
    cardUrl = (await exposureOf(one.rest, exposedAgent.id)).card_url ?? '';
    expect(cardUrl, 'binding the inbound registration should have published a card URL').toBeTruthy();

    // A tool scope on the A2A provider is legacy data the picker can no
    // longer create, and it is the only way to reach the read-mode branch
    // that suppresses the chip for A2A while showing it for everything else.
    // Without one on BOTH providers there is nothing to compare.
    // The ordinary scope names two tools the provider really exposes: an
    // unknown name refuses the whole write (#12126), which would store no
    // scope at all and leave the read-mode count with nothing to show.
    const toolsResp = await one.rest.get(`/api/v1/integrations/${ordinaryInbound.id}/tools`);
    if (!toolsResp.ok()) throw new Error(`ordinary tools: ${toolsResp.status()} ${await toolsResp.text()}`);
    const ordinaryTools = ((await toolsResp.json()) as { tools: { name: string }[] }).tools.map((tool) => tool.name);
    expect(ordinaryTools.length, 'the ordinary provider should expose at least two tools').toBeGreaterThanOrEqual(2);
    const scoped = await one.rest.put(`/api/v1/agents/${exposedAgent.id}`, {
      data: {
        integration_allowed_tools: {
          [ordinaryInbound.provider]: ordinaryTools.slice(0, 2),
          oauth_a2a: ['ask_the_caller'],
        },
      },
    });
    if (!scoped.ok()) throw new Error(`store the tool scopes: ${scoped.status()} ${await scoped.text()}`);

    // ---- B, the side that reaches out -------------------------------------
    const tokenUrl = `${one.origin}/mcp/oauth/token`;
    outboundReg = await createOutboundIntegration(one.rest, onB, {
      cardUrls: cardUrl,
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E ui outbound'),
    });
    emptyReg = await createOutboundIntegration(one.rest, onB, {
      cardUrls: '',
      tokenUrl,
      clientId: credential.clientId,
      clientSecret: credential.clientSecret,
      name: stamp('E2E ui outbound no card'),
    });

    ordinaryOutbound = { ...(await ordinaryProvider(one.rest)), id: '', instance: stamp('E2E ui ordinary B') };
    ordinaryOutbound.id = await createOrdinaryIntegration(
      one.rest,
      onB,
      ordinaryOutbound.provider,
      ordinaryOutbound.instance,
    );

    delegatingAgent = await createAgent(one.rest, onB, { name: stamp('E2E ui delegating agent') });
    await bind(one.rest, delegatingAgent.id, {
      oauth_a2a: [outboundReg.id],
      [ordinaryOutbound.provider]: [ordinaryOutbound.id],
    });
  });

  test.afterAll(async ({ one }) => {
    await cleanup(one.rest, onA);
    await cleanup(one.rest, onB);
  });

  test.beforeEach(() => {
    // One probe, one guard. NOT the integration catalog: it lists all three
    // A2A providers whatever the edition, so a catalog probe reports "available"
    // on a deployment where every A2A call is about to answer 403.
    test.skip(!available, 'A2A is not licensed on this deployment');
  });

  // -------------------------------------------------------------------------
  // The creation flow. It renders identically whichever direction you are
  // about to pick — there is no stored direction yet to differ over.
  // -------------------------------------------------------------------------

  test('the catalog offers three A2A mechanisms and none of them mentions a direction', async ({ one }) => {
    const page = one.page;
    await page.goto('/integrations/new');
    await page.getByTestId('provider-card').first().waitFor({ state: 'visible' });
    await page.getByPlaceholder('Search integrations...').fill('A2A');

    await expect(page.getByTestId('provider-card')).toHaveCount(3);
    await expect(page.getByRole('heading', { name: 'Identity & Device Security' })).toBeVisible();
    for (const name of ['OAuth A2A', 'Foundry A2A', 'Google Agent Platform A2A']) {
      await expect(page.getByText(name, { exact: true })).toBeVisible();
    }

    const tile = page.getByTestId('provider-card').filter({ hasText: 'OAuth A2A' });
    await expect(tile.getByText('Identity', { exact: true })).toBeVisible();
    await expect(tile.getByText('Agent to Agent (A2A)')).toBeVisible();
    // No platform-side OAuth defaults, so no promise of a one-click connect.
    await expect(tile.getByText('Ready to connect')).toHaveCount(0);
    // Direction is asked on the next step and nowhere on the tile.
    await expect(tile.getByText('Direction')).toHaveCount(0);

    // The auth facet is read off a mechanism nothing has connected yet. A
    // tile with integrations behind it shows that count in the facet's place,
    // and by the time this file runs the earlier ones have connected several
    // OAuth A2A registrations — so asserting it there would be asserting on
    // the run order rather than on the catalog.
    const untouched = page.getByTestId('provider-card').filter({ hasText: 'Foundry A2A' });
    await expect(untouched.getByText('No credentials')).toBeVisible();
  });

  test('the direction picker states the consequence of each direction, and only A2A gets one', async ({ one }) => {
    const page = one.page;
    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider('OAuth A2A');

    await expect(page.getByText('Direction', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /Inbound: a partner tasks our agents/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Outbound: our agents task a partner/ })).toBeVisible();
    await expect(
      page.getByText('Binding this to an agent exposes that agent to the partner.'),
    ).toBeVisible();
    await expect(
      page.getByText('Binding this to an agent gives it a tool to delegate work to the partner.'),
    ).toBeVisible();
    await expect(
      page.getByText(
        'An integration serves one direction. Changing it later requires unbinding it from every agent first.',
      ),
    ).toBeVisible();

    // `a2a_directions` is the only marker: a provider without it renders no
    // block at all. This is the half a regression that leaked the field onto
    // every catalog entry would break.
    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider(ordinaryInbound.name);
    await expect(page.getByText('Direction', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /tasks our agents|task a partner/ })).toHaveCount(0);
  });

  test('the form opens on inbound, which asks for nothing at all', async ({ one }) => {
    const page = one.page;
    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider('OAuth A2A');

    // Seeded from a2a_directions[0], so the form opens on the inbound shape
    // rather than on an empty state the operator has to resolve.
    await expect(
      page.getByRole('button', { name: /Inbound: a partner tasks our agents/ }),
    ).toHaveAttribute('aria-pressed', 'true');

    // Inbound declares no extra fields, so the auth_type "none" branch falls
    // through to the generic notice and the form collects Name and Tags only.
    await expect(page.getByText('Credentials', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Show', exact: true })).toBeVisible();
    await expect(
      page.getByText(
        "No credentials needed: this integration will use the provider's free unauthenticated API (may be rate limited).",
      ),
    ).toBeVisible();
    for (const name of ['Partner agent card URLs', 'Token URL', 'Client ID', 'Client secret']) {
      await expect(page.getByLabel(name, { exact: true })).toHaveCount(0);
    }
  });

  test('choosing outbound replaces the empty form with the partner\'s four fields', async ({ one }) => {
    const page = one.page;
    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider('OAuth A2A');
    await page.getByRole('button', { name: /Outbound: our agents task a partner/ }).click();

    // The field's name is read off the catalog DESCRIPTION (its explanation
    // goes to the info bubble, or off the key when the description is a
    // sentence), straight from the backend with no translation in between.
    const cardUrls = page.getByLabel('Partner agent card URLs', { exact: true });
    const tokenUrl = page.getByLabel('Token URL', { exact: true });
    const clientId = page.getByLabel('Client ID', { exact: true });
    const clientSecret = page.getByLabel('Client secret', { exact: true });
    await expect(cardUrls).toBeVisible();
    await expect(tokenUrl).toBeVisible();
    await expect(clientId).toBeVisible();
    await expect(clientSecret).toBeVisible();

    // Only the secret is masked: _urls, _url and _id are exempted by name.
    await expect(cardUrls).toHaveAttribute('type', 'text');
    await expect(tokenUrl).toHaveAttribute('type', 'text');
    await expect(clientId).toHaveAttribute('type', 'text');
    await expect(clientSecret).toHaveAttribute('type', 'password');
    await expect(page.getByText('No credentials needed:')).toHaveCount(0);

    // A variant's extra_fields REPLACE the base map rather than merging with
    // it, so switching back empties the form instead of keeping four inputs
    // the inbound direction would never send.
    await page.getByRole('button', { name: /Inbound: a partner tasks our agents/ }).click();
    await expect(cardUrls).toHaveCount(0);
    await expect(page.getByText('No credentials needed:')).toBeVisible();
  });

  test('the Setup Guide switches procedure with the direction, under one title', async ({ one }) => {
    const page = one.page;
    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider('OAuth A2A');

    await page.getByRole('button', { name: 'Setup guide' }).click();
    const guide = page.getByRole('dialog');
    await expect(guide.getByText('OAuth A2A Setup')).toBeVisible();
    await expect(guide.getByText('Step-by-step instructions to configure OAuth A2A.')).toBeVisible();
    await expect(guide.getByText('Setup Steps')).toBeVisible();
    await expect(guide.getByRole('link', { name: 'Open OAuth console' })).toBeVisible();
    await expect(guide.getByText(/The secret is shown ONCE, at that moment/)).toBeVisible();
    await expect(guide.getByText(/mcp\/oauth\/token/)).toHaveCount(0);
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: /Outbound: our agents task a partner/ }).click();
    await page.getByRole('button', { name: 'Setup guide' }).click();
    // The title is identical either way, so the steps are the discriminator.
    await expect(guide.getByText('OAuth A2A Setup')).toBeVisible();
    await expect(guide.getByText(/mcp\/oauth\/token/)).toBeVisible();
    await expect(guide.getByText(/The secret is shown ONCE, at that moment/)).toHaveCount(0);
  });

  // -------------------------------------------------------------------------
  // The detail page, both sides at once.
  // -------------------------------------------------------------------------

  test('the Overview names its own direction in words, and counts no tools either way', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}`);
    await expect(one.page.getByText('Inbound: this partner tasks our agents')).toBeVisible();
    await expect(
      one.page.getByText('Binding it to an agent exposes that agent to the partner.'),
    ).toBeVisible();
    await expect(one.page.getByText('Outbound: our agents task this partner')).toHaveCount(0);

    await one.page.goto(`/integrations/${outboundReg.id}`);
    await expect(one.page.getByText('Outbound: our agents task this partner')).toBeVisible();
    await expect(
      one.page.getByText('Binding it to an agent gives that agent a delegation tool.'),
    ).toBeVisible();
    await expect(one.page.getByText('Inbound: this partner tasks our agents')).toHaveCount(0);

    // A non-difference worth pinning: the tool counters are dropped for BOTH
    // directions, because a permanent zero would read as a broken binding.
    // Scoped to the page body — the sidebar has a leaf called "Tools" and it
    // is not what this is about.
    // One browser, so "both directions" means two visits.
    for (const id of [exposedReg.id, outboundReg.id]) {
      await one.page.goto(`/integrations/${id}`);
      const body = one.page.locator('main');
      await expect(body.getByText('Agents', { exact: true })).toBeVisible();
      await expect(body.getByText('Last Activity')).toBeVisible();
      await expect(body.getByText('Tools', { exact: true })).toHaveCount(0);
      await expect(body.getByText('Tool Calls')).toHaveCount(0);
      await expect(body.getByText('No partner tasks yet')).toBeVisible();
    }
  });

  test('the detail page picks the guide for the direction it stored', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}`);
    await one.page.getByRole('button', { name: 'Setup guide' }).click();
    await expect(
      one.page.getByRole('dialog').getByText(/The secret is shown ONCE, at that moment/),
    ).toBeVisible();

    await one.page.goto(`/integrations/${outboundReg.id}`);
    await one.page.getByRole('button', { name: 'Setup guide' }).click();
    const guideB = one.page.getByRole('dialog');
    await expect(guideB.getByText(/mcp\/oauth\/token/)).toBeVisible();
    await expect(guideB.getByText(/Bind it to each agent that should be able to delegate/)).toBeVisible();
  });

  test('the partner-facing cards render outbound only, and the generic cards render neither way', async ({ one }) => {
    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    await expect(one.page.getByText('Partner verification')).toBeVisible();
    await expect(one.page.getByText('Work sent to this partner')).toBeVisible();

    await one.page.goto(`/integrations/${exposedReg.id}/config`);
    // Anchor on an inbound-only card first: every one of these gates itself
    // on the same direction lookup, so an absence read before it settles
    // would pass whatever the direction turned out to be.
    await expect(one.page.getByText('Partner reachability')).toBeVisible();
    await expect(one.page.getByText('Partner verification')).toHaveCount(0);
    await expect(one.page.getByText('Work sent to this partner')).toHaveCount(0);

    // Neither direction gets the generic trio: A2A swaps the tool card for
    // its own pair, and the three A2A entries declare no events and no
    // webhook path. One visit per direction.
    for (const id of [outboundReg.id, exposedReg.id]) {
      await one.page.goto(`/integrations/${id}/config`);
      await expect(one.page.getByText('Available Tools')).toHaveCount(0);
      await expect(one.page.getByText('Event Triggers')).toHaveCount(0);
      await expect(one.page.getByText('Webhooks')).toHaveCount(0);
    }
  });

  test('the operator-facing cards render inbound only', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}/config`);
    for (const heading of ['Agent card URLs', 'Partner credential', 'Partner reachability', 'How partners use it']) {
      await expect(one.page.getByText(heading)).toBeVisible();
    }
    // The usage narrative is inbound prose end to end.
    await expect(one.page.getByText('1. Discover the agent')).toBeVisible();
    await expect(one.page.getByText('2. Authenticate')).toBeVisible();
    await expect(one.page.getByText('3. Submit and poll')).toBeVisible();
    await expect(one.page.getByText('Bounds enforced on every request')).toBeVisible();
    // The operator-visible half of the tool-cap decision. Binding an agent
    // inbound hands the partner that agent's whole tool set, and its handover
    // peers' — stated where the binding is configured rather than left to be
    // discovered, because no card or credential surface says it.
    await expect(
      one.page.getByText(/whole tool set, and its handover peers'/),
    ).toBeVisible();

    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    await expect(one.page.getByText('Partner verification')).toBeVisible();
    for (const heading of ['Agent card URLs', 'Partner credential', 'Partner reachability', 'How partners use it']) {
      await expect(one.page.getByText(heading)).toHaveCount(0);
    }
  });

  test('"What this exposes" lists seven protocol methods inbound and one callback route outbound', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}/config`);
    const inbound = cardTitled(one.page, 'What this exposes');
    await expect(inbound.getByText('7 endpoints')).toBeVisible();
    await expect(inbound.getByText('Method', { exact: true })).toBeVisible();
    for (const method of [
      'message/send',
      'tasks/get',
      'tasks/cancel',
      'tasks/pushNotificationConfig/set',
      'tasks/pushNotificationConfig/get',
      'tasks/pushNotificationConfig/list',
      'tasks/pushNotificationConfig/delete',
    ]) {
      await expect(inbound.locator('code', { hasText: method })).toHaveCount(1);
    }
    await expect(
      inbound.getByText(
        'What a federated partner may invoke on an agent this configuration is bound to. Every other JSON-RPC method is refused.',
      ),
    ).toBeVisible();

    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    const outbound = cardTitled(one.page, 'What this exposes');
    // "1 endpoints": the badge has no ICU plural, which is what a single row
    // renders as.
    await expect(outbound.getByText('1 endpoints')).toBeVisible();
    await expect(outbound.getByText('Endpoint', { exact: true })).toBeVisible();
    await expect(outbound.locator('code', { hasText: 'POST /a2a/callbacks/{delegation_id}' })).toHaveCount(1);

    // Whether this deployment declares itself reachable is an environment
    // decision, so the verdict is read from the API rather than assumed: what
    // is being asserted is that the badge and the paragraph agree with it.
    const state = await registrationState(one.rest, outboundReg.id);
    if (state.callbacks_reachable === true) {
      await expect(outbound.getByText('Reachable', { exact: true })).toBeVisible();
      await expect(
        outbound.getByText(
          'What a partner may call back on when work you delegated finishes. Registering one tells the partner where this deployment lives, which in an outbound-only relationship it would otherwise never learn.',
        ),
      ).toBeVisible();
    } else {
      await expect(outbound.getByText('Not reachable')).toBeVisible();
      await expect(
        outbound.getByText(
          'Nothing is exposed: this deployment is not reachable, so no callback is registered and every delegation is followed by asking. Set A2A_CLIENT_CALLBACK_BASE_URL, or A2A_CLIENT_CALLBACKS_REACHABLE to refuse deliberately.',
        ),
      ).toBeVisible();
    }
  });

  test('"What this brings your agents" offers one fixed tool inbound and nothing outbound until a partner agent verifies', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}/config`);
    const inbound = cardTitled(one.page, 'What this brings your agents');
    await expect(inbound.getByText('1 tools')).toBeVisible();
    await expect(inbound.getByText('Tool', { exact: true })).toBeVisible();
    await expect(inbound.locator('code', { hasText: 'ask_the_caller' })).toHaveCount(1);
    await expect(
      inbound.getByText(
        'Offered only while an external task is running: there is no caller to ask in an ordinary conversation. A2A blocks a task on its calling agent, so the question is answered by a machine and never waits for a person.',
      ),
    ).toBeVisible();

    // Nothing has verified this registration yet — the suite parks the sweep,
    // and Verify is pressed further down this file. The panel promises only
    // what the model is actually offered, so it counts nothing and says so;
    // "verifying turns the panel into what the partner declares" below is
    // where the two tools appear. Held over the API too, on registrations
    // whose targets stay unusable however often they are verified:
    // 03-outbound-registration ("the tools panel names nothing for targets no
    // agent can use") and 10-negatives ("an unverified partner is advertised
    // as bringing nothing").
    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    const outbound = cardTitled(one.page, 'What this brings your agents');
    await expect(
      cardTitled(one.page, 'Partner verification').getByText('Not verified yet: use Verify now.'),
    ).toBeVisible();
    await expect(outbound.getByText('0 tools')).toBeVisible();
    await expect(outbound.locator('code')).toHaveCount(0);
    await expect(
      outbound.getByText('None yet: no agent at this partner has verified.'),
    ).toBeVisible();

    // The description states the rule whether or not it has anything to
    // describe yet, which is how an operator learns what pressing Verify buys.
    await expect(
      outbound.getByText(
        'One per verified agent at this partner, named so a model can choose between them. Each submits, follows and concludes one unit of work; cancellation and callbacks are its internals, not separate tools.',
      ),
    ).toBeVisible();
  });

  test('the outbound Configuration card hides the card URLs it still lets you edit', async ({ one }) => {
    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    await expect(one.page.getByText('Partner verification')).toBeVisible();

    // The verification card lists one panel per agent, so repeating the raw
    // list as one undifferentiated row would say less and disagree the moment
    // discovery adds an agent. The token endpoint keeps its row.
    await expect(
      one.page.getByText(
        'Partner token endpoint, where the client id and secret are exchanged for a bearer (required)',
      ),
    ).toBeVisible();
    await expect(one.page.getByText('Partner agent card URLs —')).toHaveCount(0);

    // Hidden from the read view, never from the edit form: a partner without
    // discovery is how the list gets maintained.
    await one.page.getByRole('button', { name: 'Edit', exact: true }).click();
    await expect(one.page.getByLabel('Partner agent card URLs', { exact: true })).toHaveValue(cardUrl);
    await expect(one.page.getByPlaceholder('Stored: type to replace')).toHaveCount(1);
  });

  test('the inbound Configuration card leaves the client id to the credential card', async ({ one }) => {
    await one.page.goto(`/integrations/${exposedReg.id}/config`);
    await expect(one.page.getByText('Partner credential')).toBeVisible();
    // Minting writes the client id into extra_config, where the generic list
    // would repeat it. Exactly one occurrence means the credential card owns
    // it and the configuration card dropped it.
    await expect(one.page.getByText(clientId, { exact: true })).toHaveCount(1);
    await expect(cardTitled(one.page, 'Partner credential').getByText(clientId)).toBeVisible();
  });

  test('no direction control survives creation', async ({ one }) => {
    for (const [page, id, anchor] of [
      [one.page, exposedReg.id, 'Partner reachability'],
      [one.page, outboundReg.id, 'Partner verification'],
    ] as const) {
      await page.goto(`/integrations/${id}/config`);
      await expect(page.getByText(anchor)).toBeVisible();
      await expect(page.getByText('Direction', { exact: true })).toHaveCount(0);
      await expect(
        page.getByRole('button', { name: /tasks our agents|task (a|this) partner/ }),
      ).toHaveCount(0);
    }

    // So review-scenario 5 is not reachable through the UI at all. The server
    // does accept the change over the API, and refuses it while an agent
    // still binds the registration — the half the missing control stands in
    // for, asserted in 10-negatives ("direction cannot change while an agent
    // binds the registration, and the refusal writes nothing").
  });

  // -------------------------------------------------------------------------
  // The inbound operator's page: card URLs, credential, reachability.
  // -------------------------------------------------------------------------

  test('the Agent card URLs card counts the agents bound to the registration', async ({ one }) => {
    const page = one.page;
    await page.goto(`/integrations/${exposedReg.id}/config`);
    const bound = cardTitled(page, 'Agent card URLs');
    await expect(bound.getByText('1', { exact: true })).toBeVisible();
    await expect(bound.getByText(exposedAgent.name, { exact: true })).toBeVisible();
    await expect(bound.locator('code', { hasText: '/.well-known/agent-card.json' })).toHaveCount(1);
    await expect(
      bound.getByText('One card per exposed agent. The credential below is shared by all of them.'),
    ).toBeVisible();

    await page.goto(`/integrations/${idleReg.id}/config`);
    const unbound = cardTitled(page, 'Agent card URLs');
    await expect(unbound.getByText('0', { exact: true })).toBeVisible();
    await expect(
      unbound.getByText('Bind this integration to an agent to publish a card the partner can fetch.'),
    ).toBeVisible();
    await expect(unbound.locator('code', { hasText: '/.well-known/agent-card.json' })).toHaveCount(0);
  });

  test('the credential is minted once and afterwards only re-keyed', async ({ one }) => {
    const page = one.page;
    await page.goto(`/integrations/${idleReg.id}/config`);
    const card = cardTitled(page, 'Partner credential');

    await expect(
      card.getByText(
        'The partner exchanges these at the token URL on the agent card for a short-lived bearer token. One credential per partner: it works on every agent this integration is bound to. Regenerating the secret keeps the client ID.',
      ),
    ).toBeVisible();
    await expect(
      card.getByText('No credential yet: generate one so the partner has something to authenticate with.'),
    ).toBeVisible();
    await expect(
      card.getByText(
        'The secret is shown only once, when it is generated. If it is lost, regenerate it: the client ID stays, the old secret stops working immediately.',
      ),
    ).toBeVisible();

    // The first mint asks nothing: there is no working secret to retire.
    await card.getByRole('button', { name: 'Generate credential' }).click();
    await expect(card.getByText('Copy this now: the secret is never shown again')).toBeVisible();
    await expect(card.getByText('Client secret')).toBeVisible();
    // The pair is shown together, once, with one action that copies both.
    await expect(card.locator('code', { hasText: /^a2a-/ })).toHaveCount(1);
    await expect(card.getByTestId('a2a-credential-copy-both')).toBeVisible();

    // The secret lives in the DOM for the life of this page and nowhere else,
    // so the reload is what proves it is gone rather than merely re-rendered.
    await page.reload();
    const reloaded = cardTitled(page, 'Partner credential');
    await expect(reloaded.getByText('Client ID', { exact: true })).toBeVisible();
    await expect(reloaded.getByText('Copy this now: the secret is never shown again')).toHaveCount(0);

    await reloaded.getByRole('button', { name: 'Regenerate secret' }).click();
    const confirmDialog = page.getByRole('dialog');
    await expect(confirmDialog.getByText('Regenerate the secret?')).toBeVisible();
    // The client id stays (#105): the dialog says so, so the operator tells the
    // partner to paste the secret alone.
    await expect(
      confirmDialog.getByText(
        'The client ID stays the same. The current secret stops working immediately, on every agent this integration is bound to. The partner must paste the new secret.',
      ),
    ).toBeVisible();
    await expect(confirmDialog.getByRole('button', { name: 'Regenerate secret' })).toBeVisible();
    // Cancelling rather than confirming: re-keying here would retire nothing
    // any other test holds, but the point of the branch is the dialog.
    await confirmDialog.getByRole('button', { name: 'Cancel' }).click();

    // Changing both is its own action, and its dialog says both change.
    await reloaded.getByRole('button', { name: 'Rotate client ID and secret' }).click();
    const rotateDialog = page.getByRole('dialog');
    await expect(rotateDialog.getByText('Rotate the client ID and the secret?')).toBeVisible();
    await expect(rotateDialog.getByRole('button', { name: 'Rotate both' })).toBeVisible();
    await rotateDialog.getByRole('button', { name: 'Cancel' }).click();
  });

  test('the reachability card names what is missing, and then what is missing next', async ({ one }) => {
    const page = one.page;
    const caveat =
      'Holding a credential and exposing an agent is everything this platform can check from here. Whether the partner holds the matching secret is proven only by its first token exchange.';

    await page.goto(`/integrations/${blockedReg.id}/config`);
    const card = cardTitled(page, 'Partner reachability');
    await expect(
      card.getByText('No credential has been generated, so the partner has nothing to authenticate with.'),
    ).toBeVisible();
    await expect(card.getByText(caveat)).toBeVisible();

    // Minted over the API, then reloaded: this card reads the agent list and
    // the integration row, and neither query polls.
    await mintCredential(one.rest, blockedReg.id);
    await page.reload();
    await expect(
      cardTitled(page, 'Partner reachability').getByText(
        "No agent you can see binds this integration. If none does, bind it in an agent's Tools tab: the partner has nothing to reach until then.",
      ),
    ).toBeVisible();

    // Credential and binding both present: ready, and honest that "ready" is
    // as far as this deployment can see.
    await page.goto(`/integrations/${exposedReg.id}/config`);
    const ready = cardTitled(page, 'Partner reachability');
    await expect(ready.getByText('Ready. This partner has not connected yet.')).toBeVisible();
    await expect(ready.getByText(caveat)).toBeVisible();
  });

  // -------------------------------------------------------------------------
  // The outbound operator's page: verification and history.
  // -------------------------------------------------------------------------

  test('an unverified target says so twice over, and a lone target gets no Verify all', async ({ one }) => {
    const page = one.page;
    // The fixture registration, saved in `beforeAll` and untouched since: the
    // pair parks the re-verification sweep, so a registration nobody pressed
    // Verify on still has no verdict however long ago it was created. This
    // test runs before the one that presses it.
    await page.goto(`/integrations/${outboundReg.id}/config`);
    const card = cardTitled(page, 'Partner verification');

    // Nothing verifies a registration on save, so the card an operator meets
    // after creating one says so in both places: the provenance line is a
    // third state, neither signed nor unsigned, and the verdict box names the
    // button that fixes it.
    await expect(card.getByText('Not verified yet', { exact: true })).toHaveCount(1);
    await expect(card.getByText('Not verified yet: use Verify now.')).toBeVisible();
    await expect(card.getByText(cardUrl)).toBeVisible();
    await expect(card.getByRole('button', { name: 'Verify now' })).toBeVisible();
    // Both the count badge and the card-level button are for a partner with
    // more than one agent; one target gets neither.
    await expect(card.getByRole('button', { name: 'Verify all' })).toHaveCount(0);
    await expect(card.getByText(/\d+ agents|\d+ of \d+ reachable/)).toHaveCount(0);

    // No card URL at all is a different state again — a sentence, not an
    // empty list of panels.
    await page.goto(`/integrations/${emptyReg.id}/config`);
    await expect(
      cardTitled(page, 'Partner verification').getByText(
        'No partner agent is configured yet. Add one or more agent card URLs in the configuration above: each becomes its own delegation tool.',
      ),
    ).toBeVisible();
  });

  test('verifying turns the panel into what the partner declares', async ({ one }) => {
    const page = one.page;
    await page.goto(`/integrations/${outboundReg.id}/config`);
    const card = cardTitled(page, 'Partner verification');
    await card.getByRole('button', { name: 'Verify now' }).click();

    // The partner is another XTM One deployment, which serves its cards over
    // the connection and the URL alone — stated, because silence would read
    // as the stronger claim.
    await expect(card.getByText('Card is unsigned')).toBeVisible();
    await expect(card.getByText(exposedAgent.name, { exact: true })).toBeVisible();
    await expect(card.getByText(/^A2A \d/)).toBeVisible();
    await expect(card.getByText(/^Last verified: /)).toBeVisible();
    await expect(card.getByText('Not verified yet: use Verify now.')).toHaveCount(0);

    // And the tools panel is now promising something: a verdict is what turns
    // a target into a tool, so the count the card showed as 0 above becomes
    // one delegation tool for the verified agent plus the shared cancel.
    const brings = cardTitled(page, 'What this brings your agents');
    await expect(brings.getByText('2 tools')).toBeVisible();
    await expect(brings.locator('code', { hasText: /^delegate_to_/ })).toHaveCount(1);
    await expect(brings.locator('code', { hasText: /^cancel_task_at_/ })).toHaveCount(1);
    await expect(
      brings.getByText('None yet: no agent at this partner has verified.'),
    ).toHaveCount(0);
  });

  test('the outbound history is empty and says where work would come from', async ({ one }) => {
    await one.page.goto(`/integrations/${outboundReg.id}/config`);
    await expect(
      cardTitled(one.page, 'Work sent to this partner').getByText(
        'Nothing sent yet. Bind this integration to an agent on its Tools tab to give that agent a delegation tool.',
      ),
    ).toBeVisible();
  });

  test('Test Connection answers from a different probe on each side', async ({ one }) => {
    // The banner and the toast carry the same sentence, and the toast
    // viewport sits outside the page body — so the banner is what `main`
    // scopes this to.
    await one.page.goto(`/integrations/${exposedReg.id}`);
    await one.page.getByRole('button', { name: 'Test connection' }).click();
    // The client id is in the message, so this is a regex and not a literal.
    await expect(
      one.page
        .locator('main')
        .getByText(
          /Credential a2a-\S+ is ready and exposes 1 agent\. Whether the partner holds the matching secret is proven only by its first token exchange\./,
        ),
    ).toBeVisible();

    // Outbound delegates to the stored verification verdict rather than
    // re-deriving one, so this reads back what the previous test established.
    await one.page.goto(`/integrations/${outboundReg.id}`);
    await one.page.getByRole('button', { name: 'Test connection' }).click();
    await expect(
      one.page
        .locator('main')
        .getByText(
          /Reaches 1 agent over A2A \S+; 1 of 1 cards unsigned — trusted on transport and the URL you supplied\./,
        ),
    ).toBeVisible();
  });

  test('the Activity tab is A2A-scoped, and worded for the inbound side in both directions', async ({ one }) => {
    for (const [page, id] of [
      [one.page, exposedReg.id],
      [one.page, outboundReg.id],
    ] as const) {
      await page.goto(`/integrations/${id}/activity`);
      // On the shared DataTable (#9378) a compact search shows the short "Search..."
      // placeholder and keeps "Search runs..." as its accessible name.
      await expect(page.getByRole('searchbox', { name: 'Search runs...' })).toBeVisible();
      await expect(
        page.getByText(
          "No activity yet. Work this partner submits over A2A will appear here: only its own, never another partner's.",
        ),
      ).toBeVisible();
    }
    // On B this sentence is permanent and describes something that cannot
    // happen: an outbound registration submits nothing to itself, and the
    // history of what it sent lives in "Work sent to this partner" instead.
    // Inbound prose on an outbound page, reported rather than defended.
  });

  // -------------------------------------------------------------------------
  // The agent's own surfaces.
  // -------------------------------------------------------------------------

  test('only an inbound binding says who external work runs as', async ({ one }) => {
    await one.page.goto(`/agents/${exposedAgent.id}/config/tooling`);
    // BOTH inbound partners are listed: the Tooling tab names every binding.
    await expect(one.page.getByText(exposedReg.name, { exact: true })).toBeVisible();
    await expect(one.page.getByText(secondReg.name, { exact: true })).toBeVisible();
    // One exposure per agent, whatever it binds. Personally owned, so the line
    // names the owner rather than a user the admin-only /users read supplies.
    await expect(one.page.getByText(/External work runs as/)).toHaveCount(1);
    // The card URL lives on the integration, not the agent: Tooling names the
    // partner and who the work runs as, and publishes no address.
    await expect(one.page.getByText('Agent card URL')).toHaveCount(0);
    await expect(
      one.page.locator('code', { hasText: '/.well-known/agent-card.json' }),
    ).toHaveCount(0);
    await expect(
      one.page.getByText(
        "External work runs as this agent's owner: partners see exactly what that user sees.",
      ),
    ).toBeVisible();

    // An outbound binding creates no exposure, so there is no run-as identity
    // to state: the partner runs the work, not this deployment.
    await one.page.goto(`/agents/${delegatingAgent.id}/config/tooling`);
    await expect(one.page.getByText(outboundReg.name, { exact: true })).toBeVisible();
    await expect(one.page.getByText(/External work runs as/)).toHaveCount(0);

    // The card URL and the run-as line both moved off Channels & Bots; it
    // carries neither, in either direction.
    for (const [page, agentId] of [
      [one.page, exposedAgent.id],
      [one.page, delegatingAgent.id],
    ] as const) {
      await page.goto(`/agents/${agentId}/config/channels`);
      await expect(page.getByText('Channels & Bots', { exact: true })).toBeVisible();
      await expect(page.getByText(/External work runs as/)).toHaveCount(0);
      await expect(page.getByText('Agent card URL')).toHaveCount(0);
      await expect(page.locator('code', { hasText: '/.well-known/agent-card.json' })).toHaveCount(0);
    }
  });

  test('an A2A binding carries no tool-scope control, in either mode or direction', async ({ one }) => {
    const page = one.page;
    await page.goto(`/agents/${exposedAgent.id}/config/tooling`);

    // Read mode. Both bindings carry a stored scope; only the ordinary one
    // reports it, because a count for an A2A binding would describe tools
    // that were never filtered by it.
    await expect(bindingTile(page, ordinaryInbound.instance).getByText('2 tools')).toBeVisible();
    await expect(bindingTile(page, exposedReg.name).getByText(/\d+ tools?\b/)).toHaveCount(0);

    // Edit mode. The hint above the grid still offers the scope button, and
    // the grid still shows one — on everything except the partner binding.
    await cardTitled(page, 'Integrations').getByRole('button', { name: 'Edit' }).click();
    await expect(
      page.getByText(
        'Click to bind or unbind integrations. Use the tool scope button to limit which tools are available.',
      ),
    ).toBeVisible();
    await expect(
      bindingTile(page, ordinaryInbound.instance).getByRole('button', { name: /All tools|tools selected/ }),
    ).toHaveCount(1);
    await expect(
      bindingTile(page, exposedReg.name).getByRole('button', { name: /All tools|tools selected/ }),
    ).toHaveCount(0);
    await page.getByRole('button', { name: 'Cancel' }).first().click();

    // Direction is never read at any of these sites: an outbound binding is
    // suppressed for the same reason, one tool per verified partner agent
    // being no more a set to pick from than an exposure is.
    const b = one.page;
    await b.goto(`/agents/${delegatingAgent.id}/config/tooling`);
    await cardTitled(b, 'Integrations').getByRole('button', { name: 'Edit' }).click();
    await expect(
      bindingTile(b, ordinaryOutbound.instance).getByRole('button', { name: /All tools|tools selected/ }),
    ).toHaveCount(1);
    await expect(
      bindingTile(b, outboundReg.name).getByRole('button', { name: /All tools|tools selected/ }),
    ).toHaveCount(0);
    await b.getByRole('button', { name: 'Cancel' }).first().click();
  });

  test('A2A is never offered as a per-user dynamic integration', async ({ one }) => {
    const page = one.page;
    await page.goto(`/agents/${exposedAgent.id}/config/tooling`);
    await cardTitled(page, 'Integrations').getByRole('button', { name: 'Edit' }).click();

    const advanced = page.locator('details');
    await advanced.getByText('Advanced: Dynamic integrations (per-user)').click();
    await expect(
      advanced.getByText(
        "Dynamic bindings resolve to each user's own integration at runtime, and override specific instance bindings for the same provider.",
      ),
    ).toBeVisible();
    // Per-user mode resolves a provider to the acting user's own connection,
    // and a partner relationship has no per-user form: the credential, the
    // exposure and the partner's cards belong to the registration.
    await expect(advanced.getByText('OAuth A2A')).toHaveCount(0);
    // The grid names PROVIDERS, not instances, so the ordinary one is here
    // under its catalog label and the A2A one is not here at all.
    await expect(advanced.getByText(ordinaryInbound.name, { exact: true }).first()).toBeVisible();
    await expect(advanced.getByText(ordinaryInbound.instance)).toHaveCount(0);

    await page.getByRole('button', { name: 'Cancel' }).first().click();
  });

  test('the agent Overview names each binding by its direction', async ({ one }) => {
    // The row reads the registration's direction: binding an inbound
    // registration exposes the agent, binding an outbound one hands it a
    // delegation tool, and the label must say which.
    await one.page.goto(`/agents/${exposedAgent.id}`);
    const inbound = bindingTile(one.page, exposedReg.name);
    await expect(inbound.getByText('Exposes agent')).toBeVisible();
    await expect(inbound.getByText('Delegates to partner')).toHaveCount(0);

    // Positive anchor first: the label waits on its own direction lookup, so
    // an absence taken before it resolves would pass for the wrong reason.
    await one.page.goto(`/agents/${delegatingAgent.id}`);
    const outbound = bindingTile(one.page, outboundReg.name);
    await expect(outbound.getByText('Delegates to partner')).toBeVisible();
    await expect(outbound.getByText('Exposes agent')).toHaveCount(0);
  });

  test('a failed direction lookup costs the Overview its label, never the page', async ({ one }) => {
    // The label is optional to the page. One 503 on the lookup - what a
    // proxy answers mid-restart - must leave the row in place with no label,
    // rather than hand the agent page to its error boundary.
    //
    // The lookup runs only when the integration does not state its direction
    // (`a2a_direction`: a row with no settings, an older server). Every
    // registration here states it, so the list read drops it for this one
    // registration: without that the label never asks, and the 503 below
    // would never be served.
    const page = one.page;
    // The handlers stay installed for the whole test and fall back once the
    // backend is back: removing the last route while the click's lazy route
    // chunk is in flight leaves that request pending for good (#1192).
    let recovered = false;
    const list = (url: URL) => url.pathname === '/api/v1/integrations';
    await page.route(list, async (route) => {
      if (recovered) {
        await route.fallback();
        return;
      }
      const response = await route.fetch();
      const rows = (await response.json()) as Array<Record<string, unknown>>;
      const json = rows.map((row) => (row.id === outboundReg.id ? { ...row, a2a_direction: null } : row));
      await route.fulfill({ response, json });
    });
    const lookup = (url: URL) => url.pathname.endsWith(`/api/v1/a2a/client/${outboundReg.id}`);
    await page.route(lookup, (route) => (recovered ? route.fallback() : route.fulfill({ status: 503, body: '' })));
    const failed = page.waitForResponse((r) => lookup(new URL(r.url())) && r.status() === 503);
    await page.goto(`/agents/${delegatingAgent.id}`);
    await failed;
    const outbound = bindingTile(page, outboundReg.name);
    await expect(outbound).toBeVisible();
    await expect(outbound.getByText('Delegates to partner')).toHaveCount(0);
    await expect(outbound.getByText('Exposes agent')).toHaveCount(0);

    // The backend is back. The row opens the integration in-app, and that page
    // reads the direction afresh instead of inheriting the failure the label
    // tolerated: a strict read mounting on a cached error throws it unretried.
    recovered = true;
    await outbound.click();
    await expect(page.getByText('Outbound: our agents task this partner')).toBeVisible();

    // Back on the Overview, the label returns with the next lookup.
    await page.goBack();
    await expect(bindingTile(page, outboundReg.name).getByText('Delegates to partner')).toBeVisible();
  });
});
