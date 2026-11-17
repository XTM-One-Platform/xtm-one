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
 * Where the signature of an account's replies comes from: the
 * account's own (a mailbox: copied from its sent mail), a signature
 * integration read when each reply is sent - the person's current signature
 * and banner - or none, when the mail server signs every email in transit.
 */
import { PenLine } from "lucide-react";
import { useMemo } from "react";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { cn } from "@/lib/utils";
import type { AgentAccountSignatureSource } from "@/types";
import { SIGNATURE_SOURCE_HINTS, SIGNATURE_SOURCE_LABELS, SIGNATURE_SOURCES } from "./agent-accounts";

export function AgentAccountSignature({ source, integrationId, providers, onChange, disabled }: {
  source: AgentAccountSignatureSource;
  integrationId: string;
  /** The integration providers that hold email signatures (the catalog's). */
  providers: string[];
  onChange: (source: AgentAccountSignatureSource, integrationId: string) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: integrations } = useIntegrations();
  const candidates = useMemo(
    () => (integrations ?? []).filter((i) => providers.includes(i.provider)),
    [integrations, providers],
  );
  const names = providers.map((p) => INTEGRATION_PROVIDER_NAMES[p] ?? p).join(", ");

  return (
    <fieldset disabled={disabled} className="space-y-3 rounded-sm border border-elevation-subtle p-3">
      <div>
        <div className="flex items-center gap-1.5 content-base font-medium">
          <PenLine className="h-4 w-4 text-accent" />
          {t_i18n("Signature")}
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n("What every reply carries under the answer, above the quoted thread.")}
        </p>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t_i18n("Signature")}>
        {SIGNATURE_SOURCES.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onChange(value, value === "integration" ? integrationId || candidates[0]?.id || "" : "")}
            aria-pressed={source === value}
            className={cn(
              "rounded-sm border px-3 py-1.5 content-base transition-colors",
              source === value ? "border-accent bg-accent/10 text-accent" : "border-elevation-subtle text-default-secondary hover:border-accent/40",
            )}
          >
            {t_i18n(SIGNATURE_SOURCE_LABELS[value])}
          </button>
        ))}
      </div>
      <p className="content-compact text-default-secondary">{t_i18n(SIGNATURE_SOURCE_HINTS[source])}</p>
      {source === "integration" && (
        <div className="space-y-1">
          <SearchableSelect
            aria-label={t_i18n("Signature integration")}
            options={candidates.map((i) => ({ value: i.id, label: i.name }))}
            value={integrationId}
            onValueChange={(v) => onChange("integration", v)}
            placeholder={t_i18n("Choose an integration")}
          />
          {candidates.length === 0 && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Connect a signature integration ({providers}) first.", { providers: names })}
            </p>
          )}
        </div>
      )}
    </fieldset>
  );
}
