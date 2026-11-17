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
 * Fleet activity widget of the CTEM command center (#9050 slice 2): one tile
 * per fleet the reader can see (seeded fleets first), each figure read from
 * the fleet health and, for an administrator, the ROI report
 * (`lib/ctem-fleet-activity.ts`). A figure not readable shows a dash with a
 * tooltip saying why, never a 0; with no fleet the widget says what to set up.
 * In Community Edition (`licensed` false) it says what fleets do and carries
 * the one call to action of `AiLockedFooter` (the page's lock reason, so AI
 * off still says "Turn AI on" first), never "Open fleets" (a locked page):
 * dev-docs/ctem-open-source.md, section 6 (B3).
 *
 * Fleets are opt-in (#9566): the card covers the CTEM fleets only, each tile
 * says whether the fleet is set up (Not set up, Partially on, On, Paused)
 * and shows run health and figures only for a fleet that is on. With every
 * CTEM fleet off it is one compact invitation (what each fleet adds, its
 * documented runs per month, Set up), closable per person and shown again when
 * a fleet or a module appears; the rest of the page keeps its module data.
 *
 * UX round 1 on #9570: the invitation is a two-column list with no inner
 * scroll (B1), the runs per month come from the fleet documentation's
 * `structured.quota` range, never "No run expected" (B2), the "on, no run
 * yet" line wraps to two lines (B3), and a tile's badges sit under its name so
 * the name gets the tile's width, with three columns up to 1440 (B5).
 */
import { useRef } from "react";
import { Activity, ArrowUpRight, CheckCheck, Network } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { FleetOptInBadge } from "@/components/fleets/FleetOptInBadge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { exactCount, exactCurrency, formatCount, formatCurrency, isCompacted } from "@/lib/number-format";
import { fleetReviewPath } from "@/lib/roi-outcomes";
import { isFleetOn, type FleetActivity, type FleetActivityTile } from "@/lib/fleet-activity";
import { lastRunSentence } from "@/components/fleets/FleetTile";
import { CtemFleetInvite, CtemFleetInviteClosed } from "./CtemFleetInvite";

/** The English label of a fleet health status (`FleetHealth.status`), for a fleet that is on. @i18n-keys */
const STATUS_LABELS = {
  // Own key: the generic "Idle" reads as "off" in fr next to an "Active" fleet (UX round 2 on #9570, A1).
  idle: "Idle (fleet health)",
  stale: "Stale",
  failing: "Failing",
  disabled: "Disabled",
} as const;

const statusLabel = (status: string): string | undefined =>
  (STATUS_LABELS as Record<string, string | undefined>)[status];

const STATUS_VARIANTS: Record<string, "success" | "warning" | "destructive" | "secondary"> = {
  stale: "warning",
  failing: "destructive",
};

/**
 * One figure of a tile. A figure not readable is a dash that is a keyboard
 * focus stop (its tooltip opens on focus as on hover) and carries the reason
 * as screen-reader text, so neither a keyboard nor an assistive-tech reader
 * is left with a bare dash (#9133 A3).
 */
function Figure({ label, value, exact, why }: { label: string; value: string | null; exact?: string; why: string }) {
  return (
    <div className="min-w-0 h-full flex flex-col justify-between gap-0.5">
      <p className="content-caption text-default-secondary line-clamp-2 break-words">{label}</p>
      {value === null ? (
        <Tip content={why}>
          <span
            tabIndex={0}
            className={cn("self-start content-base font-semibold tabular-nums text-default-disabled", TIP_WRAP_FOCUS_RING)}
          >
            <span aria-hidden="true">-</span>
            <span className="sr-only">{why}</span>
          </span>
        </Tip>
      ) : (
        <p className="content-base font-semibold tabular-nums whitespace-nowrap" title={exact && exact !== value ? exact : undefined}>
          {value}
        </p>
      )}
    </div>
  );
}

export function FleetActivityCard({
  activity,
  isLoading,
  licensed = true,
  lockReason = null,
  canSetUp = false,
  inviteShown = true,
  onDismissInvite,
}: {
  activity: FleetActivity;
  isLoading: boolean;
  /** An administrator: the one who can switch a fleet on; the invitation lists the fleets with Set up. */
  canSetUp?: boolean;
  /** False once the person closed the invitation and nothing new appeared since. */
  inviteShown?: boolean;
  /** Close the invitation for this person (`users.ctem_fleet_invite_dismissed`). */
  onDismissInvite?: () => void;
  /** False in Community Edition: the card shows what fleets do, with the footer. */
  licensed?: boolean;
  /** Why AI cannot run (`useAiLockReason`), for the footer of the Community Edition state. */
  lockReason?: AiLockReason | null;
}) {
  const communityEdition = !licensed;
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  // Compact from 1,000 with the exact figure on hover (#10476): "1.2K h", never "1,234.5".
  const num = (v: number | null) => (v === null ? null : formatCount(intl.locale, v));
  const exactNum = (v: number | null) => (v === null ? undefined : exactCount(intl.locale, v));
  const healthWhy = t_i18n("Fleet health not read yet");
  const roiWhy = t_i18n("ROI figures are readable by administrators");
  // A fleet opens on About, the page with no tab (#10203).
  const open = (tile: FleetActivityTile) =>
    navigate(`/fleets/${tile.id}`, {
      state: { backTo: "/", backLabel: "Back to Command Center" },
    });
  const offWhy = t_i18n("Not shown while the fleet is not set up");
  // Closing the invitation hands keyboard focus to the header link, never to `<body>` (UX A2 on #9570).
  const allFleetsRef = useRef<HTMLButtonElement>(null);
  const dismissInvite = onDismissInvite
    ? () => {
        onDismissInvite();
        requestAnimationFrame(() => allFleetsRef.current?.focus());
      }
    : undefined;

  return (
    <Card className="@container h-full overflow-hidden flex flex-col">
      <CardContent className="p-4 flex flex-col flex-1 min-h-0 gap-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="content-base font-semibold flex items-center gap-2">
            <Network className="h-4 w-4 text-accent" /> {t_i18n("Fleet activity")}
          </h2>
          <div className="flex items-center gap-3 content-caption text-default-secondary">
            {!communityEdition && !activity.allOff && (
              <span className="tabular-nums whitespace-nowrap" title={exactNum(activity.totals.runsInFlight ?? 0) !== num(activity.totals.runsInFlight ?? 0) ? exactNum(activity.totals.runsInFlight ?? 0) : undefined}>
                {t_i18n("{count, plural, one {{shown} run in flight} other {{shown} runs in flight}}", {
                  count: activity.totals.runsInFlight ?? 0,
                  shown: num(activity.totals.runsInFlight ?? 0) ?? "",
                })}
              </span>
            )}
            {activity.totals.hoursSaved !== null && (
              <Tip
                content={
                  isCompacted(activity.totals.hoursSaved)
                    ? t_i18n("{hours} h saved over the last 7 days, from the ROI report (estimated until the baselines are set)", {
                        hours: exactNum(activity.totals.hoursSaved) ?? "",
                      })
                    : t_i18n("Hours saved over the last 7 days, from the ROI report (estimated until the baselines are set)")
                }
              >
                <span className="tabular-nums whitespace-nowrap">
                  {t_i18n("{hours} h saved (7 d)", { hours: num(activity.totals.hoursSaved) ?? "" })}
                </span>
              </Tip>
            )}
            {!communityEdition && (
              <Button ref={allFleetsRef} variant="ghost" size="sm" className="h-9 content-compact gap-1" onClick={() => navigate("/fleets")}>
                <ArrowUpRight className="h-3 w-3" /> {t_i18n("View all fleets")}
              </Button>
            )}
          </div>
        </div>
        {communityEdition ? (
          // No `requiresAi`: EmptyState would hide the footer with AI off.
          <EmptyState
            variant="inset"
            icon={Network}
            title={t_i18n("Fleets run with Enterprise Edition")}
            description={t_i18n("Fleets group the agents that work together; their runs, open work and hours saved show here.")}
          >
            <AiLockedFooter reason={lockReason ?? "unlicensed"} className="justify-center" />
          </EmptyState>
        ) : activity.allOff ? (
          inviteShown ? (
            <CtemFleetInvite
              tiles={activity.tiles}
              canSetUp={canSetUp}
              onSetUp={(tile) => open(tile)}
              onDismiss={dismissInvite}
            />
          ) : (
            // The card shrinks to this line on the command center (`withClosedFleetActivity`);
            // the header link is the way back to the fleets (UX round 3 on #9570, B1).
            <CtemFleetInviteClosed />
          )
        ) : activity.tiles.length === 0 ? (
          isLoading ? null : (
            <EmptyState
              variant="inset"
              icon={Network}
              title={t_i18n("No fleet yet")}
              description={t_i18n("Fleets group the agents that work together; their runs, open work and hours saved show here.")}
              actionLabel={t_i18n("Open fleets")}
              onAction={() => navigate("/fleets")}
            />
          )
        ) : (
          <div
            className={cn(
              // items-start: a paused or off tile keeps its own height instead of stretching to the row (UX round 2 A3).
              "grid gap-3 @xl:grid-cols-2 content-start items-start min-h-0 flex-1 overflow-y-auto p-1 -m-1",
              // Up to 3 fleets fill the row in 3 columns instead of leaving an empty fourth slot (#9133 A2);
              // more keep 3 columns up to a 1440 screen so each name reads whole (UX B5 on #9570).
              activity.tiles.length <= 3 ? "@4xl:grid-cols-3" : "@4xl:grid-cols-3 @7xl:grid-cols-4",
            )}
          >
            {activity.tiles.map((tile) => (
              <div
                key={tile.id}
                {...buttonRoleProps(() => open(tile))}
                aria-label={t_i18n("Open fleet {name}", { name: tile.name })}
                className={cn(
                  "rounded-sm ring-1 ring-[var(--border-elevation-subtle)] bg-elevation-hover/40 p-3 space-y-2.5 cursor-pointer hover:bg-elevation-hover transition-colors",
                  BUTTON_ROLE_FOCUS_RING,
                )}
              >
                <Tip content={tile.name}>
                  <p className="content-compact font-semibold truncate min-w-0">{tile.name}</p>
                </Tip>
                <div className="flex flex-wrap items-center gap-2 min-w-0">
                  {tile.builtin && (
                    <span className="shrink-0">
                      <BuiltinBadge size="sm" />
                    </span>
                  )}
                  {tile.optIn && <FleetOptInBadge optIn={tile.optIn} />}
                  {isFleetOn(tile.optIn) && tile.status && statusLabel(tile.status) && (
                    <Badge variant={STATUS_VARIANTS[tile.status] ?? "secondary"} className="shrink-0">
                      {t_i18n(statusLabel(tile.status) as string)}
                    </Badge>
                  )}
                </div>
                {isFleetOn(tile.optIn) || tile.optIn === null ? (
                  <>
                    <div className="grid grid-cols-3 gap-x-3 gap-y-2">
                      <Figure label={t_i18n("In flight")} value={num(tile.runsInFlight)} exact={exactNum(tile.runsInFlight)} why={healthWhy} />
                      <Figure label={t_i18n("Runs (24 h)")} value={num(tile.recentRuns)} exact={exactNum(tile.recentRuns)} why={healthWhy} />
                      <Figure label={t_i18n("Open work")} value={num(tile.openWorkItems)} exact={exactNum(tile.openWorkItems)} why={healthWhy} />
                      <Figure label={t_i18n("Units (7 d)")} value={num(tile.units)} exact={exactNum(tile.units)} why={tile.optIn === null ? healthWhy : roiWhy} />
                      <Figure label={t_i18n("Hours saved (7 d)")} value={num(tile.hoursSaved)} exact={exactNum(tile.hoursSaved)} why={tile.optIn === null ? healthWhy : roiWhy} />
                      <Figure
                        label={t_i18n("Net value (7 d)")}
                        value={
                          tile.netValue === null || tile.currency === null
                            ? null
                            : formatCurrency(intl.locale, tile.currency, tile.netValue)
                        }
                        exact={
                          tile.netValue === null || tile.currency === null ? undefined : exactCurrency(intl.locale, tile.currency, tile.netValue)
                        }
                        why={tile.optIn === null ? healthWhy : roiWhy}
                      />
                    </div>
                    <p className="content-caption text-default-secondary flex items-start gap-1.5">
                      <Activity className="h-3 w-3 shrink-0 mt-0.5" />
                      <span className="line-clamp-2 min-w-0">
                        {tile.lastRunAt
                          ? t_i18n(lastRunSentence(tile.lastRunStatus), { when: formatTimeAgo(intl, tile.lastRunAt) })
                          : tile.status === null
                            ? healthWhy
                            : t_i18n("On, no run yet: it runs when your connected data brings something to work on")}
                      </span>
                    </p>
                    {/* How much of the counted value a person checked, as every ROI view says it (#9807).
                        Below 100 % it opens the fleet's unreviewed runs, so a zero says what to do (#9837 A2). */}
                    {tile.reviewRate !== null && (
                      <p className="content-caption text-default-secondary flex items-start gap-1.5 tabular-nums" data-testid="fleet-tile-review-rate">
                        <CheckCheck className="h-3 w-3 shrink-0 mt-0.5" aria-hidden />
                        {tile.reviewRate < 1 ? (
                          <Link
                            to={fleetReviewPath(tile.id, "7d")}
                            state={{ backTo: "/", backLabel: "Back to Command Center" }}
                            onClick={(e) => e.stopPropagation()}
                            className={cn("min-w-0 rounded-sm text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
                            data-testid="fleet-tile-review-link"
                          >
                            {t_i18n("{rate} of the runs reviewed (7 d)", {
                              rate: intl.formatNumber(tile.reviewRate, { style: "percent", maximumFractionDigits: 0 }),
                            })}
                          </Link>
                        ) : (
                          <span className="min-w-0">
                            {t_i18n("{rate} of the runs reviewed (7 d)", {
                              rate: intl.formatNumber(tile.reviewRate, { style: "percent", maximumFractionDigits: 0 }),
                            })}
                          </span>
                        )}
                      </p>
                    )}
                  </>
                ) : (
                  <p className="content-caption text-default-secondary">
                    {tile.optIn === "paused" ? t_i18n("Paused: switched off after it ran. Open the fleet to switch it back on.") : offWhy}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
        {activity.roiAvailable && activity.estimated && activity.tiles.some((t) => isFleetOn(t.optIn)) && (
          <p className="content-caption text-default-secondary">
            {t_i18n("ROI figures are estimated from default baselines until your organization sets its own.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
