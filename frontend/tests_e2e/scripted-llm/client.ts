/**
 * Arming the scripted model from a spec.
 *
 * The server is `docker-compose.e2e.yml`'s `scripted-llm` service, common to
 * every e2e suite rather than A2A's own. Any suite that needs an agent to take
 * a turn can point the platform at it and arm it from here.
 *
 * A spec says what the agent should decide, keyed by a marker it puts in the
 * prompt it sends. The marker is what makes the arming safe under a suite
 * where the platform, its worker and both families share one model: a script
 * only fires for the conversation whose prompt carries its marker.
 *
 * The server is the `xtmone/scripted-llm` image, whose README has the step
 * vocabulary and which calls count as agent turns:
 * https://github.com/XTM-One-Platform/e2e-tests/tree/main/scripted-llm
 */

import { request, type APIRequestContext } from '@playwright/test';

/**
 * The scripted model's control plane, as the SUITE reaches it.
 *
 * The deployment reaches the same server at `http://scripted-llm:9099` over
 * the compose network; this is the published port. Only the control API
 * (`/_control/**`) is used from here — the OpenAI surface is the platform's
 * business.
 */
export const SCRIPTED_LLM = process.env.E2E_SCRIPTED_LLM ?? 'http://localhost:9099';

/**
 * The same server as the DEPLOYMENT reaches it: validation and other outbound
 * calls are made from the platform container, where `localhost` is itself.
 * The compose service name in docker-compose.e2e.yml.
 */
export const SCRIPTED_LLM_INTERNAL =
  process.env.E2E_SCRIPTED_LLM_INTERNAL ?? 'http://scripted-llm:9099/v1';

/** The provider and model a deployment seeds from the `e2e` compose profile. */
export const SCRIPTED_PROVIDER = 'custom_openai';
/**
 * The image's default model id, and all its `/v1/models` lists. The CI seeds
 * the same id; `scriptedModelReady` (helpers.ts) fails naming both if the
 * server lists another.
 */
export const SERVED_MODEL = 'scripted-llm';

/**
 * The catalogue's name for the scripted model, which an agent must be pinned
 * to. `LLM_PROVIDER_CUSTOM_OPENAI_MODELS` is a list (`parse_models_csv`) and
 * only its first entry is a chat model by convention; the server answers to
 * any id, so the served one is the fallback.
 */
export const SCRIPTED_MODEL =
  process.env.LLM_PROVIDER_CUSTOM_OPENAI_MODELS?.split(',')[0]?.trim() || SERVED_MODEL;

export type Step =
  | { text: string }
  | { tool: string; arguments?: Record<string, unknown>; regex?: boolean };

/** One model call the server saw. `streamed` is what marks it an agent turn. */
export interface Call {
  matched: string | null;
  tools_offered: string[];
  streamed: boolean;
  step: Step | null;
}

export class ScriptedModel {
  private constructor(private readonly ctx: APIRequestContext) {}

  static async connect(): Promise<ScriptedModel> {
    return new ScriptedModel(await request.newContext({ baseURL: SCRIPTED_LLM }));
  }

  async dispose(): Promise<void> {
    await this.ctx.dispose();
  }

  /**
   * Arm the turns an agent will take when it sees `marker` in its prompt.
   *
   * Steps are consumed one per turn, so `[{tool}, {text}]` is "call the tool,
   * then answer with prose" — the shape of nearly every agentic branch here.
   */
  async arm(marker: string, steps: Step[]): Promise<void> {
    const resp = await this.ctx.post('/_control/script', { data: { match: marker, steps } });
    if (!resp.ok()) {
      throw new Error(`Could not arm the scripted model: ${resp.status()} ${await resp.text()}`);
    }
  }

  /**
   * Forget EVERY script, not just this suite's. Safe where a project runs
   * alone and serially, as `a2a` does; a core spec that called it would
   * disarm whatever another file armed.
   */
  async reset(): Promise<void> {
    await this.ctx.post('/_control/reset');
  }

  /**
   * What the model was actually asked, and how many turns each script served.
   *
   * The tools-offered list is the useful half: when a delegation spec fails,
   * the first question is always whether the platform generated the tool at
   * all, and this answers it without reading backend logs.
   */
  async calls(): Promise<{
    calls: Call[];
    served: Record<string, number>;
  }> {
    const resp = await this.ctx.get('/_control/calls');
    return resp.json();
  }

  /**
   * The AGENT turns served for `marker`, in order.
   *
   * The platform also asks a model to name a conversation, summarise a run and
   * validate a provider; those carry the user's text, so they match a marker
   * like any other, and whether one lands before or after an assertion is a
   * race. An agent turn is offered tools, or streams, or both — both halves
   * are needed, since a REST chat turn offers tools without streaming and the
   * external-task loop streams. An agent carrying no tools at all would be
   * invisible here; none in this suite is.
   */
  async agentTurns(marker: string): Promise<Call[]> {
    const { calls } = await this.calls();
    return calls.filter(
      (call) => call.matched === marker && (call.streamed || call.tools_offered.length > 0),
    );
  }

  /**
   * How many agent turns the script for `marker` has served. A naming,
   * summarising or memory call that matches the marker is not one of them:
   * the server answers it without consuming a step.
   */
  async turnsServed(marker: string): Promise<number> {
    return (await this.calls()).served[marker] ?? 0;
  }
}

/** A marker no other spec will collide with. */
export function marker(label: string): string {
  return `E2E-${label}-${Math.random().toString(36).slice(2, 10)}`;
}
