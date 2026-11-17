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

import { type MouseEvent } from "react";
import { Check } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { cn } from "@/lib/utils";

/**
 * A saved model no provider of the evaluation's owner offers, read by the server
 * (`unavailable_model_ids`): a launch that includes it is refused. Shown on the Models
 * card and in the Run menus, never only at launch.
 */
export function UnavailableModelBadge({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n(EVALUATION_TERMS.unavailableModel)}>
      <Badge variant="warning" className={cn("shrink-0 text-[9px]", className)}>
        {t_i18n("Unavailable")}
      </Badge>
    </InfoLabel>
  );
}

/**
 * A model of a Run menu's "Run on" list, on the evaluation page and the evaluations list.
 * An unavailable model cannot be selected: Run Selected would be refused for it.
 */
export function RunMenuModelRow({
  modelId,
  selected,
  unavailable,
  disabled,
  onToggle,
}: {
  modelId: string;
  selected: boolean;
  unavailable: boolean;
  disabled: boolean;
  onToggle: (e: MouseEvent<HTMLButtonElement>) => void;
}) {
  const { t_i18n } = useFormatter();
  const row = (
    <button
      aria-pressed={selected}
      className={cn(
        "flex w-full items-center gap-2 px-2 py-1.5 text-left content-compact transition-colors",
        // Dimmed only for its own reason: a pending launch blocks every row unchanged.
        unavailable ? "opacity-60" : "hover:bg-accent/10",
      )}
      onClick={onToggle}
      disabled={disabled || unavailable}
    >
      <span className={selected ? "flex h-4 w-4 items-center justify-center rounded-sm border border-accent/50 bg-accent/20 text-accent" : "flex h-4 w-4 items-center justify-center rounded-sm border border-elevation-subtle-soft"}>
        {selected ? <Check className="h-2.5 w-2.5" /> : null}
      </span>
      <span className="min-w-0 flex-1 truncate font-mono-plex">{modelId}</span>
      {unavailable && (
        <span className="shrink-0 content-caption text-feedback-warning-primary">{t_i18n("Unavailable")}</span>
      )}
    </button>
  );
  // A disabled button takes no hover: the wrapper keeps the reason reachable.
  return unavailable ? (
    <Tip content={t_i18n(EVALUATION_TERMS.unavailableModel)} wrap wrapClassName="block w-full">
      {row}
    </Tip>
  ) : (
    row
  );
}
