// SPDX-License-Identifier: Apache-2.0

// D3-1 of the CTEM open-source program (#9425, `dev-docs/ctem-open-source.md`
// section 7): when AI is locked (no license, AI off for the platform or the
// group, no model), the two agentic KPI slots of the strip ("Agents",
// "Agentic executions") show numbers no agent can move. They show instead two
// deterministic exposure metrics the posture collector already reads, with
// the same present / sample logic and deep link as the vertical KPIs. No
// model, no new endpoint. Each label is a `CTEM_METRIC_INFO` entry, so the
// info icon explains it.

import { Crosshair, KeyRound, Radio, ShieldAlert, ShieldCheck, Siren, type LucideIcon } from "lucide-react";

import type { CtemStageKey } from "@/lib/constants";
import type { CtemHeadline, CtemVertical } from "@/types";

/** The headline keys that hold a number (a KPI shows one figure). */
type CtemHeadlineNumberKey = Exclude<keyof CtemHeadline, "kev_tailored_products">;

/** One KPI of the strip, read from the posture headline. `product` is the
 *  source whose presence gates a real (vs greyed sample) value; `stage` picks
 *  the deep link to the resource page of that product. */
export type KpiDef = {
  label: string;
  headlineKey: CtemHeadlineNumberKey;
  unit?: string;
  sub: string;
  icon: LucideIcon;
  accent: string;
  product: "opencti" | "openaev";
  stage: CtemStageKey;
};

/** The two deterministic KPIs per vertical, in the order of the slots they
 *  take (`kpi_agents`, then `kpi_executions`). None repeats a metric the three
 *  vertical KPIs already show. */
export const LOCKED_KPIS: Record<CtemVertical, [KpiDef, KpiDef]> = {
  cyber: [
    { label: "Critical CVEs", headlineKey: "critical_count", sub: "CVSS 9 or higher", icon: ShieldAlert, accent: "text-feedback-error-primary/40", product: "opencti", stage: "prioritization" },
    { label: "ATT&CK exercised", headlineKey: "exercised_coverage_pct", unit: "%", sub: "techniques run in simulations", icon: Crosshair, accent: "text-filigran-ia-primary/40", product: "openaev", stage: "validation" },
  ],
  fimi: [
    { label: "Channels monitored", headlineKey: "channels_total", sub: "in the information space", icon: Radio, accent: "text-accent/40", product: "opencti", stage: "scoping" },
    { label: "Open incidents", headlineKey: "incidents", sub: "open response work", icon: Siren, accent: "text-feedback-error-primary/40", product: "opencti", stage: "remediation" },
  ],
  fraud: [
    { label: "Leaked credentials", headlineKey: "credentials_total", sub: "credentials in the wild", icon: KeyRound, accent: "text-feedback-error-primary/40", product: "opencti", stage: "discovery" },
    { label: "Containment", headlineKey: "containment_pct", unit: "%", sub: "fraud indicators neutralised", icon: ShieldCheck, accent: "text-accent/40", product: "opencti", stage: "prioritization" },
  ],
};

/** The KPI a slot of the agentic pair shows: the deterministic one when AI is
 *  locked, `null` (the agentic card) when agents can run. */
export function lockedKpiFor(
  vertical: CtemVertical,
  slot: "kpi_agents" | "kpi_executions",
  locked: boolean,
): KpiDef | null {
  if (!locked) return null;
  return LOCKED_KPIS[vertical][slot === "kpi_agents" ? 0 : 1];
}
