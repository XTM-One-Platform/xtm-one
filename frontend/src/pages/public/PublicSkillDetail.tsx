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

import { ChevronDown, ExternalLink, GraduationCap } from "lucide-react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { usePublicCatalogSkill } from "@/hooks/api/usePublicCatalogApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

import { CopyButton, CatalogDetailSkeleton, DetailShell } from "./components";

const COLLAPSED_HEIGHT = 300;

export default function PublicSkillDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const { data: skill, isLoading } = usePublicCatalogSkill(id!);
  const platformName = usePlatformName();
  const [expanded, setExpanded] = useState(false);

  if (isLoading) return <CatalogDetailSkeleton backTo="/catalog/skills" backLabel="Back to Skills" />;
  if (!skill)
    return (
      <p className="text-center text-default-secondary py-20">
        {t_i18n("Skill not found")}
      </p>
    );

  const isLong = (skill.content?.length ?? 0) > 800;

  return (
    <DetailShell
      backTo="/catalog/skills"
      backLabel="Back to Skills"
      entityType="skills"
      entityId={skill.id}
      icon={<GraduationCap className="h-6 w-6 text-accent" />}
      name={skill.name}
      description={skill.description}
      tags={skill.tags}
      createdAt={skill.created_at}
      updatedAt={skill.updated_at}
      sharedBy={skill.shared_by}
      extraActions={
        skill.url ? (
          <CopyButton text={skill.url} label="Copy Skill URL" />
        ) : undefined
      }
    >
      {/* URL */}
      {skill.url && (
        <div className="rounded-sm border border-accent/20 bg-accent/[0.04] p-4 flex items-center gap-3">
          <ExternalLink className="h-4 w-4 text-accent shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="content-compact text-default-secondary mb-0.5">
              {t_i18n("Skill URL (paste into any {platformName} instance to import)", { platformName })}
            </p>
            <p className="content-base font-mono-plex text-accent truncate">{skill.url}</p>
          </div>
          <CopyButton text={skill.url} label="Copy" />
        </div>
      )}

      {/* Compatibility */}
      {skill.compatibility && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-2">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
            {t_i18n("Compatibility")}
          </h2>
          <Badge
            variant="outline"
            className="content-compact text-feedback-warning-primary border-feedback-alert-primary/30"
          >
            {skill.compatibility}
          </Badge>
        </div>
      )}

      {/* Content — raw preformatted display (matches platform skill detail) */}
      {skill.content && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-2">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <div className="flex items-center justify-between">
            <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
              {t_i18n("Content")}
            </h2>
            <div className="flex items-center gap-2">
              <span className="content-caption text-default-secondary">
                {t_i18n("{count}K chars", { count: Math.round(skill.content.length / 1000) })}
              </span>
              <CopyButton text={skill.content} label="Copy" />
            </div>
          </div>
          <div
            className={cn("relative", !expanded && isLong && "overflow-hidden")}
            style={!expanded && isLong ? { maxHeight: COLLAPSED_HEIGHT } : undefined}
          >
            <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact font-mono-plex text-accent/80 overflow-x-auto whitespace-pre-wrap">
              {skill.content}
            </pre>
            {!expanded && isLong && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent" />
            )}
          </div>
          {isLong && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-1 content-compact text-accent hover:text-accent/80 transition-colors"
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  expanded && "rotate-180",
                )}
              />
              {expanded ? t_i18n("Collapse") : t_i18n("Expand full content")}
            </button>
          )}
        </div>
      )}
    </DetailShell>
  );
}
