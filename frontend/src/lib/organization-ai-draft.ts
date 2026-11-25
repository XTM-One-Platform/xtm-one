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
 * The review rules of "Draft with AI" (#9484, dev-docs/ctem-fleets.md section 6.1):
 * pure functions over the proposals `POST /organization-context/ai-draft` answers,
 * so the dialog and its tests read one definition of "safe to accept in bulk".
 */
import type { AiDraftProposal, OrganizationDraftResponse, OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { SETTINGS_PROFILE_TABS, tabOfField } from "@/lib/organization-profile-tabs";

/**
 * The scope guard of Settings > Organization's Draft (#1991): a field the two tabs (Organization, Risk and response)
 * draw. Inventory fields and the PIRs belong to CTEM > Inventory and the Prioritization policy card: never proposed here.
 */
export function isSettingsDraftField(path: string): boolean {
  const tab = tabOfField(path);
  return tab !== null && (SETTINGS_PROFILE_TABS as readonly string[]).includes(tab);
}

function valueAt(profile: OrganizationProfile, path: string): unknown {
  return path.split(".").reduce<unknown>((node, key) => (node && typeof node === "object" ? (node as Record<string, unknown>)[key] : undefined), profile);
}

/** The platforms pass as review rows: each field the platforms filled, read as a fact (high confidence, never a conflict). */
export function platformProposals(draft: OrganizationDraftResponse, current: OrganizationProfile): AiDraftProposal[] {
  return draft.filled
    .filter((row) => isSettingsDraftField(row.field))
    .map((row) => ({
      field: row.field,
      value: valueAt(draft.profile, row.field),
      source: row.source,
      confidence: "high" as const,
      basis: row.detail,
      check: false,
      current: valueAt(current, row.field),
      conflict: false,
    }));
}

/** One review from both passes: the platform values first, the AI completes the fields they left; out-of-scope rows dropped. */
export function mergeDraftProposals(platform: readonly AiDraftProposal[], ai: readonly AiDraftProposal[]): AiDraftProposal[] {
  const taken = new Set(platform.map((row) => row.field));
  return [...platform, ...ai.filter((row) => !taken.has(row.field))].filter((row) => isSettingsDraftField(row.field));
}

/** A proposal "Accept all" may take: not a conflict, not "Check this", not low confidence. */
export function isBulkAcceptable(proposal: AiDraftProposal): boolean {
  return !proposal.conflict && !proposal.check && proposal.confidence !== "low";
}

/** What "Accept all" accepts on top of the rows already accepted, and how many rows it left to review. */
export function acceptAll(proposals: readonly AiDraftProposal[], accepted: ReadonlySet<string>): { accepted: Set<string>; skipped: number } {
  const next = new Set(accepted);
  let skipped = 0;
  for (const proposal of proposals) {
    if (isBulkAcceptable(proposal)) next.add(proposal.field);
    else if (!next.has(proposal.field)) skipped += 1;
  }
  return { accepted: next, skipped };
}

function setPath<T>(profile: T, path: string, value: unknown): T {
  const [head, ...rest] = path.split(".");
  const node = (profile ?? {}) as Record<string, unknown>;
  return { ...node, [head]: rest.length ? setPath(node[head], rest.join("."), value) : value } as T;
}

/** Equal by value: a proposal equal to the current value is "Unchanged", never a row to accept. */
export function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * The profile with the accepted proposals written in, an edited value taking
 * the place of the proposed one. Rows not accepted leave the current value as
 * it is: nothing typed is replaced without an explicit accept.
 */
export function applyAccepted(
  profile: OrganizationProfile,
  proposals: readonly AiDraftProposal[],
  accepted: ReadonlySet<string>,
  edited: Readonly<Record<string, unknown>> = {},
): OrganizationProfile {
  return proposals.reduce(
    (next, proposal) => (accepted.has(proposal.field) ? setPath(next, proposal.field, proposal.field in edited ? edited[proposal.field] : proposal.value) : next),
    profile,
  );
}

/** The provenance the version keeps: field -> {source, basis}; an edited value says who edited it. */
export function acceptedSources(
  proposals: readonly AiDraftProposal[],
  accepted: ReadonlySet<string>,
  edited: Readonly<Record<string, unknown>>,
  editor: string,
): Record<string, { source: string; basis: string }> {
  const out: Record<string, { source: string; basis: string }> = {};
  for (const proposal of proposals) {
    if (!accepted.has(proposal.field)) continue;
    out[proposal.field] = { source: proposal.source, basis: proposal.field in edited ? `edited by ${editor}` : proposal.basis };
  }
  return out;
}

/** One line for a proposed or current value: a list joined, an entry by its name or product. */
export function formatProposalValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(formatProposalValue).filter(Boolean).join(", ");
  if (typeof value === "object") {
    const entry = value as Record<string, unknown>;
    const name = entry.name ?? entry.product ?? entry.integration;
    const extra = entry.vendor ?? entry.project ?? entry.criticality;
    if (typeof name === "string" && name) return typeof extra === "string" && extra ? `${name} (${extra})` : name;
    return Object.values(entry).map(formatProposalValue).filter(Boolean).join(", ");
  }
  return String(value);
}

/** The proposal fields typed as amounts: edited as a number with the currency, shown formatted. */
export const AMOUNT_PROPOSAL_FIELDS: ReadonlySet<string> = new Set([
  "risk_appetite.annual_loss_threshold",
  "risk_appetite.approval_threshold",
  "loaded_hourly_rate",
]);

/**
 * An amount typed in the review: empty is null, a non-negative number is that
 * number (spaces and thin spaces of a grouped figure ignored), anything else
 * ("2 M EUR") is undefined, which the dialog shows as invalid and never accepts.
 */
export function parseProposalAmount(text: string): number | null | undefined {
  const trimmed = text.replace(/[\s\u00a0\u202f]/g, "");
  if (!trimmed) return null;
  if (!/^\d+(\.\d+)?$/.test(trimmed)) return undefined;
  return Number(trimmed);
}

/** An amount for display: grouped in the locale, with the currency when one is set ("2,000,000 EUR" style, Intl's own form). */
export function formatProposalAmount(value: unknown, currency: string, locale?: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return formatProposalValue(value);
  if (currency) {
    try {
      return new Intl.NumberFormat(locale, { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
    } catch {
      // A code Intl does not know: the grouped number, then the code.
    }
  }
  return `${new Intl.NumberFormat(locale).format(value)} ${currency}`.trim();
}

/** A value the review can edit in one control: a string or a number, not a list or an entry. */
export function isScalarProposal(proposal: AiDraftProposal): boolean {
  return typeof proposal.value === "string" || typeof proposal.value === "number";
}
