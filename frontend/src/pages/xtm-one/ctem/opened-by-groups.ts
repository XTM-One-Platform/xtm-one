// SPDX-License-Identifier: Apache-2.0

/**
 * The signals of a case's "Opened by" card, grouped (#1679 point 6, Sam 24 Nov
 * 2026: thirty rows that differ only by the CVE read as one compact line).
 * A group is the signals with the same trigger path, lens, source provider and
 * scope decision (`decision.reason_code` with its params): its line names them
 * once, counts the signals and lists their subjects as chips.
 */
import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";

/** A group up to this size keeps the per-signal detail; above it, the group starts collapsed. */
export const OPENED_BY_DETAIL_MAX = 3;
export const OPENED_BY_COLLAPSE_ABOVE = 5;
/** The expanded table shows a filter box above this many signals. */
export const OPENED_BY_FILTER_ABOVE = 10;

export interface OpenedByGroup {
  key: string;
  /** The newest signal first; it stands for the group (its decision sentence, its loop trace). */
  signals: CtemSignal[];
  firstSeen: string | null;
  lastSeen: string | null;
}

function decisionKey(signal: CtemSignal): string {
  const d = signal.decision;
  if (!d) return "pending";
  const params = Object.entries(d.reason_params ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${String(v)}`)
    .join("&");
  return `${d.reason_code ?? d.outcome ?? ""}?${params}`;
}

function seenAt(signal: CtemSignal): string | null {
  return signal.last_seen_at ?? signal.observed_at ?? signal.first_seen_at;
}

export function groupOpenedBySignals(signals: readonly CtemSignal[]): OpenedByGroup[] {
  const groups = new Map<string, OpenedByGroup>();
  for (const signal of signals) {
    const key = [signal.trigger_path, signal.lens, signal.source.provider, decisionKey(signal)].join("|");
    const group = groups.get(key) ?? { key, signals: [], firstSeen: null, lastSeen: null };
    group.signals.push(signal);
    const first = signal.first_seen_at ?? signal.observed_at;
    const last = seenAt(signal);
    if (first && (!group.firstSeen || first < group.firstSeen)) group.firstSeen = first;
    if (last && (!group.lastSeen || last > group.lastSeen)) group.lastSeen = last;
    groups.set(key, group);
  }
  const ordered = [...groups.values()];
  for (const group of ordered) {
    group.signals.sort((a, b) => (seenAt(b) ?? "").localeCompare(seenAt(a) ?? ""));
  }
  // The largest group first, then the most recent.
  ordered.sort((a, b) => b.signals.length - a.signals.length || (b.lastSeen ?? "").localeCompare(a.lastSeen ?? ""));
  return ordered;
}

/** The subject a signal is about, as drawn on a chip. */
export function signalSubject(signal: CtemSignal): string {
  return signal.subject.label || signal.subject.ref;
}
