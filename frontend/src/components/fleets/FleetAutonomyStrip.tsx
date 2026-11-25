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
 * The fleet's human approval strip (#9769 S3; truthful control, #10025, slice B
 * of #10000, design round 3 APPROVE): the content of the fleet's Approval tab
 * (#10176); the page header carries nothing of it.
 *
 * - A fleet with no approval gate catalog shows NO selected level: the level
 *   would gate nothing there, so the strip says so, names where approvals still
 *   apply, and offers "Review actions" (the gates computed from the members'
 *   configuration, `GET /fleets/{id}/approval-gates`, no AI, no quota).
 * - With a catalog: the four steps, the summary at the current level, and the
 *   matrix of what waits at each level, always shown with no click (#1493, Sam
 *   23 Nov 2026: the table is the content, no grouped "Waits / Runs alone"
 *   lists above it); one line says what the level does not cover (condition C2).
 * - The "i" next to "Human approval" opens the explainer inline, under the row:
 *   four headed parts, a Close button and Esc; what the level does not cover is
 *   stated once, in the strip, and the explainer points to it (UX round 1 B4 on
 *   #10034, condition C2).
 * - One wording everywhere: "N waits for approval, N runs alone" (B3).
 * - Direction is named by its effect: reducing human approval is administrator
 *   only and lists the actions that will run without approval before it
 *   applies; adding approval back applies at once, its toast saying how many
 *   actions start waiting, with Undo for an administrator.
 * - A reader who may not manage the fleet sees the same lists, read only.
 */
import { Info, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import {
  useFleetApprovalGates,
  useFleetDocumentation,
  useSetFleetAutonomy,
  type FleetAutonomyLevel,
} from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import {
  AUTONOMY_LEVEL_HINTS,
  AUTONOMY_LEVEL_LABELS,
  AUTONOMY_LEVELS,
  autonomyCounts,
  becomesAutomatic,
  becomesGated,
  effectiveAutonomyLevel,
  gateNames,
  isAutonomyRaise,
  selectableLevels,
} from "./fleet-autonomy";
import { FleetInstructionsPositionControl } from "./FleetInstructionsPosition";
import { FleetAutonomyCatalog } from "./FleetAutonomyCatalog";
import { FleetGateReview } from "./FleetGateReview";
import { FleetPendingApprovals } from "./FleetPendingApprovals";
import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetGateList } from "./FleetGateList";
import { FleetGatesAtLevel } from "./FleetGatesAtLevel";

/** What the level never covers: written once, in the strip (UX round 3 C2, round 1 B4 on #10034). */
const NOT_COVERED_LINE =
  "Not covered by this level: chat, channel and consult runs, approvals written in agent instructions, and an agent's own approval settings, which keep working.";

/** Where approvals still apply on a fleet with no catalog (UX round 3 N5). */
const STILL_APPLY_LINE = "Approvals still apply where they are written today: agent instructions and agent approval settings.";

/**
 * The explainer's parts, a heading and one sentence each (UX round 1 B4). What
 * the level does not cover is NOT repeated here: the strip states it once and
 * the "Where it applies" part points to that line (C2).
 */
const EXPLAINER_PARTS: { heading: string; body: string }[] = [
  { heading: "What is gated", body: "The actions of this fleet's approval gate catalog, each with an impact. Reads are never gated." },
  {
    heading: "Where it applies",
    body: "Automation runs of the fleet's members: a gated tool call pauses until a person approves it, and the agent is told the level. What it does not cover is stated under the strip.",
  },
  { heading: "Who may change it", body: "Only an administrator can reduce human approval. Whoever manages the fleet can add approval back." },
  {
    heading: "What happens to a paused call",
    body: "The run waits and asks a person to approve the call, then resumes. Every action stays logged and reported in Operations.",
  },
];

/** The source of the catalog, one line under the matrix (UX round 3 C3); no PR number on the page. */
const SOURCE_STORED = "Source: this fleet's approval gate catalog, stored in its configuration. Regenerating the documentation never changes it.";
const SOURCE_DOCUMENTED = "Source: the approval gates declared in this fleet's documentation.";

/** The strip's element id: the fleet chat's raise refusal scrolls to it and focuses it. */
export const FLEET_AUTONOMY_STRIP_ID = "fleet-autonomy-strip";

export function FleetAutonomyStrip({
  fleetId,
  level: storedLevel,
  canManage,
  isAdmin,
  compact = false,
}: {
  fleetId: string;
  /**
   * The level choice alone (UX r1 B3 on #1092, the CTEM loop setup): the levels and the counts at the current
   * level, with the same confirmation before approval is reduced; no gate lists, no matrix, no instructions
   * position and no table of what waits now (the fleet page keeps them).
   */
  compact?: boolean;
  /** `fleet.autonomy_level`. */
  level: string | null | undefined;
  /** The reader manages the fleet (a built-in one: an administrator), and it is not archived. */
  canManage: boolean;
  isAdmin: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: doc } = useFleetDocumentation(fleetId);
  const setLevel = useSetFleetAutonomy(fleetId);
  const [reduceTo, setReduceTo] = useState<FleetAutonomyLevel | null>(null);
  const [explainerOpen, setExplainerOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const explainerToggle = useRef<HTMLButtonElement>(null);

  const level = effectiveAutonomyLevel(doc?.autonomy_level ?? storedLevel);
  const catalog = doc?.autonomy_catalog ?? [];
  const hasCatalog = catalog.length > 0;
  // The Approval tab reads the catalog's source and the instructions position (#10176): no run, no quota.
  const { data: gates } = useFleetApprovalGates(fleetId);
  const selectable = selectableLevels(level, { canManage, isAdmin });
  // One wording on the strip, its tips and the matrix (UX round 1 B3).
  const summary = (at: FleetAutonomyLevel) =>
    t_i18n(
      "{waiting, plural, one {# action waits for approval} other {# actions wait for approval}}, {automatic, plural, one {# runs alone} other {# run alone}}",
      autonomyCounts(catalog, at),
    );

  const closeExplainer = () => {
    setExplainerOpen(false);
    explainerToggle.current?.focus();
  };
  useEffect(() => {
    if (!explainerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeExplainer();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [explainerOpen]);

  // What picking a step changes for THIS fleet, both directions, named before the click (UX round 2 B4, Sam's rule 10c).
  const changeLine = (to: FleetAutonomyLevel): string | null => {
    if (to === level) return null;
    if (isAutonomyRaise(level, to)) {
      const freed = becomesAutomatic(catalog, level, to);
      return t_i18n(
        "{count, plural, =0 {Reduce approval: no action changes} one {Reduce approval: # action runs alone: {names}} other {Reduce approval: # actions run alone: {names}}}",
        { count: freed.length, names: gateNames(freed, t_i18n) },
      );
    }
    const gated = becomesGated(catalog, level, to);
    return t_i18n(
      "{count, plural, =0 {Add approval back: no action changes} one {Add approval back: # action starts waiting: {names}} other {Add approval back: # actions start waiting: {names}}}",
      { count: gated.length, names: gateNames(gated, t_i18n) },
    );
  };

  const options: SegmentedFilterOption<FleetAutonomyLevel>[] = AUTONOMY_LEVELS.map((key) => {
    const blocked = !selectable.includes(key);
    const change = changeLine(key);
    return {
      key,
      label: AUTONOMY_LEVEL_LABELS[key],
      // Meaning, then the summary for THIS fleet at that step (UX round 3 B3). In the setup step the
      // table under the levels says what waits, row by row, so the tip keeps the meaning alone (#1373).
      tip: compact ? (
        <span className="block">{t_i18n(AUTONOMY_LEVEL_HINTS[key])}</span>
      ) : (
        <span className="block space-y-0.5">
          <span className="block">{t_i18n(AUTONOMY_LEVEL_HINTS[key])}</span>
          <span className="block tabular-nums">{summary(key)}</span>
          {change && (
            <span className="block font-medium" data-testid={`fleet-autonomy-change-${key}`}>
              {change}
            </span>
          )}
        </span>
      ),
      disabled: blocked,
      disabledHint:
        blocked && canManage && key === AUTONOMY_LEVELS[AUTONOMY_LEVELS.indexOf(level) + 1]
          ? "Only an administrator can reduce human approval. Whoever manages the fleet can add approval back."
          : undefined,
    };
  });

  // `from` is the level being left: an Undo runs from a toast created before the level
  // changed, so it passes the level it undoes, never this render's stale `level` (UX round 4 B2 on #10034).
  const apply = async (next: FleetAutonomyLevel, previous?: FleetAutonomyLevel, from: FleetAutonomyLevel = level) => {
    const reducing = isAutonomyRaise(from, next);
    const changed = reducing ? becomesAutomatic(catalog, from, next) : becomesGated(catalog, from, next);
    try {
      await setLevel.mutateAsync(next);
      const levelName = t_i18n(AUTONOMY_LEVEL_LABELS[next]);
      toast({
        title: reducing
          ? t_i18n("Human approval reduced to {level}", { level: levelName })
          : t_i18n("Human approval increased to {level}", { level: levelName }),
        description: reducing
          ? t_i18n("{count, plural, =0 {No action changes} one {# action now runs without approval} other {# actions now run without approval}}", { count: changed.length })
          : t_i18n(
              "{count, plural, =0 {No action changes} one {# action now waits for approval: {names}} other {# actions now wait for approval: {names}}}",
              { count: changed.length, names: gateNames(changed, t_i18n) },
            ),
        variant: "success",
        // A toast with an action stays 10 s, paused on hover and focus (UX #9945 A3).
        durationMs: previous && isAdmin ? 10_000 : undefined,
        // Undo of adding approval back reduces it again: an administrator only.
        action: previous && isAdmin ? { label: t_i18n("Undo"), onClick: () => void apply(previous, undefined, next) } : undefined,
      });
    } catch (err) {
      toast({ title: t_i18n("Human approval was not changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  const pick = (next: FleetAutonomyLevel) => {
    if (next === level || setLevel.isPending) return;
    if (isAutonomyRaise(level, next)) setReduceTo(next);
    else void apply(next, level);
  };

  const unlocked = reduceTo ? becomesAutomatic(catalog, level, reduceTo) : [];

  return (
    <section
      id={FLEET_AUTONOMY_STRIP_ID}
      // Focusable by script only: the Modify with AI panel's raise refusal brings the reader here (#10162 R9),
      // and the ring shows where it landed (UX r1 B4). A script focus never matches :focus-visible after a click and
      // focus: is forbidden (focusable-badge-trigger.test.ts), so the panel sets data-landed until the strip blurs.
      tabIndex={-1}
      className="space-y-2 rounded-sm outline-none data-[landed=true]:ring-2 data-[landed=true]:ring-focus data-[landed=true]:ring-offset-2"
      data-testid="fleet-autonomy-strip"
      aria-label={t_i18n("Human approval")}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="inline-flex items-center gap-1">
          {/* Same section-title style as "Waiting for approval now" in this card (UX r2 B1 of #10208). */}
          <span className="content-base font-semibold text-default-primary">{t_i18n("Human approval")}</span>
          <Button
            ref={explainerToggle}
            variant="ghost"
            size="icon"
            aria-label={t_i18n("How human approval works")}
            aria-expanded={explainerOpen}
            aria-controls="fleet-autonomy-explainer"
            onClick={() => setExplainerOpen((open) => !open)}
            data-testid="fleet-autonomy-explainer-toggle"
          >
            <Info className="h-4 w-4" aria-hidden />
          </Button>
        </span>
        {!canManage && (
          <span className="content-compact text-default-secondary" data-testid="fleet-autonomy-read-only">
            {t_i18n("Read only: you do not manage this fleet.")}
          </span>
        )}
      </div>

      {/* One scale, two sources (UX r1 B2 of #10178): labels in the first column, controls starting at the same x. */}
      <div className="grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-4 gap-y-2" data-testid="fleet-autonomy-sources">
        {(hasCatalog || doc) && (
          <span className="content-compact text-default-secondary" data-testid="fleet-autonomy-enforced-label">
            {t_i18n("Enforced by the platform")}
          </span>
        )}
        {(hasCatalog || doc) && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
        {hasCatalog ? (
          <>
            <SegmentedFilter
              options={options}
              value={level}
              onChange={pick}
              ariaLabel={t_i18n("Human approval")}
              testId="fleet-autonomy-levels"
              className="w-fit"
            />
            <span className="content-compact text-default-secondary tabular-nums" data-testid="fleet-autonomy-counts">
              {summary(level)}
            </span>
          </>
        ) : (
          doc && (
            <span className="inline-flex items-center gap-1.5 content-compact text-default-secondary" data-testid="fleet-autonomy-counts">
              {t_i18n("No action of this fleet is gated by this control yet.")}
              {/* Where approvals still apply: in the "i", not a second line under the band (#10215 slice b). */}
              <InfoTip label={t_i18n(STILL_APPLY_LINE)} content={t_i18n(STILL_APPLY_LINE)} />
            </span>
          )
        )}
        {doc && !hasCatalog && canManage && !compact && (
          <Button
            variant="outline"
            size="sm"
            aria-expanded={reviewOpen}
            onClick={() => setReviewOpen((open) => !open)}
            data-testid="fleet-autonomy-review-toggle"
          >
            {t_i18n("Review actions")}
          </Button>
        )}
        </div>
        )}
        {gates && !compact && (
          <FleetInstructionsPositionControl
            // Remounted when the saved position or flag changes, so the switch reads the stored value.
            key={`${gates.instructions_position ?? "unset"}:${gates.instructions_position_announced ? "told" : "silent"}`}
            fleetId={fleetId}
            position={gates.instructions_position ?? null}
            announced={gates.instructions_position_announced}
            canManage={canManage && gates.editable}
            isAdmin={isAdmin}
          />
        )}
      </div>

      {explainerOpen && (
        <section
          id="fleet-autonomy-explainer"
          aria-labelledby="fleet-autonomy-explainer-title"
          className="max-w-4xl space-y-3 rounded-sm bg-elevation-hover p-4 text-default-primary"
          data-testid="fleet-autonomy-explainer"
        >
          <div className="flex items-center justify-between gap-3">
            <h3 id="fleet-autonomy-explainer-title" className="content-base font-semibold">
              {t_i18n("How human approval works")}
            </h3>
            <Button variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={closeExplainer} data-testid="fleet-autonomy-explainer-close">
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
            {EXPLAINER_PARTS.map((part) => (
              <div key={part.heading} className="space-y-0.5">
                <dt className="content-compact font-semibold">{t_i18n(part.heading)}</dt>
                <dd className="content-base text-default-secondary">{t_i18n(part.body)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {/* The setup step (#1373): what each gated action is and whether it waits at the level picked, live. */}
      {hasCatalog && compact && (
        <div className="space-y-2">
          <FleetGatesAtLevel catalog={catalog} level={level} />
          <p className="content-compact text-default-secondary" data-testid="fleet-autonomy-not-covered">
            {t_i18n(NOT_COVERED_LINE)}
          </p>
        </div>
      )}

      {/* The matrix is the content, always shown (#1493): no grouped lists, no toggle. */}
      {hasCatalog && !compact && (
        <div className="space-y-2">
          {/* The one place the not-covered line is written (C2): the explainer points here. */}
          <p className="content-compact text-default-secondary" data-testid="fleet-autonomy-not-covered">
            {t_i18n(NOT_COVERED_LINE)}
          </p>
          <FleetAutonomyCatalog
            catalog={catalog}
            level={level}
            source={gates ? t_i18n(gates.stored ? SOURCE_STORED : SOURCE_DOCUMENTED) : undefined}
            fleetId={fleetId}
            canManage={canManage}
            isAdmin={isAdmin}
          />
        </div>
      )}

      {reviewOpen && !hasCatalog && <FleetGateReview fleetId={fleetId} />}

      {/* Slice D of #10000 (#10202): what waits for a person now, decided in place. */}
      {!compact && <FleetPendingApprovals fleetId={fleetId} />}

      <ConfirmDialog
        open={reduceTo !== null}
        onOpenChange={(open) => !open && setReduceTo(null)}
        variant="warning"
        title={reduceTo ? t_i18n("Reduce human approval to {level}?", { level: t_i18n(AUTONOMY_LEVEL_LABELS[reduceTo]) }) : ""}
        description={t_i18n(
          "{count, plural, =0 {No action of this fleet changes. Every action stays logged and reported in Operations.} one {# action will run without approval:} other {# actions will run without approval:}}",
          { count: unlocked.length },
        )}
        confirmLabel={reduceTo ? t_i18n("Reduce to {level}", { level: t_i18n(AUTONOMY_LEVEL_LABELS[reduceTo]) }) : ""}
        onConfirm={() => reduceTo && void apply(reduceTo)}
      >
        {unlocked.length > 0 && (
          <div className="space-y-2">
            <FleetGateList gates={unlocked} testId="fleet-autonomy-unlocked" />
            <p className="content-compact text-default-secondary">{t_i18n("Every action stays logged and reported in Operations.")}</p>
          </div>
        )}
      </ConfirmDialog>
    </section>
  );
}
