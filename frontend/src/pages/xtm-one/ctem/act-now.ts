// SPDX-License-Identifier: Apache-2.0

// The Act now queue of the CTEM command center (C2 slice V2, refocused by
// #10721): what to do first, at most five rows on the page. It holds what a
// person owes (work items and tool approvals waiting for them), the module
// facts (one row per CTEM stage) and the setup rows. The briefing's next steps
// live in the Exposure briefing widget and the stage assessments on their
// stage pages, so the queue never repeats them, and the chat is the header's
// Start chatting, never a row. Pure: no model, no run, no endpoint.

import type { LucideIcon } from "lucide-react";
import { isOpenCaseState, type CtemCasePriority, type CtemCaseSeverity } from "@/hooks/api/useCtemCasesApi";
import { CTEM_STAGE_KEYS } from "@/lib/constants";

/** Where a row comes from: it ranks the rows, decides Ask AI and the generic
 *  notice. Not drawn on the row: "Module" on every rule row said nothing
 *  (#11213). */
export type ActNowSource = "briefing" | "stage" | "module" | "work_item" | "approval" | "setup" | "agent" | "case" | "fleet";

export interface ActNowRow {
  id: string;
  icon: LucideIcon;
  tone: string;
  title: string;
  reason: string;
  /** The label of the row's one action, an i18n key. */
  cta: string;
  onAction: () => void;
  /** Higher first. */
  priority: number;
  source: ActNowSource;
  /** The briefing band of a briefing step, for its badge. */
  priorityLabel?: string;
  /** The CTEM stage a module fact is about: the facts of one stage are one row. */
  stage?: string;
  /** Whether the row is filtered and ranked by the organization context. A
   *  module fact read from a module-wide figure is not, and the card says so
   *  (#11213). */
  tailored?: boolean;
  /** What the row is about inside its stage ("Detection"), and the figure it
   *  quotes as a key ("232"): the facts of one stage that quote the same figure
   *  merge into one phrase through `mergeTitle`, never "232 ... 232 ...". */
  subject?: string;
  figure?: string;
  mergeTitle?: (subjects: string[]) => string;
  /** A case row: the case's own urgency, drawn with the chip the case header
   *  and the board use, never a queue band (UX round 2 on #897, B3). */
  caseUrgency?: { severity: CtemCaseSeverity | null; priority: CtemCasePriority };
}

/** Rows shown before "Show all (N)" expands the queue in place: three by
 *  default, the source bar above them says what else waits (design note
 *  dev-docs/ctem-stage-viz.md section 4, row act_now). */
export const ACT_NOW_COLLAPSED = 3;

/** The source groups the Act now bar counts, in its order. */
export type ActNowGroup = "posture" | "blind_spots" | "setup" | "waiting" | "working";
const ACT_NOW_GROUPS: readonly ActNowGroup[] = ["posture", "blind_spots", "setup", "waiting", "working"];

/** The legend label of each group. @i18n-keys */
export const ACT_NOW_GROUP_LABELS = {
  posture: "Posture",
  blind_spots: "Blind spots",
  setup: "Setup",
  waiting: "Waiting for you",
  working: "Fleet working",
} as const satisfies Record<ActNowGroup, string>;

/** The fill of each group: the bar segment, its legend swatch and the dot of
 *  every row of the group are the same colour (R3-A4). */
export const ACT_NOW_GROUP_FILLS: Record<ActNowGroup, string> = {
  posture: "var(--color-feedback-error-primary)",
  blind_spots: "var(--color-feedback-alert-primary)",
  setup: "var(--color-accent)",
  waiting: "var(--color-feedback-warning-primary)",
  working: "var(--color-feedback-info-primary)",
};

/** The ids of the blind spot rows: `connect-<stage>`, one per stage a missing
 *  module leaves unread ("connect-stack" is a setup step, not a blind spot). */
const BLIND_SPOT_IDS: ReadonlySet<string> = new Set(CTEM_STAGE_KEYS.map((stage) => `connect-${stage}`));

/** The group of a row: what waits for the reader, a blind spot (a module a
 *  stage needs, the `connect-<stage>` rows), another setup step, or a posture
 *  fact read from a module. */
export function actNowGroup(row: Pick<ActNowRow, "id" | "source">): ActNowGroup {
  if (row.source === "work_item" || row.source === "approval") return "waiting";
  if (row.source === "fleet") return "working";
  if (row.source === "setup") return BLIND_SPOT_IDS.has(row.id) ? "blind_spots" : "setup";
  return "posture";
}

/** The rows of the queue per group, in the bar's order, empty groups left out. */
export function actNowGroupCounts(queue: readonly Pick<ActNowRow, "id" | "source">[]): { group: ActNowGroup; count: number }[] {
  const counts = new Map<ActNowGroup, number>();
  for (const row of queue) counts.set(actNowGroup(row), (counts.get(actNowGroup(row)) ?? 0) + 1);
  return ACT_NOW_GROUPS.filter((g) => (counts.get(g) ?? 0) > 0).map((group) => ({ group, count: counts.get(group) ?? 0 }));
}

/** The most rows the expanded queue holds (design round 2 A2). */
export const ACT_NOW_MAX = 20;

/** The id of the "Ask your CTEM agent" row: never in the queue (the header's
 *  Start chatting carries it). */
export const ASK_AGENT_ROW_ID = "ask-agent";

/** The sources the queue leaves to another part of the page: the briefing's
 *  next steps (the Exposure briefing widget), the stage assessments (the stage
 *  pages, reached from the lifecycle band) and the chat (the header). */
const ELSEWHERE: ReadonlySet<ActNowSource> = new Set(["briefing", "stage", "agent"]);

/** Rank of a source when two rows share a priority: what a person owes first. */
const SOURCE_ORDER: Record<ActNowSource, number> = {
  case: -1,
  work_item: 0,
  approval: 1,
  module: 2,
  briefing: 3,
  stage: 4,
  setup: 5,
  agent: 6,
  fleet: 7,
};

function titleKey(title: string): string {
  return title.trim().toLowerCase().replace(/\s+/g, " ").replace(/[.\s]+$/, "");
}

/** Whether a row shows a priority label: a fleet at work is not a task for
 *  the reader, so its row carries none (UX round 1 on #920, B2). */
export function actNowShowsPriority(row: ActNowRow): boolean {
  return row.source !== "fleet";
}

/** The priority band a row shows as its small label. */
export function actNowPriorityLabel(row: ActNowRow): "high" | "medium" | "low" {
  if (row.priorityLabel === "high" || row.priorityLabel === "medium" || row.priorityLabel === "low") return row.priorityLabel;
  return row.priority >= 90 ? "high" : row.priority >= 70 ? "medium" : "low";
}

/**
 * The queue, ranked by priority then by source, at most `max` rows:
 * - the briefing's next steps, the stage assessments and the chat row are left
 *   out, and so is any row that says what a briefing step already says
 *   (`briefingActions`);
 * - the module facts of one stage are one row: the highest keeps its action and
 *   carries the others' titles, so "232 failed expectations" never shows twice;
 * - two rows with the same title are kept once, the higher one.
 */
export function actNowQueue(
  rows: readonly ActNowRow[],
  max = ACT_NOW_MAX,
  briefingActions: readonly string[] = [],
): ActNowRow[] {
  const briefing = new Set(briefingActions.map(titleKey));
  const ids = new Set<string>();
  const kept: ActNowRow[] = [];
  for (const row of rows) {
    if (row.id === ASK_AGENT_ROW_ID || ELSEWHERE.has(row.source) || ids.has(row.id)) continue;
    if (briefing.has(titleKey(row.title))) continue;
    ids.add(row.id);
    kept.push(row);
  }

  // One row per stage for the module facts.
  const byStage = new Map<string, ActNowRow[]>();
  const loose: ActNowRow[] = [];
  for (const row of kept) {
    if (row.source === "module" && row.stage) {
      const group = byStage.get(row.stage) ?? [];
      group.push(row);
      byStage.set(row.stage, group);
    } else {
      loose.push(row);
    }
  }
  const merged = [...byStage.values()].map((group) => {
    if (group.length === 1) return group[0];
    const [top, ...rest] = [...group].sort((a, b) => b.priority - a.priority);
    const all = [top, ...rest];
    const sameFigure =
      top.mergeTitle && top.figure !== undefined && all.every((r) => r.subject && r.figure === top.figure);
    const title = sameFigure && top.mergeTitle
      ? top.mergeTitle(all.map((r) => r.subject as string))
      : all.map((r) => r.title).join(" · ");
    return { ...top, id: `${top.id}+${rest.length}`, title };
  });

  const byTitle = new Map<string, ActNowRow>();
  for (const row of [...loose, ...merged]) {
    const key = titleKey(row.title);
    const prev = byTitle.get(key);
    if (!prev || row.priority > prev.priority) byTitle.set(key, row);
  }
  return [...byTitle.values()]
    .sort((a, b) => b.priority - a.priority || SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source])
    .slice(0, max);
}

/** The sources whose rows are facts read from a module: the ones the
 *  organization context can tailor, and the ones the generic notice is about. */
const FACT_SOURCES: ReadonlySet<ActNowSource> = new Set(["module"]);

/** Whether the card shows module facts and none of them is tailored: the
 *  rows are then generic and the card says so (#11213). What waits for the
 *  reader and the setup rows are the reader's own, never "generic". */
export function actNowIsGeneric(queue: readonly ActNowRow[]): boolean {
  const facts = queue.filter((r) => FACT_SOURCES.has(r.source));
  return facts.length > 0 && facts.every((r) => !r.tailored);
}

/** The sources Ask AI is offered on: a fact or a setup step a guided path
 *  helps with. A work item or an approval is answered where it waits. */
export function actNowOffersAskAi(row: Pick<ActNowRow, "source">): boolean {
  return row.source === "module" || row.source === "setup";
}

type Translate = (text: string, values?: Record<string, string>) => string;

/** The message Ask AI puts in the composer for *row*: what the row says, why
 *  it is there, whether it is tailored, and a request for a guided fix path.
 *  The reader sends it: the title is built from module figures, never sent on
 *  its own. */
export function actNowAskAiPrompt(t_i18n: Translate, row: Pick<ActNowRow, "title" | "reason" | "tailored" | "stage">): string {
  const values = { title: row.title, reason: row.reason, stage: row.stage ?? "" };
  const intro = row.stage
    ? t_i18n("The CTEM command center asks me to act on: \"{title}\" (stage: {stage}). Why it is there: {reason}.", values)
    : t_i18n("The CTEM command center asks me to act on: \"{title}\". Why it is there: {reason}.", values);
  const scope = row.tailored
    ? t_i18n("This action is ranked by our organization context.")
    : t_i18n("This action is generic: it is not filtered by our organization context yet. Read our organization profile first and tell me which part of it applies to us.");
  const ask = t_i18n(
    "Look at the data behind it in the connected modules and guide me step by step: what to check first, what to fix, in which tool, and how to verify it is fixed.",
  );
  return [intro, scope, ask].join("\n\n");
}

/** Priorities of the queue's own sources, against the page rows (blind spot
 *  100, KEV 95, failed expectations 90, detection gap 80, stack 50). What
 *  waits on the reader comes first: a person blocks a run or a work item until
 *  they answer. */
export const ACT_NOW_PRIORITY = { case_p1: 99, work_item: 98, approval: 97, case_p2: 96, case_p3: 72, fleet: 60 } as const;

/** The fields of a fleet timeline entry a "fleet working" row reads
 *  (`TimelineEntry` of `lib/ctem-fleet-timeline.ts`). */
interface ActNowFleetRun {
  runId: string;
  fleetName: string;
  agentName: string | null;
  outcome: string | null;
  at: string;
  caseId: string | null;
}

/** One case a fleet is working on now: the newest in-flight run of that case
 *  (#915, CTEM-04-UI slice 3). */
interface ActNowFleetWork {
  caseId: string;
  caseTitle: string;
  fleetName: string;
  /** The member running the step in progress (`null` until the fleet health names it). */
  agentName: string | null;
  /** Waiting to start (`pending`) or running. */
  pending: boolean;
  runId: string;
  /** Other in-flight runs on the same case. */
  others: number;
}

/**
 * The cases a fleet works on now, from the fleet timeline: one entry per case,
 * its newest running or pending run, newest first. A run tied to no case, or
 * to a case the reader cannot see (`caseTitles` has no entry), is left out: the
 * row would link to a case the board does not show. Pure: the page reads the
 * timeline and the cases it already holds, never a request of its own.
 */
export function actNowFleetWork(
  timeline: readonly ActNowFleetRun[],
  caseTitles: Readonly<Record<string, string | undefined>>,
): ActNowFleetWork[] {
  const byCase = new Map<string, ActNowFleetWork>();
  const sorted = [...timeline].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  for (const run of sorted) {
    if (!run.caseId || (run.outcome !== "running" && run.outcome !== "pending")) continue;
    const title = caseTitles[run.caseId];
    if (title === undefined) continue;
    const prev = byCase.get(run.caseId);
    if (prev) {
      prev.others += 1;
      continue;
    }
    byCase.set(run.caseId, {
      caseId: run.caseId, caseTitle: title, fleetName: run.fleetName, agentName: run.agentName,
      pending: run.outcome === "pending", runId: run.runId, others: 0,
    });
  }
  return [...byCase.values()];
}

/** The open loop cases Act now lists (CTEM-03-UI #892, ranked by the loop since
 *  CTEM-30 #937): five at most, so the queue keeps room for the rest. */
const ACT_NOW_CASE_MAX = 5;

const SEVERITY_RANK: Record<string, number> = { P1: 0, P2: 1, P3: 2 };
const CRITICALITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/** What the scope gate read for a case, from its origin signals the reader may read. */
export interface CaseScope {
  /** The highest scope criticality among its signals, or null when none is readable. */
  criticality: string | null;
  /** The primary assets its signals were reconciled to. */
  crownJewels: string[];
}

/**
 * The scope of each case, from the reader's own signals (`case_id` and the
 * scope gate's decision record): a case whose signals the reader cannot read
 * has no entry, and its row shows severity and SLA only.
 */
export function caseScopes(
  signals: readonly { case_id: string | null; decision: { scope?: { criticality?: string | null } | null; reconcile?: { crown_jewels_named?: string[] } } | null }[],
): Map<string, CaseScope> {
  const out = new Map<string, CaseScope>();
  for (const s of signals) {
    if (!s.case_id) continue;
    const prev = out.get(s.case_id) ?? { criticality: null, crownJewels: [] };
    const crit = s.decision?.scope?.criticality ?? null;
    const better = crit && (prev.criticality === null || (CRITICALITY_RANK[crit] ?? 9) < (CRITICALITY_RANK[prev.criticality] ?? 9));
    const jewels = [...prev.crownJewels];
    for (const j of s.decision?.reconcile?.crown_jewels_named ?? []) if (!jewels.includes(j)) jewels.push(j);
    out.set(s.case_id, { criticality: better ? crit : prev.criticality, crownJewels: jewels });
  }
  return out;
}

/**
 * The open cases of the loop, ranked as the loop ranks them: severity (P1,
 * P2, P3), then scope criticality (critical first, unknown last), then the
 * nearest SLA. A case with no loop severity is left to the cases board.
 */
export function actNowCases<T extends { id: string; state: string; severity?: string | null; sla_due_at?: string | null }>(
  cases: readonly T[],
  scopes: ReadonlyMap<string, CaseScope> = new Map(),
): T[] {
  const crit = (c: T) => CRITICALITY_RANK[scopes.get(c.id)?.criticality ?? ""] ?? 9;
  return cases
    .filter((c) => c.severity && c.severity in SEVERITY_RANK && isOpenCaseState(c.state))
    .sort(
      (a, b) =>
        SEVERITY_RANK[a.severity as string] - SEVERITY_RANK[b.severity as string] ||
        crit(a) - crit(b) ||
        (a.sla_due_at ?? "\uffff").localeCompare(b.sla_due_at ?? "\uffff"),
    )
    .slice(0, ACT_NOW_CASE_MAX);
}

/** The queue priority of the case at *index* of `actNowCases`: its severity's band, kept in the loop's order inside it. */
export function actNowCasePriority(severity: string | null | undefined, index: number): number {
  const base = severity === "P1" ? ACT_NOW_PRIORITY.case_p1 : severity === "P2" ? ACT_NOW_PRIORITY.case_p2 : ACT_NOW_PRIORITY.case_p3;
  return base - index * 0.01;
}
