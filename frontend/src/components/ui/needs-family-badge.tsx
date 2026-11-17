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
 * "Needs family" indicator for a dataset with no declared output family.
 *
 * An unset family is a problem, not "any family": every evaluation on such a dataset
 * is refused at trigger, re-run, rescore and replay. Shown wherever a dataset is listed
 * or picked, so the problem is visible before it blocks a run. It explains itself on hover
 * and keyboard focus, with the glossary's sentence.
 *
 * Usage:
 *   <NeedsFamilyBadge />           → default size
 *   <NeedsFamilyBadge size="sm" /> → small icon, for card headers / compact rows; both use the caption type token
 */

import { AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";

interface NeedsFamilyBadgeProps {
  size?: "sm" | "default";
}

export function NeedsFamilyBadge({ size = "default" }: NeedsFamilyBadgeProps) {
  const { t_i18n } = useFormatter();
  const iconClass = size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3";
  return (
    <InfoLabel content={t_i18n(EVALUATION_TERMS.needsFamily)}>
      <Badge
        variant="outline"
        className="content-caption gap-1 whitespace-nowrap border-feedback-alert-primary/40 text-feedback-warning-primary"
      >
        <AlertTriangle className={iconClass} /> {t_i18n("Needs family")}
      </Badge>
    </InfoLabel>
  );
}
