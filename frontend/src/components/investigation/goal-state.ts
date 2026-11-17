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
 * The world state of a Deep Investigation's goal plan, read from its steps -
 * `yarn test:unit` (`goal-state.test.ts`).
 *
 * Pack-agnostic on purpose: nothing here names an action or a predicate. The
 * plan comes from the server (a pack's declared plan, or the default one read
 * from the kinds of source the run planned), the steps say which action they
 * serve, and an action's state is derived from those steps alone. So the same
 * rendering serves every pack, and a new pack experiments with it by declaring
 * a plan, without a line of UI code.
 *
 * The rule the block lives by holds here too: a predicate holds only once a
 * step of the action that sets it FOUND something. An action whose sources all
 * answered "nothing" is `empty`, and its effects stay false - the state never
 * runs ahead of what the sources answered.
 */
import type {
  Investigation,
  InvestigationGoalAction,
  InvestigationGoalPlan,
  InvestigationStep,
} from "../../types/index.ts";

export type GoalActionStatus =
  | "pending"
  | "active"
  | "completed"
  | "partial"
  | "empty"
  | "failed"
  | "not_reached";

export interface GoalActionState {
  action: InvestigationGoalAction;
  status: GoalActionStatus;
  /** The steps doing this action's work, in plan order. */
  steps: InvestigationStep[];
  findings: number;
}

interface GoalTransition {
  predicate: string;
  action: string;
}

export interface GoalState {
  actions: GoalActionState[];
  /** Predicate slug -> whether it holds now. */
  holds: Record<string, boolean>;
  /** Predicates the subject already satisfied before the run: true, and
   *  earned by nobody, so they are labelled as given and never counted. */
  given: Set<string>;
  /** Goal predicates the RUN made hold, out of `goalTotal`, the goal
   *  predicates it had to achieve. */
  goalMet: number;
  goalTotal: number;
  /** Predicates that turned true, in the order the plan made them. */
  transitions: GoalTransition[];
  /** Actions still ahead: neither done nor given up on. */
  remaining: InvestigationGoalAction[];
  counts: { completed: number; active: number; pending: number; unmet: number };
  /** Steps that serve no action of the plan (a second pack's sources). */
  unplanned: InvestigationStep[];
}

/** What the report action is doing, which no step records. */
export type ReportPhase = "waiting" | "writing" | "written" | "missing";

const DONE: ReadonlySet<GoalActionStatus> = new Set(["completed", "partial"]);

function statusOfSteps(steps: InvestigationStep[], live: boolean): GoalActionStatus {
  if (steps.length === 0) return live ? "pending" : "not_reached";
  const by = (status: InvestigationStep["step_status"]) =>
    steps.filter((s) => s.step_status === status).length;
  const pending = by("pending");
  if (by("active") > 0) return "active";
  if (live && pending > 0) {
    // Some of its work answered and the rest is queued behind it: the action
    // is under way, not waiting to start.
    return pending < steps.length ? "active" : "pending";
  }
  const completed = by("completed");
  // Anything short of an answer with findings keeps the action from reading
  // complete: a failure, a partial read, a source the budget never reached,
  // one still pending when the run ended.
  const troubled = by("error") + by("degraded") + by("skipped") + (live ? 0 : pending);
  if (completed > 0) return troubled > 0 ? "partial" : "completed";
  // A partial read counts only when it found something: a predicate never
  // holds on a read that came back empty.
  if (steps.some((s) => s.step_status === "degraded" && (s.findings_count || 0) > 0)) return "partial";
  if (by("empty") > 0) return "empty";
  if (by("error") > 0) return "failed";
  return "not_reached";
}

function statusOfReport(phase: ReportPhase): GoalActionStatus {
  switch (phase) {
    case "written":
      return "completed";
    case "writing":
      return "active";
    case "waiting":
      return "pending";
    default:
      return "failed";
  }
}

/** How long after a goal run ends the block waits for its report, when the
 *  server cannot say (`report_status` "unknown", or a server without it).
 *
 *  The report is written after the run, by a model, and lands on the record
 *  without a revision bump. The writer says it is writing (`report_status`
 *  "writing") and the block reads that; this guess is only the fallback. */
export const REPORT_WAIT_MS = 3 * 60 * 1000;

/** A goal run that finished and whose report is still on its way: on the
 *  writer's word, and on a time guess only when that word cannot be read. */
export function awaitingReport(
  run: Pick<Investigation, "goal_plan" | "status" | "report" | "report_status" | "completed_at">,
  now = Date.now(),
): boolean {
  if (!run.goal_plan || run.status === "planning" || run.status === "running" || run.report) return false;
  if (run.report_status === "writing") return true;
  if (run.report_status === "none" || run.report_status === "written") return false;
  if (run.status !== "completed") return false;
  const ended = run.completed_at ? Date.parse(run.completed_at) : NaN;
  return Number.isFinite(ended) && now - ended < REPORT_WAIT_MS;
}

/** Where the report stands for this run. */
export function reportPhase(
  run: Pick<Investigation, "status" | "report">,
  { live, awaiting }: { live: boolean; awaiting: boolean },
): ReportPhase {
  if (run.report) return "written";
  if (live) return "waiting";
  if (awaiting) return "writing";
  return "missing";
}

export function deriveGoalState(
  plan: InvestigationGoalPlan,
  steps: InvestigationStep[],
  { live, report }: { live: boolean; report: ReportPhase },
): GoalState {
  const planned = new Set(plan.actions.map((a) => a.slug));
  const byAction = new Map<string, InvestigationStep[]>();
  const unplanned: InvestigationStep[] = [];
  for (const step of [...steps].sort((a, b) => a.position - b.position)) {
    const slug = step.action ?? null;
    if (slug && planned.has(slug)) {
      byAction.set(slug, [...(byAction.get(slug) ?? []), step]);
    } else {
      unplanned.push(step);
    }
  }

  const holds: Record<string, boolean> = {};
  for (const p of plan.predicates) holds[p.slug] = plan.initial_state.includes(p.slug);
  const transitions: GoalTransition[] = [];

  const actions = plan.actions.map((action): GoalActionState => {
    const own = byAction.get(action.slug) ?? [];
    // An action no source of the run can serve is not waiting for anything.
    const status = action.produces_report
      ? statusOfReport(report)
      : action.servable === false && own.length === 0
        ? "not_reached"
        : statusOfSteps(own, live);
    if (DONE.has(status)) {
      for (const effect of action.effects) {
        if (!holds[effect]) {
          holds[effect] = true;
          transitions.push({ predicate: effect, action: action.slug });
        }
      }
    }
    return {
      action,
      status,
      steps: own,
      findings: own.reduce((sum, s) => sum + (s.findings_count || 0), 0),
    };
  });

  const given = new Set(plan.initial_state);
  const toEarn = plan.goal_state.filter((p) => !given.has(p));
  const goalMet = toEarn.filter((p) => holds[p]).length;
  const counts = { completed: 0, active: 0, pending: 0, unmet: 0 };
  for (const a of actions) {
    if (DONE.has(a.status)) counts.completed += 1;
    else if (a.status === "active") counts.active += 1;
    else if (a.status === "pending") counts.pending += 1;
    else counts.unmet += 1;
  }

  return {
    actions,
    holds,
    given,
    goalMet,
    goalTotal: toEarn.length,
    transitions,
    remaining: actions
      .filter((a) => a.status === "pending" || a.status === "active")
      .map((a) => a.action),
    counts,
    unplanned,
  };
}

/** The report's opening, for the executive summary: its first section (a
 *  heading becomes a bold line, the title is dropped), cut at a line boundary
 *  under `maxChars`. Markers are kept: they are its citations. */
export function reportLead(report: string, maxChars = 900): string {
  const lines = report.split("\n");
  let i = 0;
  while (i < lines.length && (!lines[i].trim() || /^#\s/.test(lines[i]))) i += 1;
  const out: string[] = [];
  for (; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^#{1,6}\s/.test(line)) {
      // The next heading once the section has content: it ends the lead.
      if (out.some((l) => l.trim() && !l.startsWith("**"))) break;
      out.push(`**${line.replace(/^#+\s*/, "").trim()}**`);
      continue;
    }
    out.push(line);
  }
  let text = "";
  for (const line of out) {
    const next = text ? `${text}\n${line}` : line;
    if (next.length > maxChars) return `${text.trimEnd()}\n\n…`;
    text = next;
  }
  return text.trim();
}

/** Spacing and the image escape `render_markdown` applies, set aside. */
function comparable(text: string): string {
  return text.replace(/!\[/g, "[").replace(/\s+/g, " ").trim();
}

/** Whether a message's text is the run's own report, as the composer path
 *  writes it (`render_markdown`: the report first, then derived sections).
 *  Such a message reads the same report the block draws as its last action,
 *  so the block alone is shown; an agent's own prose around a run never
 *  begins with the report verbatim, and is always shown. */
export function messageIsRunReport(text: string, report: string | null | undefined): boolean {
  if (!report) return false;
  const head = comparable(report).slice(0, 240);
  return head.length > 0 && comparable(text).startsWith(head);
}
