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

import { Network } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useRelayGroups, useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * "via Paris datacenter" on the card of an integration or MCP server routed
 * through a relay group, amber while the group has no relay online.
 * Every badge of a list reads the same cached group list; a group the viewer's
 * library does not hold (an admin looking at a colleague's resource) is named
 * generically. Renders nothing for a resource that calls directly.
 */
export function RelayRouteBadge({
  groupId,
  size = "sm",
}: {
  groupId?: string | null;
  /** "sm" for tight card rows (9px), "default" for the caption token (10px). */
  size?: "sm" | "default";
}) {
  const { t_i18n } = useFormatter();
  const relaysEnabled = useRelaysEnabled();
  const relaysOff = relaysEnabled === false;
  const { data: groups } = useRelayGroups({ enabled: relaysEnabled === true && !!groupId });
  if (!groupId) return null;
  const group = groups?.find((g) => g.id === groupId);
  const offline = relaysOff || (group !== undefined && group.relays_online === 0);
  const label = group ? t_i18n("via {group}", { group: group.name }) : t_i18n("via a relay group");
  const detail = relaysOff
    ? t_i18n("Relays are turned off on this platform: requests routed through a relay group are refused until they are back on.")
    : group && group.relays_online === 0
      ? t_i18n("No relay of this group is online: its requests fail until one connects.")
      : t_i18n("Requests leave from the group's network through one of its relays, never directly from the platform.");
  return (
    <Tip content={detail}>
      <Badge
        variant="outline"
        className={cn(
          size === "sm" ? "text-[9px]" : "content-caption",
          "gap-0.5 max-w-[10rem]",
          offline ? "border-feedback-alert-primary/40 text-feedback-warning-primary" : "border-accent/30 text-accent",
        )}
        data-testid="relay-route-badge"
      >
        <Network className={cn(size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3", "shrink-0")} />
        <span className="truncate">{label}</span>
      </Badge>
    </Tip>
  );
}
