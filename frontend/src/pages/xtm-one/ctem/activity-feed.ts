// SPDX-License-Identifier: Apache-2.0

// D3-4 of the CTEM open-source program (#9567, `dev-docs/ctem-open-source.md`
// section 7): the deterministic activity feed of the Live activity card. When no
// agent run exists (Community Edition, AI off, no model, or before the first
// assignment fires), the card lists the events the page already holds instead
// of greyed demo rows. No model, no endpoint: the posture and the platform
// registrations loaded by the command center are the only sources.

import type { CtemPosture } from "@/types";
import type { CtemDecisionRecord, CtemSignal, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { listCapacity, type WidgetSize } from "@/components/dashboard-grid/widget-density";

/** Pixels of the Live activity card that are not rows: padding, header,
 *  the gaps and a two-line lock footer (measured 140 px at the default size,
 *  #9568 UX round 1 B1). */
const FEED_CHROME = 140;
/** One row of the card (`ActivityRow`): since #851 UX round 1 B2 it takes the
 *  anatomy of the compact run rows (12 px inset, 32 px icon tile), measured
 *  59 to 62 px in that round: the tallest is kept so no row is cut. */
const FEED_ROW_HEIGHT = 62;

/** Rows the Live activity card shows at its grid size, at most `max`: every
 *  row it draws is fully visible, none hides under an inner scroll (#9568 UX
 *  round 1 B1). `null` (stacked layout) draws them all, the card growing to
 *  its content. */
export function feedCapacity(size: WidgetSize | null, max: number): number {
  if (max <= 0) return 0;
  return listCapacity(size, { chrome: FEED_CHROME, rowHeight: FEED_ROW_HEIGHT, max });
}

/** The fields of a platform registration the feed reads. */
export interface FeedRegistration {
  id: string;
  platform_identifier: string;
  platform_title: string;
  registered_at: string;
  last_heartbeat_at: string;
  healthy: boolean;
}

/** One deterministic event, before the card gives it a sentence and an icon. */
export type ActivityEvent =
  /** The posture was rebuilt (scheduled sync or Force sync). */
  | { kind: "posture_synced"; id: string; at: string; covered: number; total: number }
  /** A connected product's data reached the posture. */
  | { kind: "product_synced"; id: string; at: string; product: string }
  /** A platform registered with XTM One. */
  | { kind: "platform_registered"; id: string; at: string; title: string }
  /** A registered platform stopped sending heartbeats. */
  | { kind: "platform_unhealthy"; id: string; at: string; title: string }
  /** OpenAEV validation results: expectations met of those with a result. */
  | { kind: "validation"; id: string; at: string; met: number; total: number }
  /** A CTEM signal entered the loop (#838): it opens its loop trace. */
  | {
      kind: "signal_received"; id: string; at: string; signalId: string; label: string;
      path: string; lens: string; state: CtemSignalState; decision: CtemDecisionRecord | null;
    };

/**
 * Status dot of a row. A validation with some expectations met is partial
 * (amber); red is kept for none met and for a platform that stopped
 * responding (#9594 A3).
 */
export function eventStatus(event: ActivityEvent): "running" | "completed" | "partial" | "failed" {
  if (event.kind === "platform_unhealthy") return "failed";
  // A signal still moving through the loop pulses; a stopped one is partial (amber).
  if (event.kind === "signal_received") {
    if (event.state === "new" || event.state === "confirmed") return "running";
    return event.state === "case_opened" ? "completed" : "partial";
  }
  if (event.kind === "validation" && event.met < event.total) return event.met > 0 ? "partial" : "failed";
  return "completed";
}

function validTime(at: string | null | undefined): at is string {
  return typeof at === "string" && !Number.isNaN(Date.parse(at));
}

/** The deterministic events of the command center, newest first, at most
 *  `limit`. A source without a time (never synced, no heartbeat) gives no row:
 *  the feed never invents a time. */
export function deterministicActivity(
  posture: Pick<CtemPosture, "stages" | "products" | "covered_stages" | "total_stages" | "generated_at"> | null | undefined,
  registrations: FeedRegistration[] | null | undefined,
  limit = 6,
): ActivityEvent[] {
  const events: ActivityEvent[] = [];

  if (posture) {
    if (validTime(posture.generated_at)) {
      events.push({
        kind: "posture_synced", id: "posture", at: posture.generated_at,
        covered: posture.covered_stages, total: posture.total_stages,
      });
    }
    for (const [product, p] of Object.entries(posture.products ?? {})) {
      if (!p?.present || p.coming_soon || !validTime(p.last_sync)) continue;
      events.push({ kind: "product_synced", id: `sync:${product}`, at: p.last_sync, product });
    }
    // Validation results are read only while OpenAEV is connected (the same rule
    // as the next actions): a stored snapshot never shows results of a product
    // that is gone.
    const openaev = posture.products?.openaev;
    if (openaev?.present) {
      const buckets = posture.stages.find((s) => s.key === "validation")?.viz.expectation_buckets ?? {};
      let met = 0;
      let total = 0;
      for (const b of Object.values(buckets)) {
        const success = b?.SUCCESS ?? 0;
        met += success;
        total += success + (b?.FAILED ?? 0) + (b?.PARTIAL ?? 0);
      }
      const at = validTime(openaev.last_sync) ? openaev.last_sync : posture.generated_at;
      if (total > 0 && validTime(at)) events.push({ kind: "validation", id: "validation", at, met, total });
    }
  }

  for (const r of registrations ?? []) {
    const title = r.platform_title || r.platform_identifier;
    if (validTime(r.registered_at)) {
      events.push({ kind: "platform_registered", id: `reg:${r.id}`, at: r.registered_at, title });
    }
    if (!r.healthy && validTime(r.last_heartbeat_at)) {
      events.push({ kind: "platform_unhealthy", id: `health:${r.id}`, at: r.last_heartbeat_at, title });
    }
  }

  return events
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.id.localeCompare(b.id))
    .slice(0, Math.max(0, limit));
}

/** The signals the caller reads, as feed events (newest sighting first), at most `limit`. */
export function signalActivity(signals: CtemSignal[] | null | undefined, limit = 6): ActivityEvent[] {
  const events: ActivityEvent[] = [];
  for (const s of signals ?? []) {
    const at = s.last_seen_at ?? s.first_seen_at;
    if (!validTime(at)) continue;
    events.push({
      kind: "signal_received", id: `signal:${s.id}`, at, signalId: s.id,
      label: s.subject.label || s.subject.ref, path: s.trigger_path_label || s.trigger_path,
      lens: s.lens, state: s.state, decision: s.decision,
    });
  }
  return events.slice(0, Math.max(0, limit));
}

/** Signal rows and the deterministic events in one feed, newest first. */
export function mergeActivity(a: ActivityEvent[], b: ActivityEvent[], limit = 6): ActivityEvent[] {
  return [...a, ...b]
    .sort((x, y) => Date.parse(y.at) - Date.parse(x.at) || x.id.localeCompare(y.id))
    .slice(0, Math.max(0, limit));
}
