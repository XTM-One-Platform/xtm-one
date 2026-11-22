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
 * The guided CTEM loop setup (#1087, slice 3): six steps in one dialog, each
 * saved on its own (`PUT /organization-context/setup-prompt/steps`), opened on
 * the step `GET /fleets/setup/ctem-loop` says to resume on. It reuses the
 * existing pieces: Settings > Organization for the profile, the capability
 * slots card, the autonomy strip and the Enable fleet dialog, so nothing is
 * switched on before the last step and that step goes through the same
 * dry-run plan and Confirm as the fleet page.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ORGANIZATION_SETUP_PATH } from "@/components/layout/organization-setup-path";
import { useCtemLoopSetupStatus, useFleet } from "@/hooks/api/useFleetApi";
import { useRecordSetupStep } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { FleetAutonomyStrip } from "./FleetAutonomyStrip";
import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";
import { FleetEnableDialog } from "./FleetEnableDialog";
import {
  ECOSYSTEM_CAPABILITIES,
  ECOSYSTEM_LABELS,
  LOOP_MODULES,
  MODULE_DEGRADED,
  MODULE_NAMES,
  SETUP_STEPS,
  SETUP_STEP_INTROS,
  SETUP_STEP_TITLES,
  connectedModules,
  countFact,
  ecosystemSlots,
  isSettled,
  nextStep,
  openingStep,
  previousStep,
  stepOf,
  type SetupStepKey,
} from "./ctem-loop-setup";

const SLOT_VARIANTS = { filled: "success", empty: "warning", unavailable: "outline" } as const;
/** @i18n-keys */
const SLOT_LABELS = { filled: "Bound", empty: "Not bound", unavailable: "None connected" } as const;

export function CtemLoopSetupDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const status = useCtemLoopSetupStatus(open);
  const record = useRecordSetupStep();
  const [current, setCurrent] = useState<SetupStepKey | null>(null);
  const [enableOpen, setEnableOpen] = useState(false);
  const fleetId = status.data?.fleet_id ?? null;
  const fleet = useFleet(open ? fleetId : null);

  // Open on the step the server says to resume on, once per opening.
  useEffect(() => {
    if (!open) setCurrent(null);
    else if (current === null && status.data) setCurrent(openingStep(status.data));
  }, [open, current, status.data]);

  const step = current ?? SETUP_STEPS[0];
  const index = SETUP_STEPS.indexOf(step);
  const facts = stepOf(status.data, step);
  const busy = record.isPending;

  const advance = (state: "done" | "skipped") =>
    record.mutate(
      { step, state },
      {
        onSuccess: () => {
          const next = nextStep(step);
          if (next) setCurrent(next);
          else onOpenChange(false);
        },
      },
    );

  return (
    <>
      <Dialog open={open && !enableOpen} onOpenChange={(next) => !next && onOpenChange(false)}>
        <DialogContent size="lg" data-testid="ctem-loop-setup">
          <DialogHeader>
            <DialogTitle>{t_i18n("Set up the CTEM loop")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Step {current} of {total}: {title}", { current: index + 1, total: SETUP_STEPS.length, title: t_i18n(SETUP_STEP_TITLES[step]) })}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            <ol className="flex items-center gap-2" aria-label={t_i18n("Setup steps")}>
              {SETUP_STEPS.map((key, i) => {
                const state = stepOf(status.data, key)?.state;
                return (
                  <li key={key} className="flex-1">
                    <button
                      type="button"
                      aria-label={t_i18n("Step {current} of {total}: {title}", { current: i + 1, total: SETUP_STEPS.length, title: t_i18n(SETUP_STEP_TITLES[key]) })}
                      aria-current={key === step ? "step" : undefined}
                      onClick={() => setCurrent(key)}
                      className="w-full py-2"
                      data-testid={`ctem-loop-setup-dot-${key}`}
                    >
                      <span
                        className={cn(
                          "block h-1.5 w-full rounded-full transition-colors",
                          key === step ? "bg-accent" : isSettled(state) ? "bg-accent opacity-60" : "bg-elevation-hover",
                        )}
                      />
                    </button>
                  </li>
                );
              })}
            </ol>
            <h3 className="text-lg font-semibold">{t_i18n(SETUP_STEP_TITLES[step])}</h3>
            <p className="content-base text-default-secondary">{t_i18n(SETUP_STEP_INTROS[step])}</p>
            {status.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The setup state could not be read. You can still go through the steps.")}</p>}
            {step === "organization" && (
              <div className="space-y-2 content-compact" data-testid="ctem-loop-setup-organization">
                <p>
                  {t_i18n("{count, plural, =0 {No crown jewel yet} one {# crown jewel} other {# crown jewels}}", { count: countFact(facts, "crown_jewels") })}
                  {" · "}
                  {t_i18n("{count, plural, =0 {no tech stack entry yet} one {# tech stack entry} other {# tech stack entries}}", { count: countFact(facts, "tech_stack") })}
                  {" · "}
                  {facts?.facts.ticketing_target ? t_i18n("ticketing target set") : t_i18n("no ticketing target")}
                </p>
                <Link to={ORGANIZATION_SETUP_PATH} className="text-accent hover:underline" onClick={() => onOpenChange(false)}>
                  {t_i18n("Open Settings > Organization")}
                </Link>
              </div>
            )}
            {step === "modules" && (
              <ul className="space-y-2 content-compact" data-testid="ctem-loop-setup-modules">
                {LOOP_MODULES.map((module) => {
                  const on = connectedModules(facts).includes(module);
                  return (
                    <li key={module} className="flex flex-wrap items-start gap-2">
                      <span className="w-20 shrink-0 font-medium">{MODULE_NAMES[module]}</span>
                      <Badge variant={on ? "success" : "outline"}>{on ? t_i18n("Connected") : t_i18n("Not connected")}</Badge>
                      {!on && <span className="min-w-0 basis-full text-default-secondary">{t_i18n(MODULE_DEGRADED[module])}</span>}
                    </li>
                  );
                })}
              </ul>
            )}
            {step === "ecosystem" && (
              <div className="space-y-3" data-testid="ctem-loop-setup-ecosystem">
                <ul className="flex flex-wrap gap-x-4 gap-y-2 content-compact">
                  {ECOSYSTEM_CAPABILITIES.map((capability) => {
                    const state = ecosystemSlots(facts)[capability];
                    return (
                      <li key={capability} className="flex items-center gap-2">
                        <span>{t_i18n(ECOSYSTEM_LABELS[capability])}</span>
                        <Badge variant={SLOT_VARIANTS[state]}>{t_i18n(SLOT_LABELS[state])}</Badge>
                      </li>
                    );
                  })}
                </ul>
                {fleet.data && <FleetCapabilitySlotsCard fleet={fleet.data} />}
              </div>
            )}
            {step === "gates" && fleet.data && (
              <FleetAutonomyStrip fleetId={fleet.data.id} level={fleet.data.autonomy_level} canManage isAdmin />
            )}
            {step === "preview" && (
              <p className="content-compact" data-testid="ctem-loop-setup-preview">
                {t_i18n("{count, plural, =0 {No automation to switch on} one {# automation switches on} other {# automations switch on}}", { count: countFact(facts, "automations_total") })}
                {" · "}
                {t_i18n("The next step shows the run estimate and lets you leave any automation out.")}
              </p>
            )}
            {step === "enable" && (
              <div className="space-y-2 content-compact" data-testid="ctem-loop-setup-enable">
                <p>
                  {t_i18n("{total, plural, one {{enabled} of # automation on} other {{enabled} of # automations on}}", { enabled: countFact(facts, "automations_enabled"), total: countFact(facts, "automations_total") })}
                </p>
                <Button type="button" disabled={!fleetId || busy} onClick={() => setEnableOpen(true)} data-testid="ctem-loop-setup-enable-open">
                  {t_i18n("Review and enable")}
                </Button>
              </div>
            )}
            {(step === "gates" || step === "ecosystem" || step === "enable") && !fleetId && status.data && (
              <p className="content-compact text-default-secondary">{t_i18n("The CTEM loop fleet is not installed on this platform.")}</p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="secondary" className="mr-auto" disabled={busy || !previousStep(step)} onClick={() => setCurrent(previousStep(step))}>
              {t_i18n("Back")}
            </Button>
            <Button variant="secondary" disabled={busy} onClick={() => advance("skipped")}>
              {t_i18n("Skip")}
            </Button>
            <Button disabled={busy} aria-busy={busy || undefined} onClick={() => advance("done")}>
              {nextStep(step) ? t_i18n("Continue") : t_i18n("Finish setup")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {fleetId && fleet.data && (
        <FleetEnableDialog
          fleetId={fleetId}
          fleetName={fleet.data.name}
          open={enableOpen}
          onOpenChange={(next) => {
            setEnableOpen(next);
            if (!next) void status.refetch();
          }}
        />
      )}
    </>
  );
}
