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

import { useMemo, useState } from "react";
import { Check, Library, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { usePrompts } from "@/hooks/api/usePromptApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/**
 * The platform's prompt library (the /prompts collection) as an inline
 * single-pick list, mirroring the flow builder's MultiSelectListField look so
 * it reads like the tools/integrations pickers. Picking a prompt hands its
 * content to the caller, which uses it as the agent's persona; the row whose
 * content the persona currently matches carries the check mark.
 */
export function PromptLibraryList({
  value,
  onPick,
}: {
  /** The current persona text — used to mark the matching prompt as picked. */
  value: string;
  onPick: (content: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const { data: prompts } = usePrompts();

  const sorted = useMemo(
    () => (prompts ?? []).slice().sort((a, b) => a.name.localeCompare(b.name)),
    [prompts],
  );
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return sorted;
    return sorted.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.tags.some((tag) => tag.toLowerCase().includes(q)),
    );
  }, [sorted, search]);

  if (sorted.length === 0) {
    return (
      <p className="rounded-sm border border-elevation-subtle-soft px-3 py-2 text-[11px] text-default-secondary">
        {t_i18n("No prompts yet")}
      </p>
    );
  }

  return (
    <div className="space-y-1.5">
      {sorted.length > 6 && (
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-default-secondary" />
          <Input aria-label={t_i18n("Search prompts...")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t_i18n("Search prompts...")}
            className="h-8 pl-7 content-compact"
          />
        </div>
      )}
      <div className="max-h-64 space-y-0.5 overflow-y-auto rounded-sm border border-elevation-subtle-soft p-1">
        {filtered.map((prompt) => {
          const isPicked = value.trim() !== "" && value === prompt.content;
          return (
            <button
              key={prompt.id}
              type="button"
              onClick={() => onPick(prompt.content)}
              className={cn(
                "flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left transition-colors",
                isPicked ? "bg-accent/[0.08]" : "hover:bg-elevation-hover",
              )}
            >
              <span className="mt-0.5">
                <Library className="h-4 w-4 shrink-0 text-default-secondary" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate content-compact font-medium">{prompt.name}</span>
                <span className="block truncate content-caption text-default-secondary">
                  {prompt.description || prompt.content}
                </span>
              </span>
              <Check
                className={cn(
                  "mt-0.5 h-3.5 w-3.5 shrink-0 text-accent",
                  isPicked ? "opacity-100" : "opacity-0",
                )}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}
