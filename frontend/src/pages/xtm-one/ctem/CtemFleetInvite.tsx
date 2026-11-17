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
 * The one fleets invitation of the CTEM command center (UX round 1 on #10515,
 * B1): every CTEM fleet off, "Add agentic work to this page" names what each
 * fleet adds, its documented runs per month (`GET /fleets/{id}/documentation`,
 * `structured.quota`: the sum of its triggers' ranges, an estimate; nothing
 * shown when the fleet documents none) and Set up to the fleet page, closable
 * per person (`users.ctem_fleet_invite_dismissed`). Once closed it is one quiet
 * sentence. Fleet activity and the fleets row both draw it from here, so the
 * title, the controls (36 px) and the list never differ between the two; the
 * invitation's wording lives in this file only (fleet-invite-single-source test).
 *
 * Nothing here switches a fleet on, starts a run or uses quota: Set up only
 * opens the fleet page.
 */
import { useId, type ReactNode } from "react";
import { Chip, Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useFleetDocumentation } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { documentedRunRange, type FleetActivityTile } from "@/lib/fleet-activity";
import { cn } from "@/lib/utils";

/**
 * One fleet that is off: what it adds, its documented runs per month, and Set up.
 * The Fleets widget (#11996) draws its off fleets with this same row under its
 * tiles; a fleet switched off after it ran is `paused`: a Paused chip, and the
 * row's action (`action`) is the widget's Resume instead of Set up.
 */
export function InviteRow({
  tile,
  onSetUp,
  onHide,
  paused = false,
  action,
}: {
  tile: FleetActivityTile;
  onSetUp?: () => void;
  onHide?: () => void;
  paused?: boolean;
  /** Replaces Set up (the widget's Resume of a paused fleet); `null` draws no action. */
  action?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { data: documentation } = useFleetDocumentation(tile.id);
  const range = documentedRunRange(documentation?.structured?.quota);
  return (
    <li className="flex items-start gap-3 min-w-0 py-2" data-testid="ctem-fleet-invite-row">
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="flex flex-wrap items-center gap-2 content-compact font-semibold break-words">
          {tile.name}
          {paused && <Chip label={t_i18n("Paused")} severity="medium" data-testid="ctem-fleet-invite-paused" />}
        </p>
        {tile.description && <p className="content-compact text-default-secondary line-clamp-2">{tile.description}</p>}
        {range && (
          <Tip content={t_i18n("Estimate from the fleet's documented triggers; counts against your agentic quota once set up")}>
            <span tabIndex={0} className={cn("inline-block content-compact text-default-secondary tabular-nums", TIP_WRAP_FOCUS_RING)}>
              {t_i18n("About {min} to {max, plural, one {# run} other {# runs}} per month", { min: range.min, max: range.max })}
            </span>
          </Tip>
        )}
      </div>
      {action !== undefined
        ? action
        : onSetUp && (
            <Button variant="outline" className="h-9 shrink-0" onClick={onSetUp} aria-label={t_i18n("Set up {name}", { name: tile.name })}>
              {t_i18n("Set up")}
            </Button>
          )}
      {onHide && (
        <Tip content={t_i18n("Hide this fleet from the page")}>
          <Button variant="ghost" size="icon" className="h-9 w-9 shrink-0" onClick={onHide} aria-label={t_i18n("Hide {name} from this page", { name: tile.name })}>
            <Icon name="eye-off" size={16} />
          </Button>
        </Tip>
      )}
    </li>
  );
}

/**
 * Every CTEM fleet off: one compact invitation instead of tiles, closable. The
 * page keeps its value from the modules without any fleet.
 */
export function CtemFleetInvite({
  tiles,
  canSetUp,
  onSetUp,
  onDismiss,
  onHide,
}: {
  tiles: FleetActivityTile[];
  /** An administrator: the list of fleets with Set up; anyone else reads who sets them up. */
  canSetUp: boolean;
  onSetUp: (tile: FleetActivityTile) => void;
  onDismiss?: () => void;
  /** Hide one fleet from the page (#11464): the person's own choice of fleets. */
  onHide?: (tile: FleetActivityTile) => void;
}) {
  const { t_i18n } = useFormatter();
  const titleId = useId();
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-1" data-testid="ctem-fleet-invite">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h3 id={titleId} className="content-base font-semibold">
            {t_i18n("Add agentic work to this page")}
          </h3>
          <p className="content-compact text-default-secondary">
            {canSetUp
              ? t_i18n("Fleets are off: this page reads your connected modules only. Set up a fleet to add its runs, findings and hours saved here.")
              : t_i18n("Fleets are off: this page reads your connected modules only. An administrator can set them up.")}
          </p>
        </div>
        {onDismiss && (
          <Button variant="ghost" size="icon" className="h-9 w-9 shrink-0" onClick={onDismiss} aria-label={t_i18n("Close the fleets invitation")}>
            <Icon name="x" size={16} />
          </Button>
        )}
      </div>
      {canSetUp && (
        <ul className="grid gap-x-6 @xl:grid-cols-2">
          {tiles.map((tile) => (
            <InviteRow key={tile.id} tile={tile} onSetUp={() => onSetUp(tile)} onHide={onHide ? () => onHide(tile) : undefined} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** The invitation once closed: one quiet sentence; the card's header link is the way back to the fleets. */
export function CtemFleetInviteClosed() {
  const { t_i18n } = useFormatter();
  return (
    <p className="content-compact text-default-secondary" data-testid="ctem-fleet-invite-closed">
      {t_i18n("Fleets are off: this page reads your connected modules only.")}
    </p>
  );
}
