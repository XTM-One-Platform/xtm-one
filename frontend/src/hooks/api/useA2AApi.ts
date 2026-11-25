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

/** A2A inbound-exposure API hooks.
 *
 * There is no exposure mutation here, and that is the shape of the feature
 * rather than an omission: an agent is reachable exactly while it binds an
 * A2A integration, so exposing and un-exposing happen through the ordinary
 * agent-update call that edits `integration_bindings` (the agent
 * configuration's Tools tab). What is left is a READ of the resulting
 * exposure — the card URL and the identity external work runs as — plus the
 * two integration-scoped calls that belong to the partner rather than to any
 * one agent.
 *
 * Nothing exposes an agent any other way, and nothing nominates the identity
 * external work runs as: a second path to one state disagrees with the first
 * the moment either is used. The identity is inherited from the
 * agent's stored owner. The one write on a live exposure is its #2202
 * opt-in (`useSetA2APartnerFleetReads`): it widens nothing about WHO can
 * reach the agent, only what an already-admitted partner task may read.
 */

import { useQuery, useQueries, useMutation, useQueryClient } from "@tanstack/react-query";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { api, unavailableIsOrdinary } from "@/lib/api";
import type { A2ALiaisons } from "@/lib/agent-graph-a2a";
import type { A2ARefusedTokenRequest, Integration } from "@/types";

/** Provider keys of the integrations that carry an A2A partner relationship
 * (mirrors `app.a2a.providers.A2A_PROVIDERS`). Binding ANY of them to an
 * agent is the whole exposure opt-in — there is no separate switch — for the
 * ones whose registration faces inbound. `google_a2a` is here because its
 * inbound half is the Google analogue of `entra_a2a`: the partner presents
 * its own Google-issued token and this platform verifies it against pins. */
export const OAUTH_A2A_PROVIDER = "oauth_a2a";
export const A2A_PROVIDERS = [
  OAUTH_A2A_PROVIDER,
  "entra_a2a",
  "google_a2a",
] as const;

/** Whether this provider is an A2A partner mechanism at all.
 *
 * Deliberately NOT "is this agent exposed": every mechanism can also face
 * outbound, and an outbound registration exposes nothing. Direction lives on
 * the integration's settings row and is read per integration
 * (`useA2AVerificationState`), so a call site that needs it asks for it. What
 * this answers is the question the UI asks far more often — is this an A2A
 * integration, whose binding is a partner relationship rather than a set of
 * tools. */
export function isA2AProvider(provider: string): boolean {
  return (A2A_PROVIDERS as readonly string[]).includes(provider);
}

/** Generate or rotate the credential of an `oauth_a2a` integration. The secret
 * comes back ONCE and is never retrievable again. A regenerate rotates the
 * secret and keeps the client id (#105); `{ rotateClientId: true }` is the
 * explicit action that changes both. Either retires the previous secret on
 * every agent the integration is bound to. */
export function useRegenerateA2ACredential(integrationId: string) {
  const qc = useQueryClient();
  return useMutation<{ client_id: string; client_secret: string }, Error, { rotateClientId?: boolean } | void>({
    mutationFn: (options) =>
      api.post(
        `/integrations/${integrationId}/a2a-credential${options && options.rotateClientId ? "?rotate_client_id=true" : ""}`,
        {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

/** The last token request this inbound credential refused (#105), for its
 * managers. Served by the single-integration read alone (`GET
 * /integrations/{id}`): the list rows carry no `a2a_credential`, so the card
 * reads it here rather than off the integration the page found in the list.
 * Its key sits under `integrations`, so a regenerate refreshes it. */
export function useA2ALastRefusal(integrationId: string, enabled = true) {
  return useQuery<Integration, Error, A2ARefusedTokenRequest | null>({
    queryKey: ["integrations", integrationId, "a2a-credential"],
    queryFn: () => api.get(`/integrations/${integrationId}`),
    select: (integration) => integration.a2a_credential?.last_refused_token_request ?? null,
    enabled: enabled && !!integrationId,
  });
}

/* A2A is offered exactly while agentic chat is. Both queries sit behind
 * `require_chat_enabled` server-side, which 403s otherwise — a supported
 * deployment, not a broken one — so each is also disabled client-side on
 * `chat_enabled` rather than left to hit a refusal. `useA2AExposure`
 * additionally 404s for an agent the caller has no authority to expose. Both
 * consumers read absent data as "not exposed". Both outcomes are ordinary,
 * hence `unavailableIsOrdinary`: the client-side disable does not cover the
 * window where the platform config is still resolving and `chat_enabled`
 * reads optimistically true. */

/** When THIS partner last submitted work, for either A2A provider.
 *
 * Scoped by the calling registration recorded on each task — never by the
 * agents the integration is bound to, since two A2A integrations on one agent
 * would otherwise each report the other's traffic. Derived at read time rather
 * than from a counter on the request path (`tasks/get` is a polling method, so
 * a per-call write would be a hot-row update for a number read once a day).
 * `null` means this partner has submitted nothing yet. */
export function useA2APartnerActivity(integrationId: string, enabled = true) {
  const offered = useChatSendEnabled();
  return useQuery<{ last_activity_at: string | null }>({
    queryKey: ["a2a-partner-activity", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/a2a-activity`),
    enabled: offered && enabled && !!integrationId,
    ...unavailableIsOrdinary,
  });
}

export interface A2AExposureStatus {
  exposed: boolean;
  card_url?: string | null;
  /** The user external work executes as, inherited from the agent's owner
   * when the binding exposed it. Never choosable. */
  run_as_user_id?: string | null;
  /** The re-displayable half of a LEGACY per-exposure credential, null on
   * everything created since. A partner credential belongs to the
   * `oauth_a2a` integration now and is shown there — it is valid on every
   * agent that binds it rather than on this one. There is no secret field:
   * this endpoint is read-only and can mint nothing. */
  client_id?: string | null;
  /** Where a partner exchanges its credential for a bearer: the card's
   * `tokenUrl`, set on every live exposure. */
  token_url?: string | null;
  /** The per-exposure opt-in of #2202, off by default: a partner's task on
   * this exposure also reads the exposed agent's home fleet, its health, its
   * work items and the running build. Read at each partner call. */
  partner_fleet_reads?: boolean;
}

/** Switch the #2202 opt-in on the agent's LIVE exposure. Exposing stays the
 * bindings' job: an agent that is not exposed answers 404. The answer is the
 * exposure status, written straight into the shared cache entry. */
export function useSetA2APartnerFleetReads(agentId: string) {
  const qc = useQueryClient();
  return useMutation<A2AExposureStatus, Error, boolean>({
    mutationFn: (enabled) =>
      api.put<A2AExposureStatus>(`/agents/${agentId}/a2a/partner-fleet-reads`, { enabled }),
    onSuccess: (status) => {
      qc.setQueryData(["a2a-exposure", agentId], status);
    },
  });
}

/** The A2A liaisons of the agents the caller sees, drawn by the Agentic Flow
 * map and the fleet graph (`lib/agent-graph-a2a.ts`). Absent data draws no
 * partner, as on a deployment without agentic chat. */
export function useA2ALiaisons(enabled = true) {
  const offered = useChatSendEnabled();
  return useQuery<A2ALiaisons>({
    queryKey: ["a2a-liaisons"],
    queryFn: () => api.get("/a2a/liaisons"),
    enabled: offered && enabled,
    staleTime: 60_000,
    ...unavailableIsOrdinary,
  });
}

export function useA2AExposure(agentId: string, enabled = true) {
  const offered = useChatSendEnabled();
  return useQuery<A2AExposureStatus>({
    queryKey: ["a2a-exposure", agentId],
    queryFn: () => api.get(`/agents/${agentId}/a2a`),
    enabled: offered && enabled && !!agentId,
    ...unavailableIsOrdinary,
  });
}

/** The exposure of each agent, in order: the same cache entries as
 * `useA2AExposure`, so a registration page reading every bound agent and an
 * agent page reading one share their reads. */
export function useA2AExposures(agentIds: string[]) {
  const offered = useChatSendEnabled();
  return useQueries({
    queries: agentIds.map((agentId) => ({
      queryKey: ["a2a-exposure", agentId],
      queryFn: () => api.get<A2AExposureStatus>(`/agents/${agentId}/a2a`),
      enabled: offered && !!agentId,
      ...unavailableIsOrdinary,
    })),
  });
}
