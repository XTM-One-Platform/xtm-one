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
 * One-click approval of an escalated irreversible action.
 *
 * A tool that escalates such an action (today the OpenCTI entity merge in an
 * autonomous run with human-in-the-loop on) has the agent ask the person to
 * reply `APPROVE <code>`. The run page offers that reply as one click instead
 * of a code to retype. The button sends exactly the reply the question asks
 * for, so it approves exactly what the typed reply approves (the backend
 * parser, `services/hitl_consent.py`), and a question naming no code offers
 * nothing.
 */

/** The keyword the backend reads an approval by (`tools/base.HITL_APPROVAL_KEYWORD`).
 * @public
 */
export const HITL_APPROVAL_KEYWORD = "APPROVE";

// The keyword, the marks a question may put between it and the code (Markdown,
// quotes, a colon), then an action code: a letter prefix, a dash and 8 to 16
// hex digits, not followed by another letter, digit or dash.
const APPROVAL_REQUEST_RE = /\bAPPROVED?\b[\s:*`"'([]*([A-Za-z]{1,4}-[0-9A-Fa-f]{8,16})(?![0-9A-Za-z-])/gi;

const MAX_CODES = 10;

/** The action codes `question` asks the person to approve, in order, once each. */
export function approvalCodesIn(question: string | null | undefined): string[] {
  if (!question) return [];
  const codes: string[] = [];
  for (const match of question.matchAll(APPROVAL_REQUEST_RE)) {
    const code = match[1].toUpperCase();
    if (!codes.includes(code)) codes.push(code);
    if (codes.length === MAX_CODES) break;
  }
  return codes;
}

/** The reply that approves every one of `codes`, each with its own keyword. */
export function approvalReply(codes: readonly string[]): string {
  return codes.map((code) => `${HITL_APPROVAL_KEYWORD} ${code}`).join(", ");
}
