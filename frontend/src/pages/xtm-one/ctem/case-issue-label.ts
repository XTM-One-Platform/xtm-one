// SPDX-License-Identifier: Apache-2.0

/**
 * What a case member is called on the case page (#1936, Sam 24 Nov 2026: "we don't know
 * which signal, we don't know what is it"): no cell, caption, tooltip or CSV value ever
 * shows a raw id as a label. A signal member reads its subject (the API's `name`), else
 * "Signal: <trigger path label>"; a product issue reads its CVE or name.
 */
import type { CtemCaseEvent, CtemCaseVisibleIssue } from "@/hooks/api/useCtemCasesApi";
import { modulePivotUrl, pivotModuleOf, type ModuleBaseUrls, type PivotModule } from "@/lib/module-pivot";
import { safeHttpUrl } from "@/lib/safe-url";

/** A UUID anywhere in a text: the shape no label of the case page may carry. */
export const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** The CTEM signal lens in words (same wording as the "Opened by" card). @i18n-keys */
const SIGNAL_LENS_LABEL = { inside: "Inside", outside: "Outside", world: "World" } as const;

/** The lens of a signal in words, untranslated (an unknown lens reads as written). */
export function signalLensLabel(lens: string): string {
  return (SIGNAL_LENS_LABEL as Record<string, string>)[lens] ?? lens;
}

type Translate = (key: string, values?: Record<string, string | number>) => string;

export function isSignalIssue(issue: CtemCaseVisibleIssue): boolean {
  return issue.source === "signal";
}

/** The path label of a signal member, translated, or null when the API sent none. */
export function signalPathLabel(issue: CtemCaseVisibleIssue, t: Translate): string | null {
  const label = issue.trigger_path_label || null;
  return label ? t(label) : null;
}

/**
 * The primary text of an Issue cell. A signal: its name, else "Signal: <path label>", else
 * "Signal". A product issue: its CVE, else its name, else nothing (never the id).
 */
export function issueLabel(issue: CtemCaseVisibleIssue, t: Translate): string {
  if (isSignalIssue(issue)) {
    if (issue.name && !UUID_PATTERN.test(issue.name)) return issue.name;
    const path = signalPathLabel(issue, t);
    return path ? t("Signal: {path}", { path }) : t("Signal");
  }
  const text = issue.cve ?? issue.name ?? "";
  return UUID_PATTERN.test(text) ? "" : text;
}

/** The caption of a signal's Issue cell: its path label when the name stands above it. */
export function signalCaption(issue: CtemCaseVisibleIssue, t: Translate): string | null {
  if (!isSignalIssue(issue) || !issue.name || UUID_PATTERN.test(issue.name)) return null;
  return signalPathLabel(issue, t);
}

/**
 * Where the "Open in <Module>" icon of a signal row goes (#1357): a signal from OpenCTI, OpenAEV
 * or OpenCRQ opens its object in that module, the link its source delivered first, else the
 * module page of its id. Null for a non-module source, a module not connected, or no target:
 * never a dead link.
 */
export function signalPivot(issue: CtemCaseVisibleIssue, urls: ModuleBaseUrls): { module: PivotModule; href: string } | null {
  if (!isSignalIssue(issue)) return null;
  const module = pivotModuleOf(issue.source_provider);
  if (!module || !urls[module]) return null;
  const link = safeHttpUrl(issue.source_link);
  if (link) return { module, href: link };
  const href = modulePivotUrl(module, urls[module], issue.source_external_id, module === "openaev" ? "finding" : "entity");
  return href ? { module, href } : null;
}

/** The Assets cell of a member: a signal counts its assets only when it names one. */
export function memberAssetCount(issue: CtemCaseVisibleIssue): number | null {
  const count = issue.assets?.length ?? 0;
  return isSignalIssue(issue) && count === 0 ? null : count;
}

/**
 * The KEV cell of a member. A signal carries the KEV of the CVE it names once OpenCTI was read for
 * it (the fact the case's KEV card counts); with no CVE or an unread fact it reads nothing, never "No".
 */
export function memberKev(issue: CtemCaseVisibleIssue): boolean | null {
  if (isSignalIssue(issue)) return typeof issue.kev === "boolean" ? issue.kev : null;
  return !!issue.kev;
}

/**
 * The member a history event is about, in words: the API's `issue_label` (the signal's name or
 * the issue's CVE), else "Signal: <path label>" for a signal member, the same fallback as its
 * Issues row (UX round 1 B2 on #1945); null when nothing nameable remains, so the id is never
 * drawn and the caller drops the suffix. "Restricted" stays the caller's own concern.
 */
export function eventIssueLabel(event: Pick<CtemCaseEvent, "issue" | "issue_label" | "issue_path_label">, t: Translate): string | null {
  const issue = event.issue;
  if (!issue || issue === "Restricted") return null;
  if (event.issue_label && !UUID_PATTERN.test(event.issue_label)) return event.issue_label;
  if (issue.startsWith("signal:")) return event.issue_path_label ? t("Signal: {path}", { path: t(event.issue_path_label) }) : null;
  const id = issue.includes(":") ? issue.slice(issue.indexOf(":") + 1) : issue;
  return UUID_PATTERN.test(id) ? null : id;
}
