// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { Spinner } from "@filigran/design-system";
import { CheckSquare, Plus, Search, XSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useIntegrationElements } from "@/hooks/api/useIntegrationApi";
import { cn } from "@/lib/utils";
import { INTEGRATION_PROVIDER_NAMES, providerLogoSrc } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { IntegrationElement, ProviderElementType } from "@/types";

// ---------------------------------------------------------------------------
// Element Type Editor — fetches elements and shows toggles + manual input
// ---------------------------------------------------------------------------
export function ElementTypeEditor({
  integrationId, provider, elementType, selected, onSelectionChange,
  allowedElements,
}: {
  integrationId: string;
  provider: string;
  elementType: ProviderElementType;
  selected: IntegrationElement[];
  onSelectionChange: (items: IntegrationElement[]) => void;
  /** When provided, only these elements are shown (used by assignments to scope to the agent's selection). */
  allowedElements?: IntegrationElement[];
}) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, error } = useIntegrationElements(integrationId, elementType.type);
  // When allowedElements is provided (e.g. assignment refining agent scope),
  // show only those elements instead of all integration elements.
  // If the parent has no elements scoped, show all. A failed refetch keeps the
  // last list; the refusal shown above wins over it.
  const allElements = error ? [] : (data?.elements ?? []);
  const elements = allowedElements && allowedElements.length > 0
    ? (() => {
        const allowedIds = new Set(allowedElements.map((e) => e.id));
        return allElements.filter((e) => allowedIds.has(e.id));
      })()
    : allElements;
  const [search, setSearch] = useState("");
  const [manualInput, setManualInput] = useState("");

  const selectedIds = new Set(selected.map((s) => s.id));

  const enableAll = () => {
    // Merge existing selected (including manual items) with all API elements
    const apiIds = new Set(elements.map((e) => e.id));
    const manualItems = selected.filter((s) => !apiIds.has(s.id));
    onSelectionChange([...elements, ...manualItems]);
  };

  const disableAll = () => {
    onSelectionChange([]);
  };

  const toggleElement = (el: IntegrationElement) => {
    if (selectedIds.has(el.id)) {
      onSelectionChange(selected.filter((s) => s.id !== el.id));
    } else {
      onSelectionChange([...selected, el]);
    }
  };

  const addManualElement = () => {
    const value = manualInput.trim();
    if (!value || selectedIds.has(value)) return;
    // For repos, expect owner/name format
    const name = value.includes("/") ? value.split("/").pop() || value : value;
    const group = value.includes("/") ? value.split("/")[0] : "";
    const el: IntegrationElement = {
      id: value,
      name,
      group,
      metadata: { full_name: value, manual: true },
    };
    onSelectionChange([...selected, el]);
    setManualInput("");
  };

  // Filter elements by search query (match name or group)
  const needle = search.toLowerCase().trim();
  const filtered = needle
    ? elements.filter(
        (el) =>
          el.name.toLowerCase().includes(needle) ||
          (el.group && el.group.toLowerCase().includes(needle)),
      )
    : elements;

  // Group filtered elements by their "group" field (e.g. team name)
  const groups = new Map<string, IntegrationElement[]>();
  for (const el of filtered) {
    const g = el.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)?.push(el);
  }

  // Identify manually-added items (not in API list) for display
  const apiIds = new Set(elements.map((e) => e.id));
  const manualItems = selected.filter((s) => !apiIds.has(s.id));

  const providerName = INTEGRATION_PROVIDER_NAMES[provider] ?? provider;

  return (
    <div className="rounded-sm border border-elevation-subtle-soft p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
          <span className="content-compact font-medium">{providerName} — {elementType.name}</span>
        </div>
        <div className="flex items-center gap-1.5">
          {selected.length > 0 && (
            <span className="text-[9px] text-filigran-ia-primary bg-filigran-ia-primary/10 rounded-sm px-1.5 py-0.5">
              {t_i18n("{count} selected", { count: selected.length })}
            </span>
          )}
          {!isLoading && elements.length > 0 && (
            <>
              <Button
                variant="ghost"
                size="sm"
                className="h-5 text-[9px] px-1.5 gap-1 text-default-secondary hover:text-default-primary"
                onClick={enableAll}
                disabled={selected.length >= elements.length}
              >
                <CheckSquare className="h-2.5 w-2.5" /> {t_i18n("All")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-5 text-[9px] px-1.5 gap-1 text-default-secondary hover:text-default-primary"
                onClick={disableAll}
                disabled={selected.length === 0}
              >
                <XSquare className="h-2.5 w-2.5" /> {t_i18n("None")}
              </Button>
            </>
          )}
        </div>
      </div>
      <p className="content-caption text-default-secondary mb-2">{elementType.description}</p>
      {!isLoading && error && (
        <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary mb-2">{error.message}</p>
      )}
      {isLoading ? (
        <div className="flex items-center gap-2 py-3">
          <Spinner size="sm" tone="inherit" className="text-default-secondary" />
          <span className="content-caption text-default-secondary">{t_i18n("Loading {name}s...", { name: elementType.name.toLowerCase() })}</span>
        </div>
      ) : elements.length === 0 && !elementType.allow_manual_input ? (
        error ? null : <p className="content-caption text-default-secondary italic py-2">{t_i18n("No {name}s found. Make sure the integration is connected.", { name: elementType.name.toLowerCase() })}</p>
      ) : (
        <>
          {/* Search field — shown when there are more than 8 elements */}
          {elements.length > 8 && (
            <div className="relative mb-2">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3 w-3 text-default-secondary" />
              <Input aria-label={t_i18n("Search {name}s...", { name: elementType.name.toLowerCase() })}
                placeholder={t_i18n("Search {name}s...", { name: elementType.name.toLowerCase() })}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-7 content-compact pl-7 pr-2 bg-elevation-hover border-elevation-subtle-soft"
              />
            </div>
          )}
          <div className="max-h-60 overflow-y-auto space-y-1">
            {filtered.length === 0 && !elementType.allow_manual_input ? (
              <p className="content-caption text-default-secondary italic py-2 text-center">
                {t_i18n("No matches for “{search}”", { search })}
              </p>
            ) : (
              <>
                {[...groups.entries()].map(([groupName, groupElements]) => (
                  <div key={groupName}>
                    {groups.size > 1 && (
                      <p className="text-[9px] text-default-secondary uppercase tracking-wider mt-2 mb-1 px-1">{groupName}</p>
                    )}
                    {groupElements.map((el) => (
                      <label
                        key={el.id}
                        className={cn(
                          "flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 cursor-pointer transition-colors",
                          selectedIds.has(el.id)
                            ? "bg-filigran-ia-primary/10 border border-filigran-ia-primary/30"
                            : "hover:bg-elevation-hover border border-transparent",
                        )}
                      >
                        <Switch
                          checked={selectedIds.has(el.id)}
                          onCheckedChange={() => toggleElement(el)}
                          className="h-4 w-7 data-[state=checked]:bg-filigran-ia-primary"
                        />
                        <span className="content-compact">{el.name}</span>
                      </label>
                    ))}
                  </div>
                ))}
                {/* Show manually-added items */}
                {manualItems.length > 0 && (
                  <div>
                    <p className="text-[9px] text-default-secondary uppercase tracking-wider mt-2 mb-1 px-1">{t_i18n("Manually added")}</p>
                    {manualItems.map((el) => (
                      <label
                        key={el.id}
                        className="flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 cursor-pointer transition-colors bg-filigran-ia-primary/10 border border-filigran-ia-primary/30"
                      >
                        <Switch
                          checked
                          onCheckedChange={() => toggleElement(el)}
                          className="h-4 w-7 data-[state=checked]:bg-filigran-ia-primary"
                        />
                        <span className="content-compact">{el.id}</span>
                      </label>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Manual input for types that support it (e.g. public repos) */}
          {elementType.allow_manual_input && (
            <div className="mt-2 pt-2 border-t border-elevation-subtle-soft">
              <p className="text-[9px] text-default-secondary mb-1.5">{t_i18n("Add manually (e.g. owner/repo for public repos):")}</p>
              <div className="flex gap-1.5">
                <Input aria-label={t_i18n("Add manually (e.g. owner/repo for public repos):")}
                  placeholder="owner/repository"
                  value={manualInput}
                  onChange={(e) => setManualInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addManualElement(); } }}
                  className="h-7 content-compact flex-1 bg-elevation-hover border-elevation-subtle-soft"
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 content-compact gap-1 px-2"
                  onClick={addManualElement}
                  disabled={!manualInput.trim()}
                >
                  <Plus className="h-3 w-3" /> {t_i18n("Add")}
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
