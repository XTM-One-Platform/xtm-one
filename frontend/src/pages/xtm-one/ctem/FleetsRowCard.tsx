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
 * Fleets row of the CTEM command center v2 (C2 slice V6, design
 * dev-docs/ctem-command-center-v2.md section 4.6): one compact line per CTEM
 * fleet, drawn at its content height.
 *
 * - Fleets off (the primary case): ONE invitation, "Add agentic work to this
 *   page", one line per fleet with its documented runs per month and Set up
 *   (administrators), closable per person (`users.ctem_fleet_invite_dismissed`)
 *   and back when a fleet or a module appears. Closed: one quiet sentence.
 * - A fleet on: its row says its opt-in state and its last outcome; a fleet
 *   still off in the same list keeps its Set up. The name opens the fleet's
 *   About page. No ROI figure here (#10721): the Value page carries them.
 * - Community Edition: one sentence with the one way forward of the lock.
 *
 * - The fleets are the person's choice (#11464): every row can be hidden, and
 *   "Choose fleets" picks any fleet they can see, built-in or their own, or
 *   restores the built-in ones (`users.ctem_dashboard_fleet_ids`).
 *
 * - #11547 slice S2d (design note dev-docs/ctem-stage-viz.md section 4, row
 *   fleets_row): a fleet that is on leads with a dot in its last outcome's
 *   colour and carries its runs per day over 30 days as a sparkline; the
 *   invitation state is unchanged.
 *
 * Every figure comes from the fleet health the page already reads
 * (`useCtemFleetActivity`). Nothing here switches a fleet on, starts a
 * run or uses quota: Set up only opens the fleet page.
 */
import { useEffect, useRef, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { fleetRefusalMessage } from "@/components/fleets/fleet-refusals";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { useFleetControl } from "@/hooks/api/useFleetApi";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useFormatter } from "@/i18n";
import { fleetsSummary, isFleetOn, lastTileSpan, type FleetActivity, type FleetActivityTile } from "@/lib/fleet-activity";
import { cn } from "@/lib/utils";
import { CtemFleetInvite, CtemFleetInviteClosed, InviteRow } from "./CtemFleetInvite";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import { FleetTile, FleetsGlyph } from "@/components/fleets/FleetTile";

export interface FleetsRowProps {
  activity: FleetActivity;
  isLoading: boolean;
  /** False in Community Edition: one sentence and the lock's way forward. */
  licensed?: boolean;
  lockReason?: AiLockReason | null;
  /** An administrator: Set up is offered on a fleet that is off. */
  canSetUp?: boolean;
  /** False once the person closed the invitation and nothing new appeared since. */
  inviteShown?: boolean;
  onDismissInvite?: () => void;
  /** The height its content needs, so the page sizes the card to it. */
  onContentHeight?: (px: number) => void;
  /** Open the fleet chooser (#11464); absent = no choice offered. */
  onChooseFleets?: () => void;
  /** Hide one fleet from the page (#11464). */
  onHideFleet?: (tile: FleetActivityTile) => void;
  /** The person sees no fleet at all (not one hidden): the designed empty state (#11996). */
  noFleet?: boolean;
}

const BACK_STATE = { backTo: "/", backLabel: "Back to Command Center" };

/** Resume a fleet the person switched off after it ran (`POST /fleets/{id}/resume`), as the Fleets list does. */
function ResumeButton({ tile }: { tile: FleetActivityTile }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const resume = useFleetControl(tile.id, "resume");
  const run = async () => {
    try {
      const result = await resume.mutateAsync();
      toast({
        title: t_i18n("{count, plural, =0 {No automation to resume} one {# automation resumed} other {# automations resumed}}", { count: result.changed }),
        variant: "success",
      });
    } catch (err) {
      toast({ title: t_i18n("The fleet's automations were not resumed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };
  return (
    <Button variant="outline" className="h-9 shrink-0 gap-1.5" disabled={resume.isPending} onClick={run} aria-label={t_i18n("Resume {name}", { name: tile.name })}>
      <Icon name="play" size={16} aria-hidden />
      {t_i18n("Resume")}
    </Button>
  );
}

/** The span of the last tile at the 2-column width, then at the 3-column one (static class names for the build). */
const SPAN_AT_TWO = { 1: "", 2: "@3xl:col-span-2" } as const;
const SPAN_AT_THREE = { 1: "@7xl:col-span-1", 2: "@7xl:col-span-2", 3: "@7xl:col-span-3" } as const;

export function FleetsRowCard({
  activity,
  isLoading,
  licensed = true,
  lockReason = null,
  canSetUp = false,
  inviteShown = true,
  onDismissInvite,
  onContentHeight,
  onChooseFleets,
  onHideFleet,
  noFleet = false,
}: FleetsRowProps) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const contentRef = useRef<HTMLDivElement>(null);
  const allFleetsRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const node = contentRef.current;
    if (!node || !onContentHeight) return;
    const report = () => onContentHeight(Math.ceil(node.getBoundingClientRect().height) + 2);
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);

  // A fleet opens on About, the page with no tab (#10203); Set up opens the same page.
  const open = (tile: FleetActivityTile) => navigate(`/fleets/${tile.id}`, { state: BACK_STATE });
  // Closing the invitation hands keyboard focus to the header link, never to `<body>`.
  const dismiss = onDismissInvite
    ? () => {
        onDismissInvite();
        requestAnimationFrame(() => allFleetsRef.current?.focus());
      }
    : undefined;

  // The header's one line (round 1): fleets on, their runs over 7 days and the share that succeeded.
  const summary = fleetsSummary(activity.tiles.filter((t) => isFleetOn(t.optIn)));
  const summaryRate = summary.rate == null ? null : intl.formatNumber(summary.rate, { style: "percent", maximumFractionDigits: 0 });
  const summaryText =
    licensed && summary.fleets > 0 && !activity.allOff
      ? [
          t_i18n("{count, plural, one {# fleet} other {# fleets}}", { count: summary.fleets }),
          t_i18n("{count, plural, one {# run in 7 days} other {# runs in 7 days}}", { count: summary.runs }),
          ...(summaryRate ? [t_i18n("{rate} succeeded", { rate: summaryRate })] : []),
        ].join(" · ")
      : null;

  const header = (
    <CtemWidgetHeader
      icon={<Icon name="network" size={16} aria-hidden />}
      title={t_i18n("Fleets")}
      count={summaryText}
      countTestId="ctem-fleets-row-summary"
      actions={
        licensed ? (
          <>
            {onChooseFleets && (
              <Button variant="ghost" size="sm" className="h-9 content-compact gap-1" onClick={onChooseFleets} data-testid="ctem-choose-fleets-open">
                <Icon name="settings" size={16} /> {t_i18n("Choose fleets")}
              </Button>
            )}
            <Button ref={allFleetsRef} variant="ghost" size="sm" className="h-9 content-compact gap-1" onClick={() => navigate("/fleets")}>
              <Icon name="arrow-up-right" size={16} aria-hidden /> {t_i18n("View all fleets")}
            </Button>
          </>
        ) : undefined
      }
    />
  );

  let body: ReactNode = null;
  if (!licensed) {
    body = (
      <div className="flex flex-wrap items-center justify-between gap-2" data-testid="ctem-fleets-row-ce">
        <p className="content-compact text-default-secondary">{t_i18n("Fleets run with Enterprise Edition")}</p>
        <AiLockedFooter reason={lockReason ?? "unlicensed"} />
      </div>
    );
  } else if (isLoading && activity.tiles.length === 0) {
    body = (
      <div className="grid gap-3 @3xl:grid-cols-2" aria-busy="true" data-testid="ctem-fleets-row-loading">
        <Skeleton className="h-64" />
        <Skeleton className="hidden h-64 @3xl:block" />
      </div>
    );
  } else if (activity.tiles.length === 0 && noFleet) {
    // No fleet at all yet: what a fleet is, and Create for whoever may (round 2, B7).
    body = (
      <div className="flex flex-col" data-testid="ctem-fleets-row-empty">
        <EmptyState
          variant="inset"
          icon={FleetsGlyph}
          title={t_i18n("No fleet yet")}
          description={
            canSetUp
              ? t_i18n("A fleet is a team of agents around an orchestrator that runs your recurring work.")
              : t_i18n("A fleet is a team of agents around an orchestrator. Ask an administrator to create one.")
          }
          actionLabel={canSetUp ? t_i18n("Create a fleet") : undefined}
          onAction={canSetUp ? () => navigate("/fleets") : undefined}
        />
      </div>
    );
  } else if (activity.tiles.length === 0 && onChooseFleets) {
    // Every fleet hidden: say so, with the way back (#11464).
    body = (
      <p className="content-compact text-default-secondary" data-testid="ctem-fleets-row-none">
        {t_i18n("No fleet is shown on this page. Choose fleets to add the built-in ones or your own.")}
      </p>
    );
  } else if (activity.allOff && !inviteShown) {
    body = <CtemFleetInviteClosed />;
  } else if (activity.allOff) {
    body = <CtemFleetInvite tiles={activity.tiles} canSetUp={canSetUp} onSetUp={open} onDismiss={dismiss} onHide={onHideFleet} />;
  } else if (activity.tiles.length > 0) {
    // Fleets on as tiles: 1 wide, 2 or 3 columns by the card's width, the last tile spanning the
    // free columns of its row at either width (#11996, round 1 B5 on #12015); fleets off as the invitation's rows under the grid, never a tile with no figure.
    // Each tile spans five rows of one subgrid (its five sections), so a value that wraps in one tile keeps the
    // sections of every tile of its row on one line (round 2 B1 on #12015).
    const on = activity.tiles.filter((t) => isFleetOn(t.optIn));
    const off = activity.tiles.filter((t) => !isFleetOn(t.optIn));
    const span = lastTileSpan(on.length);
    const lastClass = cn(SPAN_AT_TWO[span.two], on.length > 2 && SPAN_AT_THREE[span.three]);
    body = (
      <div className="flex flex-col gap-3">
        {on.length > 0 && (
          <ul
            className={cn("grid gap-3", on.length > 1 && "@3xl:grid-cols-2", on.length > 2 && "@7xl:grid-cols-3")}
            data-testid="ctem-fleets-row-list"
          >
            {on.map((tile, i) => (
              <FleetTile
                key={tile.id}
                tile={tile}
                onOpen={() => open(tile)}
                onHide={onHideFleet ? () => onHideFleet(tile) : undefined}
                className={i === on.length - 1 ? lastClass : undefined}
              />
            ))}
          </ul>
        )}
        {off.length > 0 && (
          <ul className="grid gap-x-6 border-t border-[var(--border-elevation-subtle-soft)] pt-1 @3xl:grid-cols-2" data-testid="ctem-fleets-row-off">
            {off.map((tile) => (
              <InviteRow
                key={tile.id}
                tile={tile}
                paused={tile.optIn === "paused"}
                onSetUp={canSetUp ? () => open(tile) : undefined}
                action={tile.optIn === "paused" ? canSetUp ? <ResumeButton tile={tile} /> : null : undefined}
                onHide={onHideFleet ? () => onHideFleet(tile) : undefined}
              />
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    <Card className="@container flex flex-col" data-testid="ctem-fleets-row">
      <CardContent ref={contentRef} className="flex flex-col gap-2 p-4">
        {header}
        {body}
      </CardContent>
    </Card>
  );
}
