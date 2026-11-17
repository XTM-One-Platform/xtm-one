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
 * The link between a fleet and the workspace it works in, drawn the
 * same way on both sides: icon, accent badge link.
 *
 * Workspace side: "[Boxes] [name]", opening the fleet.
 * Fleet side: "[FolderKanban] [name]", opening the workspace.
 * The prefix ("Hosts fleet", "Works in workspace") is the icon's tooltip and
 * the link's accessible name, never visible text: at 12 px the visible prefix
 * pushed the chip onto a second meta row at 1024 (UX round 1 B1, #10287).
 * The badge link is capped at 160 px, truncates inside its own span and
 * carries the full name as its title; the link hands the page it opens a way
 * back. Text at `content-compact` (12 px), the design standards' minimum, as
 * the rest of the header meta row (#10215, slice c).
 */
import { Link } from "react-router-dom";
import { Boxes, FolderKanban } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { fleetWorkspaceChipLink, type FleetWorkspaceChipTarget } from "@/lib/fleet-workspace-chip";
import { cn } from "@/lib/utils";

export function FleetWorkspaceChip({
  target,
  from,
  prefix,
  className,
}: {
  target: FleetWorkspaceChipTarget;
  /** The page the chip is on: the opened page's back link returns there. */
  from: { path: string; label: string };
  /** Replaces the default prefix; "" draws the badge alone (a list of several fleets). */
  prefix?: string;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const Icon = target.kind === "fleet" ? Boxes : FolderKanban;
  const link = fleetWorkspaceChipLink(target, from);
  const label =
    prefix ?? (target.kind === "fleet" ? t_i18n("Hosts fleet") : t_i18n("Works in workspace"));
  return (
    <span
      data-testid={`fleet-workspace-chip-${target.kind}`}
      className={cn(
        "relative z-10 inline-flex min-w-0 max-w-full items-center gap-1.5 content-compact text-default-secondary",
        className,
      )}
    >
      {label && (
        <Tip content={label}>
          <span role="img" aria-label={label} className="inline-flex shrink-0" data-testid="fleet-workspace-chip-icon">
            <Icon className="h-3.5 w-3.5 text-accent" aria-hidden />
          </span>
        </Tip>
      )}
      <Link
        to={link.to}
        state={link.state}
        // A catalog string, so each locale writes its own colon (fr: U+202F before it; UX R3 A2 on #10287).
        aria-label={
          label
            ? target.kind === "fleet" && target.archived
              ? t_i18n("{label}: {name} (archived)", { label, name: target.name })
              : t_i18n("{label}: {name}", { label, name: target.name })
            : undefined
        }
        className="inline-flex min-w-0 max-w-[160px] items-center gap-1 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <Badge variant="cyan" className="min-w-0 max-w-full px-1.5 py-0 hover:underline">
          <span className="truncate" title={target.name}>
            {target.name}
          </span>
        </Badge>
        {target.kind === "fleet" && target.archived && (
          <span className="shrink-0 text-default-secondary">({t_i18n("archived")})</span>
        )}
      </Link>
    </span>
  );
}
