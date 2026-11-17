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
 * One attachable resource of the General Assistant's Configure panel
 * (integrations, MCP servers, built-in tools, skills, custom tools, knowledge
 * bases): a compact card at a fixed height. The name in bold with an
 * optional inline chip after it, the description on one line with an
 * ellipsis and its full text in a tooltip, a check mark when attached and a
 * "+" when not. Every grid of the panel lays them out with
 * `RESOURCE_GRID_CLASS`: 1 column narrow, 2 medium, 3 wide.
 *
 * The tooltip leads with the full name, then the description: a name
 * cut with an ellipsis on a narrow card is still readable in full.
 */
import type { ReactNode } from "react";
import { Icon } from "@filigran/design-system";

import { Tip } from "@/components/ui/tooltip";
import { capTipText } from "@/lib/tip-text";
import { cn } from "@/lib/utils";

/** The columns of every resource grid of the panel: 1 / 2 / 3 by breakpoint. */
export const RESOURCE_GRID_CLASS = "grid gap-2 sm:grid-cols-2 lg:grid-cols-3";

interface ResourceToggleCardProps {
  attached: boolean;
  onToggle: () => void;
  /** The resource's icon or logo, at 16 to 20 px. */
  icon: ReactNode;
  name: string;
  /** One line under the name, cut with an ellipsis. */
  description?: ReactNode;
  /** The tooltip text under the name; defaults to `description` when it is a string. */
  tooltip?: string;
  /** A short chip after the name (e.g. "Built-in"): it never takes the name's room. */
  badge?: ReactNode;
  disabled?: boolean;
  testId?: string;
}

export function ResourceToggleCard({
  attached,
  onToggle,
  icon,
  name,
  description,
  tooltip,
  badge,
  disabled,
  testId,
}: ResourceToggleCardProps) {
  const detail = tooltip ?? (typeof description === "string" ? description : undefined);
  const tip = (
    <span className="block" data-testid="resource-card-tip">
      <span className="block font-medium">{name}</span>
      {detail && <span className="block">{capTipText(detail)}</span>}
    </span>
  );
  const card = (
    <button
      type="button"
      aria-pressed={attached}
      disabled={disabled}
      onClick={onToggle}
      data-testid={testId}
      className={cn(
        "flex h-14 w-full min-w-0 items-center gap-2.5 rounded-sm border px-3 text-left transition-all disabled:cursor-not-allowed disabled:opacity-60",
        attached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
      )}
    >
      <span className="flex h-5 w-5 shrink-0 items-center justify-center">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className={cn("min-w-0 truncate content-base font-medium", attached && "text-accent")} data-testid="resource-card-name">
            {name}
          </span>
          {badge}
        </span>
        {description && (
          <span className="block truncate content-caption text-default-secondary" data-testid="resource-card-description">
            {description}
          </span>
        )}
      </span>
      {attached ? (
        <Icon name="circle-check" size={16} className="shrink-0 text-accent" />
      ) : (
        <Icon name="plus" size={16} className="shrink-0 text-default-disabled" />
      )}
    </button>
  );
  return <Tip content={tip}>{card}</Tip>;
}
