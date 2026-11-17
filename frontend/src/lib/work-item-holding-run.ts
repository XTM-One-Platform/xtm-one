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
 * How an ended holder's status reads: one translated phrase per status the
 * run and background-task tables record, "ended" for any other or a run no
 * longer recorded. Never the raw status word. @i18n-keys
 */
const ENDED_LABELS = {
  completed: "ended (completed)",
  failed: "ended (failed)",
  cancelled: "ended (cancelled)",
  timed_out: "ended (timed out)",
  dead_letter: "ended (abandoned)",
} as const;

/** The i18n key of an ended holding run's state, read by `t_i18n(endedLabel(status))`. */
export const endedLabel = (status: string | null | undefined): string =>
  status && status in ENDED_LABELS ? ENDED_LABELS[status as keyof typeof ENDED_LABELS] : "ended";
