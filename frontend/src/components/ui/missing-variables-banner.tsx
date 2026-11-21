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
 * Notice for the variables an agent references that have no value yet
 * - a secret a catalog install copied without its value, typically - or that
 * no longer exist. Same amber notice as `CompanyManagedBanner`.
 *
 * Usage:
 *   <MissingVariablesBanner agentId={agent.id} />      (reads the live list)
 *   <VariablesNeedingValueList variables={res.variables_needing_value} />
 */

import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useAgentMissingVariables } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import type { VariableNeedingValue } from "@/types";

export function VariablesNeedingValueList({ variables }: { variables: VariableNeedingValue[] }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {variables.map((v) => (
        <Tip
          key={v.id}
          content={
            v.deleted
              ? t_i18n("This variable no longer exists.")
              : v.is_secret
                ? t_i18n("A secret: its value is never copied with an agent.")
                : t_i18n("This variable has no value yet.")
          }
        >
          <Badge variant="outline" className="font-mono-plex content-caption">
            {v.name ?? `{{var_id.${v.id}}}`}
          </Badge>
        </Tip>
      ))}
    </div>
  );
}

export function MissingVariablesBanner({ agentId }: { agentId: string }) {
  const { t_i18n } = useFormatter();
  const { data: variables } = useAgentMissingVariables(agentId);
  if (!variables?.length) return null;
  const unfilled = variables.filter((v) => !v.deleted);
  const deleted = variables.filter((v) => v.deleted);

  return (
    <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
      <Icon name="key-round" size={16} className="text-feedback-warning-primary shrink-0 mt-0.5" />
      <div className="space-y-2">
        {unfilled.length > 0 && (
          <>
            <p className="content-compact text-feedback-warning-primary leading-relaxed">
              {t_i18n(
                "{count, plural, one {This agent uses a variable that has no value yet. It resolves to an empty value until you set one.} other {This agent uses # variables that have no value yet. They resolve to empty values until you set them.}}",
                { count: unfilled.length },
              )}
            </p>
            <VariablesNeedingValueList variables={unfilled} />
            <Link to="/connections/variables" className="inline-block content-compact text-accent hover:underline">
              {t_i18n("Set values in Variables")}
            </Link>
          </>
        )}
        {deleted.length > 0 && (
          <>
            <p className="content-compact text-feedback-warning-primary leading-relaxed">
              {t_i18n(
                "{count, plural, one {This agent references a variable that no longer exists. Its placeholder reaches the model as written until you replace or remove it.} other {This agent references # variables that no longer exist. Their placeholders reach the model as written until you replace or remove them.}}",
                { count: deleted.length },
              )}
            </p>
            <VariablesNeedingValueList variables={deleted} />
          </>
        )}
      </div>
    </div>
  );
}
