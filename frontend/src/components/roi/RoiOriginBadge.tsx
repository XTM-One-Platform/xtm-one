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
 * Default / Customized / Custom (#9836 R3b, design dev-docs/roi-outcomes.md
 * 10.4): where a catalog value comes from, one badge for every ROI surface
 * (units, rates, declarations). A customized value names who changed it and
 * when in a tooltip anchored to the badge itself, reachable from the
 * keyboard. Display only: nothing here starts a run.
 */
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import type { RoiOriginFields } from "@/hooks/api/useRoiCatalogApi";
import { useFormatter } from "@/i18n";
import { ORIGIN_LABELS, originOf, useCustomizedLine } from "./roi-origin";

export function RoiOriginBadge({ row }: { row: RoiOriginFields }) {
  const { t_i18n } = useFormatter();
  const line = useCustomizedLine()(row);
  const origin = originOf(row);
  const badge = (
    <Badge variant={origin === "default" ? "outline" : "secondary"} className="whitespace-nowrap" data-testid="roi-origin-badge">
      {t_i18n(ORIGIN_LABELS[origin])}
    </Badge>
  );
  if (!line) return badge;
  return (
    <Tip content={line} side="top">
      <BadgeFocusTrigger aria-label={`${t_i18n(ORIGIN_LABELS[origin])}. ${line}`}>{badge}</BadgeFocusTrigger>
    </Tip>
  );
}
