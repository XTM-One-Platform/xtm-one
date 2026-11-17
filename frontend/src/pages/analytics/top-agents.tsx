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
 * The "Top agents" rows: a row opens the agent's page when the server
 * says the caller may (`can_open`, `agent_url`), and is inert otherwise. It
 * never filters the dashboard. A deleted agent shows its last known name with
 * a "Deleted" chip, a disabled one a "Disabled" chip, never a bare id. The
 * chip has its own key: "Deleted" alone is a confirmation elsewhere.
 */
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import type { TraceAnalyticsCostByAgentItem } from "@/types";

/** The page a row opens: the server's link, only where it allows it. */
export function topAgentHref(item: TraceAnalyticsCostByAgentItem): string | undefined {
  if (!item.can_open || item.agent_status === "deleted") return undefined;
  return item.agent_url ?? undefined;
}

/** What a row is called: the agent's name, its last known one once deleted. */
export function topAgentLabel(item: TraceAnalyticsCostByAgentItem, t_i18n: (id: string) => string): string {
  if (item.agent_name) return item.agent_name;
  return item.agent_status === "deleted" ? t_i18n("Deleted agent") : t_i18n("Unknown agent");
}

export function TopAgentStatusBadge({ item }: { item: TraceAnalyticsCostByAgentItem }) {
  const { t_i18n } = useFormatter();
  if (item.agent_status === "deleted") {
    return (
      <Badge variant="secondary" className="shrink-0 px-1.5 py-0 content-caption" data-testid="top-agent-status">
        {t_i18n("Deleted (status)")}
      </Badge>
    );
  }
  if (item.agent_status === "disabled") {
    return (
      <Badge variant="secondary" className="shrink-0 px-1.5 py-0 content-caption" data-testid="top-agent-status">
        {t_i18n("Disabled")}
      </Badge>
    );
  }
  return null;
}
