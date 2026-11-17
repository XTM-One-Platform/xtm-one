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

import { useId, useMemo, useState, type ReactNode } from "react";
import { Bot, Plus, Zap } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SearchInput } from "@/components/ui/search-input";
import { cn } from "@/lib/utils";
import { ICON_MAP, INTEGRATION_TYPE_LABELS } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { AgentTemplate } from "@/types";

/**
 * The agent-template gallery: search + category pills + a card grid with a
 * leading "From Scratch" card. Shared by the Advanced creation form and the
 * creation wizard so the two entry points cannot drift apart.
 *
 * Owns only its filtering state; `onSelect` receives the picked template, or
 * null for "from scratch".
 */
export function TemplateGallery({
  templates,
  onSelect,
}: {
  templates: AgentTemplate[] | undefined;
  onSelect: (tmpl: AgentTemplate | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);

  const categories = useMemo(
    () => [...new Set(templates?.map((t) => t.category) ?? [])],
    [templates],
  );
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (templates ?? [])
      .filter((t) => {
        if (categoryFilter && t.category !== categoryFilter) return false;
        if (!q) return true;
        return (
          t.name.toLowerCase().includes(q) ||
          (t.description?.toLowerCase().includes(q) ?? false)
        );
      })
      // filter() already returned a fresh array, but sort a copy anyway so
      // this never mutates a caller-visible list if the filter is removed.
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [templates, search, categoryFilter]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <SearchInput placeholder={t_i18n("Search templates...")} value={search} onChange={setSearch} className="flex-1 min-w-[200px] max-w-md" />
        <div className="flex gap-1 rounded-sm bg-elevation-hover p-1">
          <button className={cn("rounded-sm px-3 py-1 content-compact font-medium transition-colors", !categoryFilter ? "bg-elevation-highlight text-default-primary" : "text-default-secondary hover:bg-elevation-hover")} onClick={() => setCategoryFilter(null)}>{t_i18n("All")}</button>
          {categories.map((cat) => (
            <button key={cat} className={cn("rounded-sm px-3 py-1 content-compact font-medium capitalize transition-colors", categoryFilter === cat ? "bg-elevation-highlight text-default-primary" : "text-default-secondary hover:bg-elevation-hover")} onClick={() => setCategoryFilter(cat)}>{cat}</button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <GalleryCard
          className="border-dashed"
          onActivate={() => onSelect(null)}
          icon={<div className="flex h-10 w-10 items-center justify-center rounded-sm bg-elevation-hover"><Plus className="h-5 w-5 text-default-secondary" /></div>}
          title={t_i18n("From Scratch")}
          description={<p className="content-compact text-default-secondary">{t_i18n("Empty agent. Full control over persona, integrations, and assignments.")}</p>}
        />

        {filtered.map((tmpl) => {
          const Icon = ICON_MAP[tmpl.icon] ?? Bot;
          const types = tmpl.required_integration_types || [];
          return (
            <GalleryCard
              key={tmpl.slug}
              onActivate={() => onSelect(tmpl)}
              icon={<div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10"><Icon className="h-5 w-5 text-accent" /></div>}
              title={tmpl.name}
              description={<p className="content-compact text-default-secondary line-clamp-2 mb-3">{tmpl.description}</p>}
            >
              <div className="flex items-center gap-1.5 flex-wrap">
                <Badge variant="outline" className="text-[9px] capitalize text-accent border-accent/30">{tmpl.category}</Badge>
                {tmpl.assignment_count > 0 && <Badge variant="outline" className="text-[9px]"><Zap className="h-2 w-2 mr-0.5" />{t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: tmpl.assignment_count })}</Badge>}
                {types.map((t) => (
                  <Badge key={t} variant="outline" className="text-[9px] text-accent border-accent/30">{t_i18n(INTEGRATION_TYPE_LABELS[t] || t)}</Badge>
                ))}
              </div>
            </GalleryCard>
          );
        })}
      </div>
    </div>
  );
}

/**
 * One pickable card of the gallery. A `div` with `role="button"` rather than a
 * `<button>`, whose content model (phrasing content only) cannot hold the
 * card's paragraphs and badges; it holds no other control, so nothing nests
 * inside the button role. Tab reaches it, Enter and Space pick it, a focus ring
 * shows where the keyboard is, and assistive technology (and the embedded
 * assistant's page snapshot) announces it as a button named by its title and
 * described by its description.
 */
function GalleryCard({
  onActivate,
  icon,
  title,
  description,
  className,
  children,
}: {
  onActivate: () => void;
  icon: ReactNode;
  title: string;
  description: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  const titleId = useId();
  const descriptionId = useId();
  return (
    <Card
      role="button"
      tabIndex={0}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      className={cn(
        "cursor-pointer hover:bg-elevation-hover transition-all focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        className,
      )}
      onClick={onActivate}
      onKeyDown={(e) => {
        // Only the card's own key presses: a key bubbling up from inside it is
        // not a request to pick it.
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate();
        }
      }}
    >
      <CardContent className="pt-6">
        <div className="flex items-center gap-3 mb-3">
          {icon}
          <p id={titleId} className="content-base font-semibold">{title}</p>
        </div>
        <div id={descriptionId}>{description}</div>
        {children}
      </CardContent>
    </Card>
  );
}
