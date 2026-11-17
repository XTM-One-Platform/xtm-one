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
 * Settings > AI models > Render retention: how many days the Video
 * Studio keeps a render nothing else keeps - a failed or cancelled one, a
 * scene preview, a ready render a newer one of its quality superseded - after
 * it ended; 0 keeps them for good. The newest ready render of each quality, the
 * render a video and its public link play and the renders kept by hand are
 * never deleted. A nightly job applies it. Administrators edit it; everyone
 * reads it.
 */
import { useId, useState } from "react";
import { SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import { Archive, Pencil } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { useRenderRetention, useSaveRenderRetention } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import type { VideoRenderRetention } from "@/types";

/** The most days a rule takes (`render_retention.MAX_DAYS`). */
const MAX_DAYS = 3650;

/** The English label and hint of each rule. @i18n-keys */
const RULE_LABELS = {
  failed_cancelled_days: "Failed and cancelled renders",
  previews_days: "Scene previews",
  superseded_ready_days: "Older ready renders (a newer one of their quality is ready)",
} as const;

export function RenderRetentionSection({ isAdmin }: { isAdmin: boolean }) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const { toast } = useToast();
  const { data: rules, isLoading } = useRenderRetention();
  const save = useSaveRenderRetention();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<keyof VideoRenderRetention, string>>({
    failed_cancelled_days: "",
    previews_days: "",
    superseded_ready_days: "",
  });
  const keys = Object.keys(RULE_LABELS) as (keyof VideoRenderRetention)[];
  const parsed: VideoRenderRetention = {
    failed_cancelled_days: Number.parseInt(draft.failed_cancelled_days, 10),
    previews_days: Number.parseInt(draft.previews_days, 10),
    superseded_ready_days: Number.parseInt(draft.superseded_ready_days, 10),
  };
  const fieldValid = (key: keyof VideoRenderRetention) =>
    /^\d+$/.test(draft[key].trim()) && parsed[key] >= 0 && parsed[key] <= MAX_DAYS;
  const valid = keys.every(fieldValid);

  const startEditing = () => {
    if (!rules) return;
    setDraft({
      failed_cancelled_days: String(rules.failed_cancelled_days),
      previews_days: String(rules.previews_days),
      superseded_ready_days: String(rules.superseded_ready_days),
    });
    setEditing(true);
  };
  const submit = () =>
    save.mutate(parsed, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: t_i18n("Render retention saved"), variant: "success" });
      },
      onError: (err) =>
        toast({ title: t_i18n("The render retention was not saved"), description: err instanceof Error ? err.message : undefined, variant: "error" }),
    });
  const days = (count: number) => (count > 0 ? t_i18n("{count, plural, one {# day} other {# days}}", { count }) : t_i18n("for good"));

  return (
    <div data-testid="settings.render-retention">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <SettingsGroupTitle icon={Archive}>{t_i18n("Render retention")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n(
              "How long the Video Studio keeps renders after they end, deleted with their files every night. The newest ready render of each quality of a video, the render it and its public link play and the renders kept by hand are never deleted. 0 keeps them for good.",
            )}
          </SettingsGroupDescription>
        </div>
        {isAdmin && (
          <div className="ml-4 flex shrink-0 gap-2">
            {!editing ? (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing} disabled={isLoading || !rules}>
                <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
              </Button>
            ) : null}
          </div>
        )}
      </div>
      <div className="space-y-2 rounded-sm bg-elevation-default p-3.5">
        {keys.map((key) => {
          const inputId = `${fieldId}-${key}`;
          const errorId = `${inputId}-error`;
          const invalid = editing && !fieldValid(key);
          return (
            <div key={key} className="space-y-1">
              <div className="flex items-center justify-between gap-3 content-base">
                <label htmlFor={inputId} className="text-default-secondary">
                  {t_i18n(RULE_LABELS[key])}
                </label>
                {editing ? (
                  <span className="flex items-center gap-2">
                    <Input
                      id={inputId}
                      aria-label={t_i18n(RULE_LABELS[key])}
                      aria-invalid={invalid || undefined}
                      aria-describedby={invalid ? errorId : undefined}
                      type="number"
                      min={0}
                      max={MAX_DAYS}
                      className="w-28"
                      value={draft[key]}
                      onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                      disabled={save.isPending}
                    />
                    <span className="text-default-secondary">{t_i18n("days")}</span>
                  </span>
                ) : (
                  <span className="font-medium tabular-nums">{rules ? days(rules[key]) : "-"}</span>
                )}
              </div>
              {invalid && (
                <p id={errorId} role="alert" className="content-compact text-right text-feedback-error-primary">
                  {t_i18n("Enter a whole number from 0 to 3650")}
                </p>
              )}
            </div>
          );
        })}
      </div>
      {editing && (
        <SettingsSaveBar
          dirty={!!rules && keys.some((key) => draft[key] !== String(rules[key]))}
          saving={save.isPending}
          saveDisabled={!valid}
          onSave={submit}
          onCancel={() => setEditing(false)}
        />
      )}
    </div>
  );
}
