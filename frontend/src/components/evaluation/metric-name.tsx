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

// A metric's one name, with what it measures as its tooltip: the selector, the
// Scoring card, the Configuration tab and the run page all show a metric through it.

import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { metricEntryDescription, metricEntryDisplayName, metricKeyDescription } from "@/lib/constants";

export function MetricName({
  metricKey,
  fallback,
  entryLabel,
  className,
}: {
  metricKey: string;
  /** The backend's label, for a key the table does not name. */
  fallback?: string | null;
  /** The name the package gives the metric on a single entry, when it gives one. */
  entryLabel?: string | null;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const name = metricEntryDisplayName(metricKey, t_i18n, entryLabel, fallback);
  const description = entryLabel ? metricEntryDescription(metricKey) : metricKeyDescription(metricKey);
  if (!description) return <span className={className}>{name}</span>;
  return (
    <InfoLabel content={t_i18n(description)} className={className}>
      {name}
    </InfoLabel>
  );
}
