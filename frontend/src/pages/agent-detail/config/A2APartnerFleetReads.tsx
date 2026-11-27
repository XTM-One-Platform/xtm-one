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

/** The read opt-ins of a live A2A exposure, both off by default:
 * - #2202: a partner's task also reads the exposed agent's home fleet, its
 *   health, its work items and the running build;
 * - #2638: a partner's task reads fleets, fleet health and runs with the
 *   owner's read scope, and the running build, each read audited.
 *
 * Rendered only while the agent is exposed, and the exposure read already
 * answers 404 to a caller who may not manage it, so whoever sees a switch
 * may flip it. The server reads the flags at each partner call: switching one
 * off narrows the next call of a running task. The opt-in routes are behind
 * the AI gate, so the switches are disabled and explained while AI is off.
 */

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useSetA2APartnerFleetReads, type A2AExposureStatus } from "@/hooks/api/useA2AApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";

type OptIn = "partner-fleet-reads" | "partner-owner-reads";

function A2AReadOptIn({
  agentId,
  optIn,
  enabled,
  label,
  explanation,
  errorTitle,
}: {
  agentId: string;
  optIn: OptIn;
  enabled: boolean;
  label: string;
  explanation: string;
  /** The toast title when saving this opt-in fails: names this switch, never the other. */
  errorTitle: string;
}) {
  const { toast } = useToast();
  const setOptIn = useSetA2APartnerFleetReads(agentId, optIn);
  const aiOff = useAiActionDisabled();

  return (
    <div className="flex items-start justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
      <div className="space-y-0.5">
        <p className="content-base font-medium">{label}</p>
        <p className="content-caption text-default-secondary">{explanation}</p>
      </div>
      <AiDisabledTip>
        <Switch
          aria-label={label}
          checked={enabled}
          disabled={setOptIn.isPending || aiOff}
          onCheckedChange={(next) =>
            setOptIn.mutate(next, {
              onError: (e: Error) =>
                toast({ title: errorTitle, description: e.message, variant: "error" }),
            })
          }
        />
      </AiDisabledTip>
    </div>
  );
}

export function A2APartnerFleetReads({ agentId, exposure }: { agentId: string; exposure: A2AExposureStatus }) {
  const { t_i18n } = useFormatter();
  return (
    <>
      <A2AReadOptIn
        agentId={agentId}
        optIn="partner-fleet-reads"
        enabled={exposure.partner_fleet_reads === true}
        label={t_i18n("Partners read this agent's fleet")}
        explanation={t_i18n(
          "A partner's task also reads this agent's home fleet, its health, its work items and the running build. Never another fleet, never a write.",
        )}
        errorTitle={t_i18n("Could not change the partner fleet reads")}
      />
      <A2AReadOptIn
        agentId={agentId}
        optIn="partner-owner-reads"
        enabled={exposure.partner_owner_reads === true}
        label={t_i18n("Partners read fleets and runs as the owner")}
        explanation={t_i18n(
          "A partner's task reads the fleets, fleet health and runs the owner can read, and the running build. Never a write; each read is audited with the partner task.",
        )}
        errorTitle={t_i18n("Could not change the partner owner reads")}
      />
    </>
  );
}
