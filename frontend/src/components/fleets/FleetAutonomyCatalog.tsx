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

import { Icon } from "@filigran/design-system";
/**
 * The fleet's approval gates as a level matrix (#9769 S4, design round 2 A2):
 * one row per declared gate (the backend's `autonomy_catalog`, the same data
 * the run-time gate and the autonomy strip read), one column per autonomy
 * level, each cell saying whether the action waits for a person or runs on
 * its own. The fleet's current level is the highlighted column. The matrix is
 * the content itself (a level grid, not a list of records), so it is a plain
 * table on the data-table CONTENT list.
 *
 * The cells read "Waits" or "Runs alone", the words of the strip's lists
 * (UX round 1 B3 on #10034); `source` is the one line under the matrix that
 * says where the catalog comes from (C3).
 *
 * CTEM loop gates (#916, CTEM-34): a CTEM gate row carries its one-line
 * effect and an "Out of the box" mark (Mobilize waits, every other gate
 * runs), and a last column sets the gate's pin (follows the level, always
 * waits, always runs alone) on any fleet the reader manages, built-in fleets
 * included: the pins are the customer's and survive a seed upgrade.
 */

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { useSetFleetGatePins, type FleetAutonomyGate, type FleetAutonomyLevel, type FleetGatePin } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import {
  AUTONOMY_LEVEL_LABELS,
  AUTONOMY_LEVELS,
  ctemGateEffect,
  ctemGateWaitsByDefault,
  GATE_PIN_LABELS,
  gateActionLabel,
  IMPACT_LABELS,
  nextPins,
  pinLowersApproval,
} from "./fleet-autonomy";
import { FleetGateMark } from "./FleetModuleMark";
import { fleetRefusalMessage } from "./fleet-refusals";

type PinChoice = keyof typeof GATE_PIN_LABELS;
const PIN_CHOICES: PinChoice[] = ["level", "wait", "run"];

function GatePinSelect({
  fleetId,
  catalog,
  gate,
  level,
  isAdmin,
}: {
  fleetId: string;
  catalog: FleetAutonomyGate[];
  gate: FleetAutonomyGate;
  level: FleetAutonomyLevel;
  isAdmin: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const save = useSetFleetGatePins(fleetId);
  const current: PinChoice = gate.pin === "wait" || gate.pin === "run" ? gate.pin : "level";
  const name = gateActionLabel(gate, t_i18n);

  const change = async (value: string) => {
    const choice = value as PinChoice;
    if (choice === current) return;
    const pin: FleetGatePin | null = choice === "level" ? null : choice;
    try {
      await save.mutateAsync({ level, pins: nextPins(catalog, gate.key, pin) });
      toast({
        title: t_i18n("Approval of {action} set to {setting}", { action: name, setting: t_i18n(GATE_PIN_LABELS[choice]) }),
        variant: "success",
      });
    } catch (err) {
      toast({ title: t_i18n("Approval was not changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  return (
    <Select value={current} onValueChange={(value) => void change(value)} disabled={save.isPending}>
      <SelectTrigger
        className="w-fit min-w-[160px] whitespace-nowrap"
        aria-label={t_i18n("Approval setting of {action}", { action: name })}
        data-testid={`about-autonomy-pin-${gate.key}`}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {PIN_CHOICES.map((choice) => (
          <SelectItem
            key={choice}
            value={choice}
            disabled={!isAdmin && pinLowersApproval(gate.pin, choice === "level" ? null : choice)}
          >
            {t_i18n(GATE_PIN_LABELS[choice])}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function FleetAutonomyCatalog({
  catalog,
  level,
  source,
  fleetId,
  canManage = false,
  isAdmin = false,
}: {
  catalog: FleetAutonomyGate[];
  level: FleetAutonomyLevel;
  /** Where the catalog comes from, one translated line under the matrix. */
  source?: string;
  /** Given with `canManage`: the last column sets each gate's pin. */
  fleetId?: string;
  /** The reader manages the fleet (a built-in one: an administrator). */
  canManage?: boolean;
  isAdmin?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const editable = Boolean(fleetId) && canManage;
  return (
    <Card data-testid="about-autonomy-catalog">
      <CardContent className="space-y-3 p-4">
        <p className="content-compact text-default-secondary">
          {t_i18n("What waits for a person at each level of human approval. The highlighted column is this fleet's level.")}
        </p>
        <div className="overflow-x-auto">
          {/* The Action column keeps 320 px at the 1080 minimum; the setting column scrolls inside this wrapper (UX round 1 B1 on #934). */}
          <table className={cn("w-full border-collapse content-compact", editable ? "min-w-[880px]" : "min-w-[640px]")}>
            <caption className="sr-only">{t_i18n("Approval gates by level of human approval")}</caption>
            <thead>
              <tr className="border-b border-[var(--border-elevation-subtle-soft)]">
                <th scope="col" className="py-2 pr-3 text-left font-medium text-default-secondary">
                  {t_i18n("Action")}
                </th>
                {AUTONOMY_LEVELS.map((key) => (
                  <th
                    key={key}
                    scope="col"
                    aria-current={key === level ? "true" : undefined}
                    className={cn(
                      "px-2 py-2 text-left font-medium whitespace-nowrap",
                      // The current column: an accent top rule (3:1 or more) on top of the tint (UX #9945 A2).
                      key === level
                        ? "border-t-2 border-t-accent bg-elevation-highlight text-default-primary"
                        : "text-default-secondary",
                    )}
                    data-testid={`about-autonomy-col-${key}`}
                  >
                    {t_i18n(AUTONOMY_LEVEL_LABELS[key])}
                  </th>
                ))}
                {editable && (
                  <th scope="col" className="px-2 py-2 text-left font-medium whitespace-nowrap text-default-secondary">
                    {t_i18n("Setting")}
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {catalog.map((gate) => {
                const effect = ctemGateEffect(gate);
                return (
                  <tr key={gate.key} className="border-b border-[var(--border-elevation-subtle-soft)] last:border-0">
                    <th scope="row" className="min-w-[320px] max-w-[480px] py-2 pr-3 text-left font-normal">
                      <span className="flex items-start gap-2">
                        <FleetGateMark gate={gate} />
                        <span className="min-w-0">
                          <span className="block font-semibold">{gateActionLabel(gate, t_i18n)}</span>
                          <span className="block text-default-secondary">
                            {t_i18n(IMPACT_LABELS[gate.impact])}
                            {effect && (
                              <span data-testid={`about-autonomy-default-${gate.key}`}>
                                {" · "}
                                {ctemGateWaitsByDefault(gate) ? t_i18n("Out of the box: waits") : t_i18n("Out of the box: runs alone")}
                              </span>
                            )}
                          </span>
                          {/* Whole, never truncated (#1493): the matrix is the content of the Approval tab. */}
                          {effect && (
                            <span className="block text-default-secondary" data-testid={`about-autonomy-effect-${gate.key}`}>
                              {t_i18n(effect)}
                            </span>
                          )}
                        </span>
                      </span>
                    </th>
                    {AUTONOMY_LEVELS.map((key) => {
                      const waits = gate.by_level?.[key] ?? true;
                      return (
                        <td key={key} className={cn("px-2 py-2 align-middle", key === level && "bg-elevation-highlight")}>
                          {waits ? (
                            <Badge variant="warning" className="gap-1 whitespace-nowrap">
                              <Icon name="shield-check" size={16} />
                              {t_i18n("Waits")}
                            </Badge>
                          ) : (
                            <span className="whitespace-nowrap text-default-secondary">{t_i18n("Runs alone")}</span>
                          )}
                        </td>
                      );
                    })}
                    {editable && fleetId && (
                      <td className="px-2 py-2 align-middle">
                        <GatePinSelect fleetId={fleetId} catalog={catalog} gate={gate} level={level} isAdmin={isAdmin} />
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {source && (
          <p className="content-compact text-default-secondary" data-testid="about-autonomy-source">
            {source}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
