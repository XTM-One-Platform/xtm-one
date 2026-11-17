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
 * The fleet About view's pure parts (`FleetAboutView`, #9174 slice 6b): the
 * title of each documentation section, the sections a reader sees, and the
 * sentence a refused regenerate reads. Kept out of the component so they are
 * tested without rendering. The documentation is AI generated only (#9768 S2):
 * there is no editor, so no draft or save helper.
 *
 * The section keys come from the API (`section_keys`, in reading order,
 * `app.services.fleet_docs.DOC_SECTIONS`); a key this table does not know
 * still shows, under its key, so a section added on the server is never lost.
 */
import type { FleetDismissedFinding, FleetDocFinding, FleetDocNote, FleetDocumentation, FleetDocumentationRestoredFrom } from "@/hooks/api/useFleetApi";
import { platformEventLabel } from "@/lib/platform-events";

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The English title of each documentation section, read by `t_i18n(SECTION_TITLES[key])`. @i18n-keys */
const SECTION_TITLES = {
  purpose: "Purpose",
  use_case: "Use case",
  triggers: "What starts it",
  flow: "How it works",
  members: "Members and roles",
  approvals: "Approval gates",
  outcomes: "Outcomes",
  roi_units: "Unit of work",
  quota_estimate: "Quota estimate",
  objects_created: "Objects it creates",
  prerequisites: "Prerequisites",
  switch_off: "How to switch it off",
} as const;

const TITLE_OF: Readonly<Record<string, string>> = SECTION_TITLES;

/** A section's title in the reader's language; an unknown key reads as itself. */
export function sectionTitle(key: string, t: Translate): string {
  const title = TITLE_OF[key];
  return title ? t(title) : key;
}

/** The written sections, in the API's reading order. */
export function writtenSections(doc: Pick<FleetDocumentation, "section_keys" | "sections">): { key: string; text: string }[] {
  return doc.section_keys
    .map((key) => ({ key, text: (doc.sections[key] ?? "").trim() }))
    .filter((s) => s.text.length > 0);
}

/** The status of a refused call, read off the API error (`{status}`), if any. */
function statusOf(error: unknown): number | undefined {
  return (error as { status?: number } | null)?.status;
}

/** The catalog sentence of a refused regenerate. @i18n-keys */
const REGENERATE_REFUSALS = {
  stale: "The documentation changed since this page opened. Reload it, then regenerate again.",
  unusable: "The model could not generate this. Try again in a moment.",
  failed: "The documentation could not be generated. Try again in a moment.",
} as const;

/** The catalog sentence of a failed datasheet export: the reason and what to do. @i18n-keys */
const EXPORT_REFUSALS = {
  busy: "Too many exports in a short time. Wait a minute, then export the PDF again.",
  undocumented: "This fleet has no documentation yet. Regenerate it, then export the PDF again.",
  failed: "The PDF could not be rendered. Try the export again in a moment.",
} as const;

/**
 * The sentence a failed datasheet export reads (UX round 1 B1 on #9863): 429
 * is the export rate limit, 404 a fleet without documentation, anything else
 * (503 render failure, a network error) a plain retry.
 */
export function exportRefusal(error: unknown, t: Translate): string {
  const status = statusOf(error);
  if (status === 429) return t(EXPORT_REFUSALS.busy);
  if (status === 404) return t(EXPORT_REFUSALS.undocumented);
  return t(EXPORT_REFUSALS.failed);
}

/**
 * The sentence a refused regenerate reads, in the reader's language: 409 is a
 * newer version saved meanwhile, 422 an answer the server could not use (cut,
 * empty, unreadable); anything else (no model configured, a network error) a
 * plain retry.
 */
export function regenerateRefusal(error: unknown, t: Translate): string {
  const status = statusOf(error);
  if (status === 409) return t(REGENERATE_REFUSALS.stale);
  if (status === 422) return t(REGENERATE_REFUSALS.unusable);
  return t(REGENERATE_REFUSALS.failed);
}

/** The visual blocks a version note names, by structured field (`fleet_docs.BLOCK_NAMES`). @i18n-keys */
const NOTE_BLOCKS = {
  purpose_line: "The purpose line",
  outcome_line: "The outcome line",
  triggers: "Trigger cards",
  steps: "The numbered steps",
  gates: "Approval gates",
  objects: "Objects it creates",
  prerequisites: "Prerequisites",
  quota: "Quota lines",
  automations: "The automations table",
  integrations: "Integrations and tools",
} as const;

/**
 * Each version note code (`fleet_docs.NOTE_TEXTS`) in plain words (UX r1 B6 on #9988): never the
 * server's English or its internal keys. `{block}` is a `NOTE_BLOCKS` label. @i18n-keys
 */
const NOTE_TEXTS = {
  section_left_out: "A section the AI was not asked for was left out",
  section_cut: "A section was cut to {max} characters",
  sections_not_drafted: "{count, plural, one {# section was} other {# sections were}} not written by the AI",
  part_not_model: "The AI wrote a block it does not write: it was left out",
  part_cut: "{block}: {kept} of {total} kept, the most the page shows",
  part_item_left_out: "{block}: an entry the AI wrote was not valid and was left out",
  part_left_out: "{block}: not valid, left out",
  part_derived: "{block} come from the fleet's configuration: the AI wrote none",
  part_kept: "{block} kept from the previous version: neither the AI nor the configuration gave them",
  block_lost: "{block}: missing from this version, the previous version had them",
  quota_grouped: "Quota estimate: {grouped} triggers grouped in one line past {shown} lines, the total is unchanged",
  prerequisites_grouped: "Prerequisites: {grouped} integrations grouped in one line past {shown} lines, each one named",
  second_call_asked: "The AI answered without the visual blocks: they were asked for again",
  structured_cut: "The AI's answer for the visual blocks was cut: they come from the configuration",
  second_call_empty: "The second answer had no visual blocks: they come from the configuration",
} as const;

/** A version note in the reader's language; an older note stored as a sentence reads as written. */
export function versionNoteText(note: FleetDocNote, t: Translate): string {
  const params = note.params ?? {};
  if (note.code === "text") return String(params.text ?? "");
  const template = NOTE_TEXTS[note.code as keyof typeof NOTE_TEXTS];
  if (!template) return "";
  const blockKey = String(params.block ?? "");
  const block = blockKey in NOTE_BLOCKS ? t(NOTE_BLOCKS[blockKey as keyof typeof NOTE_BLOCKS]) : t("A block");
  return t(template, { ...params, block });
}

/** The label of each configuration finding kind (`fleet_doc_findings.FINDING_KINDS`, #10246). @i18n-keys */
const FINDING_KINDS = {
  description_mismatch: "Description does not match",
  missing_automation: "Automation missing",
  no_gate_on_sensitive_action: "Sensitive action without approval",
  missing_integration: "Integration missing",
  member_without_role: "Member without a role",
  member_cannot_consult_orchestrator: "Member cannot consult the orchestrator",
  member_chat_open: "Member open to chat",
  owner_without_inbox: "Work items without an inbox",
  orchestrator_without_listeners: "Orchestrator without listeners",
  fleet_without_working_knowledge: "No working knowledge base",
  orchestrator_cannot_write_knowledge: "Orchestrator cannot write the knowledge base",
  member_missing_working_knowledge: "Member without the working knowledge base",
  several_writers_on_working_knowledge: "Knowledge base with several writers",
  other: "Configuration",
} as const satisfies Record<FleetDocFinding["kind"], string>;

/**
 * The words of a good-practice suggestion (`fleet_practice_suggestions`, #11948) in the reader's language,
 * by kind; `{member}` is the member, `{target}` the orchestrator, or the knowledge base for the knowledge
 * practices (#11972). @i18n-keys
 */
const PRACTICE_TEXTS = {
  member_cannot_consult_orchestrator: {
    finding: "{member} cannot consult {target} for a route, scope or context question.",
    fix: "Add a Consult handover from {member} to {target}.",
  },
  member_chat_open: {
    finding: "{member} takes chats. People usually reach a fleet through its orchestrator.",
    fix: "Turn chat off on {member} unless people talk to it directly.",
  },
  owner_without_inbox: {
    finding: "{member} owns open work items and no automation triggered by its work items takes them.",
    fix: "Give {member} an automation triggered by its work items.",
  },
  orchestrator_without_listeners: {
    finding: "{member} has no automation started by {first}, {second} or {third}.",
    fix: "Add the listeners the skill Fleet design and operations describes to {member}.",
  },
  fleet_without_working_knowledge: {
    finding: "The fleet has no knowledge base that {member} writes and every member reads.",
    fix: "Create one working knowledge base for the fleet, written by {member} and read by every member.",
  },
  orchestrator_cannot_write_knowledge: {
    finding: "The whole fleet reads {target}, and {member} cannot write it.",
    fix: "Give {member} write access to {target}.",
  },
  member_missing_working_knowledge: {
    finding: "{member} does not read {target}, which the orchestrator writes for the fleet.",
    fix: "Bind {target} to {member}, read-only.",
  },
  several_writers_on_working_knowledge: {
    finding: "{target} has more than one writer: {member} and at least one member.",
    fix: "Keep {member} as the only writer of {target}. Members propose updates in their results.",
  },
} as const;

/** What a one-click Apply changes, shown before the person confirms it (#11948, #11972). @i18n-keys */
const APPLY_TEXTS = {
  member_cannot_consult_orchestrator: "{member} gets a Consult handover to {target}. Its other handovers keep their type.",
  orchestrator_cannot_write_knowledge: "{member} gets write access to {target}. Its other knowledge bases keep their access.",
  member_missing_working_knowledge: "{target} is bound to {member}, read-only. Its other knowledge bases keep their access.",
} as const;

/** The confirmation line of a one-click Apply in the reader's language; empty for a kind that does not apply. */
export function applyText(finding: FleetDocFinding, t: Translate): string {
  const template = APPLY_TEXTS[finding.kind as keyof typeof APPLY_TEXTS];
  return template ? t(template, { member: finding.member, target: finding.subject ?? "" }) : "";
}

/**
 * The events an orchestrator listens on (`fleet_practice_suggestions.LISTENER_EVENTS`), shown by their
 * platform event labels, never their keys (UX r1 B2 on #11961).
 */
const LISTENER_EVENTS = ["work_item.completed", "run.orphaned", "run.failed"] as const;

/** A finding's line and fix as the page shows them: a practice suggestion in the reader's language, a generated one as written. */
export function findingTexts(finding: FleetDocFinding, t: Translate): { finding: string; fix: string } {
  const texts = finding.source === "practice" ? PRACTICE_TEXTS[finding.kind as keyof typeof PRACTICE_TEXTS] : undefined;
  if (!texts) return { finding: finding.finding, fix: finding.suggested_fix };
  const [first, second, third] = LISTENER_EVENTS.map((event) => t(platformEventLabel(event) ?? event));
  const values = { member: finding.member, target: finding.subject ?? "", first, second, third };
  return { finding: t(texts.finding, values), fix: t(texts.fix, values) };
}

/** A dismissed row's line: a practice suggestion in the reader's language like the open row (#11961 UX r2), else as written. */
export function dismissedText(dismissed: FleetDismissedFinding, t: Translate): string {
  if (dismissed.source !== "practice") return dismissed.finding || dismissed.subject;
  return findingTexts({ ...dismissed, suggested_fix: "", source: "practice" }, t).finding;
}

/** The catalog label of a finding kind; an unknown kind reads as Configuration. */
export function findingKindLabel(kind: string): string {
  return FINDING_KINDS[kind as keyof typeof FINDING_KINDS] ?? FINDING_KINDS.other;
}

/** The request Fix with AI puts in a new chat's composer: the fleet, the finding and its suggested fix. */
export function findingRequest(fleetId: string, finding: FleetDocFinding, t: Translate): string {
  const lines = [
    t("Help me fix this in the configuration of fleet {fleetId}: read the fleet, make the change your tools allow, tell me what only I can do, then check again.", { fleetId }),
    "",
    finding.member ? `${finding.member}: ${finding.finding}` : finding.finding,
  ];
  if (finding.suggested_fix) lines.push(t("Suggested fix: {fix}", { fix: finding.suggested_fix }));
  return lines.join("\n");
}

/**
 * The request Fix all with AI puts in a new chat's composer (#10536): every visible
 * finding numbered, fixed one by one or dismissed with its reason when it is a
 * standing decision or a false positive, with one line of report per finding.
 */
export function findingsRequest(fleetId: string, findings: readonly FleetDocFinding[], t: Translate): string {
  const lines = [
    t(
      "Help me with {count, plural, one {this # suggestion} other {these # suggestions}} on the configuration of fleet {fleetId}. For each one, make the change your tools allow, or dismiss it with its reason through list_fleet_doc_findings when it is a standing decision or a false positive. Then report one line per suggestion.",
      { count: findings.length, fleetId },
    ),
    "",
  ];
  findings.forEach((f, i) => {
    lines.push(`${i + 1}. [${f.id}] ${f.member ? `${f.member}: ${f.finding}` : f.finding}`);
    if (f.suggested_fix) lines.push(`   ${t("Suggested fix: {fix}", { fix: f.suggested_fix })}`);
  });
  return lines.join("\n");
}

/**
 * The reasons a dismissal offers (#10536), keyed by option. The English label is
 * the reason stored and fed back to the generator as a decided point. @i18n-keys
 */
export const DISMISS_REASONS = {
  standing: "Standing decision",
  false_positive: "False positive",
} as const;

/** The label of a stored reason: a known one in the reader's language, free text as written. */
export function dismissReasonLabel(reason: string, t: Translate): string {
  const known = Object.values(DISMISS_REASONS).find((label) => label === reason);
  return known ? t(known) : reason;
}

/** Who wrote a version, as the version history reads it. @i18n-keys */
const VERSION_AUTHORS = {
  seed: "Written by Filigran",
  fork: "Copied from the original fleet",
  person: "Generated by AI",
} as const;

/** The catalog label of a version's author (`authorKind`); an unknown author reads as AI generated. */
export function versionAuthorLabel(kind: "seed" | "fork" | "person" | null): string {
  return VERSION_AUTHORS[kind ?? "person"];
}

/** What a restored version reads: the version it brought back and who wrote that text. @i18n-keys */
const RESTORED_LABELS = {
  shipped: "Restored from the shipped version, written by Filigran",
  seed: "Restored from version {source}, written by Filigran",
  fork: "Restored from version {source}, copied from the original fleet",
  ai: "Restored from version {source}, generated by AI",
} as const;

/**
 * The label of a restored version (UX round 2 B1): a restore is never read as
 * "Generated by AI"; it names the version brought back and its text's author.
 */
export function restoredLabel(restored: FleetDocumentationRestoredFrom, t: Translate): string {
  if (restored.version == null) return t(RESTORED_LABELS.shipped);
  return t(RESTORED_LABELS[restored.author], { source: restored.version });
}

/** Who last wrote the documentation, as a short phrase: the platform, a fork, or a person. */
export function authorKind(updatedBy: string | null): "seed" | "fork" | "person" | null {
  if (!updatedBy) return null;
  if (updatedBy === "seed") return "seed";
  if (updatedBy === "fork") return "fork";
  return "person";
}
