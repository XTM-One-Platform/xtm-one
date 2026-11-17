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
 * Family badge for a dataset or evaluation. The label comes from the
 * server's family catalog (`useEvaluationFamilies()`), never a local copy, translated
 * with the server's text as the key (`lib/evaluation-families.ts`).
 *
 * Renders nothing without a family; pair with `NeedsFamilyBadge` to flag that case.
 * The catalog's description of the family is its tooltip, on hover and keyboard focus;
 * a family the catalog does not describe shows the bare badge.
 */

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useEvaluationFamilies } from "@/hooks/api/useEvaluationApi";
import { useFormatter } from "@/i18n";
import { familyDescription, familyLabel } from "@/lib/evaluation-families";

interface FamilyBadgeProps {
  family: string | null | undefined;
  size?: "sm" | "default";
}

export function FamilyBadge({ family, size = "default" }: FamilyBadgeProps) {
  const { t_i18n } = useFormatter();
  const { data: familyCatalog } = useEvaluationFamilies();
  if (!family) return null;
  // Both sizes use the caption type token: a card row never mixes type sizes.
  const badge = (
    <Badge variant="cyan" className="content-caption" data-size={size}>
      {familyLabel(family, familyCatalog, t_i18n)}
    </Badge>
  );
  const description = familyDescription(familyCatalog?.find((f) => f.id === family), t_i18n);
  return description ? <InfoLabel content={description}>{badge}</InfoLabel> : badge;
}
