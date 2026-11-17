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
 * Enable fleet, the setup dialog (#9769 S4b, design round 4 APPROVE frame 2,
 * UX #9835 N1): switches on every automation of the fleet that is off, in one
 * call (`POST /fleets/{id}/enable`), after the person saw what it does.
 *
 * - Run as: who the seeded automations run as (their connected modules, model
 *   and memory). The caller by default; another user is an administrator's
 *   choice, as the backend decides it.
 * - Prerequisites: while it is open the dialog reads the plan (`dry_run`,
 *   nothing written) for the chosen user: one line per automation with its
 *   module marks, and the modules that user has not connected. Confirm stays
 *   disabled until none is missing.
 * - Estimate: the runs a month documented for the fleet's triggers, the same
 *   basis as About > Quota estimate (UX #9959 r1 B3), an optional line (a
 *   failed read says so in that line).
 * - Run as is a `SettingsField` (helper and info tied to the control, UX #9959
 *   r1 B1); missing modules are a warning notice with a link to Integrations
 *   (B2).
 * - Partial enable (#9173 S5): every automation that is off is a row with the
 *   shared `SelectionBox`, all picked on opening; the ones left out stay off.
 *   Missing modules and Confirm read the picked rows only; the call names the
 *   pick (`assignment_ids`) only when it is not every row.
 * - Confirm carries the count. Opening the dialog never enables anything.
 * - A row whose automation misses a module says so (#10250 UX r1 B1): the
 *   missing module's mark is dimmed and "Needs <module>" follows the name;
 *   the notice names each picked automation that misses one, so the person
 *   knows which row to leave out.
 */
import { useMemo, useState } from "react";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { Input } from "@/components/ui/input";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useUsers } from "@/hooks/api/useAuthApi";
import {
  useFleetEnable,
  useFleetDocumentation,
  useFleetEnablePlan,
  type FleetDocModule,
  type FleetEnableItem,
} from "@/hooks/api/useFleetApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";

import { quotaRange } from "./fleet-about-showcase";
import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetModuleMark } from "./FleetModuleMark";

/** The run-as select's value for "the caller": the API reads `null` as the caller. */
const SELF_VALUE = "__self__";

const RUN_AS_ID = "fleet-enable-run-as";

const PROVIDER_NAMES: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/** A provider's product name, the raw key for one the table does not know. */
export function providerName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? provider;
}

/** The rows a pick keeps (every row but the ones left out) and the modules they miss, in the plan's order. */
export function pickedPlan(items: readonly FleetEnableItem[], left: ReadonlySet<string>) {
  const picked = items.filter((item) => !left.has(item.id));
  const seen = new Set(picked.flatMap((item) => item.missing));
  const order = items.flatMap((item) => item.missing).filter((p, i, all) => all.indexOf(p) === i);
  return {
    picked,
    missing: order.filter((p) => seen.has(p)),
    /** The ids to send: none when every row is picked (the server picks every one). */
    assignmentIds: picked.length === items.length ? undefined : picked.map((item) => item.id),
  };
}

export function FleetEnableDialog({
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
  const { user, isAdmin } = useAuth();
  const [runAs, setRunAs] = useState<string | null>(null);
  // The rows left out (unchecked); empty on opening, so every automation is picked.
  const [left, setLeft] = useState<ReadonlySet<string>>(() => new Set());
  // The user list is administrator only: never read for anyone else (a 403 would reach the page).
  const { data: users } = useUsers(open && isAdmin);
  const plan = useFleetEnablePlan(fleetId, runAs, open);
  // The same basis as About > Quota estimate: the runs a month documented per trigger.
  const documentation = useFleetDocumentation(fleetId, open);
  const enable = useFleetEnable(fleetId);
  // The enable endpoint is AI gated: Confirm is disabled, with the reason, while AI is off.
  const aiOff = useAiActionDisabled();

  const close = () => {
    setRunAs(null);
    setLeft(new Set());
    onOpenChange(false);
  };

  const data = plan.data;
  const items = useMemo(() => data?.items ?? [], [data]);
  const pick = useMemo(() => pickedPlan(items, left), [items, left]);
  const count = pick.picked.length;
  const ready = count > 0 && pick.missing.length === 0;
  const toggle = (id: string) =>
    setLeft((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allState: boolean | "mixed" = count === items.length ? true : count === 0 ? false : "mixed";
  const toggleAll = () => setLeft(count === items.length ? new Set(items.map((item) => item.id)) : new Set());
  const selfLabel = user
    ? t_i18n("You ({user})", { user: user.full_name?.trim() || user.email })
    : t_i18n("You");

  const apply = async () => {
    try {
      const result = await enable.mutateAsync({ runAs, assignmentIds: pick.assignmentIds });
      toast({
        title: t_i18n("{count, plural, one {# automation switched on} other {# automations switched on}}", { count: result.changed }),
        description: t_i18n("Pause all switches them off again at any time."),
        variant: "success",
      });
      close();
    } catch (err) {
      toast({ title: t_i18n("The fleet was not enabled"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  const range = quotaRange(documentation.data?.structured);
  const missingCount = pick.missing.length;
  const missingModules = pick.missing.map(providerName).join(", ");
  const blocked = pick.picked.filter((item) => item.missing.length > 0);
  const blockedCount = blocked.length;
  const modulesOf = (item: FleetEnableItem) => item.missing.map(providerName).join(", ");

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-w-lg" data-testid="fleet-enable-dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("Enable {fleet}?", { fleet: fleetName })}</DialogTitle>
          <DialogDescription>
            {t_i18n("Pick the automations to switch on. The others stay off until you enable them. Approval gates keep the fleet's level of human approval.")}
          </DialogDescription>
        </DialogHeader>

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
              <SelectionBox
                checked={allState}
                onToggle={toggleAll}
                label={t_i18n("Select every automation that is off")}
                disabled={enable.isPending}
              />
            )}
            <p className="content-compact font-medium text-default-primary">
              {!data
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
                <li
                  key={item.id}
                  className="flex items-center gap-2 content-compact"
                  data-selected={!left.has(item.id)}
                  data-missing={item.missing.length > 0}
                >
                  <SelectionBox
                    checked={!left.has(item.id)}
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
          {missingCount > 0 && (
            <div role="alert" data-testid="fleet-enable-missing">
              <DashboardNotice
                variant="warning"
                title={
                  runAs
                    ? t_i18n("{count, plural, one {This user has not connected {modules}. Connect it in Integrations, or run as another user.} other {This user has not connected {modules}. Connect them in Integrations, or run as another user.}}", { count: missingCount, modules: missingModules })
                    : t_i18n("{count, plural, one {You have not connected {modules}. Connect it in Integrations, or run as another user.} other {You have not connected {modules}. Connect them in Integrations, or run as another user.}}", { count: missingCount, modules: missingModules })
                }
                actions={
                  <NoticeAction variant="warning" to="/integrations">
                    {t_i18n("Open Integrations")}
                  </NoticeAction>
                }
              >
                {blocked.map((item) => (
                  <p key={item.id}>{t_i18n("{name} needs {modules}.", { name: item.name, modules: modulesOf(item) })}</p>
                ))}
                {blockedCount < count && (
                  <p>
                    {t_i18n("{count, plural, one {Leave it out to switch on the others.} other {Leave them out to switch on the others.}}", { count: blockedCount })}
                  </p>
                )}
              </DashboardNotice>
            </div>
          )}
          {plan.error && (
            <p role="alert" className="content-compact text-default-primary">
              {fleetRefusalMessage(plan.error, t_i18n) ?? t_i18n("Try again in a moment.")}
            </p>
          )}
        </div>

        <p className="content-compact text-default-secondary tabular-nums" data-testid="fleet-enable-estimate">
          {documentation.isError
            ? t_i18n("The run estimate could not be read.")
            : !documentation.data
              ? t_i18n("Estimating runs a month...")
              : range
                ? t_i18n("{min} to {max, plural, one {# run} other {# runs}} a month, from the fleet's documented triggers (About > Quota estimate)", { min: range.min, max: range.max })
                : t_i18n("No estimate written for this fleet")}
        </p>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          <AiDisabledTip>
            <Button
              disabled={!data || !ready || enable.isPending || aiOff}
              onClick={() => void apply()}
              data-testid="fleet-enable-confirm"
            >
              {count > 0
                ? t_i18n("{count, plural, one {Enable # automation} other {Enable # automations}}", { count })
                : t_i18n("Enable fleet")}
            </Button>
          </AiDisabledTip>
        </div>
      </DialogContent>
    </Dialog>
  );
}
