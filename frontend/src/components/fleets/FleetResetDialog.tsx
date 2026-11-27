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
 * Reset a built-in fleet to its factory state (#1508): the header's
 * "Reset to factory state" opens this dialog. It is the shared typed-name
 * destructive confirmation (`ConfirmDeleteDialog`, UX round 1 B2) with the dry
 * run's counts (what will be reset) and what is kept in its body; the reset
 * enables once the fleet's name is typed exactly. A refusal or a failure is
 * shown inline under the field, never in a toast alone (UX round 1 B3).
 * Administrators only; the backend refuses a custom fleet and a reset during
 * an emergency stop.
 */
import { useState } from "react";

import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { useToast } from "@/components/ui/toast";
import { useFleetReset, useFleetResetPlan } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { fleetRefusalMessage } from "./fleet-refusals";
import { resetHeldByStop, resetPlanLines } from "./fleet-reset";

export function FleetResetDialog({
  fleetId,
  fleetName,
  open,
  onOpenChange,
}: {
  fleetId: string;
  fleetName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [error, setError] = useState<string | null>(null);
  const plan = useFleetResetPlan(fleetId, open);
  const reset = useFleetReset(fleetId);
  const lines = plan.data ? resetPlanLines(plan.data, t_i18n) : [];
  // An emergency stop holding automations of the fleet refuses the reset (UX round 2 B1):
  // the fleet is then never "already in its factory state", whatever the other counts say.
  const held = resetHeldByStop(plan.data);
  // A fleet already in its factory state is still reset: its value (ROI) restarts from now (#2580).
  const settingsFactory = !!plan.data && lines.length === 0 && held.length === 0;

  const close = (next: boolean) => {
    if (!next) setError(null);
    onOpenChange(next);
  };

  const confirm = async () => {
    setError(null);
    try {
      await reset.mutateAsync(fleetName);
      toast({ title: t_i18n("Fleet reset to its factory state"), description: t_i18n("It is off and not set up."), variant: "success" });
      close(false);
    } catch (err) {
      // Inline, under the field: the reason stays on screen until the next try or the close.
      setError(`${t_i18n("The fleet was not reset")}: ${fleetRefusalMessage(err, t_i18n)}`);
    }
  };

  return (
    <ConfirmDeleteDialog
      open={open}
      onOpenChange={close}
      name={fleetName}
      title={t_i18n("Reset {name} to its factory state?", { name: fleetName })}
      description={t_i18n("The fleet goes back to what a fresh install ships: off, not set up, its agents bound only to the integrations it ships with.")}
      inputLabel={t_i18n("Type {name} to confirm", { name: fleetName })}
      inputAriaLabel={t_i18n("Type {name} to confirm", { name: fleetName })}
      confirmLabel={t_i18n("Reset fleet")}
      cancelLabel={t_i18n("Cancel")}
      confirmDisabled={plan.isLoading || plan.isError || held.length > 0}
      pending={reset.isPending}
      error={error}
      closeOnConfirm={false}
      onConfirm={() => void confirm()}
    >
      <div className="space-y-3" data-testid="fleet-reset-dialog">
        {plan.isLoading ? (
          <p className="content-compact text-default-secondary">{t_i18n("Reading what will be reset…")}</p>
        ) : plan.isError ? (
          <p role="alert" className="content-compact text-feedback-error-tertiary">
            {fleetRefusalMessage(plan.error, t_i18n)}
          </p>
        ) : held.length > 0 ? (
          <div role="alert" data-testid="fleet-reset-held">
            <p className="content-compact font-medium text-feedback-error-tertiary">
              {t_i18n("{count, plural, one {An emergency stop holds # automation of this fleet} other {An emergency stop holds # automations of this fleet}}", { count: held.length })}
            </p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 content-compact text-default-secondary">
              {held.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
            <p className="mt-1 content-compact text-default-secondary">
              {t_i18n("Resume them from the emergency stop before resetting the fleet.")}
            </p>
          </div>
        ) : (
          <div>
            {settingsFactory && (
              <p className="mb-2 content-compact text-default-secondary">{t_i18n("The fleet is already in its factory state.")}</p>
            )}
            <p className="content-compact font-medium text-default-primary">{t_i18n("What is reset")}</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 content-compact text-default-secondary" data-testid="fleet-reset-plan">
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
              <li>{t_i18n("Value (ROI) restarts from now")}</li>
            </ul>
          </div>
        )}
        <div>
          <p className="content-compact font-medium text-default-primary">{t_i18n("What is kept")}</p>
          <p className="mt-1 content-compact text-default-secondary">
            {t_i18n("Run history, cases and findings, the audit trail and the workspace.")}
          </p>
        </div>
      </div>
    </ConfirmDeleteDialog>
  );
}
