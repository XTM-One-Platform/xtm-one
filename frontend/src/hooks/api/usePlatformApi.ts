// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import type { PlatformMCPServers, RegistrationTokenInfo } from "@/types";

export interface PlatformRegistration {
  id: string;
  platform_identifier: string;
  platform_id: string | null;
  platform_url: string;
  platform_title: string;
  platform_version: string;
  contract: string;
  license_type: string | null;
  license_validated: boolean;
  integration_id: string | null;
  status: string;
  registered_at: string;
  last_heartbeat_at: string;
  /** What the platform reports about itself: can its embedded assistant show
   *  a tool-approval prompt and return a decision? `null` means it has never
   *  said — every build predating the capability. Not the same as `false`,
   *  which is the platform actively telling us it cannot ask. */
  supports_approval_prompts: boolean | null;
  /** What happens to this platform's tools while no prompt can be shown. */
  approval_policy: "require" | "skip";
  /** Provenance: "default" (built-in), "deployment" (env var at first
   *  registration), or "admin" (an explicit choice, never overwritten). */
  approval_policy_set_by: "default" | "deployment" | "admin";
  /** The decision actually applied. Computed server-side so this page and the
   *  dashboard card can never disagree about whether a platform is gated. */
  gates_tools: boolean;
  /** Something can now prompt, but the tools are still set to skip — offer to
   *  restore the safer posture rather than changing it silently. */
  capability_nudge: boolean;
  /** Who that something is. `declared` — the platform said so at registration,
   *  speaking for its whole assistant. `observed` — a client of it was seen
   *  declaring, per message, that it will answer a prompt; weaker, since an
   *  unattended caller of the same platform may still send nothing. Word the
   *  two differently: the second is one caller speaking, not the platform. */
  capability_nudge_reason: "declared" | "observed" | null;
  /** Live connectivity health (status active + recent heartbeat), computed
   *  server-side. Refetched every 30s, so a recovered platform turns green
   *  without waiting on the (up to 24h) posture cache. */
  healthy: boolean;
}

interface PurgeSummary {
  platform_identifier: string;
  integrations_deleted: number;
  agents_cleaned: number;
}

export function usePlatformRegistrations() {
  return useQuery<PlatformRegistration[]>({
    queryKey: ["platform-registrations"],
    queryFn: () => api.get("/platform/registrations"),
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
}

/** Set whether one registered platform's tools ask for approval.
 *  Writing marks the choice as an administrator's, which is what protects it
 *  from being overwritten by a later heartbeat or capability declaration. */
export function useSetPlatformApproval() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, policy }: { id: string; policy: "require" | "skip" }) =>
      api.patch(`/platform/registrations/${id}/approval`, {
        approval_policy: policy,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platform-registrations"] });
    },
  });
}

/** Describe the platform MCP servers (OpenCTI / OpenAEV / OpenCRQ) —
 *  endpoint URLs, live connection status, tool counts, global toggle. */
export function usePlatformMCPServers() {
  return useQuery<PlatformMCPServers>({
    queryKey: ["platform-mcp-servers"],
    queryFn: () => api.get("/platform/mcp-servers"),
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
}

export function usePurgeRegistration() {
  const qc = useQueryClient();
  return useMutation<PurgeSummary, Error, string>({
    mutationFn: (registrationId: string) =>
      api.delete(`/platform/registrations/${registrationId}/purge`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["platform-registrations"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

// The registration token is a shared secret and the backend endpoint is
// admin-only, so the safe default is NOT to fetch: callers opt in by passing
// `enabled=isAdmin`, ensuring a non-admin can never trigger the (now 403) fetch
// even if a call site forgets the flag.
export function useRegistrationToken(enabled = false) {
  return useQuery<RegistrationTokenInfo>({
    queryKey: ["platform-registration-token"],
    queryFn: () => api.get("/platform/registration-token"),
    staleTime: 5 * 60 * 1000,
    enabled,
  });
}


/** Package formats offered by `POST /platform/mcp-servers/xtm/package`. */
export type XtmMcpPackageFormat = "claude-code" | "mcpb" | "agent-plugin";

/**
 * Download an installer package for the platform tools MCP server and save it.
 *
 * Goes through `api.postDownload` rather than a bespoke `fetch`: the endpoint
 * is a POST (with `include_key` it mints an API key, which no GET should do)
 * and the caller needs a response header back, but everything else — base URL,
 * auth, backend-down detection, the shared 401/403 handling — must stay
 * inherited. A hand-rolled fetch here worked until the day the app is served
 * under a path prefix or a session expires, and then it fails alone.
 *
 * Returns the name of the API key the server created, when it created one, so
 * the UI can name it instead of telling the user to open the archive.
 */
export async function downloadXtmMcpPackage(
  format: XtmMcpPackageFormat,
  includeKey: boolean,
  target = "xtm",
): Promise<{ keyCreated: string | null }> {
  const { blob, filename, headers } = await api.postDownload(
    "/platform/mcp-servers/xtm/package",
    { format, include_key: includeKey, target },
  );
  saveBlobAs(blob, filename ?? "xtm-one-platform.zip");
  return { keyCreated: headers.get("X-Api-Key-Created") };
}


/** A client configured by editing a file rather than installing a package. */
export interface XtmMcpSnippet {
  client: string;
  display: string;
  path: string;
  language: string;
  content: string;
  api_key_created: string | null;
}

/**
 * Fetch the config fragment for Codex (shared with the ChatGPT desktop app and
 * the Codex IDE extension) or Cursor.
 *
 * A fragment, not a file: both targets are files that already hold the user's
 * other MCP servers, so this is something to merge — handing over a whole
 * `config.toml` would invite overwriting a working configuration.
 */
export async function fetchXtmMcpSnippet(
  client: string,
  includeKey: boolean,
  target = "xtm",
): Promise<XtmMcpSnippet> {
  return api.post<XtmMcpSnippet>("/platform/mcp-servers/xtm/snippet", {
    client,
    include_key: includeKey,
    target,
  });
}
