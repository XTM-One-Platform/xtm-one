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
 * The quality check in the editor: what the audience would see go
 * wrong in the saved video - text covering text, text past the frame or
 * shrunk to fit, words on screen too briefly to read, subtitles too fast -
 * measured by playing the video in the render's compositor after each save,
 * the check the assistant reads in its tool result - and the pictures and the
 * sound: a shot failed or still being made, a locked-off shot whose
 * camera moved, an image smaller than the frame, a screen insert leaving the
 * frame, music ending early, a narration with no voice, a shot or a recording
 * still holding its last frame under a longer narration. One line until
 * opened; a finding opens its scene. Nothing when the video is clean or the
 * check is switched off; without a browser on the server, only the pictures'
 * and the sound's findings. `Fix with my assistant` hands the findings
 * to the studio rail as a primed message, like the deck's layout check.
 */
import { useState } from "react";
import { ChevronDown, ScanEye, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoQualityFinding, VideoQualityReport } from "@/types";

/** @i18n-keys */
const SEVERITY_LABELS = { overloaded: "Unreadable", dense: "Hard to read", advisory: "Could be better" } as const;
/** The findings about the pictures and the sound (`group: "content"`): kept even when the layout was not measured. @i18n-keys */
const CONTENT_SEVERITY_LABELS = { overloaded: "Blocks the render", dense: "Shows in the video", advisory: "To check" } as const;
/** `MAX_REPORTED_FINDINGS` in `services/video_studio/quality.py`: the primed message keeps the agent's bound. */
const MAX_PRIMED_FINDINGS = 8;

/** A finding's scene as `_where` in `services/video_studio/quality.py` names it: the spec index operations use and the id. */
function sceneReference(finding: VideoQualityFinding): string {
  const kind = finding.scene_type || "scene";
  const where = finding.scene_index === null ? `the template's ${kind} scene` : `scene ${finding.scene_index} [${finding.scene_id}] ${kind}`;
  return finding.label ? `${where} "${finding.label}"` : where;
}

/** The findings as a message for the studio, in the words of the `Quality check` section the agent reads after a save. */
function primedMessage(findings: VideoQualityFinding[], intro: string, more: (hidden: number) => string): string {
  const lines = findings
    .slice(0, MAX_PRIMED_FINDINGS)
    .map((finding) => `- ${sceneReference(finding)} (${finding.severity}): ${finding.message}`);
  const hidden = findings.length - MAX_PRIMED_FINDINGS;
  if (hidden > 0) lines.push(`- ${more(hidden)}`);
  return `${intro}\n${lines.join("\n")}`;
}

export function QualityStrip({
  report,
  onOpenScene,
  onFixWithAssistant,
}: {
  report: VideoQualityReport | undefined;
  onOpenScene: (sceneId: string) => void;
  /** Open the studio rail with this message in its composer (editors only). Absent: no button. */
  onFixWithAssistant?: (message: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const aiOffReason = useAiDisabledReason();
  const [open, setOpen] = useState(false);
  // A report whose layout was not measured still carries the pictures' and the sound's findings.
  const findings = report ? report.findings : [];
  if (!findings.length) return null;
  const blocking = findings.some((f) => f.severity !== "advisory");
  return (
    <div
      className={cn(
        "border-b px-3 py-1.5 text-[11px]",
        blocking ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/10" : "border-elevation-subtle-soft bg-elevation-hover",
      )}
      data-testid="video.editor.quality"
    >
      <div className="flex items-center gap-2">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          <ScanEye className={cn("h-3.5 w-3.5 shrink-0", blocking ? "text-feedback-warning-primary" : "text-default-secondary")} />
          <span className="min-w-0 flex-1 truncate font-medium">
            {t_i18n("Quality check: {count, plural, one {# thing} other {# things}} the audience would see", { count: findings.length })}
          </span>
          <ChevronDown className={cn("h-3.5 w-3.5 shrink-0 transition-transform", open && "rotate-180")} />
        </button>
        {onFixWithAssistant && (
          <Tip content={aiOffReason ?? t_i18n("Open the studio with these findings ready to send")} wrap={!!aiOffReason}>
            <Button
              variant="outline"
              size="sm"
              className="h-6 shrink-0 gap-1 px-2 text-[11px]"
              disabled={!!aiOffReason}
              onClick={() =>
                onFixWithAssistant(
                  primedMessage(
                    findings,
                    t_i18n("Fix this video. The quality check found these issues; fix them and save again:"),
                    (hidden) => t_i18n("...and {count} more; fix the above and save, the next check lists the rest.", { count: hidden }),
                  ),
                )
              }
              data-testid="video.editor.quality.fix"
            >
              <Sparkles className="h-3 w-3 text-accent" />
              {t_i18n("Fix with my assistant")}
            </Button>
          </Tip>
        )}
      </div>
      {open && (
        <ul className="mt-1.5 space-y-1">
          {findings.map((finding, index) => (
            <li key={`${finding.scene_id}-${finding.kind}-${index}`}>
              <FindingRow
                finding={finding}
                onOpenScene={onOpenScene}
                label={t_i18n((finding.group === "content" ? CONTENT_SEVERITY_LABELS : SEVERITY_LABELS)[finding.severity])}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingRow({
  finding,
  label,
  onOpenScene,
}: {
  finding: VideoQualityFinding;
  label: string;
  onOpenScene: (sceneId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const where =
    finding.scene_index === null
      ? t_i18n("Template scene")
      : t_i18n("Scene {n}", { n: finding.scene_index + 1 });
  return (
    <button
      type="button"
      disabled={!finding.scene_id}
      onClick={() => finding.scene_id && onOpenScene(finding.scene_id)}
      className="flex w-full items-start gap-2 rounded-sm px-1 py-0.5 text-left hover:bg-elevation-hover disabled:cursor-default"
    >
      <span
        className={cn(
          "mt-px shrink-0 rounded-sm px-1 text-[10px] font-medium",
          finding.severity === "advisory" ? "bg-elevation-hover text-default-secondary" : "bg-feedback-alert-primary/20 text-feedback-warning-tertiary",
        )}
      >
        {label}
      </span>
      <span className="min-w-0 flex-1 text-default-primary">
        <span className="font-medium">{where}</span>
        {finding.label ? <span className="text-default-secondary"> - {finding.label}</span> : null}: {finding.message}
      </span>
    </button>
  );
}
