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

// What versions a run ran on: its persona, its agent settings and its prompts,
// one chip each with its tooltip. The evaluation, run and compare pages all show a run's
// versions through it, so a chip reads the same on each.

import { Brain, FileText, Repeat, Shield, Thermometer, User } from "lucide-react";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EvaluationRun } from "@/types";

const CHIP = "inline-flex items-center gap-0.5 font-mono-plex content-caption rounded-sm px-1 py-0.5 leading-none";

const FIELD_ICON: Record<string, React.ComponentType<{ className?: string }> | undefined> = {
  temperature: Thermometer,
  reasoning_effort: Brain,
  guardrails: Shield,
  max_iterations: Repeat,
};

/** Agent setting → its name, read by `t_i18n`. @i18n-keys */
const FIELD_LABEL = {
  temperature: "Temperature",
  reasoning_effort: "Reasoning effort",
  guardrails: "Guardrails",
  max_iterations: "Max iterations",
} as const;

function fieldLabel(field: string, t_i18n: (id: string) => string): string {
  return Object.prototype.hasOwnProperty.call(FIELD_LABEL, field)
    ? t_i18n(FIELD_LABEL[field as keyof typeof FIELD_LABEL])
    : field;
}

type VersionedRun = Pick<EvaluationRun, "persona_version_number" | "agent_config_versions" | "prompt_versions">;

function hasRunVersions(run: VersionedRun): boolean {
  return (
    run.persona_version_number != null ||
    Object.keys(run.agent_config_versions ?? {}).length > 0 ||
    Object.keys(run.prompt_versions ?? {}).length > 0
  );
}

/** `fallback` renders when the run recorded no version a chip shows. */
export function RunVersionChips({ run, fallback = null }: { run: VersionedRun; fallback?: React.ReactNode }) {
  const { t_i18n } = useFormatter();
  if (!hasRunVersions(run)) return fallback;
  return (
    <>
      {run.persona_version_number != null && (
        <InfoLabel className="inline-flex" content={t_i18n("The version of the agent's persona this run used.")}>
          <span className={cn(CHIP, "bg-filigran-ia-primary/10 text-filigran-ia-primary border border-filigran-ia-primary/20")}>
            <User className="h-2.5 w-2.5" />v{run.persona_version_number}
          </span>
        </InfoLabel>
      )}
      {Object.entries(run.agent_config_versions ?? {}).map(([field, ver]) => {
        // An own key only: a field named after an inherited property is not an icon.
        const Icon = Object.prototype.hasOwnProperty.call(FIELD_ICON, field) ? FIELD_ICON[field] : undefined;
        // ver === null marks an explicit "use the platform default" snapshot, not "version null".
        const isDefault = ver === null;
        return (
          <InfoLabel
            key={field}
            className="inline-flex"
            content={
              isDefault
                ? t_i18n("{setting}: the platform default, with no saved version.", { setting: fieldLabel(field, t_i18n) })
                : t_i18n("{setting}: version {version} of this agent setting.", { setting: fieldLabel(field, t_i18n), version: ver })
            }
          >
            <span
              className={cn(
                CHIP,
                isDefault
                  ? "bg-elevation-highlight text-default-secondary border border-elevation-subtle-soft"
                  : "bg-accent/10 text-accent/80 border border-accent/20",
              )}
            >
              {Icon && <Icon className="h-2.5 w-2.5" />}
              {isDefault ? t_i18n("default") : `v${ver}`}
            </span>
          </InfoLabel>
        );
      })}
      {Object.keys(run.prompt_versions ?? {}).length > 0 && (
        <InfoLabel
          className="inline-flex"
          content={t_i18n("The prompt versions this run used: {list}.", {
            list: Object.entries(run.prompt_versions ?? {})
              .map(([k, v]) => `${k} v${v}`)
              .join(", "),
          })}
        >
          <span className={cn(CHIP, "bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20")}>
            <FileText className="h-2.5 w-2.5" />
            {Object.keys(run.prompt_versions ?? {}).length}
          </span>
        </InfoLabel>
      )}
    </>
  );
}
