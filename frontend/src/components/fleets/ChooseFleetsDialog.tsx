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
 * Choose the fleets the command center shows (#11464): every fleet the reader
 * can see, built-in or their own, each with a box; the page's runs, findings
 * and hours saved follow the fleets kept. "Restore the built-in fleets" goes
 * back to the default (`users.ctem_dashboard_fleet_ids` null). Saving writes
 * the whole list, in the order shown (the built-in ones first, then by name).
 */
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { Fleet } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { dashboardFleets, type FleetDefault } from "@/lib/fleet-activity";

export interface ChooseFleetsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Every fleet the reader can see. */
  fleets: readonly Fleet[];
  /** The saved choice; null = the default of the home (`fallback`). */
  chosenIds: readonly string[] | null;
  /** Save a list, or null to restore the default. */
  onSave: (ids: string[] | null) => void;
  /** The fleets shown with no choice: the built-in CTEM ones (command center) or every fleet (Copilot home, #12063). */
  fallback?: FleetDefault;
}

/** The fleets offered, the built-in ones first, then by name. */
function offeredFleets(fleets: readonly Fleet[]): Fleet[] {
  return fleets
    .filter((f) => !f.is_archived)
    .slice()
    .sort((a, b) => Number(!!b.is_builtin) - Number(!!a.is_builtin) || a.name.localeCompare(b.name));
}

export function ChooseFleetsDialog({ open, onOpenChange, fleets, chosenIds, onSave, fallback = "ctem" }: ChooseFleetsDialogProps) {
  const { t_i18n } = useFormatter();
  const offered = useMemo(() => offeredFleets(fleets), [fleets]);
  const initial = useMemo(() => new Set(dashboardFleets(fleets, chosenIds, fallback).map((f) => f.id)), [fleets, chosenIds, fallback]);
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const selected = picked ?? initial;

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPicked(next);
  };
  const close = (value: boolean) => {
    if (!value) setPicked(null);
    onOpenChange(value);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent size="md" data-testid="ctem-choose-fleets">
        <DialogHeader>
          <DialogTitle>{t_i18n("Choose the fleets of this page")}</DialogTitle>
          <DialogDescription>
            {t_i18n("The page shows the runs, findings and hours saved of the fleets you keep. Hide a built-in fleet or add one of your own.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {offered.length === 0 ? (
            <p className="content-compact text-default-secondary">{t_i18n("No fleet to choose yet.")}</p>
          ) : (
            <ul
              className="max-h-[min(50vh,28rem)] overflow-y-auto divide-y divide-[var(--border-elevation-subtle-soft)]"
              data-testid="ctem-choose-fleets-list"
            >
              {offered.map((fleet) => (
                <li key={fleet.id} className="flex min-h-12 items-center gap-3 py-1.5" data-testid="ctem-choose-fleet-row">
                  <SelectionBox
                    checked={selected.has(fleet.id)}
                    onToggle={() => toggle(fleet.id)}
                    label={t_i18n("Show {name} on this page", { name: fleet.name })}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="content-compact font-medium break-words">{fleet.name}</p>
                    {/* Two lines, then cut with the whole description in the DS tooltip (#11471 UX r2 A2). */}
                    {fleet.description && (
                      <TruncatedText text={fleet.description} lines={2} className="content-compact text-default-secondary" />
                    )}
                  </div>
                  {fleet.is_builtin && (
                    <Badge variant="outline" className="shrink-0 content-caption">
                      {t_i18n("Built-in")}
                    </Badge>
                  )}
                </li>
              ))}
            </ul>
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => {
              onSave(null);
              close(false);
            }}
          >
            {fallback === "all" ? t_i18n("Show every fleet") : t_i18n("Restore the built-in fleets")}
          </Button>
          <Button variant="outline" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={() => {
              onSave(offered.filter((f) => selected.has(f.id)).map((f) => f.id));
              close(false);
            }}
          >
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
