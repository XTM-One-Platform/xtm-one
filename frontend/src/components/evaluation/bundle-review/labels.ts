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

// The words and colours of the STIX bundle review: every status, near-miss kind and
// mention kind the page names, and the design-system token each status is drawn with.

import type { DisplayStatus, MentionHow, NearKind, ReviewMode } from "@/lib/evaluation-bundle-review";

/** What each status reads as, per mode, read by `t_i18n`. @i18n-keys */
const STATUS_LABELS = {
  score: {
    tp: "Matched",
    fn: "Missing",
    fp: "Spurious",
    near: "Near-miss",
    cascade: "Broken by an entity",
    type: "Wrong relationship type",
    sim: "Matched (what-if)",
  },
  alone: {
    tp: "Correct",
    fn: "Missing",
    fp: "Not expected",
    near: "Near-miss",
    cascade: "Broken by an entity",
    type: "Wrong relationship type",
    sim: "Correct (what-if)",
  },
  runs: {
    same: "Unchanged",
    fixed: "Fixed",
    regressed: "Regressed",
    new: "New error",
    still: "Still wrong",
  },
} as const;

export function statusLabel(st: DisplayStatus, mode: ReviewMode): string {
  const table: Record<string, string> = STATUS_LABELS[mode];
  const score: Record<string, string> = STATUS_LABELS.score;
  const runs: Record<string, string> = STATUS_LABELS.runs;
  return table[st] ?? score[st] ?? runs[st] ?? st;
}

/** The status filter chips of each mode, read by `t_i18n`. @i18n-keys */
export const FILTER_LABELS = {
  tp: "Matched",
  fn: "Missing",
  fp: "Spurious",
  near: "Near-miss",
  fixed: "Fixed",
  regressed: "Regressed",
  new: "New error",
  still: "Still wrong",
  same: "Unchanged",
} as const;

/** The same chips when an answer is reviewed alone. @i18n-keys */
export const ALONE_FILTER_LABELS = {
  tp: "Correct",
  fp: "Not expected",
  near: "Near-miss",
} as const;

/** Why two names of one object have two canonical ids, read by `t_i18n`. @i18n-keys */
export const NEAR_KIND_LABELS = {
  type: "Different type",
  "same-name": "Same name, other identifying properties",
  "attack-id": "Same ATT&CK ID",
  defanged: "Defanged",
  alias: "Alias",
  case: "Case or punctuation",
  length: "Longer or shorter name",
  wording: "Different wording",
} as const satisfies Record<NearKind, string>;

/** How a mention was found in the report text, read by `t_i18n`. @i18n-keys */
export const MENTION_LABELS = {
  name: "its name",
  alias: "an alias",
  id: "its ATT&CK ID",
  hash: "its hash",
  defang: "its value, refanged or defanged",
  plural: "its name in the plural",
} as const satisfies Record<MentionHow, string>;

/** The token each status is drawn with (SVG fill and stroke). */
export const STATUS_COLOR: Record<DisplayStatus, string> = {
  tp: "var(--color-feedback-success-primary)",
  sim: "var(--color-feedback-success-primary)",
  fixed: "var(--color-feedback-success-primary)",
  fn: "var(--color-feedback-warning-primary)",
  still: "var(--color-feedback-warning-primary)",
  fp: "var(--color-feedback-error-primary)",
  regressed: "var(--color-feedback-error-primary)",
  new: "var(--color-feedback-error-primary)",
  near: "var(--color-filigran-ia-primary)",
  cascade: "var(--color-filigran-ia-primary)",
  type: "var(--color-filigran-ia-primary)",
  same: "var(--color-feedback-neutral-primary)",
};

/** The text colour of each status. */
export const STATUS_TEXT: Record<DisplayStatus, string> = {
  tp: "text-feedback-success-primary",
  sim: "text-feedback-success-primary",
  fixed: "text-feedback-success-primary",
  fn: "text-feedback-warning-primary",
  still: "text-feedback-warning-primary",
  fp: "text-feedback-error-primary",
  regressed: "text-feedback-error-primary",
  new: "text-feedback-error-primary",
  near: "text-filigran-ia-primary",
  cascade: "text-filigran-ia-primary",
  type: "text-filigran-ia-primary",
  same: "text-default-secondary",
};

/** A row or a mention of each status: a tinted surface with its edge. */
export const STATUS_SURFACE: Record<DisplayStatus, string> = {
  tp: "border-feedback-success-primary bg-feedback-success-primary/10",
  sim: "border-feedback-success-primary border-dashed bg-feedback-success-primary/10",
  fixed: "border-feedback-success-primary bg-feedback-success-primary/10",
  fn: "border-feedback-warning-primary border-dashed bg-feedback-warning-primary/10",
  still: "border-feedback-warning-primary bg-feedback-warning-primary/10",
  fp: "border-feedback-error-primary bg-feedback-error-primary/10",
  regressed: "border-feedback-error-primary bg-feedback-error-primary/10",
  new: "border-feedback-error-primary bg-feedback-error-primary/10",
  near: "border-filigran-ia-primary bg-filigran-ia-primary/10",
  cascade: "border-filigran-ia-primary bg-filigran-ia-primary/10",
  type: "border-filigran-ia-primary bg-filigran-ia-primary/10",
  same: "border-elevation-subtle bg-elevation-hover",
};

/** The colour dot of each status. */
export const STATUS_DOT: Record<DisplayStatus, string> = {
  tp: "bg-feedback-success-primary",
  sim: "bg-feedback-success-primary",
  fixed: "bg-feedback-success-primary",
  fn: "bg-feedback-warning-primary",
  still: "bg-feedback-warning-primary",
  fp: "bg-feedback-error-primary",
  regressed: "bg-feedback-error-primary",
  new: "bg-feedback-error-primary",
  near: "bg-filigran-ia-primary",
  cascade: "bg-filigran-ia-primary",
  type: "bg-filigran-ia-primary",
  same: "bg-feedback-neutral-primary",
};

/** The OpenCTI entity colour of each STIX type (knowledge graph). */
export function typeColor(type: string): string {
  if (["threat-actor", "intrusion-set", "campaign"].includes(type)) return "var(--color-entities-all-threats)";
  if (["malware", "tool", "software", "vulnerability"].includes(type)) return "var(--color-entities-arsenal)";
  if (type === "attack-pattern") return "var(--color-entities-techniques)";
  if (type === "identity") return "var(--color-entities-victimology)";
  if (type === "location") return "var(--color-entities-location)";
  if (type === "report") return "var(--color-entities-analyses)";
  return "var(--color-entities-observations)";
}
