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
 * "What starts it": the fleet's documented triggers (`structured.triggers`),
 * one line each with the module's mark, its name and its detail. Shared by the
 * setup wizard's first step and the About tab's "How it works" card (#1319), so
 * both read the same list; a trigger whose automation is switched off says so.
 */
import { Badge } from "@/components/ui/badge";
import type { FleetDocStructured } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { FleetModuleMark } from "./FleetModuleMark";

type FleetTrigger = NonNullable<FleetDocStructured["triggers"]>[number];

export function FleetTriggerList({ triggers, testId }: { triggers: readonly FleetTrigger[] | undefined; testId?: string }) {
  const { t_i18n } = useFormatter();
  if (!triggers?.length) return null;
  return (
    <ul className="space-y-1" aria-label={t_i18n("What starts it")} data-testid={testId}>
      {triggers.map((trigger) => {
        // The built-in fleets document when a trigger fires as `filter`; a generated documentation as `detail`.
        const when = trigger.detail?.trim() || trigger.filter?.trim();
        return (
          <li key={`${trigger.module}-${trigger.name}`} className="flex min-w-0 items-center gap-2" data-testid={testId ? `${testId}-row` : undefined}>
            <FleetModuleMark module={trigger.module} className="h-4 w-4" />
            <span className="min-w-0">{when ? `${trigger.name}: ${when}` : trigger.name}</span>
            {trigger.enabled === false && (
              <Badge variant="secondary" className="shrink-0">
                {t_i18n("Off")}
              </Badge>
            )}
          </li>
        );
      })}
    </ul>
  );
}
