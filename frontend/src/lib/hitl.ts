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
 * The vocabulary of the approval gate, shared by every surface that draws it.
 *
 * Kept out of the icon module so the mapping can be imported by a hook or a
 * plain function without dragging a component along — and so the icon module
 * exports components only.
 *
 * See `@/components/ui/hitl-icon` for what each state looks like and why.
 */

import {
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  ShieldQuestion,
  type LucideIcon,
} from "lucide-react";

export type HitlState = "gated" | "open" | "unknown" | "waiting" | "topic";

/** What each state means when the surface offers no wording of its own.
 *  @i18n-keys */
export const DEFAULT_LABEL = {
  gated: "Asks first",
  open: "Runs without asking",
  unknown: "Not known whether it asks first",
  waiting: "Waiting for someone to decide",
  topic: "Tool approvals",
} as const satisfies Record<HitlState, string>;

export const GLYPH: Record<HitlState, { Icon: LucideIcon; tone: string }> = {
  gated: { Icon: ShieldCheck, tone: "text-feedback-success-primary" },
  open: { Icon: ShieldOff, tone: "text-feedback-warning-primary" },
  unknown: { Icon: ShieldQuestion, tone: "text-default-secondary" },
  waiting: { Icon: ShieldAlert, tone: "text-feedback-warning-primary" },
  topic: { Icon: Shield, tone: "text-accent" },
};

/** The glyph alone, for a prop that takes a component rather than an element. */
export function hitlGlyph(state: HitlState): LucideIcon {
  return GLYPH[state].Icon;
}


/**
 * Whether a tool, agent or connection pauses, as a state.
 *
 * `null` / `undefined` is `unknown` rather than `open`: not knowing whether
 * something gates is not the same as knowing it does not, and only one of those
 * two is safe to show as "runs unattended".
 */
export function hitlState(gates: boolean | null | undefined): HitlState {
  if (gates === null || gates === undefined) return "unknown";
  return gates ? "gated" : "open";
}

export {
  approvalsShown,
  effectiveApproval,
  onPersonalList,
  PERSONAL_APPROVALS_PATH,
  PLATFORM_APPROVALS_PATH,
  toolApprovalLink,
  toolApprovalsHref,
} from "./tool-approval-scope";
