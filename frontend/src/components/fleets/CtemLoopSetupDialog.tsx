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
import { Link, useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ORGANIZATION_SETUP_PATH } from "@/components/layout/organization-setup-path";
import { useRecordCtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { useCtemLoopSetupStatus, useFleet } from "@/hooks/api/useFleetApi";
import { useRecordSetupStep } from "@/hooks/api/useOrganizationContextApi";
import { StepBar } from "@/components/ui/step-bar";
import { useFormatter } from "@/i18n";

import { CtemSetupRunAs } from "./CtemSetupRunAs";
import { CtemSetupGateChoice } from "./CtemSetupGateChoice";
import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";
import { FleetSetupWizard } from "./FleetSetupWizard";
import {
  LOOP_MODULES,
  MODULE_DEGRADED,
  MODULE_NAMES,
  SETUP_STEPS,
  SETUP_STEP_INTROS,
  SETUP_STEP_TITLES,
  connectedModules,
  countFact,
  isSettled,
  loopRuns,
  nextStep,
  openingStep,
  previousStep,
  stepOf,
  testSignal,
  type SetupStepKey,
} from "./ctem-loop-setup";
import { signalsHref } from "@/pages/xtm-one/ctem/loop/signals-route";

export function CtemLoopSetupDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const status = useCtemLoopSetupStatus(open);
  const record = useRecordSetupStep();
  const [current, setCurrent] = useState<SetupStepKey | null>(null);
  const [enableOpen, setEnableOpen] = useState(false);
  // After the last step: the end screen with "Send a test signal" (slice 4).
  const [finished, setFinished] = useState(false);
  const sendTest = useRecordCtemSignal();
  const navigate = useNavigate();
  const fleetId = status.data?.fleet_id ?? null;
  const fleet = useFleet(open ? fleetId : null);

  // Open on the step the server says to resume on, once per opening.
  useEffect(() => {
    if (!open) {
      setCurrent(null);
      setFinished(false);
    } else if (current === null && status.data) setCurrent(openingStep(status.data));
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
          else setFinished(true);
        },
      },
    );

  // The end of the setup: the Signals page with the loop trace of the first
  // signal open, sent now through the real intake when nothing has arrived yet.
  const sendTestSignal = () =>
    sendTest.mutate(testSignal(new Date()), {
      onSuccess: ({ signal }) => {
        onOpenChange(false);
        navigate(signalsHref({ signal: signal.id }));
      },
    });

  // The fleet setup's enable steps (#1151), shared by step 6 and the end screen (UX r2 B1): the same plan and
  // enable call as before, walking only Run as and automations and Review since this flow already walked the slots
  // and gates, under this flow's title (UX r1 B1 of #1157).
  const enableDialog = fleetId && fleet.data && (
    <FleetSetupWizard
      fleetId={fleetId}
      fleetName={fleet.data.name}
      startAt="automations"
      title={t_i18n("Set up the CTEM loop")}
      open={enableOpen}
      onOpenChange={(next) => {
        setEnableOpen(next);
        if (!next) void status.refetch();
      }}
    />
  );

  if (open && finished) {
    // The end screen tells what is true (UX r2 B1): "set up" and the test signal
    // only once an automation is on; otherwise the setup is saved and the loop is off.
    const runs = loopRuns(status.data);
    const openCommandCenter = () => {
      onOpenChange(false);
      navigate("/");
    };
    return (
      <>
        <Dialog open={!enableOpen} onOpenChange={(next) => !next && onOpenChange(false)}>
          <DialogContent size="md" data-testid="ctem-loop-setup-done" data-loop-runs={runs ? "true" : "false"}>
            <DialogHeader>
              <DialogTitle>{runs ? t_i18n("The CTEM loop is set up") : t_i18n("The setup is saved, the CTEM loop is off")}</DialogTitle>
              <DialogDescription>
                {runs
                  ? t_i18n("Every signal now goes through reconcile and the scope gate, and the command center shows where each one stands.")
                  : t_i18n("No automation of the CTEM loop fleet is on, so no signal is processed yet. Review and enable the fleet to start the loop.")}
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-2 content-base">
              {runs ? (
                <p>{t_i18n("Send a test signal to watch the loop move: it is recorded through the real intake and opens on its loop trace.")}</p>
              ) : (
                <p>{t_i18n("A test signal can be sent once the fleet is enabled.")}</p>
              )}
              {sendTest.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The test signal could not be recorded.")}</p>}
            </DialogBody>
            <DialogFooter>
              <Button variant="secondary" className="mr-auto" disabled={sendTest.isPending} onClick={openCommandCenter}>
                {t_i18n("Open the command center")}
              </Button>
              {runs ? (
                <Button
                  className="gap-1.5"
                  disabled={sendTest.isPending}
                  aria-busy={sendTest.isPending || undefined}
                  onClick={sendTestSignal}
                  data-testid="ctem-loop-setup-test-signal"
                >
                  <Icon name="send" size={16} aria-hidden />
                  {t_i18n("Send a test signal")}
                </Button>
              ) : (
                <Button disabled={!fleetId} onClick={() => setEnableOpen(true)} data-testid="ctem-loop-setup-done-enable">
                  {t_i18n("Review and enable")}
                </Button>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>
        {enableDialog}
      </>
    );
  }

  return (
    <>
      <Dialog open={open && !enableOpen} onOpenChange={(next) => !next && onOpenChange(false)}>
        <DialogContent size="lg" data-testid="ctem-loop-setup">
          <DialogHeader>
            <DialogTitle>{t_i18n("Set up the CTEM loop")}</DialogTitle>
            {/* The step's name is the h3 below: the description names the position only (UX r1 A2). */}
            <DialogDescription>{t_i18n("Step {current} of {total}", { current: index + 1, total: SETUP_STEPS.length })}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            {/* The shared step bar of the new agent wizard (UX r1 B1): tooltips, hit area and aria-current in one place. */}
            <StepBar
              ariaLabel={t_i18n("Setup steps")}
              testIdPrefix="ctem-loop-setup-dot"
              steps={SETUP_STEPS.map((key) => ({
                key,
                title: t_i18n(SETUP_STEP_TITLES[key]),
                state: key === step ? "current" : isSettled(stepOf(status.data, key)?.state) ? "settled" : "todo",
              }))}
              onSelect={(i) => setCurrent(SETUP_STEPS[i])}
            />
            <h3 className="text-lg font-semibold">{t_i18n(SETUP_STEP_TITLES[step])}</h3>
            <p className="content-base text-default-secondary">{t_i18n(SETUP_STEP_INTROS[step])}</p>
            {status.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The setup state could not be read. You can still go through the steps.")}</p>}
            {step === "organization" && (
              <div className="space-y-2 content-compact" data-testid="ctem-loop-setup-organization">
                <p>
                  {t_i18n("{count, plural, =0 {No primary asset yet} one {# primary asset} other {# primary assets}}", { count: countFact(facts, "primary_assets") })}
                  {" · "}
                  {t_i18n("{count, plural, =0 {no software entry yet} one {# software entry} other {# software entries}}", { count: countFact(facts, "tech_stack") })}
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
            {/* Whom Platform Health reads the connected modules as (dev-docs 12.3), saved on its own. */}
            {step === "modules" && connectedModules(facts).length > 0 && <CtemSetupRunAs />}
            {step === "ecosystem" && (
              // One status per capability (UX r1 B4 of #1209): the card's rows, no second strip read
              // from other facts; inSetup keeps every exit in a new tab (B3).
              <div className="space-y-3" data-testid="ctem-loop-setup-ecosystem">
                {fleet.data && <FleetCapabilitySlotsCard fleet={fleet.data} inSetup />}
              </div>
            )}
            {step === "gates" && fleet.data && (
              // One choice on the CTEM gate catalog (#1087 item 4, #1830); Add gates opens the level choice.
              <CtemSetupGateChoice fleetId={fleet.data.id} level={fleet.data.autonomy_level} />
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
      {enableDialog}
    </>
  );
}
