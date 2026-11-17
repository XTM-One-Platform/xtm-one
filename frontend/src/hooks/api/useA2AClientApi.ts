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

/** A2A outbound (client) API hooks — reading and repairing a partner registration.
 *
 * There is no create, edit or delete here, and no direction mutation, for the
 * same reason the inbound hook has no exposure mutation: a partner IS an
 * integration, so those are the ordinary integration calls, and direction is a
 * field on the ordinary integration update. A second path to any of them would
 * disagree with the first the moment either was used.
 *
 * What is left is a READ of the verification state plus the two repair actions
 * an operator needs when a partner stops working.
 *
 * Which directions a mechanism serves is read from each catalog entry's own
 * `a2a_directions` — no local table.
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { api, isNotFoundError, unavailableIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { A2A_PROVIDERS } from "@/hooks/api/useA2AApi";

/** Provider keys whose integrations can face outward. Mirrors
 * `app.a2a.providers.ALL_A2A_PROVIDERS`, which — like this alias — currently
 * equals the exposable set: a different QUESTION with the same answer. A
 * single-direction mechanism would split the two lists again. */
export const A2A_CLIENT_PROVIDERS = A2A_PROVIDERS;

export interface A2ATargetState {
  /** This target's row id. Required by `accept-card`, which names ONE card. */
  id: string;
  card_url: string;
  /** The health verdict: `usable`, `limited` or `unusable`. Written when a
   * verdict is produced and read everywhere else — never inferred here, so
   * this view and the delegation pre-flight cannot disagree. */
  usability: string;
  unusable_reason?: string | null;
  last_verified_at?: string | null;
  last_verification_error?: string | null;
  /** `verified` when the partner's card proved itself, `unsigned` when it
   * carried no signature, `null` before a first verdict. An unsigned card is
   * usable and must never be presented as verified. */
  signature_state?: string | null;
  agreed_protocol_version?: string | null;
  /** Which A2A binding the verified endpoint speaks: JSONRPC or HTTP+JSON. */
  transport?: string | null;
  declared_name?: string | null;
  declared_description?: string | null;
  declared_skills?: string[] | null;
  declared_input_types?: string[] | null;
  declared_output_types?: string[] | null;
  /** Whether the partner's card says it will call back. Null until the next
   * verification records it; absent reads as not declared. */
  declares_callbacks?: boolean | null;
  /** The tool an agent bound to this registration sees for this partner
   * agent. Computed server-side, so it is the name the model is given. */
  tool_name?: string | null;
  circuit_open_until?: string | null;
  /** The partner changed its card materially; delegation is suspended until
   * someone with authority accepts it. */
  card_change_pending: boolean;
  /** The fingerprint currently observed. Pass it back to accept the change —
   * the server refuses anything else, so an operator cannot accept a card
   * that moved again between reading and clicking. */
  card_fingerprint?: string | null;
}

/* Every endpoint under `/a2a/client/` answers only for an OUTBOUND
 * registration, and 404s for an inbound one or a non-A2A provider. The
 * integration detail page renders both cases, so the 404 is an ordinary
 * answer — hence `unavailableIsOrdinary` on every query below. A present answer
 * therefore means "outbound"; a 404 means inbound — read by `useA2ADirection`.
 * A2A is offered exactly while agentic chat is; otherwise the server refuses
 * with a 403, so the queries are disabled on `chat_enabled` rather than left
 * to ask. */

interface A2AVerificationState {
  /** One per agent the partner exposes. A card describes exactly one agent,
   * so a partner offering three publishes three cards and contributes three
   * delegation tools. */
  targets: A2ATargetState[];
  /** Whether a partner could reach this deployment to call back. What the
   * outbound direction exposes depends on it. */
  callbacks_reachable?: boolean;
  /** The one cancel tool this registration confers, alongside a delegation
   * tool per agent. Null when no agent has verified. */
  cancel_tool_name?: string | null;
}

/** The verification state of one outbound partner registration.
 *
 * Polls on the SLOW interval: a verdict changes when the sweep runs (hourly by
 * default) or when an operator acts, so anything faster would be requests
 * spent watching a value that does not move.
 *
 * `throwOnError: false` for a consumer the answer is optional to - the agent
 * Overview's binding badge - where a 5xx or a network failure must
 * leave the badge out rather than hand the page to its ErrorBoundary. Such a
 * read keeps a cache entry of its own: a strict reader that mounts on an error
 * it would throw skips its retry-on-mount and throws it at once, so a failure
 * the badge tolerated would replace the integration page the row opens. The
 * key extends the strict one, so invalidating that prefix reaches both.
 *
 * `refetchInterval: false` for a consumer that needs one answer rather than a
 * watched verdict - the same badge, which reads only the direction: a
 * registration cannot change direction while it is bound, and a read by
 * someone who manages it may run partner discovery. */
export function useA2AVerificationState(
  integrationId: string,
  enabled = true,
  options: { throwOnError?: boolean; refetchInterval?: false } = {},
) {
  const offered = useChatSendEnabled();
  const tolerant = options.throwOnError === false;
  return useQuery<A2AVerificationState>({
    queryKey: ["a2a-client-verification", integrationId, ...(tolerant ? ["tolerant"] : [])],
    queryFn: () => api.get(`/a2a/client/${integrationId}`),
    enabled: offered && enabled && !!integrationId,
    refetchInterval: options.refetchInterval ?? REFETCH_INTERVAL_SLOW,
    ...unavailableIsOrdinary,
    ...(tolerant ? { throwOnError: false } : {}),
  });
}

/** Re-verify the partner's card now.
 *
 * Also the way out of a cooldown: a successful verification closes the
 * circuit, so an operator who has just corrected a credential is never made to
 * wait one out. */
export function useVerifyA2APartner(integrationId: string) {
  const qc = useQueryClient();
  return useMutation<A2AVerificationState, Error, string | void>({
    mutationFn: (targetId) =>
      api.post(
        `/a2a/client/${integrationId}/verify` +
          (targetId ? `?target_id=${encodeURIComponent(targetId)}` : ""),
        {},
      ),
    onSuccess: (state) => {
      qc.setQueryData(["a2a-client-verification", integrationId], state);
      // The integration's own status line derives from the same verdict.
      qc.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

/** Accept a partner's changed card and resume delegation.
 *
 * The fingerprint is required and is checked server-side against what is
 * currently observed: a trust decision applies to ONE card, not to whichever
 * arrives next. Sending the fingerprint the operator was shown is what makes
 * "they accepted what they read" true rather than assumed. */
export function useAcceptA2ACardChange(integrationId: string) {
  const qc = useQueryClient();
  return useMutation<
    A2ATargetState,
    Error,
    { target_id: string; fingerprint: string }
  >({
    mutationFn: (body) =>
      api.post(`/a2a/client/${integrationId}/accept-card`, body),
    onSuccess: () => {
      // Refetched rather than written into the cache: this answers with the
      // ONE target that was accepted, and splicing it into the list by hand
      // would be a second place deciding what the set now looks like.
      qc.invalidateQueries({ queryKey: ["a2a-client-verification", integrationId] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

interface A2ADelegationEntry {
  id: string;
  agent_name?: string | null;
  outcome?: string | null;
  partner_reason?: string | null;
  protocol_version: string;
  /** The card provenance THIS delegation ran under. The registration's may
   * have changed since, which is why it is recorded per delegation. */
  signature_state: string;
  continuation_count: number;
  submitted_at: string;
}

/** What this partner was asked to do, newest first.
 *
 * Scoped server-side by the registration recorded on each delegation at
 * submission time — never by the agents this integration is bound to. Two
 * registrations may be bound to one agent, and filtering by binding would make
 * each report the other's traffic. */
export function useA2ADelegations(integrationId: string, enabled = true) {
  const offered = useChatSendEnabled();
  return useQuery<A2ADelegationEntry[]>({
    queryKey: ["a2a-client-delegations", integrationId],
    queryFn: () => api.get(`/a2a/client/${integrationId}/delegations`),
    enabled: offered && enabled && !!integrationId,
    refetchInterval: REFETCH_INTERVAL_SLOW,
    ...unavailableIsOrdinary,
  });
}

/** Which direction an A2A registration serves, or `undefined` until known and
 * for every other provider. The integration states it (`a2a_direction`, its
 * settings row), so an inbound one never calls the outbound-only endpoints
 * (#11790). Without it (a row with no settings, an older server) the
 * verification state is asked: it answers only for outbound and 404s for
 * inbound. Only a 404 means inbound: any other failure is not an answer, and
 * a wrong "inbound" makes the configuration card drop the outbound keys from
 * its next save. `options` go to `useA2AVerificationState`. */
export function useA2ADirection(
  integration: { id: string; provider: string; a2a_direction?: "inbound" | "outbound" | null } | undefined,
  options: { throwOnError?: boolean; refetchInterval?: false } = {},
): "inbound" | "outbound" | undefined {
  const capable = !!integration && (A2A_CLIENT_PROVIDERS as readonly string[]).includes(integration.provider);
  const stated = capable ? integration?.a2a_direction ?? undefined : undefined;
  const { data, isPending, error } = useA2AVerificationState(integration?.id ?? "", capable && !stated, options);
  if (!capable) return undefined;
  if (stated) return stated;
  if (isPending) return undefined;
  if (data) return "outbound";
  return isNotFoundError(error) ? "inbound" : undefined;
}
