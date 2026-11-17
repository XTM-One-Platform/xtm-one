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
 * What the wizard assistant's answer left out, worded for the person
 * (AGENT_WIZARD_ASSIST). The route returns codes (`NOTE_CODES` in
 * `backend/app/services/agent_wizard_assistant.py`); the wizard translates
 * them. A code this table does not know reads as the generic sentence, so a
 * code added on the server is never shown raw.
 *
 * Values are catalog keys `validate-i18n` checks. @i18n-keys
 */
export const ASSIST_NOTE_LABELS = {
  template_unavailable: "The template the assistant chose is not available; it was left out.",
  icon_unknown: "The icon the assistant chose does not exist; it was left out.",
  model_unavailable: "The model the assistant chose is not available to you; it was left out.",
  tools_unavailable: "A tool the assistant named is not available to you; it was left out.",
  integrations_unavailable: "An integration the assistant named is not available to you; it was left out.",
  knowledge_bases_unavailable: "A knowledge base the assistant named is not available to you; it was left out.",
  skills_unavailable: "A skill the assistant named is not available to you; it was left out.",
  handover_agents_unavailable: "A handover agent the assistant named is not available to you; it was left out.",
  unknown: "Part of the assistant's change was not available to you; it was left out.",
} as const;

export function assistNoteLabel(code: string): string {
  // An own key only: a code is a value the server sent, `toString` included.
  return Object.prototype.hasOwnProperty.call(ASSIST_NOTE_LABELS, code)
    ? ASSIST_NOTE_LABELS[code as keyof typeof ASSIST_NOTE_LABELS]
    : ASSIST_NOTE_LABELS.unknown;
}

/**
 * A failed round, worded for the person. The route answers a 503 whose
 * `detail` is `{code, message}` (`lib/api.ts` keeps it on the error); a code
 * this table knows reads in the person's language, anything else as the
 * error's own message.
 *
 * Values are catalog keys `validate-i18n` checks. @i18n-keys
 */
export const ASSIST_ERROR_LABELS = {
  assistant_model_unavailable: "The assistant could not reach the model. Try again in a moment.",
  assistant_no_playable_answer: "The assistant could not prepare a change the wizard accepts. Rephrase and try again.",
} as const;

/** The catalog key of a failed round's message, or null when the error
 *  carries no code this table knows (its own message is shown then). */
export function assistErrorLabel(error: unknown): string | null {
  const code = (error as { detail?: { code?: unknown } } | null)?.detail?.code;
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(ASSIST_ERROR_LABELS, code)
    ? ASSIST_ERROR_LABELS[code as keyof typeof ASSIST_ERROR_LABELS]
    : null;
}
