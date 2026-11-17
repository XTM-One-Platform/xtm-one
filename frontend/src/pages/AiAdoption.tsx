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

import { useState, useRef } from "react";
import { Navigate } from "react-router-dom";
import { BarChart3, UserMinus, X, Users } from "lucide-react";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { SearchInput } from "@/components/ui/search-input";
import { FilterClearButton } from "@/components/ui/filter-pill";
import { useTabParam } from "@/hooks/useTabParam";
import { useGroups } from "@/hooks/api/useGroupApi";
import { useClickOutside } from "@/hooks/useClickOutside";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { GroupFilterContext } from "./ai-adoption/shared-utils";

import OverviewTab from "./ai-adoption/OverviewTab";
import GapsTab from "./ai-adoption/GapsTab";

type Tab = "overview" | "gaps";

/** The tabs the page has; any other segment (an old deep link to Agentic AI,
 *  Development or Status) lands on Overview. */
const TABS: readonly Tab[] = ["overview", "gaps"];

function GroupMultiSelect({ value, onChange, groups }: { value: string[]; onChange: (v: string[]) => void; groups: { id: string; name: string }[] }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false));

  const selected = groups.filter((g) => value.includes(g.id));
  const q = search.toLowerCase();
  const filtered = groups.filter((g) => !value.includes(g.id) && g.name.toLowerCase().includes(q));

  const toggle = (id: string) => {
    onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id]);
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          "flex items-center gap-1.5 h-10 rounded-sm border border-elevation-subtle bg-elevation-default px-3 content-compact transition-colors",
          "hover:bg-elevation-hover min-w-[200px] max-w-[320px]",
          open && "ring-1 ring-accent border-accent",
        )}
      >
        <Users className="h-3.5 w-3.5 text-default-secondary shrink-0" />
        {selected.length === 0 ? (
          <span className="text-default-secondary">{t_i18n("All users")}</span>
        ) : (
          <div className="flex items-center gap-1 overflow-hidden flex-1">
            {selected.slice(0, 2).map((g) => (
              <span key={g.id} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-sm bg-accent/10 text-accent content-caption font-medium shrink-0">
                {g.name}
                <X className="h-2.5 w-2.5 cursor-pointer hover:text-negative-primary" onClick={(e) => { e.stopPropagation(); toggle(g.id); }} />
              </span>
            ))}
            {selected.length > 2 && <span className="content-caption text-default-secondary shrink-0">+{selected.length - 2}</span>}
          </div>
        )}
        {selected.length > 0 && (
          <X className="h-3.5 w-3.5 text-default-secondary hover:text-negative-primary shrink-0 ml-1" onClick={(e) => { e.stopPropagation(); onChange([]); }} />
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-[280px] rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay">
          <div className="p-2 border-b border-elevation-subtle-soft">
            <SearchInput
              autoFocus
              placeholder={t_i18n("Search groups...")}
              value={search}
              onChange={setSearch}
            />
          </div>
          <div className="max-h-48 overflow-y-auto p-1">
            {selected.length > 0 && (
              <FilterClearButton onClick={() => onChange([])} className="w-full px-2.5 hover:bg-elevation-hover">
                {t_i18n("Clear all filters")}
              </FilterClearButton>
            )}
            {filtered.length > 0 ? filtered.map((g) => (
              <button
                key={g.id}
                onClick={() => toggle(g.id)}
                className="w-full text-left px-2.5 py-1.5 content-compact hover:bg-elevation-hover rounded-sm flex items-center justify-between"
              >
                <span className="truncate">{g.name}</span>
              </button>
            )) : (
              <p className="px-2.5 py-2 content-caption text-default-secondary">{search ? t_i18n("No matching groups") : t_i18n("All groups selected")}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function AiAdoption() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<Tab>("overview");
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const { data: groups } = useGroups();
  const platformName = usePlatformName();

  const groupFilterValue = groupIds.length > 0 ? groupIds.join(",") : undefined;

  if (!TABS.includes(tab)) return <Navigate to="/ai-adoption" replace />;

  const tabs = [
    { key: "overview" as Tab, label: "Overview", icon: BarChart3 },
    { key: "gaps" as Tab, label: "Adoption Gaps", icon: UserMinus },
  ];

  return (
    <GroupFilterContext.Provider value={groupFilterValue}>
      <div className="space-y-6">
        <div>
          <h1 className="font-sans-geologica text-3xl font-bold">{t_i18n("AI Adoption")}</h1>
          <p className="mt-1 text-default-secondary">{t_i18n("Monitor agentic AI usage across {platform}.", { platform: platformName })}</p>
        </div>
        <div className="flex items-center justify-between">
          <DetailTabs tabs={tabs} active={tab} />
          {(groups ?? []).length > 0 && (
            <GroupMultiSelect value={groupIds} onChange={setGroupIds} groups={(groups ?? []).map((g) => ({ id: g.id, name: g.name }))} />
          )}
        </div>
        {tab === "overview" && <OverviewTab />}
        {tab === "gaps" && <GapsTab />}
      </div>
    </GroupFilterContext.Provider>
  );
}
