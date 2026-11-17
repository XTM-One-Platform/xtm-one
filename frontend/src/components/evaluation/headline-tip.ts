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

// A headline value's tooltip on the evaluation and run pages: what the headline
// is, then the metrics that composed this value.

import { metricDisplayName } from "@/lib/constants";
import type { Headline } from "@/lib/evaluation-headlines";

export function headlineTip(
  headline: Headline,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): string {
  const from = headline.metricKeys.map((k) => metricDisplayName(k, t_i18n)).join(", ");
  return from ? `${t_i18n(headline.note)} ${t_i18n("From: {keys}", { keys: from })}` : t_i18n(headline.note);
}
