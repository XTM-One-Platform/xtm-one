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

/** The status of a recording, clip or render, and its progress while it runs. */
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import type { VideoJobStatus, VideoRenderEta } from "@/types";

import { JOB_STATUS_LABELS, isUnfinished } from "./video-format";

const VARIANTS: Record<VideoJobStatus, "secondary" | "cyan" | "success" | "destructive" | "outline"> = {
  pending: "secondary",
  running: "cyan",
  ready: "success",
  failed: "destructive",
  cancelled: "outline",
};

export function JobStatusBadge({ status, progress }: { status: VideoJobStatus; progress?: number }) {
  const { t_i18n } = useFormatter();
  const label = t_i18n(JOB_STATUS_LABELS[status]);
  return (
    <Badge variant={VARIANTS[status]} className="tabular-nums">
      {isUnfinished(status) && progress ? `${label} ${progress}%` : label}
    </Badge>
  );
}

export function JobProgress({ progress, stage, eta }: { progress: number; stage: string | null; eta?: VideoRenderEta | null }) {
  return (
    <div className="space-y-1">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-elevation-hover">
        <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${Math.max(3, Math.min(progress, 100))}%` }} />
      </div>
      {stage && <p className="content-compact text-default-secondary">{stage}</p>}
      {eta && <RenderEta eta={eta} />}
    </div>
  );
}

/** When a whole render should end: its pre-flight's estimate, replaced by the pace its frames measure. */
function RenderEta({ eta }: { eta: VideoRenderEta }) {
  const { t_i18n, intl } = useFormatter();
  let said: string | null = null;
  if (eta.at) {
    const time = intl.formatTime(eta.at, { hour: "2-digit", minute: "2-digit" });
    said =
      eta.source === "measured"
        ? t_i18n("Ends about {time}, at the pace its first {drawn} frames measured", { time, drawn: intl.formatNumber(eta.drawn ?? 0) })
        : t_i18n("Ends about {time}, as its pre-flight estimated", { time });
  } else if (eta.expected_seconds) {
    said = t_i18n("Expected to take about {minutes} min once a renderer starts it", { minutes: Math.max(1, Math.ceil(eta.expected_seconds / 60)) });
  }
  if (!said) return null;
  return (
    <p className="content-compact text-default-secondary" data-testid="video.render.eta" data-source={eta.source}>
      {said}
    </p>
  );
}
