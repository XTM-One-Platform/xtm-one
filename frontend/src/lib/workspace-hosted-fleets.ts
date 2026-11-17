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
 * How many fleets a workspace hosts. A fleet lives in one workspace,
 * its coordination: any hosted fleet blocks the workspace's delete, a live
 * (not archived) one blocks its archive. The API sends the counts of every
 * fleet, seen or not, and names only the ones the reader can see; an older
 * response without the counts falls back on the named list.
 */
import type { Workspace } from "@/types";

/** How many fleets the workspace hosts, seen or not. */
export function hostedFleetCount(workspace: Workspace): number {
  return Math.max(workspace.hosted_fleet_count ?? 0, workspace.hosted_fleets?.length ?? 0);
}

/** How many live (not archived) fleets the workspace hosts. */
export function liveHostedFleetCount(workspace: Workspace): number {
  const named = (workspace.hosted_fleets ?? []).filter((f) => !f.is_archived).length;
  return Math.max(workspace.live_hosted_fleet_count ?? 0, named);
}
