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
 * The "Filters" pill of the Fleets toolbar: the workspace and
 * orchestrator pickers and the archived switch, behind one button so the
 * toolbar keeps one row at 1440 px. The button carries how many of them narrow
 * the list, and reads like the `Properties` pill beside it.
 *
 * The pickers open their own lists in a portal, outside this panel: a press
 * inside such a list is not a press outside the panel (`isInsideFloatingList`),
 * or the panel would close under the option being picked. The pill, its panel
 * and the outside-press handling are the shared `FilterPill`, not a copy.
 */

import { useId, useRef, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { FilterPillPanel, FilterPillTrigger, isInsideFloatingList, useFilterPopover } from "@/components/ui/filter-pill";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import type { FleetFilters } from "@/components/fleets/fleet-list-filter";

/** How many of the fleet-specific filters narrow the list. */
export function fleetFilterCount(filters: FleetFilters): number {
  return (filters.workspaceId ? 1 : 0) + (filters.orchestratorId ? 1 : 0) + (filters.includeArchived ? 1 : 0);
}

interface FleetMoreFiltersProps {
  filters: FleetFilters;
  onChange: (patch: Partial<FleetFilters>) => void;
  workspaceOptions: { value: string; label: string }[];
  orchestratorOptions: { value: string; label: string }[];
}

export function FleetMoreFilters({ filters, onChange, workspaceOptions, orchestratorOptions }: FleetMoreFiltersProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const count = fleetFilterCount(filters);

  useFilterPopover(rootRef, open, setOpen, isInsideFloatingList);

  return (
    <div ref={rootRef} className="relative">
      <FilterPillTrigger
        icon={SlidersHorizontal}
        label={t_i18n("Filters")}
        open={open}
        count={count}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      />
      {open && (
        <FilterPillPanel id={panelId} role="group" aria-label={t_i18n("Filters")} className="space-y-3 p-3">
          <SearchableSelect
            aria-label={t_i18n("Workspace")}
            className="w-full"
            searchable="auto"
            clearable
            options={workspaceOptions}
            value={filters.workspaceId}
            onValueChange={(value) => onChange({ workspaceId: value || null })}
            placeholder={t_i18n("All workspaces")}
          />
          <SearchableSelect
            aria-label={t_i18n("Orchestrator")}
            className="w-full"
            searchable="auto"
            clearable
            options={orchestratorOptions}
            value={filters.orchestratorId}
            onValueChange={(value) => onChange({ orchestratorId: value || null })}
            placeholder={t_i18n("All orchestrators")}
          />
          <div className="flex items-center gap-2">
            <Switch
              id="fleets-show-archived"
              checked={filters.includeArchived}
              onCheckedChange={(checked) => onChange({ includeArchived: checked })}
            />
            <Label htmlFor="fleets-show-archived" className="text-default-secondary">
              {t_i18n("Show archived")}
            </Label>
          </div>
        </FilterPillPanel>
      )}
    </div>
  );
}
