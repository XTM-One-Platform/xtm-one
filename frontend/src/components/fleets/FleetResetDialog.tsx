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
 * Reset a built-in fleet to its factory state (#1508): the header menu's
 * "Reset to factory state" opens this dialog. It reads the dry run's counts
 * (what will be reset), says what is kept, and enables Reset once the fleet's
 * name is typed exactly. Administrators only; the backend refuses a custom
 * fleet and a reset during an emergency stop (the refusal is shown).
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { useFleetReset, useFleetResetPlan } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { fleetRefusalMessage } from "./fleet-refusals";
import { resetPlanLines } from "./fleet-reset";

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
  const [typed, setTyped] = useState("");
  const plan = useFleetResetPlan(fleetId, open);
  const reset = useFleetReset(fleetId);
  const lines = plan.data ? resetPlanLines(plan.data, t_i18n) : [];
  const nothing = !!plan.data && lines.length === 0;
  const matches = typed.trim() === fleetName;

  const close = (next: boolean) => {
    if (!next) setTyped("");
    onOpenChange(next);
  };

  const confirm = async () => {
    try {
      await reset.mutateAsync(typed.trim());
      toast({ title: t_i18n("Fleet reset to its factory state"), description: t_i18n("It is off and not set up."), variant: "success" });
      close(false);
    } catch (err) {
      toast({ title: t_i18n("The fleet was not reset"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-lg" data-testid="fleet-reset-dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("Reset {name} to its factory state?", { name: fleetName })}</DialogTitle>
          <DialogDescription>
            {t_i18n("The fleet goes back to what a fresh install ships: off, not set up, with no integration bound to its agents.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {plan.isLoading ? (
            <p className="content-compact text-default-secondary">{t_i18n("Reading what will be reset…")}</p>
          ) : plan.isError ? (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {fleetRefusalMessage(plan.error, t_i18n)}
            </p>
          ) : nothing ? (
            <p className="content-compact text-default-secondary">{t_i18n("The fleet is already in its factory state.")}</p>
          ) : (
            <div>
              <p className="content-compact font-medium text-default-primary">{t_i18n("What is reset")}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 content-compact text-default-secondary" data-testid="fleet-reset-plan">
                {lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <p className="content-compact font-medium text-default-primary">{t_i18n("What is kept")}</p>
            <p className="mt-1 content-compact text-default-secondary">
              {t_i18n("Run history, cases and findings, the audit trail and the workspace.")}
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="fleet-reset-confirm" className="content-compact text-default-secondary">
              {t_i18n("Type {name} to confirm", { name: fleetName })}
            </label>
            <Input
              id="fleet-reset-confirm"
              aria-label={t_i18n("Type {name} to confirm", { name: fleetName })}
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={fleetName}
            />
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={!matches || plan.isLoading || plan.isError || nothing || reset.isPending}
            onClick={confirm}
            data-testid="fleet-reset-confirm"
          >
            {t_i18n("Reset fleet")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
