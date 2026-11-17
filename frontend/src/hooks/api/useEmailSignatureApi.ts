// SPDX-License-Identifier: Apache-2.0

/** Email signature cards of an integration and of an agent. */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AgentEmailSignatures, IntegrationEmailSignature } from "@/types";

/** The Email signature setting of an integration whose tools send email, and what it resolves to. */
export function useIntegrationEmailSignature(integrationId: string, enabled = true) {
  return useQuery<IntegrationEmailSignature>({
    queryKey: ["integrations", integrationId, "email-signature"],
    queryFn: () => api.get(`/integrations/${integrationId}/email-signature`),
    enabled: enabled && !!integrationId,
  });
}

/** Each email-sending binding of an agent: its override, what it inherits and the result. */
export function useAgentEmailSignatures(agentId: string, enabled = true) {
  return useQuery<AgentEmailSignatures>({
    queryKey: ["agent", agentId, "email-signatures"],
    queryFn: () => api.get(`/agents/${agentId}/email-signatures`),
    enabled: enabled && !!agentId,
  });
}
