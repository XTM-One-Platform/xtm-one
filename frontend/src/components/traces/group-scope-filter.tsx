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

import { Icon } from "@filigran/design-system";
/**
 * The Groups filter of the Traces and Sessions pages: the members of
 * the groups picked. Administrators only, like the pages: a group's
 * managers read its LLM Analytics, never its traces.
 */
import { useId } from "react";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { useGroups } from "@/hooks/api/useGroupApi";
import { useFormatter } from "@/i18n";

export function GroupScopeFilter({ selected, onToggle }: { selected: string[]; onToggle: (groupId: string) => void }) {
  const { t_i18n } = useFormatter();
  const { data: groups = [], isLoading } = useGroups();
  const fieldId = useId();
  const options = groups.map((group) => ({ value: group.id, label: group.name }));
  // The filter keeps its place while the groups are read, so the table below does not move
  // when they land (#11345); a deployment with no group drops it once read.
  if (options.length === 0 && !isLoading) return null;
  return (
    <div className="space-y-2">
      <Label htmlFor={fieldId} className="flex items-center gap-1.5">
        <Icon name="users" size={16} />
        {t_i18n("Groups")}
      </Label>
      <MultiSelect
        id={fieldId}
        options={options}
        selected={selected}
        onToggle={onToggle}
        placeholder={t_i18n("All groups")}
        searchPlaceholder={t_i18n("Search groups...")}
        emptyOptionsLabel={t_i18n("No groups available")}
        noMatchLabel={t_i18n("No matching groups")}
      />
    </div>
  );
}
