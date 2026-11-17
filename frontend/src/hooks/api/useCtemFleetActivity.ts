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
 * The data of the CTEM command center's fleet activity (#9050 slice 2): the
 * fleets the reader can see, the health of each, and, for an administrator,
 * the ROI report of the window. The shape and the rules (seeded fleets first,
 * `null` for a figure not readable) live in `lib/ctem-fleet-activity.ts`.
 *
 * Every read tolerates a refusal: without the agentic AI licence the fleet
 * list is empty, a non-admin never asks for `GET /roi` (admin only), and a
 * failed health read leaves that tile's figures `null`. Without the licence
 * (`licensed` false, Community Edition) nothing is asked at all: the card
 * shows its Enterprise Edition state, so a refused `GET /fleets` on every load
 * would only fill the console (dev-docs/ctem-open-source.md, section 6).
 */

import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { dashboardFleets, fleetActivity, type ActivityFleetHealth, type FleetActivity, type FleetDefault, type RoiReport } from "@/lib/fleet-activity";
import { fleetTimeline, type TimelineEntry, type TimelineFleetActivity } from "@/lib/ctem-fleet-timeline";
import { FLEET_QUERY_POLICY, useFleets } from "./useFleetApi";

/** The ROI report of `period` (`GET /roi`, admin only). */
function useRoiReport(period: "7d" | "30d" | "90d" | "all", enabled: boolean) {
  return useQuery<RoiReport>({
    queryKey: ["roi", "report", period],
    queryFn: () => api.get(`/roi?window=${period}`),
    enabled,
    staleTime: 60_000,
    retry: false,
    throwOnError: false,
  });
}

/** The fleet activity tiles and totals of the command center. */
export function useCtemFleetActivity({
  isAdmin,
  licensed = true,
  chosenIds = null,
  fallback = "ctem",
  withTimeline = true,
}: {
  isAdmin: boolean;
  licensed?: boolean;
  /** The fleets the person chose for the page (#11464); null = the default (`fallback`). */
  chosenIds?: readonly string[] | null;
  /** The fleets shown with no choice saved: the CTEM ones, or every fleet (the Copilot home, #12063). */
  fallback?: FleetDefault;
  /** False: no per-fleet activity read (a home with no events timeline, #12063). */
  withTimeline?: boolean;
}): FleetActivity & { isLoading: boolean; timeline: TimelineEntry[]; timelineLoading: boolean } {
  const { data: fleetData, isLoading } = useFleets({ optional: true, enabled: licensed });
  // A list cached before the licence went away is not shown either.
  const fleets = licensed ? fleetData : undefined;
  // Only the fleets the page shows are read: their runs, findings and hours
  // saved are the page's (#11464).
  const active = dashboardFleets(fleets, chosenIds, fallback);
  const health = useQueries({
    queries: active.map((fleet) => ({
      // The same cache entry as `useFleetHealth`, so the fleet page and the
      // command center share one read.
      queryKey: ["fleets", "health", fleet.id],
      queryFn: () => api.get(`/fleets/${fleet.id}/health`) as Promise<ActivityFleetHealth>,
      ...FLEET_QUERY_POLICY,
      throwOnError: false,
      refetchInterval: 30_000,
    })),
  });
  const healthById: Record<string, ActivityFleetHealth | undefined> = {};
  active.forEach((fleet, i) => {
    healthById[fleet.id] = health[i]?.data;
  });
  // The members' latest runs per fleet (slice 3, the events timeline): what
  // each fleet did and what started it.
  const activity = useQueries({
    queries: (withTimeline ? active : []).map((fleet) => ({
      queryKey: ["fleets", "activity", fleet.id],
      queryFn: () => api.get(`/fleets/${fleet.id}/activity?limit=20`) as Promise<TimelineFleetActivity>,
      ...FLEET_QUERY_POLICY,
      throwOnError: false,
      refetchInterval: 30_000,
    })),
  });
  const activityById: Record<string, TimelineFleetActivity | undefined> = {};
  active.forEach((fleet, i) => {
    activityById[fleet.id] = activity[i]?.data;
  });
  const { data: roi } = useRoiReport("7d", isAdmin && active.length > 0);
  return {
    ...fleetActivity(fleets, healthById, roi ?? null, chosenIds, fallback),
    isLoading: licensed && isLoading,
    timeline: fleetTimeline(active, activityById, healthById),
    timelineLoading: (licensed && isLoading) || activity.some((q) => q.isLoading),
  };
}
