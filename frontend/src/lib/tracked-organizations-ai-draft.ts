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
 * Review rules of a tracked-organizations AI draft (#9484 S-c2, dev-docs/ctem-fleets.md
 * section 6.1): which rows can be accepted, what the decide call sends, and the
 * count lines shown before and after a run. Pure, so the page and its tests share them.
 */
import type { TrackedDraftRow } from "@/hooks/api/useOrganizationsApi";
import { TRACKED_DRAFT_BATCH, TRACKED_DRAFT_MAX_NAMES } from "@/hooks/api/useOrganizationsApi";

/** The review page of a stored draft. */
export function trackedDraftPath(proposalId: string): string {
  return `/settings/organization/tracked/ai-draft/${encodeURIComponent(proposalId)}`;
}

/** The distinct names typed, one per line, in order (the backend reads a file's first column itself). */
export function typedNames(text: string): string[] {
  const seen = new Map<string, string>();
  for (const line of text.split(/\r?\n/)) {
    const name = line.trim().replace(/\s+/g, " ");
    if (name && !seen.has(name.toLowerCase())) seen.set(name.toLowerCase(), name);
  }
  return [...seen.values()];
}

/** What a run of `count` names costs before it runs: names drafted and metered requests (an upper bound). */
export function runEstimate(count: number): { drafted: number; requests: number; overLimit: boolean } {
  const drafted = Math.min(count, TRACKED_DRAFT_MAX_NAMES);
  return { drafted, requests: Math.ceil(drafted / TRACKED_DRAFT_BATCH), overLimit: count > TRACKED_DRAFT_MAX_NAMES };
}

/** A row the administrator may accept: drafted, not already tracked, its batch not failed. */
export function isDecidable(row: TrackedDraftRow): boolean {
  return row.status === "pending";
}

/** The OpenCTI Organization a row links to: the one picked on the page, else the draft's own exact match. */
export function linkedOpenCti(row: TrackedDraftRow, picked: Record<string, string>): string {
  return picked[row.name] ?? row.opencti_organization_id ?? "";
}

/** The decide call's body for the accepted names; conflict and failed rows are never sent. */
export function decideBody(
  rows: readonly TrackedDraftRow[],
  accepted: ReadonlySet<string>,
  picked: Record<string, string>,
): { accept: string[]; opencti_ids: Record<string, string> } {
  const accept = rows.filter((row) => isDecidable(row) && accepted.has(row.name)).map((row) => row.name);
  const opencti_ids: Record<string, string> = {};
  for (const name of accept) {
    const row = rows.find((r) => r.name === name);
    const id = row ? linkedOpenCti(row, picked) : "";
    if (id) opencti_ids[name] = id;
  }
  return { accept, opencti_ids };
}

/** The batches that failed (one line each, other batches kept): rows in error grouped by message. */
export function failedBatches(rows: readonly TrackedDraftRow[]): { error: string; names: string[] }[] {
  const byError = new Map<string, string[]>();
  for (const row of rows) {
    if (row.status !== "error") continue;
    const key = row.error || "";
    byError.set(key, [...(byError.get(key) ?? []), row.name]);
  }
  return [...byError.entries()].map(([error, names]) => ({ error, names }));
}
