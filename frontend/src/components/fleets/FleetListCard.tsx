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
 * A card of the Agentic Flow > Fleets list (#9977 S2, mockups r3 APPROVE
 * under f88d65c6): every card the same height in an equal-column grid, never
 * expandable, few and visual facts on fixed lines so the rows align.
 *
 *   1. the name, its provenance marks;
 *   2. the opt-in badge (`FleetOptInBadge`, "N of M automations on" on hover)
 *      and the workspace it works in;
 *   3. the description, clamped to two lines, the whole text on hover;
 *   4. two icon lines: orchestrator | human approval, what starts it | last run;
 *   4bis. the fleet's net value over 30 days (#9836 R6c, design 10.8): the
 *      list cell's `RoiListCell` (shared `RoiFigure`, "How this is computed"),
 *      fed by the page's one `GET /roi/summary` read; absent on Community
 *      Edition (`roi` undefined), so the row is never a dead end;
 *   5. the modules row (`ProviderLogo`), one line, "+N" with the full list on
 *      hover and focus, "No modules" muted when none;
 *   6. the footer pinned to the bottom: members, connected, and the one action
 *      the status calls for (Resume, Set up), outside the card's link and only
 *      for a reader who may switch the fleet (`canControlFleet`).
 *
 * The card's link is the name, stretched over the card (`after:inset-0`), so
 * the hover tips and the "+N" modules sit above it, reachable by keyboard and
 * never nested in an anchor (UX r1 B4 on #10010). Modules read their display
 * name (`moduleDisplayName`), never the provider id (B1). What sits above the
 * stretched link only to host a hover tip (the description, the status, the
 * autonomy line) still opens the fleet: `openFromCardClick` on the card makes
 * the whole card one target (#10547), a text drag and the interactive children
 * excepted.
 *
 * Opening the list starts no run: Set up opens the one fleet setup
 * (`FleetSetupDialog`, #1151), which confirms before anything is enabled.
 */
import { useState, type MouseEvent } from "react";
import { Link, useNavigate, type NavigateFunction } from "react-router-dom";
import { Boxes, Clock, Crown, FolderKanban, Gauge, Link2, Play, Power, TrendingUp, Users, Zap } from "lucide-react";

import { RoiListCell } from "@/components/roi/RoiListCell";

import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { SharedIcon } from "@/components/ui/shared-badge";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useFleetControl, type Fleet } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import type { RoiSummaryItem } from "@/hooks/api/useRoiReportApi";
import { cardStartsAndModules, moduleDisplayName, type FleetStarts } from "@/lib/fleet-card-facts";
import { ROI_COLUMN_HEADERS } from "@/lib/roi-summary";
import { cn, isEntityShared, timeAgo } from "@/lib/utils";

import { autonomyStatusLabel, effectiveAutonomyLevel } from "./fleet-autonomy";
import { fleetControlState } from "./fleet-control";
import { FleetHealthSettingsLink, FleetModuleLogos, useFleetStartsLine } from "./FleetHealthStarts";
import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetSetupDialog } from "./FleetSetupDialog";
import { FleetOptInBadge } from "./FleetOptInBadge";

/** What keeps its own action on a card: a click on it never opens the fleet. */
const CARD_INTERACTIVE = "a[href], button, input, select, textarea, label, [role='button'], [role='checkbox'], [role='link'], [tabindex]";

/**
 * A click that lands on the card outside the name link (a hover-tip host above the
 * stretched link, #10547) opens the fleet like the link would: a plain click in the
 * tab, Ctrl / Cmd / Shift or the middle button in a new tab. Never on an interactive
 * child, never after a drag that selected text, never from a portal (a dialog the
 * card opened bubbles through React, not through the DOM).
 */
function openFromCardClick(event: MouseEvent<HTMLElement>, to: string, navigate: NavigateFunction) {
  if (event.defaultPrevented || (event.button !== 0 && event.button !== 1)) return;
  const target = event.target;
  if (!(target instanceof Element) || !event.currentTarget.contains(target)) return;
  if (target.closest(CARD_INTERACTIVE)) return;
  if (window.getSelection()?.toString()) return;
  if (event.button === 1 || event.ctrlKey || event.metaKey || event.shiftKey) {
    window.open(to, "_blank", "noopener,noreferrer");
    return;
  }
  navigate(to);
}

function FleetCardAction({ fleet, fleetName }: { fleet: Fleet; fleetName: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const resume = useFleetControl(fleet.id, "resume");
  const [enableOpen, setEnableOpen] = useState(false);
  const state = fleetControlState(fleet.automations);
  if (!state || (!state.canResume && !state.canEnable)) return null;
  const doResume = async () => {
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
    <>
      {state.canResume ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="gap-1.5"
          disabled={resume.isPending}
          onClick={doResume}
          aria-label={t_i18n("Resume {name}", { name: fleetName })}
          data-testid="fleet-card-resume"
        >
          <Play className="h-4 w-4" aria-hidden />
          {t_i18n("Resume")}
        </Button>
      ) : (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={() => setEnableOpen(true)}
          aria-label={t_i18n("Set up {name}", { name: fleetName })}
          data-testid="fleet-card-set-up"
        >
          <Power className="h-4 w-4" aria-hidden />
          {t_i18n("Set up")}
        </Button>
      )}
      <FleetSetupDialog fleetId={fleet.id} fleetName={fleetName} fleetSlug={fleet.slug} open={enableOpen} onOpenChange={setEnableOpen} />
    </>
  );
}

export function FleetListCard({
  fleet,
  orchestratorName,
  userId,
  selected,
  onToggle,
  modules: memberModules,
  starts,
  canControl,
  roi,
}: {
  fleet: Fleet;
  orchestratorName: string | null;
  userId?: string;
  selected: boolean;
  /** Absent: the reader cannot select this fleet. */
  onToggle?: () => void;
  /** The integration providers its members use, sorted (`fleetModules`). */
  modules: readonly string[];
  /** What starts it (`fleetStarts`). */
  starts: FleetStarts;
  /** `canControlFleet`: the card offers its status action. */
  canControl: boolean;
  /** The fleet's row of the list's `GET /roi/summary` read; absent on Community Edition (no value line). */
  roi?: { item: RoiSummaryItem | undefined; currency: string; costMasked: boolean; loading: boolean };
}) {
  const { t_i18n, locale } = useFormatter();
  const navigate = useNavigate();
  const fleetPath = `/fleets/${fleet.id}`;
  const level = effectiveAutonomyLevel(fleet.autonomy_level);
  // The About chip's words (#10457): the level only where it gates an action, never an untrue status.
  const levelLabel = autonomyStatusLabel(level, fleet.gated_action_count, fleet.instructions_position, t_i18n);
  const state = fleetControlState(fleet.automations);
  // The Platform Health fleet starts from its Settings schedules and signals, never its manual assignments (#974).
  const healthStarts = fleet.health_starts ?? null;
  // Its modules are the ones Platform Health covers (XTM One modes; none in Copilot mode), not its members' bindings.
  const { starts: shownStarts, modules } = cardStartsAndModules(healthStarts, starts, memberModules);
  // Platform Health off starts nothing: the line says so, never a next run (UX r1 B2 on #980).
  const startsLine = useFleetStartsLine(shownStarts, healthStarts);
  const names = modules.map((m) => moduleDisplayName(m));
  const lastRun = fleet.last_run_at
    ? // A short verb, not "Last run": the fr line at a 1024 px card cut the value (UX r3 C1 on #9977).
      // Own key: the fleet is feminine in fr and es ("Ran {time}" is the masculine audit line).
      t_i18n("Ran {time} (fleet card)", { time: timeAgo(fleet.last_run_at, locale) })
    : t_i18n("Never run");
  // The box and the action sit beside the card's link, never inside it: a button nested in an anchor is invalid.
  return (
    // The pointer twin of the name link (#10547); the keyboard reaches the fleet through that link.
    <div
      className="relative h-full"
      data-testid="fleet-card"
      onClick={(event) => openFromCardClick(event, fleetPath, navigate)}
      onAuxClick={(event) => openFromCardClick(event, fleetPath, navigate)}
    >
      {onToggle && (
        <SelectionBox
          checked={selected}
          onToggle={onToggle}
          label={t_i18n("Select {name}", { name: fleet.name })}
          className="absolute left-4 top-4 z-10"
        />
      )}
      <Card className="h-full cursor-pointer border-elevation-subtle transition-all hover:bg-elevation-hover focus-within:border-accent/30">
          <CardContent className="flex h-full flex-col gap-3 p-4">
            <div className={onToggle ? "flex items-start gap-3 pl-7" : "flex items-start gap-3"}>
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                <Boxes className="h-5 w-5 text-accent" aria-hidden />
              </div>
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex min-w-0 items-center gap-1.5">
                  <Link
                    to={fleetPath}
                    className="block min-w-0 truncate rounded-sm content-base font-medium after:absolute after:inset-0 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                    title={fleet.name}
                  >
                    {fleet.name}
                  </Link>
                  {/* The marks never wrap: the name truncates first, so every status row sits on one y (UX r1 B2). */}
                  {isEntityShared(fleet, userId) && (
                    <span className="relative z-10 flex shrink-0">
                      <SharedIcon ownerName={fleet.owner_name} />
                    </span>
                  )}
                  {/* One provenance mark, as on the Templates cards: a built-in fleet is company-managed by construction. */}
                  {fleet.is_builtin ? (
                    <span className="relative z-10 flex shrink-0 whitespace-nowrap" data-testid="fleet-card-builtin">
                      <BuiltinBadge size="sm" />
                    </span>
                  ) : (
                    fleet.is_company_managed && (
                      <span className="relative z-10 flex shrink-0">
                        <CompanyManagedIcon label="Company-managed fleet" />
                      </span>
                    )
                  )}
                </div>
                <div className="flex min-w-0 items-center gap-2 content-compact text-default-secondary">
                  {state && fleet.automations && (
                    <Tip
                      content={t_i18n("{enabled} of {total, plural, one {# automation} other {# automations}} on", {
                        enabled: fleet.automations.enabled,
                        total: fleet.automations.total,
                      })}
                    >
                      <span className="relative z-10 shrink-0" data-testid="fleet-card-status">
                        <FleetOptInBadge optIn={state.optIn} />
                      </span>
                    </Tip>
                  )}
                  {fleet.workspace_name && (
                    <span className="flex min-w-0 items-center gap-1" title={fleet.workspace_name}>
                      <FolderKanban className="h-3 w-3 shrink-0" aria-hidden />
                      <span className="truncate">{t_i18n("Works in {name}", { name: fleet.workspace_name })}</span>
                    </span>
                  )}
                </div>
              </div>
            </div>

            <Tip content={fleet.description || t_i18n("No description")}>
              <p className="relative z-10 line-clamp-2 min-h-[2lh] content-compact text-default-secondary" data-testid="fleet-card-description">
                {fleet.description || t_i18n("No description")}
              </p>
            </Tip>

            <div className="grid grid-cols-2 gap-x-3 gap-y-1 content-compact text-default-secondary">
              <span className="flex min-w-0 items-center gap-1.5">
                <Crown className="h-3 w-3 shrink-0" aria-hidden />
                <span className="truncate" title={orchestratorName ?? undefined}>{orchestratorName ?? t_i18n("No orchestrator")}</span>
              </span>
              <span className="flex min-w-0 items-center gap-1.5" data-testid="fleet-card-last-run">
                <Clock className="h-3 w-3 shrink-0" aria-hidden />
                <span className="truncate" title={lastRun}>{lastRun}</span>
              </span>
              {/* The Platform Health line wraps instead of cutting its next time (UX r1 B4 on #980, fr at 1080). */}
              <span
                className={cn("col-span-2 flex min-w-0 items-center gap-x-1.5 gap-y-0.5", healthStarts && "flex-wrap")}
                data-testid="fleet-card-starts"
              >
                <Zap className="h-3 w-3 shrink-0" aria-hidden />
                {healthStarts ? (
                  <span className="min-w-0">{startsLine}</span>
                ) : (
                  <span className="truncate" title={startsLine}>{startsLine}</span>
                )}
                {healthStarts && (
                  // Its schedules live in Settings > Platform Health, the one place they change (#974).
                  <FleetHealthSettingsLink healthStarts={healthStarts} testId="fleet-card-health-settings" className="relative z-10" />
                )}
              </span>
              {/* Its own full-width line on every card (UX r1 B1/B2 of #10462): "<position>, by instruction" and the fr
                  status do not fit half a card; the same rows on every card keep the heights equal. */}
              <span className="relative z-10 col-span-2 flex min-w-0 items-center gap-1.5" data-testid="fleet-card-autonomy">
                <Gauge className="h-3 w-3 shrink-0" aria-hidden />
                <Tip content={t_i18n("Human approval: {level}", { level: levelLabel })}>
                  {/* relative: the sr-only sentence is clipped by this span and never widens the page (UX r1 B3). */}
                  <span className="relative truncate">
                    <span aria-hidden>{levelLabel}</span>
                    <span className="sr-only">{t_i18n("Human approval: {level}", { level: levelLabel })}</span>
                  </span>
                </Tip>
              </span>
              {roi && (
                // One line on every card, one height (h-7) whether it holds a figure (28 px "How this is computed"
                // button) or "-", so neighbouring cards keep their rows aligned. The figure's button sits above the card's link.
                <span className="col-span-2 flex h-7 min-w-0 items-center gap-1.5" data-testid="fleet-card-value">
                  <TrendingUp className="h-3 w-3 shrink-0" aria-hidden />
                  <span className="shrink-0">{t_i18n(roi.costMasked ? ROI_COLUMN_HEADERS.value : ROI_COLUMN_HEADERS.net)}</span>
                  <span className="relative z-10 min-w-0 tabular-nums text-default-primary">
                    <RoiListCell item={roi.item} currency={roi.currency} costMasked={roi.costMasked} loading={roi.loading} />
                  </span>
                </span>
              )}
            </div>

            <div
              className="relative z-10 flex h-6 w-fit max-w-full min-w-0 items-center gap-2"
              data-testid="fleet-card-modules"
              aria-label={modules.length ? t_i18n("Modules: {names}", { names: names.join(", ") }) : undefined}
            >
              {modules.length === 0 ? (
                // Copilot mode: Platform Health covers no module, so its line stays empty (the row keeps the card heights equal).
                healthStarts ? null : <span className="content-compact text-default-secondary">{t_i18n("No modules")}</span>
              ) : (
                // Focusable outside the card's link: the "+N" names open on focus (UX r1 B4).
                <FleetModuleLogos modules={modules} testId="fleet-card-modules" />
              )}
            </div>

            <div className="mt-auto flex h-8 min-w-0 items-center gap-3 border-t border-elevation-subtle-soft pt-3 content-compact text-default-secondary">
              <span className="flex shrink-0 items-center gap-1">
                <Users className="h-3 w-3 shrink-0" aria-hidden />
                {t_i18n("{count, plural, one {# member} other {# members}}", { count: fleet.member_count })}
              </span>
              {fleet.connected_count > 0 && (
                <span className="flex shrink-0 items-center gap-1">
                  <Link2 className="h-3 w-3 shrink-0" aria-hidden />
                  {t_i18n("{count} connected", { count: fleet.connected_count })}
                </span>
              )}
            </div>
          </CardContent>
      </Card>
      {canControl && !healthStarts && (
        <div className="absolute bottom-3 right-4 z-10">
          <FleetCardAction fleet={fleet} fleetName={fleet.name} />
        </div>
      )}
    </div>
  );
}
