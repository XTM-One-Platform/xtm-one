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

import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { EmergencyStopBadge } from "@/components/ui/emergency-stop-badge";
import { Tip } from "@/components/ui/tooltip";
import { isEmergencyStopped } from "@/lib/emergency-stop";
import { useFormatter } from "@/i18n";
import type { Assignment } from "@/types";
import { AssignmentStatusBadge } from "../assignment-detail/AssignmentStatus";
import { OWNER_DEACTIVATED_SUBTITLE, PAUSE_REASON_SUBTITLES, isAutoPaused } from "../assignment-detail/pause-reasons";
import { Spinner } from "@filigran/design-system";

const ROW_BADGE = "text-[8px]";

/**
 * The state badge of a row of the agent page's assignment list. Running, an emergency stop, a
 * deactivated owner and an auto-pause keep their own badge; otherwise the row reuses the shared
 * AssignmentStatusBadge of the assignment page, so an ended platform trigger reads "Ended" on both
 * screens, never "Active" here and "Ended" there.
 */
export function AssignmentRowBadge({
  assignment,
  running,
  ownerPausedRemedy,
}: {
  assignment: Assignment;
  running: boolean;
  ownerPausedRemedy: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  if (running) {
    return (
      <Badge variant="cyan" className={`${ROW_BADGE} gap-0.5`}>
        <Spinner size="sm" tone="inherit" /> {t_i18n("Running")}
      </Badge>
    );
  }
  if (isEmergencyStopped(assignment)) return <EmergencyStopBadge className={ROW_BADGE} details={assignment.paused_details} />;
  if (assignment.paused_by_owner_deactivation) {
    return (
      <Tip
        content={
          <div className="space-y-1">
            <p>{t_i18n(OWNER_DEACTIVATED_SUBTITLE)}</p>
            <p>{ownerPausedRemedy}</p>
          </div>
        }
      >
        <Badge variant="outline" className={ROW_BADGE}>{t_i18n("Paused")}</Badge>
      </Tip>
    );
  }
  if (isAutoPaused(assignment)) {
    return (
      <Tip content={t_i18n((assignment.paused_reason && PAUSE_REASON_SUBTITLES[assignment.paused_reason]) || "triggers paused, cause not recorded")}>
        <Badge variant="destructive" className={ROW_BADGE}>{t_i18n("Auto-paused")}</Badge>
      </Tip>
    );
  }
  return <AssignmentStatusBadge assignment={assignment} className={ROW_BADGE} />;
}
