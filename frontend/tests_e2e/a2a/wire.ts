/**
 * The A2A protocol, as a test speaks it.
 *
 * The suite plays the partner directly wherever it can: mint a credential on
 * an inbound registration, exchange it for a bearer, and drive `message/send`,
 * `tasks/get` and `tasks/cancel` over JSON-RPC. That covers most of the
 * inbound surface with no model and no agent turn, so it runs in seconds and
 * cannot flake on a model's opinion.
 *
 * What it cannot cover is the other half — a caller *deciding* to delegate,
 * and everything the outbound client does around that decision. Those specs
 * drive an outbound-bound agent through the chat API and let the scripted
 * model make the choice, and the call lands back on this same deployment's
 * inbound surface.
 *
 * Everything here carries `WIRE_HEADERS`: the partner-facing surface is
 * HTTPS-only with the suite's own network exempt by address, which is why
 * those headers must state nothing (see instance.ts).
 */

import { request, type APIRequestContext, type APIResponse } from '@playwright/test';
import { SELF, WIRE_HEADERS, type Instance } from './instance';

/** The 0.3 vocabulary. `SendMessage`/`GetTask`/`CancelTask` are the 1.0 spelling of the same three. */
export const METHOD = {
  send: 'message/send',
  get: 'tasks/get',
  cancel: 'tasks/cancel',
  pushSet: 'tasks/pushNotificationConfig/set',
  pushGet: 'tasks/pushNotificationConfig/get',
  pushList: 'tasks/pushNotificationConfig/list',
  pushDelete: 'tasks/pushNotificationConfig/delete',
} as const;

export interface JsonRpcEnvelope {
  jsonrpc: '2.0';
  id: string | number | null;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

/** A partner's view of one exposure: the card it fetched and the bearer it holds. */
export class PartnerClient {
  private constructor(
    readonly ctx: APIRequestContext,
    readonly cardToken: string,
    private token: string,
    private readonly credentials: { clientId: string; clientSecret: string },
    private readonly instance: Instance,
  ) {}

  /**
   * Become a partner: exchange the minted credential for a bearer and
   * remember which exposure it is for.
   *
   * `cardUrl` is the full URL the exposing side published; the token in its
   * path is what every subsequent call is addressed to. Taking the URL rather
   * than the token keeps the spec honest — it uses the string the product
   * actually gave it, not one reassembled from parts.
   */
  static async connect(
    cardUrl: string,
    credentials: { clientId: string; clientSecret: string },
    instance: Instance = SELF,
  ): Promise<PartnerClient> {
    const ctx = await request.newContext({
      baseURL: instance.origin,
      extraHTTPHeaders: { ...WIRE_HEADERS },
    });
    const cardToken = cardTokenOf(cardUrl);
    const token = await mintToken(ctx, credentials);
    return new PartnerClient(ctx, cardToken, token, credentials, instance);
  }

  get bearer(): string {
    return this.token;
  }

  /** Exchange the credential again — after the exposing side regenerated it, say. */
  async refreshToken(credentials?: { clientId: string; clientSecret: string }): Promise<string> {
    this.token = await mintToken(this.ctx, credentials ?? this.credentials);
    return this.token;
  }

  async dispose(): Promise<void> {
    await this.ctx.dispose();
  }

  /** The card, fetched anonymously exactly as a stranger would. */
  async card(): Promise<APIResponse> {
    return this.ctx.get(`/a2a/${this.cardToken}/.well-known/agent-card.json`);
  }

  /** Every agent this credential may reach. 404 — not 401 — without a bearer. */
  async discover(opts: { authenticated?: boolean } = {}): Promise<APIResponse> {
    const authenticated = opts.authenticated ?? true;
    return this.ctx.get('/a2a/partner/agents', {
      headers: authenticated ? { Authorization: `Bearer ${this.token}` } : {},
    });
  }

  /** One JSON-RPC call, returned raw so a spec can assert on an error envelope. */
  async rpc(
    method: string,
    params: unknown,
    opts: { id?: string | number | null; bearer?: string | null } = {},
  ): Promise<{ status: number; body: JsonRpcEnvelope }> {
    const bearer = opts.bearer === undefined ? this.token : opts.bearer;
    const resp = await this.ctx.post(`/a2a/${this.cardToken}/jsonrpc`, {
      headers: bearer ? { Authorization: `Bearer ${bearer}` } : {},
      data: {
        jsonrpc: '2.0',
        id: opts.id ?? 1,
        method,
        params,
      },
    });
    let body: JsonRpcEnvelope;
    try {
      body = (await resp.json()) as JsonRpcEnvelope;
    } catch {
      // Say what came back, rather than letting the parse error stand as the
      // whole explanation.
      const text = await resp.text();
      throw new Error(
        `Non-JSON answer from ${this.instance.name} (${resp.status()}) at ` +
          `${this.instance.origin}. First 200 chars: ${text.slice(0, 200)}`,
      );
    }
    return { status: resp.status(), body };
  }

  /**
   * Submit work. Omitting `configuration` is what makes the send
   * non-blocking, which is what the suite wants nearly everywhere: the task id
   * comes back at once and the spec polls, instead of holding a connection
   * open for the length of an agent run.
   */
  async send(
    text: string,
    opts: {
      messageId?: string;
      contextId?: string;
      taskId?: string;
      blocking?: boolean;
      pushNotificationConfig?: Record<string, unknown>;
      id?: string | number;
    } = {},
  ): Promise<{ status: number; body: JsonRpcEnvelope }> {
    const message: Record<string, unknown> = {
      role: 'user',
      parts: [{ kind: 'text', text }],
      messageId: opts.messageId ?? `e2e-${crypto.randomUUID()}`,
    };
    if (opts.contextId) message.contextId = opts.contextId;
    if (opts.taskId) message.taskId = opts.taskId;

    const params: Record<string, unknown> = { message };
    const configuration: Record<string, unknown> = {};
    if (opts.blocking) configuration.blocking = true;
    if (opts.pushNotificationConfig) {
      configuration.pushNotificationConfig = opts.pushNotificationConfig;
    }
    if (Object.keys(configuration).length > 0) params.configuration = configuration;

    return this.rpc(METHOD.send, params, { id: opts.id });
  }

  async getTask(taskId: string, opts: { bearer?: string | null } = {}) {
    return this.rpc(METHOD.get, { id: taskId }, opts);
  }

  async cancelTask(taskId: string, opts: { bearer?: string | null } = {}) {
    return this.rpc(METHOD.cancel, { id: taskId }, opts);
  }

  /**
   * Poll until the task reaches one of `states`, or throw naming what it
   * actually reached.
   *
   * Every A2A state transition here is driven by work happening elsewhere —
   * an agent loop, a reconciler tick, a reaper sweep — so a bare assertion
   * after a send is a race. The failure message carries the last state seen
   * because "timed out" alone never says whether the task was still working
   * or had failed twenty seconds ago.
   */
  async waitForState(
    taskId: string,
    states: readonly string[],
    opts: { timeoutMs?: number; intervalMs?: number } = {},
  ): Promise<Record<string, unknown>> {
    const timeoutMs = opts.timeoutMs ?? 60_000;
    const intervalMs = opts.intervalMs ?? 500;
    const deadline = Date.now() + timeoutMs;
    let last = '<never answered>';

    while (Date.now() < deadline) {
      const { body } = await this.getTask(taskId);
      const task = body.result as Record<string, unknown> | undefined;
      const state = readState(task);
      if (state) {
        last = state;
        if (states.includes(state)) return task as Record<string, unknown>;
      } else if (body.error) {
        last = `error ${body.error.code}: ${body.error.message}`;
      }
      await sleep(intervalMs);
    }
    throw new Error(
      `Task ${taskId} never reached ${states.join('|')} within ${timeoutMs}ms; last saw "${last}".`,
    );
  }
}

/** `.../a2a/<token>/.well-known/agent-card.json` -> `<token>`. */
export function cardTokenOf(cardUrl: string): string {
  const match = /\/a2a\/([^/]+)\/\.well-known\/agent-card\.json/.exec(cardUrl);
  if (!match) throw new Error(`Not an agent card URL: ${cardUrl}`);
  return match[1];
}

/**
 * RFC 6749 §4.4 against the platform's own authorization server.
 *
 * Every rejection is the same `invalid_client`, deliberately — a caller must
 * not be able to tell "no such client" from "wrong secret" — so a spec
 * asserting on a refusal asserts on that one string, and a spec that expected
 * success gets the status in its error rather than a bare parse failure.
 */
export async function mintToken(
  ctx: APIRequestContext,
  credentials: { clientId: string; clientSecret: string },
): Promise<string> {
  const resp = await requestToken(ctx, credentials);
  if (!resp.ok()) {
    throw new Error(`Partner token refused: ${resp.status()} ${await resp.text()}`);
  }
  return (await resp.json()).access_token as string;
}

/** The same exchange, unwrapped, for specs that expect it to fail. */
export async function requestToken(
  ctx: APIRequestContext,
  credentials: { clientId: string; clientSecret: string },
): Promise<APIResponse> {
  return ctx.post('/mcp/oauth/token', {
    form: {
      grant_type: 'client_credentials',
      client_id: credentials.clientId,
      client_secret: credentials.clientSecret,
    },
  });
}

/** The state string, from either vocabulary's shape. */
export function readState(task: unknown): string | undefined {
  if (!task || typeof task !== 'object') return undefined;
  const status = (task as Record<string, unknown>).status;
  if (status && typeof status === 'object') {
    const state = (status as Record<string, unknown>).state;
    if (typeof state === 'string') return state;
  }
  return undefined;
}

/** Artifacts by name — `delegated-result` is the one most specs are after. */
export function artifactsNamed(task: unknown, name: string): Record<string, unknown>[] {
  const artifacts = (task as Record<string, unknown>)?.artifacts;
  if (!Array.isArray(artifacts)) return [];
  return artifacts.filter((a) => (a as Record<string, unknown>)?.name === name);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Retry an assertion until it holds.
 *
 * Playwright's own `expect.poll` covers most of this, but several A2A facts
 * are read through a plain API call rather than a locator — a delegation's
 * outcome, a child run's state — and those need the same patience.
 */
export async function eventually<T>(
  fn: () => Promise<T>,
  opts: { timeoutMs?: number; intervalMs?: number; what?: string } = {},
): Promise<T> {
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const intervalMs = opts.intervalMs ?? 500;
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      await sleep(intervalMs);
    }
  }
  throw new Error(
    `${opts.what ?? 'Condition'} never held within ${timeoutMs}ms. Last failure: ${String(lastError)}`,
  );
}
