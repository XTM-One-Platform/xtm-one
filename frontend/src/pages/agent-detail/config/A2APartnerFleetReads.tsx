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

/** The #2202 opt-in of a live A2A exposure: whether a partner's task also
 * reads the exposed agent's home fleet, its health, its work items and the
 * running build. Off by default.
 *
 * Rendered only while the agent is exposed, and the exposure read already
 * answers 404 to a caller who may not manage it, so whoever sees the switch
 * may flip it. The server reads the flag at each partner call: switching it
 * off narrows the next call of a running task.
 */

import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useSetA2APartnerFleetReads, type A2AExposureStatus } from "@/hooks/api/useA2AApi";
import { useFormatter } from "@/i18n";

export function A2APartnerFleetReads({ agentId, exposure }: { agentId: string; exposure: A2AExposureStatus }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const setFleetReads = useSetA2APartnerFleetReads(agentId);
  const enabled = exposure.partner_fleet_reads === true;
  const label = t_i18n("Partners read this agent's fleet");

  return (
    <div className="flex items-start justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
      <div className="space-y-0.5">
        <p className="content-base font-medium">{label}</p>
        <p className="content-caption text-default-secondary">
          {t_i18n("A partner's task also reads this agent's home fleet, its health, its work items and the running build. Never another fleet, never a write.")}
        </p>
      </div>
      <Switch
        aria-label={label}
        checked={enabled}
        disabled={setFleetReads.isPending}
        onCheckedChange={(next) =>
          setFleetReads.mutate(next, {
            onError: (e: Error) =>
              toast({ title: t_i18n("Could not change the partner fleet reads"), description: e.message, variant: "error" }),
          })
        }
      />
    </div>
  );
}
