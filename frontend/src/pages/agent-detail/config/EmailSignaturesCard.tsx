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
 * The email signature of each email-sending integration the agent binds:
 * inherited from the integration's Email signature setting, or
 * overridden for this agent - on or off, and the signature integration read.
 * Each change saves at once (`integration_signatures`).
 */
import { PenLine } from "lucide-react";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAgentEmailSignatures } from "@/hooks/api/useEmailSignatureApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import {
  NO_SIGNATURE_INTEGRATION_CHOSEN,
  SIGNATURE_MODES,
  nextIntegrationSignatures,
  signatureOptions,
} from "@/lib/email-signatures";
import type { Agent, AgentBindingSignature, ResolvedSignature, SignatureMode } from "@/types";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** "on, <name>" / "off" / "on, none available", for the inherited and effective values. */
function resolvedLabel(t_i18n: Translate, resolved: ResolvedSignature): string {
  if (!resolved.enabled) return t_i18n("off");
  return resolved.integration_name
    ? t_i18n("on, {name}", { name: resolved.integration_name })
    : t_i18n("on, no signature integration");
}

function modeLabel(t_i18n: Translate, mode: SignatureMode, row: AgentBindingSignature): string {
  if (mode === "inherit") return t_i18n("Inherited: {value}", { value: resolvedLabel(t_i18n, row.inherited) });
  return mode === "on" ? t_i18n("On") : t_i18n("Off");
}

export function EmailSignaturesCard({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAgent();
  const aiOff = useAiActionDisabled();
  const { data } = useAgentEmailSignatures(agent.id);
  if (!data || data.bindings.length === 0) return null;
  const editable = canManage && !update.isPending && !aiOff;

  const save = (row: AgentBindingSignature, change: Parameters<typeof nextIntegrationSignatures>[2]) =>
    update.mutate(
      {
        id: agent.id,
        integration_signatures: nextIntegrationSignatures(agent.integration_signatures, row.integration_id, change),
      },
      {
        onSuccess: () => toast({ title: t_i18n("Email signature saved"), variant: "success" }),
        onError: (error) =>
          toast({ title: t_i18n("The email signature could not be saved"), description: error.message, variant: "error" }),
      },
    );

  return (
    <Card>
      <CardHeader className="p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <PenLine className="h-4 w-4 text-accent" /> {t_i18n("Email signatures")}
        </CardTitle>
        <p className="content-caption text-default-secondary mt-0.5">
          {t_i18n("What this agent's emails end with, per bound email integration: its own Email signature setting, or an override for this agent.")}
        </p>
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0 space-y-2">
        {data.bindings.map((row) => {
          const inheritedSource = row.inherited.integration_name
            ? t_i18n("Inherited ({name})", { name: row.inherited.integration_name })
            : t_i18n("Inherited (automatic)");
          return (
            <div key={row.integration_id} data-slot="email-signature-binding" className="rounded-sm bg-elevation-hover px-3 py-2.5 space-y-2">
              <div className="flex items-center gap-3">
                <ProviderLogo provider={row.provider} className="h-5 w-5 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="content-base font-medium truncate">{row.integration_name}</p>
                  <p className="content-caption text-default-secondary">
                    {INTEGRATION_PROVIDER_NAMES[row.provider] ?? row.provider}
                    {" · "}
                    {t_i18n("Signs: {value}", { value: resolvedLabel(t_i18n, row.effective) })}
                  </p>
                </div>
              </div>
              <AiDisabledTip wrapClassName="block">
                <div className="grid gap-2 sm:grid-cols-2">
                  <Select
                    value={row.signature_mode}
                    disabled={!editable}
                    onValueChange={(value) => save(row, { signature_mode: value as SignatureMode })}
                  >
                    <SelectTrigger className="w-full" aria-label={t_i18n("Email signature")}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SIGNATURE_MODES.map((mode) => (
                        <SelectItem key={mode} value={mode}>{modeLabel(t_i18n, mode, row)}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <SearchableSelect
                    aria-label={t_i18n("Signature integration")}
                    searchable="auto"
                    pinned={[{ value: NO_SIGNATURE_INTEGRATION_CHOSEN, label: inheritedSource }]}
                    options={signatureOptions(
                      data.signature_integrations,
                      row.signature_integration_id,
                      row.effective.integration_name,
                      t_i18n("Unavailable integration"),
                    )}
                    value={row.signature_integration_id ?? NO_SIGNATURE_INTEGRATION_CHOSEN}
                    disabled={!editable || !row.effective.enabled}
                    onValueChange={(value) =>
                      save(row, { signature_integration_id: value === NO_SIGNATURE_INTEGRATION_CHOSEN ? null : value })
                    }
                  />
                </div>
              </AiDisabledTip>
              {row.effective.enabled && !row.effective.integration_id && (
                <p className="content-caption text-default-secondary">
                  {t_i18n("No signature integration resolves for it: choose one here or in the integration's Email signature setting.")}
                </p>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
