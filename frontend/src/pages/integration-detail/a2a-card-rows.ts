/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/** What one bound agent's row says on an inbound A2A registration (#11786).
 *
 * Every bound agent gets a row, so the count beside "Agent card URLs" never
 * exceeds what is listed: an agent with a card shows it, one without says why
 * from what the page knows (the same refusals as the backend's flush
 * listener: a disabled agent, an agent with no owner to run as), and a
 * missing card with neither refusal is one the platform has not exposed yet.
 */

import { useAgents } from "@/hooks/api/useAgentApi";
import { useA2AExposures, type A2AExposureStatus } from "@/hooks/api/useA2AApi";
import type { Agent } from "@/types";

export type CardRowState =
  | { kind: "card"; cardUrl: string }
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "missing"; reason: "disabled" | "no_owner" | "not_exposed" };

export function cardRowState(
  agent: Pick<Agent, "enabled" | "user_id">,
  exposure: A2AExposureStatus | undefined,
  { isLoading, isError }: { isLoading: boolean; isError: boolean },
): CardRowState {
  if (exposure?.card_url) return { kind: "card", cardUrl: exposure.card_url };
  // A 404 is what a viewer who may not manage the agent gets: its card is
  // not this viewer's to read, which is not the same as having none.
  if (isError) return { kind: "hidden" };
  if (isLoading || !exposure) return { kind: "loading" };
  if (agent.enabled === false) return { kind: "missing", reason: "disabled" };
  if (!agent.user_id) return { kind: "missing", reason: "no_owner" };
  return { kind: "missing", reason: "not_exposed" };
}

/** How many bound agents are known to have no live card.
 *
 * Partner reachability reads "Ready" only at zero. Rows still loading or
 * hidden from this viewer are not counted: they are not known to be missing.
 */
export function missingCardCount(states: CardRowState[]): number {
  return states.filter((s) => s.kind === "missing").length;
}

/** The token URL the bound agents' cards publish, or null while none is read.
 *
 * Taken from the exposure status, which the backend builds with the same
 * helper as the card's `tokenUrl`, so it follows the deployment's public
 * origin and never a host spelled in the UI. Every live card of one
 * deployment states the same URL; the first one read is the answer.
 */
export function boundTokenUrl(exposures: Array<A2AExposureStatus | undefined>): string | null {
  for (const exposure of exposures) {
    if (exposure?.token_url) return exposure.token_url;
  }
  return null;
}

/** The agents that bind *integrationId*, the card state of each and the
 * token URL their cards publish. Read by Agent card URLs, Partner credential
 * and Partner reachability, so the three agree. */
export function useBoundAgentCards(integrationId: string) {
  const { data: agents } = useAgents({ scope: "all" });
  const bound = (agents ?? []).filter(
    (a) =>
      a.integration_bindings &&
      Object.values(a.integration_bindings).flat().includes(integrationId),
  );
  const exposures = useA2AExposures(bound.map((a) => a.id));
  const states = bound.map((agent, i) =>
    cardRowState(agent, exposures[i]?.data, {
      isLoading: exposures[i]?.isLoading ?? true,
      isError: exposures[i]?.isError ?? false,
    }),
  );
  const tokenUrl = boundTokenUrl(exposures.map((e) => e.data));
  return { bound, states, tokenUrl };
}
