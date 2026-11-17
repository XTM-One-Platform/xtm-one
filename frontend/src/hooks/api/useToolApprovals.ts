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

/**
 * A user's own standing tool approvals.
 *
 * Created by answering "Yes, always" to an approval prompt, or from the tools
 * list. There is
 * deliberately no cross-user variant: an administrator who needs to act on
 * someone else's preferences impersonates them, which routes through these same
 * endpoints and is audited against both identities.
 */

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { approvalsShown } from "@/lib/tool-approval-scope";

export interface ToolApprovalPreference {
  id: string;
  tool_key: string;
  tool_source: string;
  granted_at: string | null;
  /** Set when an administrator granted this while impersonating the user. */
  granted_by_admin_id: string | null;
}

interface InvalidatedApproval {
  id: string;
  tool_key: string;
  tool_source: string;
  revoked_at: string | null;
  reason: string | null;
}

export function useToolApprovals(enabled = true) {
  return useQuery<{
    /** The caller's allow-all mode: no tool ever asks them. */
    approve_all_tools: boolean;
    /** The platform-wide kill switch — false means nothing pauses for anyone. */
    tool_approvals_enabled: boolean;
    /** What an agent with no `approve_tool_calls` of its own does. */
    approve_tool_calls_default?: boolean;
    /** Whether any tool call can pause on this platform right now: an enabled
     *  agent or the General Assistant asks, and approvals are not suspended. */
    approvals_in_use?: boolean;
    preferences: ToolApprovalPreference[];
    /** Approvals the platform revoked because the tool changed under them. */
    recently_invalidated: InvalidatedApproval[];
  }>({
    queryKey: ["tool-approvals"],
    queryFn: () => api.get("/tool-approvals"),
    enabled,
  });
}

/**
 * Whether tool approvals are in use on this platform.
 *
 * Every surface that labels a tool "Asks first" reads it first: a tool's place
 * on the allowed list says what would happen IF an agent asked, and saying it
 * while no agent asks is what made an opt-in feature look switched on for
 * everyone. False until the answer is known, so nothing flashes in.
 */
export function useApprovalsInUse(): boolean {
  const { data } = useToolApprovals();
  return approvalsShown(data);
}

/** The tools the reader always allows, by name. */
export function useApprovedToolKeys(): ReadonlySet<string> {
  const { data } = useToolApprovals();
  return useMemo(() => new Set((data?.preferences ?? []).map((p) => p.tool_key)), [data?.preferences]);
}

interface AskingAgent {
  id: string;
  name: string;
  /** Asks because it follows the platform default rather than its own setting. */
  follows_default: boolean;
}

export interface ToolApprovalUsage {
  in_use: boolean;
  /** Something is set to ask, whether or not approvals are suspended. */
  configured: boolean;
  suspended: boolean;
  asks_by_default: boolean;
  /** Chat is available, so the General Assistant follows the platform default. */
  general_assistant_available: boolean;
  general_assistant_asks: boolean;
  asking_agents: AskingAgent[];
  agents_following_default: number;
  agents_set_to_ask: number;
  agents_set_not_to_ask: number;
}

export const TOOL_APPROVAL_USAGE_QUERY_KEY = ["tool-approval-usage"] as const;

/** Who asks before running tools, for Settings > Access and security > Tool approvals (admins). */
export function useToolApprovalUsage(enabled = true) {
  return useQuery<ToolApprovalUsage>({
    queryKey: TOOL_APPROVAL_USAGE_QUERY_KEY,
    queryFn: () => api.get("/tool-approvals/usage"),
    enabled,
  });
}

/**
 * Refresh whether approvals are in use, and who asks. Both follow every
 * enabled agent's setting and whether the General Assistant is available, so
 * any agent mutation calls this, not only the agent's own approval card, and
 * so do the platform settings save (the AI switch) and every license save.
 */
export function invalidateApprovalUsage(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: ["tool-approvals"] });
  qc.invalidateQueries({ queryKey: TOOL_APPROVAL_USAGE_QUERY_KEY });
}

/**
 * Grant a standing approval from a list instead of from inside a pause.
 *
 * The same decision as "Yes, always", reached somewhere else: same scope, same
 * fingerprint, same revocation path, and it lands in the same profile list.
 * Refused for a tool whose parameters we cannot read yet (MCP, integration) —
 * a fingerprint-less grant would ask again on the first real call, which is the
 * one thing this control must never do.
 */
export function useGrantToolApproval() {
  const qc = useQueryClient();
  return useMutation<
    { id: string; tool_key: string; tool_source: string },
    Error,
    string
  >({
    mutationFn: (toolKey: string) =>
      api.post("/tool-approvals", { tool_key: toolKey }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tool-approvals"] }),
  });
}

export function useRevokeToolApproval() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (preferenceId: string) =>
      api.delete(`/tool-approvals/${preferenceId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tool-approvals"] }),
  });
}

/** Turn the caller's allow-all mode on or off. */
export function useSetApprovalMode() {
  const qc = useQueryClient();
  return useMutation<{ approve_all_tools: boolean }, Error, boolean>({
    mutationFn: (approveAll: boolean) =>
      api.put("/tool-approvals/mode", { approve_all_tools: approveAll }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tool-approvals"] }),
  });
}

/**
 * How many OTHER people hold a standing approval for one tool.
 *
 * A count, never identities. Editing a custom tool's parameters changes its
 * schema fingerprint, so every "always allow" for it stops describing the tool
 * and the gate asks again — correct, since consent was to the old shape, but
 * silent. "3 people will be asked again" is what makes that visible.
 *
 * Excludes the caller: their own approval is already on their profile and on
 * the tool's own row, and they are the one holder who will not be surprised.
 */
export function useApprovalHolders(toolKey: string | undefined) {
  return useQuery<{ tool_key: string; others: number }>({
    queryKey: ["tool-approval-holders", toolKey],
    queryFn: () =>
      api.get(`/tool-approvals/holders?tool_key=${encodeURIComponent(toolKey!)}`),
    enabled: !!toolKey,
  });
}
