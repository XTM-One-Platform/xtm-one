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
 * The gates step of the CTEM loop setup (#1087 item 4, #1830): one choice on the
 * CTEM gate catalog, Fully autonomous / Recommended gates / Add gates. The two
 * first write the level with no pin (`PUT /fleets/{id}/autonomy`); Add gates
 * opens the level choice of the fleet autonomy component. A gate that waits is
 * one decision item between steps (#1705): the setup adds no tool pause and no
 * approval card. Reducing approval names the actions that run alone first.
 */
import { ShieldCheck, SlidersHorizontal, Zap } from "lucide-react";
import { useState, type KeyboardEvent } from "react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { OptionCard } from "@/components/ui/option-card";
import { useToast } from "@/components/ui/toast";
import { useFleetDocumentation, useSetFleetGatePins, type FleetAutonomyLevel } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import {
  CTEM_GATE_CHOICE_LABELS,
  CTEM_GATE_CHOICES,
  CTEM_RECOMMENDED_LEVEL,
  ctemChoiceFreed,
  ctemGateChoiceLevel,
  ctemGateChoiceOf,
  effectiveAutonomyLevel,
  gateActionLabel,
  gateNames,
  gatesWaitingAt,
  type CtemGateChoice,
} from "./fleet-autonomy";
import { FleetAutonomyStrip } from "./FleetAutonomyStrip";
import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetGateMark } from "./FleetModuleMark";

const CHOICE_ICONS = { autonomous: Zap, recommended: ShieldCheck, custom: SlidersHorizontal } as const;

export function CtemSetupGateChoice({ fleetId, level: storedLevel }: { fleetId: string; level: string | null | undefined }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: doc } = useFleetDocumentation(fleetId);
  const save = useSetFleetGatePins(fleetId);
  const [picked, setPicked] = useState<CtemGateChoice | null>(null);
  const [confirmTo, setConfirmTo] = useState<CtemGateChoice | null>(null);

  const catalog = doc?.autonomy_catalog ?? [];
  const level = effectiveAutonomyLevel(doc?.autonomy_level ?? storedLevel);
  const choice = picked ?? ctemGateChoiceOf(level, catalog);
  const recommendedWaits = gatesWaitingAt(catalog, CTEM_RECOMMENDED_LEVEL);

  const descriptions: Record<CtemGateChoice, string> = {
    autonomous: t_i18n("Nothing waits for a person. Every action is still logged and reported in Operations."),
    recommended:
      recommendedWaits.length > 0
        ? t_i18n(
            "{count, plural, one {Waits for a decision: {names}. Every other gate runs and the case records it.} other {Wait for a decision: {names}. Every other gate runs and the case records it.}}",
            { count: recommendedWaits.length, names: gateNames(recommendedWaits, t_i18n) },
          )
        : t_i18n("The level the CTEM loop is seeded with. Every gate that runs is recorded on the case."),
    custom: t_i18n("Pick the level yourself: the table shows what waits at each one. Per-action settings stay on the fleet page."),
  };

  const write = async (next: CtemGateChoice) => {
    const to = ctemGateChoiceLevel(next);
    if (!to) return;
    try {
      await save.mutateAsync({ level: to, pins: {} });
      toast({ title: t_i18n("Gates set to {choice}", { choice: t_i18n(CTEM_GATE_CHOICE_LABELS[next]) }), variant: "success" });
    } catch (err) {
      setPicked(null);
      toast({ title: t_i18n("Human approval was not changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  const select = (next: CtemGateChoice) => {
    if (save.isPending) return;
    const to = ctemGateChoiceLevel(next);
    if (to && ctemChoiceFreed(catalog, level, to).length > 0) {
      setConfirmTo(next);
      return;
    }
    setPicked(next);
    if (next !== choice || catalog.some((g) => g.pin)) void write(next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = CTEM_GATE_CHOICES[(CTEM_GATE_CHOICES.indexOf(choice) + step + CTEM_GATE_CHOICES.length) % CTEM_GATE_CHOICES.length];
    document.getElementById(`ctem-setup-gates-${next}`)?.focus();
    select(next);
  };

  const confirmLevel: FleetAutonomyLevel | null = confirmTo ? ctemGateChoiceLevel(confirmTo) : null;
  const freed = confirmLevel ? ctemChoiceFreed(catalog, level, confirmLevel) : [];

  return (
    <div className="space-y-3" data-testid="ctem-loop-setup-gates" data-choice={choice}>
      <div role="radiogroup" aria-label={t_i18n("Gates and autonomy")} className="grid gap-2" onKeyDown={onKeyDown}>
        {CTEM_GATE_CHOICES.map((key) => (
          <OptionCard
            key={key}
            id={`ctem-setup-gates-${key}`}
            selected={choice === key}
            tabIndex={choice === key ? 0 : -1}
            disabled={save.isPending}
            onSelect={() => select(key)}
            icon={CHOICE_ICONS[key]}
            title={key === "recommended" ? t_i18n("{choice} (recommended)", { choice: t_i18n(CTEM_GATE_CHOICE_LABELS[key]) }) : t_i18n(CTEM_GATE_CHOICE_LABELS[key])}
            description={descriptions[key]}
          />
        ))}
      </div>
      {choice === "custom" && <FleetAutonomyStrip fleetId={fleetId} level={level} canManage isAdmin compact />}

      <ConfirmDialog
        open={confirmTo !== null}
        onOpenChange={(open) => !open && setConfirmTo(null)}
        variant="warning"
        title={confirmTo ? t_i18n("Set the gates to {choice}?", { choice: t_i18n(CTEM_GATE_CHOICE_LABELS[confirmTo]) }) : ""}
        description={t_i18n(
          "{count, plural, =0 {No action of this fleet changes. Every action stays logged and reported in Operations.} one {# action will run without approval:} other {# actions will run without approval:}}",
          { count: freed.length },
        )}
        confirmLabel={confirmTo ? t_i18n("Set to {choice}", { choice: t_i18n(CTEM_GATE_CHOICE_LABELS[confirmTo]) }) : ""}
        onConfirm={() => {
          if (!confirmTo) return;
          setPicked(confirmTo);
          void write(confirmTo);
          setConfirmTo(null);
        }}
      >
        {freed.length > 0 && (
          <div className="space-y-2" data-testid="ctem-loop-setup-gates-freed">
            <ul className="space-y-1.5">
              {freed.map((gate) => (
                <li key={gate.key} className="flex items-center gap-2 content-compact">
                  <FleetGateMark gate={gate} />
                  <span className="min-w-0 truncate">{gateActionLabel(gate, t_i18n)}</span>
                </li>
              ))}
            </ul>
            <p className="content-compact text-default-secondary">{t_i18n("Every action stays logged and reported in Operations.")}</p>
          </div>
        )}
      </ConfirmDialog>
    </div>
  );
}
