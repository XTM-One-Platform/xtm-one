// SPDX-License-Identifier: Apache-2.0

/**
 * What an evidence health signal says (CTEM-28-UI): the blind spots and the
 * evidence decay the evidence health pass records on trigger paths 9 and 12
 * (backend services/ctem_evidence_health.py, dev-docs/ctem-evidence-health.md).
 *
 * A gap never reads as safe: the state is named in words, "No evidence since"
 * gives the date of the newest evidence or says it was never measured (never
 * an empty dash), and an emptied connector sync says its findings stay open.
 */

/** The evidence health states in words. @i18n-keys */
export const EVIDENCE_STATE_LABEL = {
  expired: "Evidence expired: older than the re-test delay",
  attested_only: "Attested by a person only, never measured by a connector",
  never_measured: "Reported by a connector, never measured",
  no_source: "Blind spot: no source reports this asset",
  version_unknown: "Blind spot: the version is unknown",
  source_emptied: "Blind spot: a connector sync emptied",
} as const;

type EvidenceState = keyof typeof EVIDENCE_STATE_LABEL;

/** The evidence keys the rows below say, left out of the raw evidence list. */
export const EVIDENCE_HEALTH_ROW_KEYS = [
  "event",
  "state",
  "last_evidence_at",
  "opencrq_asset_ids",
  "risk_scenario_id",
  "risk_scenario",
  "impact",
  "sources",
  "sync_kind",
  "last_run_at",
  "latest_total",
  "previous_total",
  // The backend reason sentence (English, unlocalized): the state row says it in words.
  "reason",
] as const;

const EVENTS = new Set(["evidence_expired", "coverage_gap"]);

/** One source that reported the asset, and when it last did. */
type EvidenceSource = { origin: string; lastSeen: string | null };

/** The named facts of an evidence health signal. */
export type EvidenceHealthFact = {
  state: EvidenceState;
  /** The newest evidence of the asset; null = never measured. */
  lastEvidence: string | null;
  scenario: string | null;
  sources: EvidenceSource[];
  /** An emptied connector sync: its kind and row counts. */
  sync: { kind: string; latest: number | null; previous: number | null } | null;
};

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The facts of a signal the evidence health pass recorded, read from the keys
 * `gaps_of` writes (`event`, `state`, `last_evidence_at`, `risk_scenario`,
 * `sources`; for an emptied sync `sync_kind`, `latest_total`,
 * `previous_total`). A signal from any other source, or a state the table does
 * not name, gives null and keeps its raw evidence rows.
 */
export function evidenceHealthFacts(evidence: Record<string, unknown> | null | undefined): EvidenceHealthFact | null {
  if (!evidence || typeof evidence.event !== "string" || !EVENTS.has(evidence.event)) return null;
  const state = str(evidence.state);
  if (!state || !(state in EVIDENCE_STATE_LABEL)) return null;
  const sources: EvidenceSource[] = (Array.isArray(evidence.sources) ? evidence.sources : [])
    .filter((s): s is Record<string, unknown> => Boolean(s) && typeof s === "object")
    .map((s) => ({ origin: str(s.origin) ?? "", lastSeen: str(s.last_seen_at) }))
    .filter((s) => s.origin);
  const kind = str(evidence.sync_kind);
  return {
    state: state as EvidenceState,
    lastEvidence: state === "source_emptied" ? str(evidence.last_run_at) : str(evidence.last_evidence_at),
    scenario: str(evidence.risk_scenario),
    sources,
    sync: state === "source_emptied" && kind
      ? { kind, latest: num(evidence.latest_total), previous: num(evidence.previous_total) }
      : null,
  };
}
