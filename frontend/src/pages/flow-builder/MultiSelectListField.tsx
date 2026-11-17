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
 * List-view multi-select for capability fields.
 *
 * Tools / integrations / skills / knowledge bases are picked from a list that
 * shows each option's icon, name and description — and several can be selected
 * at once, so one node declares a whole set rather than needing one node per
 * id. The value is a string[] of the selected option values.
 *
 * Icons come from the catalog option: a lucide name (built-in tools, family
 * defaults) resolved through FLOW_NODE_ICONS, or an http(s) URL (a custom
 * tool's own icon) rendered as an image.
 */

import { useMemo, useState } from "react";
import { Check, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { matchesQuery, sortByLabel } from "@/lib/select-options";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { FlowFieldOption } from "@/types";
import { DEFAULT_NODE_ICON, FLOW_NODE_ICONS } from "./node-visuals";
import { withUnlistedSelections } from "./unlisted-options";

interface MultiSelectListFieldProps {
  options: FlowFieldOption[];
  value: string[];
  disabled?: boolean;
  /** Show a selected value `options` does not list as a removable
   *  "Unavailable" row. Only once the options have loaded: until then every
   *  selection would be claimed unavailable. */
  showUnlisted?: boolean;
  onChange: (value: string[]) => void;
}

function OptionIcon({ icon }: { icon?: string | null }) {
  if (icon && /^https?:\/\//.test(icon)) {
    return (
      <img
        src={icon}
        alt=""
        className="h-4 w-4 shrink-0 rounded-sm object-cover"
      />
    );
  }
  const Icon = (icon && FLOW_NODE_ICONS[icon]) || DEFAULT_NODE_ICON;
  return <Icon className="h-4 w-4 shrink-0 text-default-secondary" />;
}

export function MultiSelectListField({
  options: listedOptions,
  value,
  disabled,
  showUnlisted,
  onChange,
}: MultiSelectListFieldProps) {
  const { t_i18n, locale } = useFormatter();
  const [search, setSearch] = useState("");
  const selected = new Set(value);

  const sortedOptions = useMemo(
    () => sortByLabel(listedOptions, (option) => option.label, locale),
    [listedOptions, locale],
  );
  const options = useMemo(
    () =>
      showUnlisted
        ? withUnlistedSelections(sortedOptions, value, (unlisted) => ({
            value: unlisted,
            label: t_i18n("Unavailable"),
            // A read-only picker cannot remove it, so it does not say to.
            description: disabled
              ? null
              : t_i18n("No longer available to you. Click to remove it from the selection."),
          }))
        : sortedOptions,
    [sortedOptions, value, showUnlisted, disabled, t_i18n],
  );

  const filtered = useMemo(
    () => options.filter((o) => matchesQuery(search, [o.label, o.description])),
    [options, search],
  );

  const toggle = (optionValue: string) => {
    if (disabled) return;
    onChange(
      selected.has(optionValue)
        ? value.filter((v) => v !== optionValue)
        : [...value, optionValue],
    );
  };

  if (options.length === 0) {
    return (
      <p className="rounded-sm border border-elevation-subtle-soft px-3 py-2 text-[11px] text-default-secondary">
        {t_i18n("Nothing available to select.")}
      </p>
    );
  }

  return (
    <div className="space-y-1.5">
      {options.length > 6 && (
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-default-secondary" />
          <Input aria-label={t_i18n("Search…")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t_i18n("Search…")}
            className="h-8 pl-7 content-compact"
            disabled={disabled}
          />
        </div>
      )}
      <div className="max-h-64 space-y-0.5 overflow-y-auto rounded-sm border border-elevation-subtle-soft p-1">
        {filtered.map((option) => {
          const isSelected = selected.has(option.value);
          return (
            <Tip
              key={option.value}
              content={
                option.description ? (
                  <span className="block space-y-1">
                    <span className="block">{option.label}</span>
                    <span className="block text-default-secondary">{option.description}</span>
                  </span>
                ) : (
                  option.label
                )
              }
              // A read-only picker disables its rows, and a disabled button
              // takes no focus: the wrapper keeps the full text reachable.
              wrap={disabled}
              wrapClassName="flex w-full"
            >
              <button
                type="button"
                disabled={disabled}
                onClick={() => toggle(option.value)}
                className={cn(
                  "flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left transition-colors",
                  isSelected ? "bg-accent/[0.08]" : "hover:bg-elevation-hover",
                  disabled && "opacity-50",
                )}
              >
                <span className="mt-0.5">
                  <OptionIcon icon={option.icon} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate content-compact font-medium">
                    {option.label}
                  </span>
                  {option.description && (
                    <span className="block truncate content-caption text-default-secondary">
                      {option.description}
                    </span>
                  )}
                </span>
                <Check
                  className={cn(
                    "mt-0.5 h-3.5 w-3.5 shrink-0 text-accent",
                    isSelected ? "opacity-100" : "opacity-0",
                  )}
                />
              </button>
            </Tip>
          );
        })}
        {filtered.length === 0 && (
          <p className="px-2 py-3 text-center text-[11px] text-default-secondary">
            {t_i18n("No match")}
          </p>
        )}
      </div>
      {value.length > 0 && (
        <p className="px-1 content-caption text-default-secondary">
          {t_i18n("{count} selected", { count: value.length })}
        </p>
      )}
    </div>
  );
}
