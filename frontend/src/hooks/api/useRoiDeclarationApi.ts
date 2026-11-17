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
 * The unit of work an agent, an assignment or a fleet declares and how it is
 * counted (#9285 API, #9536 UI, slice O6 of #9206; design
 * dev-docs/roi-outcomes.md 3.2 and 6.1). Reads and writes only: nothing here
 * starts a run or calls a model.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RoiOriginFields } from "./useRoiCatalogApi";

type RoiSubjectType = "agent" | "assignment" | "fleet";
export type RoiCountMode = "per_run" | "per_output_item" | "per_work_item";

/** `GET /roi/declarations`: the subject's own declaration, `null` when it declares none. */
export interface RoiDeclaration extends RoiOriginFields {
  subject_type: RoiSubjectType;
  subject_id: string;
  unit: string;
  count_mode: RoiCountMode;
  count_path: string | null;
  units_per_count: number;
  updated_at: string | null;
  /** A built-in agent's seeded declaration under this one (#9836 R3a), `null` for any other subject. */
  default?: Pick<RoiDeclaration, "unit" | "count_mode" | "count_path" | "units_per_count"> | null;
}

export interface RoiDeclarationInput {
  unit: string;
  count_mode: RoiCountMode;
  count_path: string | null;
  units_per_count: number;
}

export interface RoiSubject {
  type: RoiSubjectType;
  id: string;
}

const declarationKey = (subject: RoiSubject) => ["roi-declaration", subject.type, subject.id] as const;
const query = (subject: RoiSubject) =>
  `/roi/declarations?subject_type=${encodeURIComponent(subject.type)}&subject_id=${encodeURIComponent(subject.id)}`;

/** `PUT /roi/declarations` outside a hook: the create wizard saves the accepted suggestion once the agent exists. */
export function putRoiDeclaration(subject: RoiSubject, body: RoiDeclarationInput) {
  return api.put<RoiDeclaration>(query(subject), body);
}

/** The unit `GET /roi/suggest` proposes (#9836 R8a): a token match over the catalog, no model call. */
export interface RoiUnitSuggestion {
  unit: string;
  name: string;
  builtin: boolean;
  score: number;
  matched_terms: string[];
}

interface RoiSuggestResponse {
  agent_id: string | null;
  declared: boolean;
  suggestion: RoiUnitSuggestion | null;
}

/** What the suggestion is read for: a saved agent, or the draft name and description of the create wizard. */
type RoiSuggestFor = { agentId: string } | { name: string; description: string };

function suggestQuery(target: RoiSuggestFor): string {
  const params = new URLSearchParams();
  if ("agentId" in target) params.set("agent_id", target.agentId);
  else {
    if (target.name.trim()) params.set("name", target.name.trim().slice(0, 200));
    if (target.description.trim()) params.set("description", target.description.trim().slice(0, 4000));
  }
  return `/roi/suggest?${params.toString()}`;
}

/**
 * The suggested unit of work for an agent (#9836 R8b). `null` target, or a draft with
 * neither name nor description, reads nothing. A failed read is quiet: a suggestion is
 * optional, the card and the wizard work without it.
 */
export function useRoiSuggestion(target: RoiSuggestFor | null) {
  const empty = target !== null && !("agentId" in target) && !target.name.trim() && !target.description.trim();
  const key = target === null || empty ? null : suggestQuery(target);
  return useQuery<RoiSuggestResponse>({
    queryKey: ["roi-suggest", key],
    queryFn: () => api.get(key as string),
    enabled: key !== null,
    throwOnError: false,
    retry: false,
    staleTime: 60_000,
    meta: { suppressGlobalErrorToast: true },
  });
}

// `throwOnError: false`: a read error (a fleet the viewer cannot manage) stays
// in the card instead of the app ErrorBoundary replacing the page.
export function useRoiDeclaration(subject: RoiSubject | null) {
  return useQuery<RoiDeclaration | null>({
    queryKey: subject ? declarationKey(subject) : ["roi-declaration", "none"],
    queryFn: () => api.get(query(subject as RoiSubject)),
    enabled: subject !== null,
    throwOnError: false,
    retry: false,
  });
}

export function usePutRoiDeclaration(subject: RoiSubject) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RoiDeclarationInput) => putRoiDeclaration(subject, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: declarationKey(subject) });
      void qc.invalidateQueries({ queryKey: ["roi-suggest"] });
    },
    // The card shows the refusal inline next to the field.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useDeleteRoiDeclaration(subject: RoiSubject) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete(query(subject)),
    onSuccess: () => qc.invalidateQueries({ queryKey: declarationKey(subject) }),
    meta: { suppressGlobalErrorToast: true },
  });
}
