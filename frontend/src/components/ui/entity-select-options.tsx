// SPDX-License-Identifier: Apache-2.0

import { Bot } from "lucide-react";

import type { SelectOption } from "@/components/ui/searchable-select";
import { ICON_MAP } from "@/lib/constants";
import { cn } from "@/lib/utils";

/**
 * Entities as `SearchableSelect` options, built once so every picker
 * of agents or users reads and searches alike.
 */

/** The fields of an agent a picker reads. */
export interface AgentLike {
  id: string;
  name: string;
  icon?: string | null;
  slug?: string | null;
}

/** An agent: its own icon, its name, and its slug as a search term. */
export function agentSelectOption(
  agent: AgentLike,
  extra: Partial<Omit<SelectOption, "value">> = {},
): SelectOption {
  const Icon = (agent.icon && ICON_MAP[agent.icon]) || Bot;
  return {
    value: agent.id,
    label: agent.name,
    icon: <Icon className={cn("h-3.5 w-3.5", extra.disabled ? "text-default-secondary" : "text-accent")} />,
    keywords: [agent.slug],
    ...extra,
  };
}

/** The fields of a user a picker reads. */
export interface UserLike {
  id: string;
  email: string;
  full_name?: string | null;
}

/** A user: the name, else the email; the email beside a name, and searched either way. */
export function userSelectOption(
  user: UserLike,
  extra: Partial<Omit<SelectOption, "value">> = {},
): SelectOption {
  const name = user.full_name?.trim();
  return {
    value: user.id,
    label: name || user.email,
    description: name ? user.email : undefined,
    keywords: [user.email],
    ...extra,
  };
}
