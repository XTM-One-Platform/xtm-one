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

import { Info, Lock } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Says whether a Scope(s) selection is a boundary or a hint.
 *
 * A selection is enforced on the agent's tools only when "Enforce as a
 * boundary" is on for it (`isScopeEnforced` in `lib/element-scope.ts`) - on
 * the agent, or for an assignment on the agent or the assignment. Every other
 * selection is written into the prompt and nothing more, so the card must not
 * let the operator believe it confines the agent.
 */
export function ScopeEnforcementBadge({ enforced, byAgent = false }: { enforced: boolean; byAgent?: boolean }) {
  const { t_i18n } = useFormatter();
  const Icon = enforced ? Lock : Info;
  const tip = !enforced
    ? t_i18n("Guidance only: the agent is asked to focus on this selection, but its tools are not restricted to it.")
    : byAgent
      ? t_i18n("Enforced by the agent: an assignment can only narrow this boundary.")
      : t_i18n("Enforced: the agent's tools refuse anything outside this selection.");
  return (
    <Tip content={tip}>
      <span
        data-testid="scope-enforcement-badge"
        data-enforced={enforced ? "true" : "false"}
        className={cn(
          "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[9px] font-medium",
          enforced
            ? "bg-feedback-success-primary/10 text-feedback-success-primary light:text-feedback-success-primary"
            : "bg-feedback-alert-primary/10 text-feedback-warning-tertiary light:text-feedback-warning-tertiary",
        )}
      >
        <Icon className="h-2.5 w-2.5" />
        {enforced ? t_i18n("Enforced") : t_i18n("Guidance only")}
      </span>
    </Tip>
  );
}

/**
 * The "Enforce as a boundary" switch of one Scope(s) selection. Off,
 * the default, keeps the selection prompt guidance; on, the provider refuses
 * anything outside it. Offered only for the element types a provider can
 * enforce, and only once something is selected. On an assignment, a type its
 * agent enforces is shown on and locked: the stricter of the two wins.
 */
export function ScopeEnforcementSwitch({
  checked,
  hasSelection,
  lockedByAgent = false,
  onCheckedChange,
}: {
  checked: boolean;
  hasSelection: boolean;
  lockedByAgent?: boolean;
  onCheckedChange: (enforced: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const interactive = hasSelection && !lockedByAgent;
  const tip = lockedByAgent
    ? t_i18n("Enforced by the agent: an assignment can only narrow this boundary.")
    : hasSelection
      ? t_i18n("On: the agent's tools refuse anything outside this selection, and so do the agents it hands work to. Off: the selection is only guidance.")
      : t_i18n("Select at least one element to enforce this selection.");
  return (
    <Tip content={tip} wrap={!interactive}>
      <label
        data-testid="scope-enforcement-switch"
        className={cn(
          "inline-flex items-center gap-1.5 content-caption text-default-secondary",
          interactive ? "cursor-pointer" : "cursor-not-allowed",
        )}
      >
        {t_i18n("Enforce as a boundary")}
        <Switch
          checked={lockedByAgent || (hasSelection && checked)}
          disabled={!interactive}
          onCheckedChange={onCheckedChange}
          className="h-4 w-7 [&>span]:h-3 [&>span]:w-3 [&>span]:data-[state=checked]:translate-x-3"
        />
      </label>
    </Tip>
  );
}
