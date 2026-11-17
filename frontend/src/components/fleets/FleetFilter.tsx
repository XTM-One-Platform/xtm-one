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
 * The fleet filter of the Operations lists.
 *
 * `?fleet=<id>` narrows a list to what the fleet's current home members
 * produced or own (orchestrator included, connected agents left out). The
 * population is resolved server-side when the query runs
 * (`fleet_views.fleet_scope_clause`), so the counts are the lists' own and a
 * member added or removed shows at the next read.
 *
 * A list rendered inside the fleet page's Operations tab is locked on that
 * fleet: it receives `fleet` as a prop and shows no picker. Anywhere else the
 * picker reads and writes the URL (`useFleetParam`), beside the agent filter.
 */
import { useMemo } from "react";

import { SearchableSelect } from "@/components/ui/searchable-select";
import { useAccessibleFleets } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

const ANY_FLEET = "__any_fleet__";

/**
 * The "All fleets" picker. Rendered only for a reader who can access a fleet
 * (Mine, Shared or Company, `useAccessibleFleets`); otherwise nothing,
 * not even a placeholder, and `useFleetParam` ignores a `?fleet=`. A URL naming
 * a fleet the list does not hold stays a choice, so a shared link still says
 * what it filters on and can be cleared.
 */
export function FleetFilter({
  value,
  onChange,
  className,
}: {
  value: string | undefined;
  onChange: (next: string | undefined) => void;
  /** Width classes, for a toolbar that needs a narrower picker (Executions). */
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { fleets, access } = useAccessibleFleets();
  const options = useMemo(() => {
    const listed = fleets.map((f) => ({ value: f.id, label: f.name }));
    if (value && !listed.some((o) => o.value === value)) {
      listed.push({ value, label: t_i18n("The selected fleet") });
    }
    return listed;
  }, [fleets, value, t_i18n]);

  if (access !== "some") return null;
  return (
    <SearchableSelect
      aria-label={t_i18n("Filter by fleet")}
      className={cn("h-8 w-[200px] content-compact", className)}
      searchable="auto"
      value={value ?? ANY_FLEET}
      onValueChange={(next) => onChange(next === ANY_FLEET ? undefined : next)}
      pinned={[{ value: ANY_FLEET, label: t_i18n("All fleets") }]}
      options={options}
    />
  );
}
