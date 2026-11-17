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

import { platformTriggerEnded } from "@/lib/platform-events";
import type { Assignment } from "@/types";
import { isAssignmentActive } from "./pause-reasons";

export type StatusAssignment = Pick<
  Assignment,
  "enabled" | "paused_by_owner_deactivation" | "paused_reason" | "trigger_type" | "trigger_config"
>;

/**
 * What the assignment page says about an assignment's state: `active`, `paused` (by hand, by the
 * platform, an emergency stop or a deactivated owner) or `ended`, an enabled platform event
 * trigger whose deadline passed or a one-off watch that fired: it no longer starts
 * runs, so it never reads "Active".
 */
export function assignmentDisplayStatus(assignment: StatusAssignment, now: Date = new Date()): AssignmentDisplayStatus {
  if (!isAssignmentActive(assignment)) return "paused";
  return platformTriggerEnded(assignment.trigger_type, assignment.trigger_config, now) ? "ended" : "active";
}

export type AssignmentDisplayStatus = "active" | "ended" | "paused";

/**
 * Icon tile and icon tint of an assignment by its displayed state (assignment page header, agent
 * page assignment list): accent when active, warning when ended, muted when paused.
 */
export function assignmentStatusTint(status: AssignmentDisplayStatus): { tile: string; icon: string } {
  if (status === "active") return { tile: "bg-accent/10", icon: "text-accent" };
  if (status === "ended") return { tile: "bg-feedback-warning-primary/10", icon: "text-feedback-warning-primary" };
  return { tile: "bg-elevation-hover", icon: "text-default-secondary" };
}
