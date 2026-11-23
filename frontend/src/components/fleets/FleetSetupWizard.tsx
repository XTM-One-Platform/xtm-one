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
 * Set up a fleet, one guided setup (#1151): replaces the separate Enable
 * fleet dialog (#9769 S4b) with steps, Back / Skip / Continue, resumed where
 * the person stopped (`fleet-setup-wizard.ts`, kept in the browser).
 *
 * 1. What it does: purpose, triggers in plain words, the monthly run
 *    estimate (the same basis as About > Quota estimate). Read only.
 * 2. Integrations: the capability slots (`FleetCapabilitySlotsCard`),
 *    skippable ("I will do it later"); dropped for a fleet without slots.
 * 3. Autonomy: the level choice (`FleetAutonomyStrip` compact). The setup
 *    adds no human decision.
 * 4. Run as and automations: the plan (`dry_run`, nothing written) for the
 *    chosen user, one row per automation that is off. A row that misses a
 *    module starts unpicked with "Needs <module>": the backend refuses a pick
 *    that misses one, so it never blocks the others.
 * 5. Review and enable: what switches on and what is still missing; Confirm
 *    carries the count and makes the one `POST /fleets/{id}/enable` call.
 *
 * Opening the wizard never enables anything. After Confirm, the end screen
 * says what runs and what is missing, with a link to the Members screen.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { Input } from "@/components/ui/input";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { StepBar } from "@/components/ui/step-bar";
import { useToast } from "@/components/ui/toast";
import { useUsers } from "@/hooks/api/useAuthApi";
import {
  useFleet,
  useFleetCapabilitySlots,
  useFleetDocumentation,
  useFleetEnable,
  useFleetEnablePlan,
  type FleetDocModule,
  type FleetEnableItem,
} from "@/hooks/api/useFleetApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { fleetViewPath } from "@/lib/fleet-operations";
import { exactCost, formatCost } from "@/lib/trace-utils";

import { quotaRange } from "./fleet-about-showcase";
import { fleetRefusalMessage } from "./fleet-refusals";
import {
  WIZARD_STEP_INTROS,
  WIZARD_STEP_TITLES,
  initiallyLeftOut,
  nextWizardStep,
  openingWizardStep,
  pickedLoad,
  pickedPlan,
  previousWizardStep,
  providerName,
  readWizardProgress,
  slotCounts,
  wizardSteps,
  writeWizardProgress,
  type WizardStepKey,
} from "./fleet-setup-wizard";
import { FleetAutonomyStrip } from "./FleetAutonomyStrip";
import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";
import { FleetModuleMark } from "./FleetModuleMark";
import { FleetTriggerList } from "./FleetTriggerList";

/** The run-as select's value for "the caller": the API reads `null` as the caller. */
const SELF_VALUE = "__self__";
const RUN_AS_ID = "fleet-setup-run-as";

export function FleetSetupWizard({
  fleetId,
  fleetName,
  open,
  onOpenChange,
  startAt,
  title,
}: {
  fleetId: string;
  fleetName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Walk only the steps from this one on (the CTEM loop setup opens on Run as and automations; UX r1 B1). */
  startAt?: WizardStepKey;
  /** The parent flow's title, kept when the wizard finishes another setup (UX r1 B1). */
  title?: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const fleet = useFleet(open ? fleetId : null);
  const slots = useFleetCapabilitySlots(fleetId, open);
  const documentation = useFleetDocumentation(fleetId, open);
  const [runAs, setRunAs] = useState<string | null>(null);
  // The rows left out (unchecked): the ones that miss a module once the plan is read.
  const [left, setLeft] = useState<ReadonlySet<string> | null>(null);
  const { data: users } = useUsers(open && isAdmin);
  const plan = useFleetEnablePlan(fleetId, runAs, open);
  const enable = useFleetEnable(fleetId);
  const aiOff = useAiActionDisabled();
  const [current, setCurrent] = useState<WizardStepKey | null>(null);
  const [settled, setSettled] = useState<WizardStepKey[]>([]);
  const [skippedIntegrations, setSkippedIntegrations] = useState(false);
  const [enabledCount, setEnabledCount] = useState<number | null>(null);

  const hasSlots = slots.data ? slots.data.members.some((member) => member.slots.length > 0) : slots.isError ? false : null;
  const steps = useMemo(() => wizardSteps(hasSlots, startAt), [hasSlots, startAt]);

  // Open on the saved step (or startAt), once per opening; reset on close.
  useEffect(() => {
    if (!open) {
      setCurrent(null);
      setRunAs(null);
      setLeft(null);
      setEnabledCount(null);
      return;
    }
    if (current !== null) return;
    const saved = readWizardProgress(fleetId);
    setSettled(saved?.settled ?? []);
    setSkippedIntegrations(saved?.skippedIntegrations ?? false);
    setCurrent(openingWizardStep(steps, saved, startAt));
  }, [open, current, fleetId, steps, startAt]);

  const items = useMemo(() => plan.data?.items ?? [], [plan.data]);
  // A new plan (run-as changed) resets the pick to "every row that can run".
  useEffect(() => setLeft(null), [runAs]);
  const leftOut = useMemo(() => left ?? initiallyLeftOut(items), [left, items]);
  const pick = useMemo(() => pickedPlan(items, leftOut), [items, leftOut]);
  const count = pick.picked.length;
  const ready = count > 0 && pick.missing.length === 0;
  // The expected runs a day and cost of the pick (#1538): an order of magnitude, read from the plan.
  const load = useMemo(() => pickedLoad(pick.picked, plan.data?.estimate), [pick.picked, plan.data]);

  // A step the fleet no longer walks (its slots read after opening) falls back to the next one.
  const step: WizardStepKey = current && steps.includes(current) ? current : (current === "integrations" ? "autonomy" : current) ?? steps[0];
  const index = Math.max(0, steps.indexOf(step));

  const go = (next: WizardStepKey, passed: { skippedIntegrations?: boolean } = {}) => {
    const nextSettled = settled.includes(step) ? settled : [...settled, step];
    const skipped = passed.skippedIntegrations ?? (step === "integrations" ? false : skippedIntegrations);
    setSettled(nextSettled);
    setSkippedIntegrations(skipped);
    setCurrent(next);
    writeWizardProgress(fleetId, { step: next, settled: nextSettled, skippedIntegrations: skipped });
  };
  const moveTo = (next: WizardStepKey) => {
    setCurrent(next);
    writeWizardProgress(fleetId, { step: next, settled, skippedIntegrations });
  };

  const toggle = (id: string) =>
    setLeft(() => {
      const next = new Set(leftOut);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allState: boolean | "mixed" = count === items.length ? true : count === 0 ? false : "mixed";
  const toggleAll = () => setLeft(count === items.length ? new Set(items.map((item) => item.id)) : new Set());

  const confirm = async () => {
    try {
      const result = await enable.mutateAsync({ runAs, assignmentIds: pick.assignmentIds });
      writeWizardProgress(fleetId, null);
      setEnabledCount(result.changed);
      toast({
        title: t_i18n("{count, plural, one {# automation switched on} other {# automations switched on}}", { count: result.changed }),
        description: t_i18n("Pause all switches them off again at any time."),
        variant: "success",
      });
    } catch (err) {
      toast({ title: t_i18n("The fleet was not enabled"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  const close = () => onOpenChange(false);
  // One sentence each, never joined: their order and punctuation stay each locale's own.
  const loadLines = [
    t_i18n("{count, plural, =0 {No scheduled run a day.} one {About # scheduled run a day.} other {About # scheduled runs a day.}}", { count: load.runsPerDay }),
    load.eventDriven > 0
      ? t_i18n("{count, plural, one {# automation runs once per event, as often as its events arrive.} other {# automations run once per event, as often as their events arrive.}}", { count: load.eventDriven })
      : null,
    load.costPerDay === null
      ? t_i18n("No cost yet: these automations have not run in the last {days, plural, one {# day} other {# days}}.", { days: load.costDays })
      : t_i18n("About {cost} a day for the scheduled runs, from the mean cost of their runs over the last {days, plural, one {# day} other {# days}}.", { cost: formatCost(load.costPerDay, intl.locale), days: load.costDays }),
  ].filter((line): line is string => Boolean(line));
  const range = quotaRange(documentation.data?.structured);
  const estimate = documentation.isError
    ? t_i18n("The run estimate could not be read.")
    : !documentation.data
      ? t_i18n("Estimating runs a month...")
      : range
        ? t_i18n("{min} to {max, plural, one {# run} other {# runs}} a month, from the fleet's documented triggers (the same estimate as on the fleet's About tab)", { min: range.min, max: range.max })
        : t_i18n("No estimate written for this fleet");
  const slotTotals = slotCounts(slots.data?.members);
  const blocked = items.filter((item) => item.missing.length > 0);
  const modulesOf = (item: FleetEnableItem) => item.missing.map(providerName).join(", ");
  const membersPath = fleetViewPath(fleetId, "members");
  const selfLabel = user ? t_i18n("You ({user})", { user: user.full_name?.trim() || user.email }) : t_i18n("You");

  if (open && enabledCount !== null) {
    return (
      <Dialog open onOpenChange={(next) => !next && close()}>
        <DialogContent size="md" data-testid="fleet-setup-done">
          <DialogHeader>
            <DialogTitle>{t_i18n("{fleet} is set up", { fleet: fleetName })}</DialogTitle>
            <DialogDescription>
              {t_i18n("{count, plural, one {# automation is now on.} other {# automations are now on.}}", { count: enabledCount })}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-2 content-base">
            {blocked.length > 0 && (
              <p data-testid="fleet-setup-done-off">
                {t_i18n("{count, plural, one {# automation stays off until its module is connected.} other {# automations stay off until their modules are connected.}}", { count: blocked.length })}
              </p>
            )}
            {slotTotals.missing > 0 && (
              <p data-testid="fleet-setup-done-slots">
                {t_i18n("{count, plural, one {# capability slot is not bound yet.} other {# capability slots are not bound yet.}}", { count: slotTotals.missing })}{" "}
                <Link to={membersPath} className="text-accent hover:underline" onClick={close}>
                  {t_i18n("Complete them on the Members screen")}
                </Link>
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button onClick={close}>{t_i18n("Done")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  const isLast = nextWizardStep(steps, step) === null;
  const canSkip = step === "integrations";

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent size="lg" data-testid="fleet-setup-wizard" data-step={step}>
        <DialogHeader>
          <DialogTitle>{title ?? t_i18n("Set up {fleet}", { fleet: fleetName })}</DialogTitle>
          <DialogDescription>{t_i18n("Step {current} of {total}", { current: index + 1, total: steps.length })}</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <StepBar
            ariaLabel={t_i18n("Setup steps")}
            testIdPrefix="fleet-setup-dot"
            steps={steps.map((key) => ({
              key,
              title: t_i18n(WIZARD_STEP_TITLES[key]),
              state: key === step ? "current" : settled.includes(key) ? "settled" : "todo",
            }))}
            onSelect={(i) => moveTo(steps[i])}
          />
          <h3 className="text-lg font-semibold">{t_i18n(WIZARD_STEP_TITLES[step])}</h3>
          <p className="content-base text-default-secondary">{t_i18n(WIZARD_STEP_INTROS[step])}</p>

          {step === "about" && (
            <div className="space-y-3 content-compact" data-testid="fleet-setup-about">
              {documentation.data?.structured?.purpose_line && <p className="text-default-primary">{documentation.data.structured.purpose_line}</p>}
              <FleetTriggerList triggers={documentation.data?.structured?.triggers} />
              <p className="text-default-secondary tabular-nums" data-testid="fleet-setup-estimate">
                {estimate}
              </p>
            </div>
          )}

          {step === "integrations" && fleet.data && (
            <div className="space-y-3" data-testid="fleet-setup-integrations">
              <FleetCapabilitySlotsCard fleet={fleet.data} inSetup />
              <p className="content-compact text-default-secondary">
                <a href="/integrations" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline" onClick={() => window.addEventListener("focus", () => void slots.refetch(), { once: true })}>
                  {t_i18n("Connect a new integration")}
                </a>
                {" · "}
                {t_i18n("The list refreshes when you come back to this tab.")}
              </p>
            </div>
          )}

          {step === "autonomy" && fleet.data && (
            <FleetAutonomyStrip fleetId={fleetId} level={fleet.data.autonomy_level} canManage isAdmin={Boolean(isAdmin)} compact />
          )}

          {step === "automations" && (
            <div className="space-y-4" data-testid="fleet-setup-automations">
              <SettingsField
                id={RUN_AS_ID}
                label={t_i18n("Run as")}
                helper={t_i18n("Its automations use this user's connected modules, model and memory.")}
                info={t_i18n("The user the fleet's automations run as: they use this user's connected modules, model and memory. Example: a service account with OpenCTI and OpenAEV connected. Read every time an automation runs. Left on you, they run as you; only an administrator picks another user.")}
              >
                {isAdmin ? (
                  <SearchableSelect
                    id={RUN_AS_ID}
                    aria-label={t_i18n("Run as")}
                    aria-describedby={settingsFieldHintId(RUN_AS_ID)}
                    options={(users ?? []).filter((u) => u.id !== user?.id).map((u) => userSelectOption(u))}
                    pinned={[{ value: SELF_VALUE, label: selfLabel }]}
                    value={runAs ?? SELF_VALUE}
                    onValueChange={(value) => setRunAs(value === SELF_VALUE ? null : value)}
                    disabled={enable.isPending}
                    placeholder={t_i18n("Search users...")}
                  />
                ) : (
                  <Input id={RUN_AS_ID} aria-label={t_i18n("Run as")} aria-describedby={settingsFieldHintId(RUN_AS_ID)} value={selfLabel} readOnly />
                )}
              </SettingsField>
              <div className="space-y-1.5" data-testid="fleet-enable-plan" aria-live="polite">
                <div className="flex items-center gap-2">
                  {items.length > 1 && (
                    <SelectionBox checked={allState} onToggle={toggleAll} label={t_i18n("Select every automation that is off")} disabled={enable.isPending} />
                  )}
                  <p className="content-compact font-medium text-default-primary">
                    {!plan.data
                      ? t_i18n("Counting what changes...")
                      : items.length === 0
                        ? t_i18n("Nothing to enable: every automation of this fleet is on.")
                        : count === 0
                          ? t_i18n("Pick at least one automation to switch on.")
                          : t_i18n("{count, plural, one {# automation switches on:} other {# automations switch on:}}", { count })}
                  </p>
                </div>
                {items.length > 0 && (
                  <ul className="max-h-48 space-y-1 overflow-y-auto" data-testid="fleet-enable-items">
                    {items.map((item) => (
                      <li key={item.id} className="flex items-center gap-2 content-compact" data-selected={!leftOut.has(item.id)} data-missing={item.missing.length > 0}>
                        <SelectionBox
                          checked={!leftOut.has(item.id)}
                          onToggle={() => toggle(item.id)}
                          label={t_i18n("Switch on {name}", { name: item.name })}
                          disabled={enable.isPending}
                        />
                        <span className="flex shrink-0 items-center gap-1">
                          {item.providers.map((provider) => (
                            <span key={provider} className={item.missing.includes(provider) ? "flex opacity-40" : "flex"}>
                              <FleetModuleMark module={provider as FleetDocModule} className="h-4 w-4" />
                            </span>
                          ))}
                        </span>
                        <span className="min-w-0 truncate">{item.name}</span>
                        {item.missing.length > 0 && (
                          <span className="shrink-0 text-feedback-warning-primary light:text-feedback-warning-tertiary" data-testid="fleet-enable-item-missing">
                            {t_i18n("Needs {modules}", { modules: modulesOf(item) })}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {pick.missing.length > 0 && (
                  <div role="alert" data-testid="fleet-enable-missing">
                    <DashboardNotice
                      variant="warning"
                      title={
                        runAs
                          ? t_i18n("{count, plural, one {This user has not connected {modules}. Connect it in Integrations, or run as another user.} other {This user has not connected {modules}. Connect them in Integrations, or run as another user.}}", { count: pick.missing.length, modules: pick.missing.map(providerName).join(", ") })
                          : t_i18n("{count, plural, one {You have not connected {modules}. Connect it in Integrations, or run as another user.} other {You have not connected {modules}. Connect them in Integrations, or run as another user.}}", { count: pick.missing.length, modules: pick.missing.map(providerName).join(", ") })
                      }
                      actions={
                        <NoticeAction variant="warning" to="/integrations">
                          {t_i18n("Open Integrations")}
                        </NoticeAction>
                      }
                    >
                      {pick.picked
                        .filter((item) => item.missing.length > 0)
                        .map((item) => (
                          <p key={item.id}>{t_i18n("{name} needs {modules}.", { name: item.name, modules: modulesOf(item) })}</p>
                        ))}
                      <p>{t_i18n("{count, plural, one {Leave it out to switch on the others.} other {Leave them out to switch on the others.}}", { count: pick.picked.filter((item) => item.missing.length > 0).length })}</p>
                    </DashboardNotice>
                  </div>
                )}
                {plan.data && count > 0 && (
                  <div className="content-compact text-default-secondary tabular-nums" data-testid="fleet-enable-load">
                    {loadLines.map((line) => (
                      <p key={line}>{line}</p>
                    ))}
                  </div>
                )}
                {plan.error && (
                  <p role="alert" className="content-compact text-default-primary">
                    {fleetRefusalMessage(plan.error, t_i18n) ?? t_i18n("Try again in a moment.")}
                  </p>
                )}
              </div>
            </div>
          )}

          {step === "review" && (
            <ul className="space-y-2 content-compact" data-testid="fleet-setup-review">
              <li className="flex flex-wrap items-center gap-2">
                <span className="w-40 shrink-0 font-medium">{t_i18n("Automations")}</span>
                <span>{t_i18n("{count, plural, =0 {No automation picked} one {# automation switches on} other {# automations switch on}}", { count })}</span>
                {blocked.length > 0 && (
                  <Badge variant="warning">{t_i18n("{count, plural, one {# stays off} other {# stay off}}", { count: blocked.length })}</Badge>
                )}
              </li>
              {hasSlots && (
                <li className="flex flex-wrap items-center gap-2" data-testid="fleet-setup-review-slots">
                  <span className="w-40 shrink-0 font-medium">{t_i18n("Integrations")}</span>
                  <span>{t_i18n("{total, plural, one {{bound} of # slot bound} other {{bound} of # slots bound}}", { bound: slotTotals.bound, total: slotTotals.total })}</span>
                  {slotTotals.missing > 0 && (
                    <Link to={membersPath} className="text-accent hover:underline" onClick={close}>
                      {skippedIntegrations ? t_i18n("Left for later: Members screen") : t_i18n("Complete on the Members screen")}
                    </Link>
                  )}
                </li>
              )}
              <li className="flex flex-wrap items-center gap-2">
                <span className="w-40 shrink-0 font-medium">{t_i18n("Run estimate")}</span>
                <span className="tabular-nums">{estimate}</span>
              </li>
              {plan.data && count > 0 && (
                <li className="flex flex-wrap items-center gap-2" data-testid="fleet-setup-review-load">
                  <span className="w-40 shrink-0 font-medium">{t_i18n("Expected load")}</span>
                  <span className="min-w-0 flex-1 space-y-0.5 tabular-nums" title={load.costPerDay === null ? undefined : exactCost(load.costPerDay, intl.locale)}>
                    {loadLines.map((line) => (
                      <span key={line} className="block">
                        {line}
                      </span>
                    ))}
                  </span>
                </li>
              )}
            </ul>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" className="mr-auto" disabled={enable.isPending || !previousWizardStep(steps, step)} onClick={() => moveTo(previousWizardStep(steps, step) ?? step)}>
            {t_i18n("Back")}
          </Button>
          {canSkip && (
            <Button variant="secondary" onClick={() => go(nextWizardStep(steps, step) ?? step, { skippedIntegrations: true })} data-testid="fleet-setup-later">
              {t_i18n("I will do it later")}
            </Button>
          )}
          {isLast ? (
            <AiDisabledTip>
              <Button disabled={!plan.data || !ready || enable.isPending || aiOff} onClick={() => void confirm()} data-testid="fleet-enable-confirm">
                {count > 0 ? t_i18n("{count, plural, one {Enable # automation} other {Enable # automations}}", { count }) : t_i18n("Enable fleet")}
              </Button>
            </AiDisabledTip>
          ) : (
            <Button onClick={() => go(nextWizardStep(steps, step) ?? step)} data-testid="fleet-setup-next">
              {t_i18n("Continue")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
