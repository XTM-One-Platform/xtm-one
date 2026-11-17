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
 * The Fleets widget of the Copilot home (#12063, UX design rounds 1 and 2):
 * domain neutral, drawn with the shared `FleetTile` of the command center's
 * Fleets row (#11996), never a copy of it.
 *
 * - One grid row: 2 tiles below 1000 px of card, 3 from 1000 px; the header
 *   says "2 of 3 fleets" when the row holds fewer than the fleets shown, and
 *   its runs and rate count the tiles drawn (round 2 B2).
 * - With no choice saved, every fleet the person sees, attention first
 *   (failing, stale work items, automations off) then by 7-day runs
 *   (`byAttention`, round 2 B3); a saved choice keeps its own order.
 * - No fleet at all: what a fleet is in one line, Create a fleet for an
 *   administrator, else Open Fleets; one body action, no header link (B1).
 * - No fleet on: a neutral sentence over the fleets' set-up rows (no CTEM
 *   invitation in Copilot mode, design D3).
 * - Community Edition: the lock sentence and its way forward.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { Skeleton } from "@/components/ui/skeleton";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { byAttention, fleetsSummary, isFleetOn, type FleetActivity, type FleetActivityTile } from "@/lib/fleet-activity";
import { cn } from "@/lib/utils";
import { InviteRow } from "@/pages/xtm-one/ctem/CtemFleetInvite";
import { CtemWidgetHeader } from "@/pages/xtm-one/ctem/CtemWidgetHeader";
import { FleetTile, FleetsGlyph, OpenGlyph } from "./FleetTile";

/** The card width from which the row holds three tiles (round 2 B2). */
const THREE_TILES_FROM_PX = 1000;

/** How many tiles one row holds at `widthPx` of card (unmeasured: two). */
export function tilesPerRow(widthPx: number | null): 2 | 3 {
  return widthPx != null && widthPx >= THREE_TILES_FROM_PX ? 3 : 2;
}

/** The tiles drawn: the fleets on, in the default attention order unless the person chose an order, capped to one row. */
export function shownTiles(tiles: readonly FleetActivityTile[], ordered: boolean, perRow: number): FleetActivityTile[] {
  const on = tiles.filter((t) => isFleetOn(t.optIn));
  return (ordered ? on : byAttention(on)).slice(0, perRow);
}

const BACK_STATE = { backTo: "/", backLabel: "Back to Home" };

export interface CopilotFleetsCardProps {
  activity: FleetActivity;
  isLoading: boolean;
  /** False in Community Edition: one sentence and the lock's way forward. */
  licensed?: boolean;
  lockReason?: AiLockReason | null;
  /** An administrator: Create a fleet and Set up are offered. */
  canSetUp?: boolean;
  /** The person saved a choice: its order wins over the attention order. */
  ordered?: boolean;
  /** The person sees no fleet at all (not one hidden). */
  noFleet?: boolean;
  onChooseFleets?: () => void;
  onHideFleet?: (tile: FleetActivityTile) => void;
  /** The height its content needs, so the page sizes the widget to it. */
  onContentHeight?: (px: number) => void;
}

export function CopilotFleetsCard({
  activity,
  isLoading,
  licensed = true,
  lockReason = null,
  canSetUp = false,
  ordered = false,
  noFleet = false,
  onChooseFleets,
  onHideFleet,
  onContentHeight,
}: CopilotFleetsCardProps) {
  const { t_i18n } = useFormatter();
  const numbers = useNumberFormat();
  const navigate = useNavigate();
  const contentRef = useRef<HTMLDivElement>(null);
  const [widthPx, setWidthPx] = useState<number | null>(null);

  useEffect(() => {
    const node = contentRef.current;
    if (!node) return;
    const report = () => {
      const box = node.getBoundingClientRect();
      setWidthPx(Math.round(box.width) || null);
      onContentHeight?.(Math.ceil(box.height) + 2);
    };
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);

  const open = (tile: FleetActivityTile) => navigate(`/fleets/${tile.id}`, { state: BACK_STATE });
  const on = activity.tiles.filter((t) => isFleetOn(t.optIn));
  const shown = shownTiles(activity.tiles, ordered, tilesPerRow(widthPx));
  const empty = activity.tiles.length === 0;

  // The header's one line: the fleets drawn (of all shown when the row is capped), their runs and rate.
  const summary = fleetsSummary(shown);
  const rate = summary.rate == null ? null : numbers.percent(summary.rate);
  const fleetsPart =
    shown.length < on.length
      ? t_i18n("{shown} of {count, plural, one {# fleet} other {# fleets}}", { shown: shown.length, count: on.length })
      : t_i18n("{count, plural, one {# fleet} other {# fleets}}", { count: shown.length });
  const summaryText =
    licensed && shown.length > 0
      ? [
          fleetsPart,
          t_i18n("{count, plural, one {# run in 7 days} other {# runs in 7 days}}", { count: summary.runs }),
          ...(rate ? [t_i18n("{rate} succeeded", { rate })] : []),
        ].join(" · ")
      : null;

  // The header links only where fleets are listed: never in the empty states nor in CE (round 2 B1).
  const headerActions =
    licensed && !(empty && noFleet) ? (
      <>
        {onChooseFleets && (
          <Button variant="ghost" size="sm" className="h-9 content-compact gap-1" onClick={onChooseFleets} data-testid="copilot-choose-fleets-open">
            <Icon name="settings" size={16} /> {t_i18n("Choose fleets")}
          </Button>
        )}
        <Button variant="ghost" size="sm" className="h-9 content-compact gap-1" onClick={() => navigate("/fleets")}>
          <Icon name="arrow-up-right" size={16} aria-hidden /> {t_i18n("View all fleets")}
        </Button>
      </>
    ) : undefined;

  let body: ReactNode;
  if (!licensed) {
    body = (
      <div className="flex flex-wrap items-center justify-between gap-2" data-testid="copilot-fleets-ce">
        <p className="content-compact text-default-secondary">{t_i18n("Fleets run with Enterprise Edition")}</p>
        <AiLockedFooter reason={lockReason ?? "unlicensed"} />
      </div>
    );
  } else if (isLoading && empty) {
    body = (
      <div className="grid gap-3 @3xl:grid-cols-2" aria-busy="true" data-testid="copilot-fleets-loading">
        <Skeleton className="h-64" />
        <Skeleton className="hidden h-64 @3xl:block" />
      </div>
    );
  } else if (empty && noFleet) {
    body = (
      <div className="flex flex-col" data-testid="copilot-fleets-empty">
        <EmptyState
          variant="inset"
          icon={FleetsGlyph}
          title={t_i18n("No fleet yet")}
          description={t_i18n("A fleet is a team of agents around an orchestrator that runs your recurring work.")}
          actionLabel={canSetUp ? t_i18n("Create a fleet") : t_i18n("Open Fleets")}
          actionIcon={canSetUp ? undefined : OpenGlyph}
          onAction={() => navigate("/fleets")}
        />
      </div>
    );
  } else if (empty) {
    body = (
      <p className="content-compact text-default-secondary" data-testid="copilot-fleets-none">
        {t_i18n("No fleet is shown on this page. Choose fleets to add the built-in ones or your own.")}
      </p>
    );
  } else if (on.length === 0) {
    // No fleet on: a neutral sentence over each fleet's set-up row (design D3, no CTEM invitation).
    body = (
      <div className="flex flex-col gap-1" data-testid="copilot-fleets-all-off">
        <p className="content-compact font-semibold">{t_i18n("Your fleets are ready to set up")}</p>
        <p className="content-compact text-default-secondary">
          {t_i18n(
            "{count, plural, =0 {No fleet runs yet.} one {None of the # fleet runs yet. Set it up to see its activity here.} other {None of the # fleets runs yet. Set one up to see its activity here.}}",
            { count: activity.tiles.length },
          )}
        </p>
        <ul className="grid gap-x-6 @3xl:grid-cols-2">
          {activity.tiles.map((tile) => (
            <InviteRow
              key={tile.id}
              tile={tile}
              paused={tile.optIn === "paused"}
              onSetUp={canSetUp ? () => open(tile) : undefined}
              onHide={onHideFleet ? () => onHideFleet(tile) : undefined}
            />
          ))}
        </ul>
      </div>
    );
  } else {
    // One grid row of tiles (round 2 B2); each tile spans five rows of the subgrid so its sections line up.
    body = (
      <ul
        className={cn("grid gap-3", shown.length > 1 && "grid-cols-2", shown.length > 2 && "grid-cols-3")}
        data-testid="copilot-fleets-list"
      >
        {shown.map((tile) => (
          <FleetTile key={tile.id} tile={tile} onOpen={() => open(tile)} onHide={onHideFleet ? () => onHideFleet(tile) : undefined} />
        ))}
      </ul>
    );
  }

  return (
    <Card className="@container flex flex-col" data-testid="copilot-fleets">
      <CardContent ref={contentRef} className="flex flex-col gap-2 p-4">
        <CtemWidgetHeader
          icon={<Icon name="network" size={16} aria-hidden />}
          title={t_i18n("Fleets")}
          count={summaryText}
          countTestId="copilot-fleets-summary"
          testId="copilot-fleets-header"
          actions={headerActions}
        />
        {body}
      </CardContent>
    </Card>
  );
}
