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
 * The closed `kind` picker of the template forms: Presentation or
 * Report, each with the one sentence that says what it dresses — mirrors
 * `TEMPLATE_KINDS` on the backend, whose routes refuse anything else with a
 * 422. A row of a RETIRED kind (`asset-library`, `dashboard`) still opens: its
 * stored value is offered as one extra "(legacy)" entry so the field can
 * render it, and the picker is how it is moved to a kind that still exists.
 */
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useFormatter } from "@/i18n";
import { TEMPLATE_KIND_CONFIG, TEMPLATE_KINDS, templateKindLabel, type TemplateKind } from "@/lib/constants";
import { cn } from "@/lib/utils";

export function TemplateKindSelect({
  id,
  value,
  onChange,
  disabled,
  className,
}: {
  /** The id a field label points at (htmlFor); its helper is tied by `${id}-hint`. */
  id?: string;
  value: string;
  onChange: (kind: string) => void;
  disabled?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const current = value.trim().toLowerCase();
  const isLegacy = current !== "" && !(TEMPLATE_KINDS as readonly string[]).includes(current);
  // Video is offered behind `VIDEO_STUDIO`; a video template still
  // reads as one while the flag is off.
  const videoStudio = useFeatureFlag("VIDEO_STUDIO");
  const selected = current || "presentation";
  // The trigger shows the kind's name only and stays inside its column; the
  // one-line meaning of each kind is read in the list.
  const selectedLabel = (TEMPLATE_KINDS as readonly string[]).includes(selected)
    ? t_i18n(TEMPLATE_KIND_CONFIG[selected as TemplateKind].label)
    : templateKindLabel(selected);
  return (
    <Select value={selected} onValueChange={onChange} disabled={disabled}>
      <SelectTrigger
        id={id}
        className={cn("w-full min-w-0", className)}
        aria-label={t_i18n("Kind")}
        aria-describedby={id ? `${id}-hint` : undefined}
        data-testid="template-kind-trigger"
      >
        <SelectValue>
          <span className="block truncate">{selectedLabel}</span>
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {TEMPLATE_KINDS.filter((kind) => kind !== "video" || videoStudio || current === "video").map((kind: TemplateKind) => (
          <SelectItem key={kind} value={kind}>
            <span className="font-medium">{t_i18n(TEMPLATE_KIND_CONFIG[kind].label)}</span>
            <span className="ml-2 content-compact text-default-secondary">
              {t_i18n(TEMPLATE_KIND_CONFIG[kind].description)}
            </span>
          </SelectItem>
        ))}
        {isLegacy && (
          <SelectItem value={current}>
            <span className="font-medium">{templateKindLabel(current)}</span>
            <span className="ml-2 content-compact text-default-secondary">
              {t_i18n("Legacy kind: it can no longer be created. Pick Presentation, Report or Video to move this template.")}
            </span>
          </SelectItem>
        )}
      </SelectContent>
    </Select>
  );
}
