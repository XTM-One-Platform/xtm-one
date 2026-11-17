// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  LentResourceKind,
  ResourceExposure,
  ResourceExposureSummary,
  ResourcePreviewRequest,
  ResourcePreviewResponse,
} from "@/types";

/** URL segment of each lent resource under `/exposure`. */
const EXPOSURE_SEGMENTS: Record<LentResourceKind, string> = {
  integration: "integrations",
  mcp_server: "mcp-servers",
  custom_tool: "custom-tools",
  channel_bot: "channel-bots",
  variable: "variables",
};

/** Every path through which one resource reaches people (its managers only).
 *  Optional: the card renders nothing when this fails, so the app-wide
 *  `throwOnError` must not hand the failure to the page's ErrorBoundary. */
export function useResourceExposure(kind: LentResourceKind, id: string | undefined, enabled = true) {
  return useQuery<ResourceExposure>({
    queryKey: ["exposure", kind, id],
    queryFn: () => api.get(`/exposure/${EXPOSURE_SEGMENTS[kind]}/${id}`),
    enabled: enabled && !!id,
    retry: false,
    throwOnError: false,
  });
}

/** How many ways each resource of a kind the caller manages is shared (list
 *  cards). Optional like the card: a failure leaves the badges out. */
export function useResourceExposureSummary(kind: LentResourceKind, enabled = true) {
  return useQuery<{ summaries: Record<string, ResourceExposureSummary>; truncated: boolean }>({
    queryKey: ["exposure", kind, "summary"],
    queryFn: () => api.get(`/exposure/${EXPOSURE_SEGMENTS[kind]}/summary`),
    enabled,
    retry: false,
    throwOnError: false,
  });
}

/** Whose resources a proposed change lends to new people, or takes back.
 *  `useCredentialSharingConfirm()` answers every failure itself (the change
 *  proceeds, or the page's own confirmation stands), so it raises no toast;
 *  it writes nothing, so it does not refresh the exposure reads either. */
export function useResourceExposurePreview() {
  return useMutation<ResourcePreviewResponse, Error, ResourcePreviewRequest>({
    mutationFn: (body) => api.post("/exposure/preview", body),
    meta: { suppressGlobalErrorToast: true, readOnly: true },
  });
}
