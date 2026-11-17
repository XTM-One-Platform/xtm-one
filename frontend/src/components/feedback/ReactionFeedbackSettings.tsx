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
 * Reactions as feedback: whether the reactions people put on an
 * agent's answers rate them, and which emojis read as positive or negative.
 * One editor for every surface that posts answers on a platform.
 */
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import {
  MAX_EMOJIS_PER_SIGN,
  type ReactionSign,
  nextSign,
  normalizeEmoji,
  paletteFor,
  signOf,
  withSign,
} from "@/lib/reaction-feedback";
import { cn } from "@/lib/utils";
import type { ReactionFeedbackSetting } from "@/types";

/** What a palette button says it does, by the sign its emoji has now (i18n keys).
 *  @i18n-keys */
const SIGN_LABELS = {
  positive: "{emoji}: positive. Click to make it negative.",
  negative: "{emoji}: negative. Click to ignore it.",
  none: "{emoji}: ignored. Click to make it positive.",
} as const;

interface Props {
  value: ReactionFeedbackSetting;
  onChange: (value: ReactionFeedbackSetting) => void;
  /** The platform can report reactions at all. */
  available: boolean;
  /** Where reactions are read periodically rather than at once, said under the palette. */
  note?: string;
  disabled?: boolean;
}

export function ReactionFeedbackSettings({ value, onChange, available, note, disabled }: Props) {
  const { t_i18n } = useFormatter();
  const full = (sign: ReactionSign) =>
    sign !== null && value[sign].length >= MAX_EMOJIS_PER_SIGN;

  return (
    <div className="space-y-3 rounded-sm border border-elevation-subtle p-3">
      <label className="flex items-center justify-between gap-3">
        <span className="content-base font-medium">
          {t_i18n("Reactions as feedback")}
          <span className="block content-compact font-normal text-default-secondary">
            {t_i18n("A reaction someone puts on an answer rates it, in the agent's Feedback tab. Nobody needs an XTM One account; who reacted is kept only as a keyed fingerprint, never by name.")}
          </span>
        </span>
        <Switch
          checked={value.enabled && available}
          disabled={disabled || !available}
          onCheckedChange={(enabled) => onChange({ ...value, enabled })}
        />
      </label>
      {!available ? (
        <p className="content-compact text-default-secondary">{t_i18n("This platform does not report reactions.")}</p>
      ) : (
        value.enabled && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={t_i18n("Reactions as feedback")}>
              {paletteFor(value).map((emoji) => {
                const sign = signOf(value, emoji);
                const next = nextSign(sign);
                return (
                  <button
                    key={normalizeEmoji(emoji)}
                    type="button"
                    disabled={disabled || full(next)}
                    onClick={() => onChange(withSign(value, emoji, next))}
                    aria-label={t_i18n(SIGN_LABELS[sign ?? "none"], { emoji })}
                    title={t_i18n(SIGN_LABELS[sign ?? "none"], { emoji })}
                    className={cn(
                      "flex h-9 w-9 items-center justify-center rounded-sm border text-lg transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                      sign === "positive" && "border-accent bg-accent/10",
                      sign === "negative" && "border-feedback-error-primary bg-feedback-error-primary/10",
                      sign === null && "border-elevation-subtle opacity-60 hover:opacity-100",
                    )}
                  >
                    {emoji}
                  </button>
                );
              })}
            </div>
            <p className="content-compact text-default-secondary">
              {t_i18n("Click an emoji to cycle it: positive (cyan), negative (red), ignored. Skin tones count as the same emoji; someone holding a positive and a negative one at once does not rate the answer.")}
            </p>
            {note && <p className="content-compact text-default-secondary">{note}</p>}
          </div>
        )
      )}
    </div>
  );
}
