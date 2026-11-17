// SPDX-License-Identifier: Apache-2.0

/**
 * Organizations (#9157, slice M2): the primary organization and the tracked
 * ones (providers, customers, partners, subsidiaries). The list is read by
 * every signed-in user; add, edit and archive are for administrators. A
 * tracked organization starts with monitoring off (dev-docs/ctem-fleets.md
 * section 9.7): nothing here switches it on.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type OrganizationRelationship = "provider" | "customer" | "partner" | "subsidiary";
export type OrganizationCriticality = "low" | "medium" | "high" | "critical";

export interface Organization {
  id: string;
  name: string;
  role: "primary" | "tracked";
  relationship: OrganizationRelationship | null;
  criticality: OrganizationCriticality;
  opencti_organization_id: string | null;
  monitoring: string;
  created_at: string;
  archived_at: string | null;
}

export interface OrganizationInput {
  name: string;
  relationship: OrganizationRelationship;
  criticality: OrganizationCriticality;
  opencti_organization_id: string | null;
}

const KEY = ["organizations"] as const;

/** An OpenCTI Organization an administrator links a tracked organization to (slice M3, GET /organizations/opencti-identities). */
export interface OpenCtiIdentity {
  id: string;
  name: string;
  aliases: string[];
  organization_type: string | null;
}

/**
 * The connected OpenCTI's Organizations matching *search* by name or alias
 * (administrators). Nothing is asked before a character is typed; an OpenCTI
 * that is not connected or does not answer is the query's error (409), shown
 * on the picker's own line: it never throws to the page's error boundary
 * (the app's queries throw by default, main.tsx), so the drawer and what was
 * typed stay (UX round 1 B1 of #9443).
 */
export function useOpenCtiIdentities(search: string, enabled: boolean) {
  const query = search.trim();
  return useQuery<OpenCtiIdentity[]>({
    queryKey: [...KEY, "opencti-identities", query],
    queryFn: () => api.get(`/organizations/opencti-identities?search=${encodeURIComponent(query)}`),
    enabled: enabled && query.length > 0,
    retry: false,
    throwOnError: false,
    staleTime: 60_000,
  });
}

/**
 * The OpenCTI Organization a tracked organization is linked to, read by id so
 * the Edit drawer names it instead of showing its raw id (UX round 1 B5 of
 * #9443). A failed read stays on the field, never on the page.
 */
export function useOpenCtiIdentity(identityId: string | null, enabled: boolean) {
  return useQuery<OpenCtiIdentity>({
    queryKey: [...KEY, "opencti-identity", identityId],
    queryFn: () => api.get(`/organizations/opencti-identities/${encodeURIComponent(identityId ?? "")}`),
    enabled: enabled && !!identityId,
    retry: false,
    throwOnError: false,
    staleTime: 300_000,
  });
}

export function useOrganizations() {
  return useQuery<Organization[]>({
    queryKey: KEY,
    queryFn: () => api.get("/organizations"),
  });
}

export function useCreateOrganization() {
  const qc = useQueryClient();
  return useMutation<Organization, Error, OrganizationInput>({
    mutationFn: (body) => api.post("/organizations", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useUpdateOrganization() {
  const qc = useQueryClient();
  return useMutation<Organization, Error, { id: string } & OrganizationInput>({
    mutationFn: ({ id, ...body }) => api.patch(`/organizations/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

/**
 * One drafted row of a tracked-organizations AI draft (#9484 S-c, `organization_tracked_ai_draft.Row`).
 * `status` is `pending`, `conflict` (already tracked: never created), `error` (its batch failed),
 * then `accepted` or `rejected` once the review is decided.
 */
export interface TrackedDraftRow {
  name: string;
  relationship: OrganizationRelationship;
  criticality: OrganizationCriticality;
  sector: string;
  regions: string[];
  website: string;
  confidence: "high" | "medium" | "low";
  basis: string;
  /** OpenCTI Organizations whose name or alias matches, exact first. */
  opencti_matches: { id: string; name: string }[];
  /** Pre-set only when exactly one OpenCTI Organization matches exactly. */
  opencti_organization_id: string;
  already_tracked_id: string;
  error: string;
  notes: string[];
  status: "pending" | "conflict" | "error" | "accepted" | "rejected";
}

export interface TrackedDraftResponse {
  /** The pending `new_tracked` proposal: the review page's id and the decide call's. */
  proposal_id: string;
  status: string;
  rows: TrackedDraftRow[];
  names_read: number;
  names_drafted: number;
  /** Metered agentic requests this run used (one per batch of 10 names not yet tracked). */
  requests: number;
  /** Why OpenCTI was not searched, when it was not. */
  opencti_unavailable: string | null;
  created_at: string;
}

export interface TrackedDraftInput {
  names: string;
  file: File | null;
  relationship: OrganizationRelationship;
}

/** At most this many names per run; one metered call per batch of TRACKED_DRAFT_BATCH (backend MAX_NAMES, BATCH_SIZE). */
export const TRACKED_DRAFT_MAX_NAMES = 50;
export const TRACKED_DRAFT_BATCH = 10;

/** Drafts tracked organizations with a model on the administrator's click; nothing is created (429 = quota refusal). */
export function useDraftTrackedOrganizations() {
  return useMutation<TrackedDraftResponse, Error, TrackedDraftInput>({
    mutationFn: ({ names, file, relationship }) => {
      const form = new FormData();
      form.append("names", names);
      form.append("relationship", relationship);
      if (file) form.append("file", file, file.name);
      return api.postForm("/organizations/tracked/ai-draft", form);
    },
    // The dialog says every failure inline; the global toast would repeat the backend text.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** A stored tracked-organizations draft, for its review page. */
export function useTrackedOrganizationsDraft(proposalId: string | undefined) {
  return useQuery<TrackedDraftResponse>({
    queryKey: [...KEY, "ai-draft", proposalId],
    queryFn: () => api.get(`/organizations/tracked/ai-draft/${encodeURIComponent(proposalId ?? "")}`),
    enabled: !!proposalId,
    retry: false,
    throwOnError: false,
  });
}

export interface DecideTrackedDraftInput {
  proposalId: string;
  /** The names accepted; empty rejects the whole draft. */
  accept: string[];
  /** Accepted name -> the OpenCTI Organization picked. */
  openctiIds: Record<string, string>;
}

/** Accepts the named rows (created tracked, monitoring off) or rejects the draft with none. */
export function useDecideTrackedDraft() {
  const qc = useQueryClient();
  return useMutation<{ status: "accepted" | "rejected"; version: number | null; created: string[] }, Error, DecideTrackedDraftInput>({
    mutationFn: ({ proposalId, accept, openctiIds }) =>
      api.post(`/organization-context/proposals/${encodeURIComponent(proposalId)}/decide`, { accept, opencti_ids: openctiIds }),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useArchiveOrganization() {
  const qc = useQueryClient();
  return useMutation<Organization, Error, string>({
    mutationFn: (id) => api.post(`/organizations/${id}/archive`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}
