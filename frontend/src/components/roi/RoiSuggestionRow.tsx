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
 * The unit of work suggested for a custom agent, accepted in one click (#9836 R8b;
 * design dev-docs/roi-outcomes.md 10.3). One row shared by the agent's Outcomes
 * card and the create wizard, so the wording and the "why" never differ. The
 * suggestion is a token match over the catalog (`GET /roi/suggest`): no run, no
 * model call. The row says which words matched, so the pick is never a black box.
 */
import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import type { RoiUnitSuggestion } from "@/hooks/api/useRoiDeclarationApi";
import { useFormatter } from "@/i18n";

export function RoiSuggestionRow({
  suggestion,
  unitName,
  accepted = false,
  pending = false,
  onAccept,
  onUndo,
  testId = "roi-suggestion",
}: {
  suggestion: RoiUnitSuggestion;
  /** The unit's display name as the catalog shows it (falls back to the suggestion's own name). */
  unitName?: string;
  /** The wizard keeps the choice until the agent is created: the row then reads as chosen, with Undo. */
  accepted?: boolean;
  pending?: boolean;
  onAccept: () => void;
  onUndo?: () => void;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  const name = unitName || suggestion.name;
  const why = (
    <div className="max-w-xs space-y-1 content-compact">
      <p>{t_i18n("Suggested from the words this agent's name, description and output share with the unit: {terms}.", { terms: suggestion.matched_terms.join(", ") })}</p>
      <p>{t_i18n("Accepting counts one unit per finished run. You can change the unit or the counting mode afterwards.")}</p>
    </div>
  );
  return (
    <div
      data-testid={testId}
      className="flex flex-wrap items-center justify-between gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2"
    >
      <div className="flex min-w-0 items-center gap-1.5 content-compact">
        <Sparkles className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
        <span className="min-w-0 text-default-primary">
          {accepted ? t_i18n("Counted in ROI as {unit}", { unit: name }) : t_i18n("Suggested unit: {unit}", { unit: name })}
        </span>
        <InfoTip label={t_i18n("Why this unit")} content={why} />
      </div>
      {accepted ? (
        onUndo && (
          <Button type="button" variant="ghost" onClick={onUndo}>
            {t_i18n("Undo")}
          </Button>
        )
      ) : (
        <Button type="button" variant="outline" onClick={onAccept} disabled={pending}>
          {t_i18n("Use this unit")}
        </Button>
      )}
    </div>
  );
}
