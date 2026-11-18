/**
 * Building both sides of an A2A relationship over the API, on one deployment.
 *
 * Every spec needs some prefix of the same story — an agent is exposed, a
 * registration points at its card, an agent bound to that registration gains a
 * delegation tool — and building it through the UI each time would make every
 * spec a UI test of the setup rather than of its own subject. So the story is
 * built here, over the API, and the UI specs assert on what the UI says about
 * state this created.
 *
 * THE TWO FAMILIES
 * ----------------
 * An agent's family is decided by which A2A registration it binds, and the two
 * are genuinely different roles rather than two labels:
 *
 *   family 1 — bound to the INBOUND registration. The binding IS the exposure:
 *              the agent gains a card URL, a partner holding the credential
 *              may task it, and it answers as itself. It has no delegation
 *              tool and cannot reach outward.
 *   family 2 — bound to the OUTBOUND registration. The agent gains one
 *              `delegate_to_*` tool per verified target and can reach the
 *              partner. It is not exposed and no partner can address it.
 *
 * `federation()` builds both on one platform, pointing the outbound half at
 * the inbound half's own cards — the self-federation target
 * (`backend/tests/test_a2a_client_self_federation.py`), which is the only one
 * of the three exercisable without a vendor account, and the reason this suite
 * needs one deployment rather than two.
 *
 * Two conventions, shared with the core suite: names carry a stamp so a re-run
 * against a database someone kept cannot collide, and everything created is
 * recorded so `cleanup` can take it away again in `afterAll`.
 */

import { expect, type APIRequestContext } from '@playwright/test';
import { REAL_MODEL, REAL_PROVIDER, SCRIPTED_MODEL, SCRIPTED_PROVIDER, SELF } from './instance';

/**
 * A unique-enough suffix. Worker index keeps parallel files apart; the clock
 * keeps runs apart.
 *
 * Base 36 rather than decimal, which costs nothing in uniqueness and saves
 * five characters — and characters are not free here. A delegation tool's name
 * is built from the registration's name and the partner agent's
 * (`a2a/client/tool.py::tool_name_for`), and the tool definition the model is
 * finally handed is capped at 64 characters
 * (`integration_tools.py::_MAX_TOOL_NAME_LEN`). Two stamped names either side
 * of a `delegate_to_` prefix reach that ceiling easily, and what then fails is
 * a spec asserting the model was offered the tool the API named.
 */
export function stamp(label: string): string {
  const worker = process.env.TEST_WORKER_INDEX ?? '0';
  return `${label} ${Date.now().toString(36)}-${worker}`;
}

export interface Created {
  agents: string[];
  integrations: string[];
}

export function tracker(): Created {
  return { agents: [], integrations: [] };
}

async function ok(
  resp: Awaited<ReturnType<APIRequestContext['post']>>,
  what: string,
): Promise<Record<string, string>> {
  if (!resp.ok()) {
    throw new Error(`${what} failed: ${resp.status()} ${await resp.text()}`);
  }
  return (await resp.json()) as Record<string, string>;
}

/**
 * Is A2A on offer at all?
 *
 * Do NOT gate on the integration catalog: it lists all three A2A providers
 * regardless of edition, so a catalog probe reports "available" on a deployment
 * where every A2A call is about to 403. The licence gate is `chat_enabled` on
 * the platform config, which is the same predicate the backend itself uses.
 */
export async function a2aAvailable(rest: APIRequestContext): Promise<boolean> {
  const resp = await rest.get('/api/v1/platform/config');
  if (!resp.ok()) return false;
  const config = await resp.json();
  return config.chat_enabled === true;
}

// ---------------------------------------------------------------------------
// The inbound half — what makes an agent reachable
// ---------------------------------------------------------------------------

/** An inbound registration. Its credential is minted separately, as an operator would. */
export async function createInboundIntegration(
  rest: APIRequestContext,
  created: Created,
  name = stamp('E2E inbound'),
  opts: { companyManaged?: boolean } = {},
): Promise<{ id: string; name: string }> {
  const body = await ok(
    await rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name,
        auth_type: 'none',
        a2a_direction: 'inbound',
        // A company-managed agent — every built-in is one — may only bind a
        // company-managed integration, so exposing a built-in needs this and
        // an ordinary registration is refused with a 400 naming the mismatch.
        ...(opts.companyManaged ? { is_company_managed: true } : {}),
      },
    }),
    'create inbound integration',
  );
  created.integrations.push(body.id);
  return { id: body.id, name };
}

/**
 * Mint the partner credential, or rotate it.
 *
 * The secret is returned here and nowhere else — it is stored as a bcrypt
 * hash — so a spec that needs to authenticate must keep what this returns.
 * Calling it again rotates the SECRET and keeps the client id (#105);
 * `rotateClientId` is the explicit action that changes both.
 */
export async function mintCredential(
  rest: APIRequestContext,
  integrationId: string,
  options: { rotateClientId?: boolean } = {},
): Promise<{ clientId: string; clientSecret: string }> {
  const query = options.rotateClientId ? '?rotate_client_id=true' : '';
  const body = await ok(
    await rest.post(`/api/v1/integrations/${integrationId}/a2a-credential${query}`),
    'mint A2A credential',
  );
  return { clientId: body.client_id, clientSecret: body.client_secret };
}

/**
 * An agent, told explicitly which model answers for it.
 *
 * Naming the provider and model rather than taking the platform default is
 * what lets the two lanes share one deployment: the launcher seeds both the
 * scripted endpoint and, where the machine has one, a real tool-capable model
 * through ollama. An agent that took the default would drift between them
 * depending on which provider happened to be seeded first, and an agentic
 * spec would fail for a reason having nothing to do with A2A.
 */
export async function createAgent(
  rest: APIRequestContext,
  created: Created,
  opts: {
    name?: string;
    persona?: string;
    /**
     * Which model answers for this agent. `scripted` — the default — makes the
     * agent's every decision something the spec armed. `real` hands it to
     * whatever tool-capable model ollama is serving, which is the only way to
     * test that the real tool descriptions still earn the choice; a spec asking
     * for it must skip itself when `HAS_REAL_MODEL` is false.
     */
    model?: 'scripted' | 'real';
    extra?: Record<string, unknown>;
  } = {},
): Promise<{ id: string; name: string }> {
  const name = opts.name ?? stamp('E2E agent');
  const real = opts.model === 'real';
  if (real && !REAL_MODEL) {
    throw new Error(
      'createAgent asked for the real model, but the deployment was started without one. ' +
        'Gate the test on HAS_REAL_MODEL.',
    );
  }
  const body = await ok(
    await rest.post('/api/v1/agents', {
      data: {
        name,
        persona: opts.persona ?? 'An agent used by the A2A e2e suite.',
        llm_provider: real ? REAL_PROVIDER : SCRIPTED_PROVIDER,
        llm_model: real ? REAL_MODEL : SCRIPTED_MODEL,
        // A2A behaviour, not tool approvals: CI is a fresh install, where the
        // gate is on by default. A spec about approvals passes it in `extra`.
        approve_tool_calls: false,
        ...opts.extra,
      },
    }),
    'create agent',
  );
  created.agents.push(body.id);
  return { id: body.id, name };
}

/**
 * Bind integrations to an agent.
 *
 * On the inbound side the binding IS the exposure — there is no switch — so
 * this is what makes the agent reachable, and unbinding is what takes it
 * away. On the outbound side it is what gives the agent its delegation tools.
 */
export async function bind(
  rest: APIRequestContext,
  agentId: string,
  bindings: Record<string, string[]>,
): Promise<void> {
  const resp = await rest.put(`/api/v1/agents/${agentId}`, {
    data: { integration_bindings: bindings },
  });
  if (!resp.ok()) throw new Error(`bind failed: ${resp.status()} ${await resp.text()}`);
}

/** Give an agent a peer, and say how work reaches it. */
export async function setHandover(
  rest: APIRequestContext,
  agentId: string,
  peers: { id: string; type: 'consult' | 'transfer' | 'delegate' | 'mandatory_transfer'; condition?: string }[],
): Promise<void> {
  const handover_agent_ids = peers.map((p) => p.id);
  const handover_types = Object.fromEntries(peers.map((p) => [p.id, p.type]));
  const conditions = peers.filter((p) => p.condition);
  const data: Record<string, unknown> = { handover_agent_ids, handover_types };
  if (conditions.length > 0) {
    data.handover_conditions = Object.fromEntries(
      conditions.map((p) => [p.id, p.condition as string]),
    );
  }
  const resp = await rest.put(`/api/v1/agents/${agentId}`, { data });
  if (!resp.ok()) throw new Error(`set handover failed: ${resp.status()} ${await resp.text()}`);
}

/** The exposure as the platform reports it — card URL, run-as user, token URL. */
export async function exposureOf(
  rest: APIRequestContext,
  agentId: string,
): Promise<{ exposed: boolean; card_url: string | null; run_as_user_id: string | null; token_url: string | null }> {
  const resp = await rest.get(`/api/v1/agents/${agentId}/a2a`);
  if (!resp.ok()) throw new Error(`exposure read failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

/** Bind, then read back the card URL the binding created. */
export async function exposeAgent(
  rest: APIRequestContext,
  agentId: string,
  integrationId: string,
): Promise<string> {
  await bind(rest, agentId, { oauth_a2a: [integrationId] });
  const exposure = await exposureOf(rest, agentId);
  expect(
    exposure.card_url,
    'binding an inbound A2A integration should have produced a card URL',
  ).toBeTruthy();
  return exposure.card_url as string;
}

// ---------------------------------------------------------------------------
// The outbound half — what lets an agent reach out
// ---------------------------------------------------------------------------

/**
 * An outbound registration pointed at a partner.
 *
 * `card_urls` accepts several, comma- or newline-separated, and each becomes
 * one target. Nothing is verified by creating this: `verify_registration` has
 * exactly three callers — the /verify endpoint, the delegation pre-flight and
 * the sweep — so a freshly saved registration sits at "unusable" until one of
 * them runs. Specs must call `verify` rather than assume.
 */
export async function createOutboundIntegration(
  rest: APIRequestContext,
  created: Created,
  opts: {
    cardUrls: string | string[];
    tokenUrl: string;
    clientId: string;
    clientSecret: string;
    name?: string;
  },
): Promise<{ id: string; name: string }> {
  const name = opts.name ?? stamp('E2E outbound');
  const card_urls = Array.isArray(opts.cardUrls) ? opts.cardUrls.join(',') : opts.cardUrls;
  const body = await ok(
    await rest.post('/api/v1/integrations', {
      data: {
        provider: 'oauth_a2a',
        name,
        auth_type: 'none',
        a2a_direction: 'outbound',
        extra_config: {
          card_urls,
          token_url: opts.tokenUrl,
          client_id: opts.clientId,
          client_secret: opts.clientSecret,
        },
      },
    }),
    'create outbound integration',
  );
  created.integrations.push(body.id);
  return { id: body.id, name };
}

/** The registration as B sees it: targets, verdicts, tool names. */
export async function registrationState(
  rest: APIRequestContext,
  integrationId: string,
): Promise<Record<string, unknown>> {
  const resp = await rest.get(`/api/v1/a2a/client/${integrationId}`);
  if (!resp.ok()) throw new Error(`registration read failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

/** Verify every target now — the one call that also runs discovery unconditionally. */
export async function verifyNow(
  rest: APIRequestContext,
  integrationId: string,
): Promise<Record<string, unknown>> {
  const resp = await rest.post(`/api/v1/a2a/client/${integrationId}/verify`);
  if (!resp.ok()) throw new Error(`verify failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

/** What B recorded about each exchange: one row per delegation, with mechanism and outcome. */
export async function delegations(
  rest: APIRequestContext,
  integrationId: string,
): Promise<Record<string, unknown>[]> {
  const resp = await rest.get(`/api/v1/a2a/client/${integrationId}/delegations`);
  if (!resp.ok()) throw new Error(`delegations read failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

/**
 * The whole outbound setup in one call, since most specs want it as a
 * precondition rather than as their subject: register the partner, verify, and
 * hand back the targets so the caller can read the generated tool names off
 * them.
 */
export async function registerPartner(
  rest: APIRequestContext,
  created: Created,
  opts: {
    cardUrls: string | string[];
    tokenUrl: string;
    clientId: string;
    clientSecret: string;
    name?: string;
  },
): Promise<{ id: string; name: string; state: Record<string, unknown> }> {
  const integration = await createOutboundIntegration(rest, created, opts);
  const state = await verifyNow(rest, integration.id);
  return { ...integration, state };
}

/** The generated delegation tool names, in the order the platform reports them. */
export function toolNames(state: Record<string, unknown>): string[] {
  const targets = (state.targets as Record<string, unknown>[]) ?? [];
  return targets.map((t) => t.tool_name as string).filter(Boolean);
}

// ---------------------------------------------------------------------------
// Both halves at once — the self-federated partner relationship
// ---------------------------------------------------------------------------

/**
 * One deployment, federated with itself: an inbound registration whose agents
 * are the partner, and an outbound registration that reaches them.
 *
 * Built in two steps rather than one because the order is forced by the
 * product. An outbound registration is created with the card URLs it points
 * at, and a card URL only exists once an agent has been bound to the inbound
 * registration — so every family-1 agent is exposed first, and `link()` then
 * creates the registration that reaches them.
 *
 * After that, `verify()` is enough: a verification runs partner discovery
 * against the credential, so an agent exposed on the SAME inbound
 * registration later becomes a target without anyone editing a card URL.
 * That is the product's own behaviour (`03-outbound-registration.spec.ts`
 * pins it), and relying on it here is what keeps a spec's setup to "expose,
 * link, verify".
 */
export interface Federation {
  /** The registration family 1 binds, and the one a partner authenticates against. */
  readonly inbound: { id: string; name: string };
  /** The credential a partner presents. The secret exists here and nowhere else. */
  readonly credentials: { clientId: string; clientSecret: string };
  /** The registration family 2 binds. Empty until `link()`. */
  readonly outbound: { id: string; name: string };
  /** Where a partner exchanges the credential for a bearer. */
  readonly tokenUrl: string;
  /** Expose an agent — family 1 — and hand back the card URL the binding minted. */
  expose(agentId: string): Promise<string>;
  /** Bind an agent to the outbound registration — family 2. */
  enrol(agentId: string): Promise<void>;
  /** Create the outbound registration over everything exposed so far, and verify it. */
  link(): Promise<Record<string, unknown>>;
  /** Verify again, picking up every agent exposed since the last look. */
  verify(): Promise<Record<string, unknown>>;
  /** The registration as it stands: targets, verdicts, tool names. */
  state(): Promise<Record<string, unknown>>;
  /** The delegation tool generated for one exposed agent, named by that agent. */
  toolFor(agentName: string): Promise<string>;
  /** The one cancel tool the registration confers, whatever the target. */
  cancelTool(): Promise<string>;
  /** Every card URL exposed so far, in the order they were exposed. */
  readonly cards: readonly string[];
}

export async function federation(
  rest: APIRequestContext,
  created: Created,
  opts: { label?: string; companyManaged?: boolean } = {},
): Promise<Federation> {
  const label = opts.label ?? 'E2E';
  const inbound = await createInboundIntegration(rest, created, stamp(`${label} inbound`), {
    companyManaged: opts.companyManaged,
  });
  const credentials = await mintCredential(rest, inbound.id);
  const cards: string[] = [];
  const outbound = { id: '', name: '' };
  const tokenUrl = `${SELF.origin}/mcp/oauth/token`;

  async function state(): Promise<Record<string, unknown>> {
    if (!outbound.id) throw new Error('federation: link() has not run, there is no registration yet');
    return registrationState(rest, outbound.id);
  }

  async function targets(): Promise<Record<string, unknown>[]> {
    return ((await state()).targets as Record<string, unknown>[]) ?? [];
  }

  return {
    inbound,
    credentials,
    outbound,
    tokenUrl,
    get cards() {
      return cards;
    },

    async expose(agentId: string): Promise<string> {
      const card = await exposeAgent(rest, agentId, inbound.id);
      cards.push(card);
      return card;
    },

    async enrol(agentId: string): Promise<void> {
      if (!outbound.id) throw new Error('federation: link() has not run, there is nothing to bind');
      await bind(rest, agentId, { oauth_a2a: [outbound.id] });
    },

    async link(): Promise<Record<string, unknown>> {
      if (cards.length === 0) {
        throw new Error('federation: expose at least one agent before linking — a registration needs a card URL');
      }
      const registered = await registerPartner(rest, created, {
        // Every card, not merely the first. Discovery would find the rest,
        // but naming them makes the registration independent of it — and one
        // spec here deliberately breaks discovery.
        cardUrls: cards,
        tokenUrl,
        clientId: credentials.clientId,
        clientSecret: credentials.clientSecret,
        name: stamp(`${label} outbound`),
      });
      outbound.id = registered.id;
      outbound.name = registered.name;
      return registered.state;
    },

    async verify(): Promise<Record<string, unknown>> {
      if (!outbound.id) throw new Error('federation: link() has not run, there is nothing to verify');
      return verifyNow(rest, outbound.id);
    },

    state,

    async toolFor(agentName: string): Promise<string> {
      const found = (await targets()).find((t) => t.declared_name === agentName);
      if (!found) {
        const known = (await targets()).map((t) => t.declared_name ?? t.card_url).join(', ');
        throw new Error(
          `No verified target declares itself "${agentName}". Verified targets: ${known || '(none)'}. ` +
            'An agent exposed after the last verify() is not a target yet.',
        );
      }
      const tool = found.tool_name as string | null;
      if (!tool) {
        throw new Error(
          `The target for "${agentName}" is ${found.usability} (${found.unusable_reason}), so it has no tool.`,
        );
      }
      return tool;
    },

    async cancelTool(): Promise<string> {
      const name = (await state()).cancel_tool_name as string | null;
      if (!name) throw new Error('the registration confers no cancel tool — nothing is usable');
      return name;
    },
  };
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

/**
 * Take back everything a spec created.
 *
 * Agents first: an integration still bound to one is refused, and the refusal
 * would leave both behind. Failures are swallowed — a cleanup that throws
 * turns one red test into a red file, and the stamped names mean leftovers
 * cost nothing but noise.
 */
export async function cleanup(rest: APIRequestContext, created: Created): Promise<void> {
  for (const id of created.agents.reverse()) {
    await rest.delete(`/api/v1/agents/${id}`).catch(() => undefined);
  }
  for (const id of created.integrations.reverse()) {
    await rest.delete(`/api/v1/integrations/${id}`).catch(() => undefined);
  }
  created.agents = [];
  created.integrations = [];
}
