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
 * HITL whitelist administration.
 *
 * The whitelist names the tools that may run without human approval. Editing it
 * is the escape hatch that makes a wrong shipped default correctable for a
 * given deployment, rather than requiring a code change.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export interface ToolGateEntry {
  tool_key: string;
  /** Where it comes from, and so who can act on it. */
  source:
    | "builtin"
    | "custom"
    | "platform"
    | "mcp"
    | "integration"
    | "channel"
    | "other";
  /**
   * Builtin id the tool belongs to, for the tools list's builtin rows: those
   * are groups, so a row covers several names and reports them in aggregate.
   */
  group: string | null;
  /** What contributes it: a platform, an MCP server, or an integration provider. */
  provider: string | null;
  /** Whether it currently pauses — the whitelist decision only. */
  gates: boolean;
  /** ``admin_added`` | ``shipped_default`` | ``platform_read``, or null. */
  exempt_source: string | null;
  /**
   * Gated because somebody here re-gated it, rather than because nothing ever
   * exempted it. The two look identical otherwise, and the control differs:
   * one restores the shipped default, the other makes a new exemption.
   */
  regated: boolean;
  /**
   * Classified a read, yet gated by a decision in the product rather than by
   * anything this deployment set. Nothing an operator changes here explains
   * it, so the row has to say so itself.
   */
  hand_gated: boolean;
}

interface ToolGateCatalog {
  tools: ToolGateEntry[];
  platform_reads_available?: boolean;
}

/**
 * Every tool this deployment can name, and whether it pauses (FR-045).
 *
 * Readable by any signed-in user, unlike the whitelist itself: what pauses is
 * what the tools list shows everyone, and a user grants their own standing
 * approval from the same row. Only changing the platform whitelist is admin-only.
 */
export function useToolGateCatalog(enabled = true) {
  return useQuery<ToolGateCatalog>({
    queryKey: ["tool-gate-catalog"],
    queryFn: () => api.get("/hitl-whitelist/catalog"),
    enabled,
  });
}

export function useAddToWhitelist() {
  const qc = useQueryClient();
  return useMutation<
    { tool_key: string; recognised: boolean; suggestion: string | null },
    Error,
    { toolKey: string; reason?: string }
  >({
    mutationFn: ({ toolKey, reason }) =>
      api.post("/hitl-whitelist", { tool_key: toolKey, reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["hitl-whitelist"] });
      qc.invalidateQueries({ queryKey: ["tool-gate-catalog"] });
    },
  });
}

export function useRemoveFromWhitelist() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (toolKey: string) =>
      api.delete(`/hitl-whitelist/${encodeURIComponent(toolKey)}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["hitl-whitelist"] });
      qc.invalidateQueries({ queryKey: ["tool-gate-catalog"] });
    },
  });
}

interface PlatformReadBulkResult {
  provider: string;
  changed: number;
  tool_keys: string[];
}

/**
 * Re-gate, or restore, every read tool one platform contributes.
 *
 * The derived list arrives dozens of names at a time, so rejecting it has to
 * be one action. Without the `restore` direction the rejection would be a
 * one-way door as wide as the list.
 */
export function usePlatformReadBulk() {
  const qc = useQueryClient();
  return useMutation<
    PlatformReadBulkResult,
    Error,
    { provider: string; action: "require-approval" | "restore-default" }
  >({
    mutationFn: ({ provider, action }) =>
      api.post(
        `/hitl-whitelist/platform-reads/${encodeURIComponent(provider)}/${action}`,
        {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["hitl-whitelist"] });
      qc.invalidateQueries({ queryKey: ["tool-gate-catalog"] });
    },
  });
}
