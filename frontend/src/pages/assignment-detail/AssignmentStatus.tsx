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

import { Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { StatCard } from "@/components/ui/stat-card";
import { isEmergencyStopped } from "@/lib/emergency-stop";
import { platformWatchState } from "@/lib/platform-events";
import { useFormatter } from "@/i18n";
import { assignmentDisplayStatus, type StatusAssignment } from "./assignment-status";
import { OWNER_DEACTIVATED_SUBTITLE, PAUSE_REASON_SUBTITLES } from "./pause-reasons";

/** The "Status" tile of the assignment overview. */
export function AssignmentStatusTile({ assignment }: { assignment: StatusAssignment }) {
  const { t_i18n } = useFormatter();
  const status = assignmentDisplayStatus(assignment);
  // The paused tile names the cause; the PausedBanner at the top of the page tells the whole story.
  // Subtitles stay English keys: StatCard translates a string subtitle itself.
  const pausedSubtitle = assignment.paused_by_owner_deactivation
    ? OWNER_DEACTIVATED_SUBTITLE
    : (assignment.paused_reason && PAUSE_REASON_SUBTITLES[assignment.paused_reason]) ||
      "triggers paused, cause not recorded";
  if (status === "ended") {
    return (
      <StatCard
        icon={Zap}
        iconClassName="text-feedback-warning-primary"
        label="Status"
        value={<span className="text-feedback-warning-primary">{t_i18n("Ended")}</span>}
        subtitle={platformWatchState(assignment.trigger_config) === "fired" ? "watch fired" : "deadline passed"}
      />
    );
  }
  return (
    <StatCard
      icon={Zap}
      label="Status"
      value={
        status === "active"
          ? t_i18n("Active")
          : isEmergencyStopped(assignment)
            ? t_i18n("Emergency stop")
            : t_i18n("Paused")
      }
      subtitle={status === "active" ? "accepting triggers" : pausedSubtitle}
    />
  );
}

/**
 * The state badge of an assignment: the assignment page header and the agent page's assignment
 * list (`className` sizes it there). An emergency stop has its own badge.
 */
export function AssignmentStatusBadge({ assignment, className }: { assignment: StatusAssignment; className?: string }) {
  const { t_i18n } = useFormatter();
  const status = assignmentDisplayStatus(assignment);
  if (status === "ended") return <Badge variant="warning" className={className}>{t_i18n("Ended")}</Badge>;
  return (
    <Badge variant={status === "active" ? "success" : "outline"} className={className}>
      {status === "active" ? t_i18n("Active") : t_i18n("Paused")}
    </Badge>
  );
}
