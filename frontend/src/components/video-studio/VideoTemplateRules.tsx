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
 * What a video template keeps every video to: what it is made for,
 * the formats it ships, the backdrop, the cards it adds, the art
 * direction of its AI shots and the choices it locks. Read from the
 * picker summary (`/video-studio/templates`) or from a template's own
 * `extra_metadata.video` (`rulesFromMetadata`).
 */
import { Activity, Camera, Lock, Ratio, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { VideoShotDirection } from "@/types";

import { BACKGROUND_LABELS, lockLabel, MOTION_PERSONALITY_LABELS, TEMPLATE_PURPOSE_LABELS, type VideoRulesView } from "./video-format";

function ShotDirectionBadge({ shots }: { shots: VideoShotDirection }) {
  const { t_i18n } = useFormatter();
  const avoid = shots.avoid ?? [];
  if (!shots.style && !avoid.length) return null;
  const details = (
    <div className="max-w-xs space-y-1.5 content-caption">
      {shots.style && <p>{shots.style}</p>}
      {avoid.length > 0 && <p>{t_i18n("Never shows: {terms}", { terms: avoid.join(", ") })}</p>}
    </div>
  );
  return (
    <Tip content={details}>
      <Badge variant="cyan" className="gap-1 content-caption" data-testid="video.template.shots">
        <Camera className="h-2.5 w-2.5" />
        {t_i18n("Directs its AI shots")}
      </Badge>
    </Tip>
  );
}

export function VideoTemplateRules({ rules, compact = false }: { rules: VideoRulesView; compact?: boolean }) {
  const { t_i18n } = useFormatter();
  const backdrop = rules.background ? (BACKGROUND_LABELS as Record<string, string>)[rules.background] : undefined;
  const personality = typeof rules.motion === "string" ? rules.motion : rules.motion?.personality;
  const motion = personality ? (MOTION_PERSONALITY_LABELS as Record<string, string>)[personality] : undefined;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {(rules.purposes ?? []).map((purpose) => (
        <Badge key={purpose} variant="cyan" className="content-caption">
          {t_i18n(TEMPLATE_PURPOSE_LABELS[purpose])}
        </Badge>
      ))}
      {rules.aspect_ratios.map((ratio) => (
        <Badge key={ratio} variant="outline" className="gap-1 content-caption">
          <Ratio className="h-2.5 w-2.5" />
          {ratio}
        </Badge>
      ))}
      {backdrop && (
        <Badge variant="outline" className="content-caption">
          {t_i18n(backdrop)}
        </Badge>
      )}
      {(rules.intro || rules.outro) && (
        <Badge variant="cyan" className="gap-1 content-caption">
          <Sparkles className="h-2.5 w-2.5" />
          {rules.intro && rules.outro
            ? t_i18n("Adds its title and closing cards")
            : rules.intro
              ? t_i18n("Adds its title card")
              : t_i18n("Adds its closing card")}
        </Badge>
      )}
      {motion && (
        <Badge variant="outline" className="gap-1 content-caption" data-testid="video.template.motion">
          <Activity className="h-2.5 w-2.5" />
          {t_i18n(motion)}
        </Badge>
      )}
      {rules.shots && <ShotDirectionBadge shots={rules.shots} />}
      {compact
        ? rules.locked.length > 0 && (
            <Badge variant="outline" className="gap-1 content-caption">
              <Lock className="h-2.5 w-2.5" />
              {t_i18n("{count} locked", { count: rules.locked.length })}
            </Badge>
          )
        : rules.locked.map((path) => {
            const label = lockLabel(path);
            return (
              <Badge key={path} variant="outline" className="gap-1 content-caption">
                <Lock className="h-2.5 w-2.5" />
                {label ? t_i18n(label) : <span className="font-mono-plex">{path}</span>}
              </Badge>
            );
          })}
    </div>
  );
}
