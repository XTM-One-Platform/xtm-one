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
 * The answer link of a decision (`/decide/:token`, dev-docs/human-gates.md
 * I.4): what the link opens (`GET /decisions/links/{token}`) and the answer
 * it carries (`POST /decisions/links/{token}/answer`). The server checks the
 * signature, the expiry and that the signed-in person is the recipient; a
 * decision no longer open answers nothing and comes back decided.
 */
import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { WorkItemAskOption } from "@/types";

/** Who answered, which option, when: shown once the decision is closed. */
interface DecisionLinkDecided {
  status: string;
  option: string | null;
  label: string | null;
  text: string | null;
  answered_by: string | null;
  answered_at: string | null;
  surface: string | null;
}

/** What an answer link opens. `option` is `text` for the "Answer with text" link. */
export interface DecisionLinkView {
  decision_id: string;
  status: string;
  option: string;
  option_label: string | null;
  ask: {
    question?: string | null;
    context?: string | null;
    /** The whole context when the card's cap cut `context` (#2800). */
    context_full?: string | null;
    options?: WorkItemAskOption[] | null;
    recommendation?: string | null;
    deadline?: string | null;
    links?: { label: string; url: string }[] | null;
  };
  deadline: string | null;
  source_link: string | null;
  decided: DecisionLinkDecided | null;
  /** On an answer: true when this click recorded it, false when it was already closed. */
  answered_now?: boolean;
}

/** The option slot of the link that opens the text field. */
export const TEXT_LINK_OPTION = "text";

/** The HTTP status a failed link call carries (`lib/api.ts` attaches it). */
export function linkErrorStatus(error: unknown): number | null {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : null;
}

/** Read the link: never answers. Its refusals (403, 404, 410) are states of the page, not faults. */
export function useDecisionLink(token: string) {
  return useQuery<DecisionLinkView, Error>({
    queryKey: ["decision-link", token],
    queryFn: () => api.get(`/decisions/links/${encodeURIComponent(token)}`),
    enabled: !!token,
    retry: false,
    throwOnError: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Answer with the link's option, or on the text link's page with an option picked on the card
 * or a free text. The page says the outcome itself.
 */
export function useAnswerDecisionLink(token: string) {
  return useMutation<DecisionLinkView, Error, { option?: string; text?: string }>({
    mutationFn: (body) => api.post(`/decisions/links/${encodeURIComponent(token)}/answer`, body),
    meta: { suppressGlobalErrorToast: true },
  });
}
