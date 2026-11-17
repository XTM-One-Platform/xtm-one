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
 * The skills the General Assistant carries: company-managed skills,
 * offered to it as a catalog it loads from (`load_skill`), like an agent's.
 *
 * Until an admin chooses, it carries the built-in ones the platform declares
 * for it (`default_skill_slugs`: Video Production, Presentation Design - the
 * built-in skills carry their slug as a `slug:` tag), so a release's new one
 * reaches it; a choice is kept as made, and "Use the built-in skills" goes
 * back to the platform's (`skill_ids: null`).
 *
 * A section of the Configure panel, in the grammar of its other
 * resources: the choice is part of the panel's draft and its Save writes it
 * to the same field (`skill_ids`). A filter above the grid narrows a
 * long catalog by name and description, as on an agent's Skills card.
 */
import { useState } from "react";
import { BookMarked, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { useFormatter } from "@/i18n";
import { cn, toggleArrayItem } from "@/lib/utils";
import { carriedSkillIds, type GeneralAssistantSkillCatalog } from "./ga-skills";
import { RESOURCE_GRID_CLASS, ResourceToggleCard } from "./ResourceToggleCard";
import { Spinner } from "@filigran/design-system";

interface GeneralAssistantSkillsProps {
  catalog: GeneralAssistantSkillCatalog;
  /** The draft choice: skill ids, or `null` for the built-in ones. */
  choice: string[] | null;
  onChange: (choice: string[] | null) => void;
}

export function GeneralAssistantSkills({ catalog, choice, onChange }: GeneralAssistantSkillsProps) {
  const { t_i18n } = useFormatter();
  const { offered, defaults, isLoading } = catalog;
  const carried = carriedSkillIds(choice, defaults);
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = needle
    ? offered.filter((skill) => skill.name.toLowerCase().includes(needle) || (skill.description ?? "").toLowerCase().includes(needle))
    : offered;

  return (
    <div className="space-y-2" data-testid="settings.ga.skills">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <BookMarked className="h-4 w-4 text-accent" />
          <span className="content-base font-medium">{t_i18n("Skills")}</span>
          {choice === null && (
            <Badge variant="outline" className="h-4 px-1.5 content-caption leading-none">
              {t_i18n("Built-in")}
            </Badge>
          )}
        </div>
        {choice !== null && (
          <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 gap-1.5 content-compact" onClick={() => onChange(null)}>
            <RotateCcw className="h-3 w-3" /> {t_i18n("Use the built-in skills")}
          </Button>
        )}
      </div>
      <p className="content-compact text-default-secondary">
        {t_i18n(
          "Instructions the General Assistant loads when a task matches them - how to make a video, how to build a deck. Only their one-line descriptions sit in its prompt. Click a company-managed skill to attach or detach it.",
        )}
      </p>
      {isLoading ? (
        <div className="flex items-center gap-2 py-3 content-compact text-default-secondary">
          <Spinner size="sm" tone="inherit" /> {t_i18n("Loading skills...")}
        </div>
      ) : offered.length === 0 ? (
        <div className="py-3 text-center">
          <BookMarked className="mx-auto mb-1 h-5 w-5 text-default-disabled" />
          <p className="content-compact text-default-secondary">{t_i18n("No company-managed skill yet.")}</p>
        </div>
      ) : (
        <>
        {offered.length > 3 && (
          <SearchInput placeholder={t_i18n("Filter skills...")} value={query} onChange={setQuery} className="max-w-full" />
        )}
        {shown.length === 0 ? (
          <p className="py-3 text-center content-compact text-default-secondary" data-testid="settings.ga.skills.no-match">
            {t_i18n("No skills match your filter.")}
          </p>
        ) : (
        <div className={RESOURCE_GRID_CLASS}>
          {shown.map((skill) => {
            const attached = carried.includes(skill.id);
            return (
              <ResourceToggleCard
                key={skill.id}
                testId={`ga-skill-${skill.id}`}
                attached={attached}
                onToggle={() => onChange(toggleArrayItem(carried, skill.id))}
                icon={<BookMarked className={cn("h-4 w-4", attached ? "text-accent" : "text-default-secondary")} />}
                name={skill.name}
                description={skill.description || undefined}
                badge={
                  defaults.includes(skill.id) ? (
                    <Badge
                      variant="outline"
                      className="h-4 shrink-0 px-1.5 content-caption leading-none"
                      title={t_i18n("Built-in for the General Assistant")}
                    >
                      {t_i18n("Built-in")}
                    </Badge>
                  ) : undefined
                }
              />
            );
          })}
        </div>
        )}
        </>
      )}
    </div>
  );
}
