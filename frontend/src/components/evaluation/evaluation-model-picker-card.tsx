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

import { type ReactNode } from "react";
import { Cpu, X } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ModelSelect } from "@/components/ui/model-select";
import { Switch } from "@/components/ui/switch";
import { InfoLabel } from "@/components/ui/info-tip";
import { Tip } from "@/components/ui/tooltip";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import { parseModelChoice } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { UnavailableModelBadge } from "@/components/evaluation/unavailable-model";

interface EvaluationModelPickerCardModel {
  model: string;
  provider: string;
  /**
   * ConfigTab-only enable/disable state: a disabled model keeps its score
   * history, unlike a removed one. `undefined` on the creation page; the
   * Enabled/Disabled badge renders only when it is set.
   */
  disabled?: boolean;
  /** A saved model no provider of the owner offers: marked, its provider unknown. */
  unavailable?: boolean;
}

export interface EvaluationModelPickerCardProps {
  editing: boolean;
  /** Only rendered/used when `editing`. */
  useAgentDefaultModel: boolean;
  onUseAgentDefaultModelChange: (v: boolean) => void;
  canDisableDefaultModel: boolean;
  /** Extra models list — always shown regardless of mode. */
  selectedModels: EvaluationModelPickerCardModel[];
  /** Add/remove props — only rendered when `editing`. The models not added yet; the picker searches them. */
  availableModelsToAdd: EvaluationModelPickerCardModel[];
  onAddModel: (model: string) => void;
  onRemoveModel: (model: string) => void;
  /** Read-only-mode display value — ignored when `editing`. */
  evaluationUseAgentDefaultModel: boolean;
  /**
   * ConfigTab-only per-model "Active" toggle, rendered in `editing` mode just
   * before the row's remove button. `undefined` on the creation page.
   */
  modelRowExtra?: (model: EvaluationModelPickerCardModel) => ReactNode;
}

/**
 * What the Models card's states mean: each agent runs on its own model and on
 * every extra one (`services/evaluation/launch.py`, `plan_launch`), and a disabled extra
 * model is skipped by a launch (`active_model_ids`). @i18n-keys
 */
const MODEL_NOTES = {
  count:
    "Default: each agent runs on its own model. Extra: models added here, each run with every agent. A launch makes one run per agent and model.",
  cannotTurnOff: "Add a model first: with this off and no model added, nothing would run.",
} as const;

/** A term about an extra model's enabled state ("Active", "Enabled"), explaining it. */
export function ModelActiveLabel({ className, children }: { className?: string; children: ReactNode }) {
  const { t_i18n } = useFormatter();
  return <InfoLabel content={t_i18n(EVALUATION_TERMS.enabledModel)} className={className}>{children}</InfoLabel>;
}

/**
 * The "Models" card of the evaluation creation page and of `EvaluationDetail.tsx`'s
 * Configuration tab, in `editing` or read-only mode.
 */
export function EvaluationModelPickerCard({
  editing,
  useAgentDefaultModel,
  onUseAgentDefaultModelChange,
  canDisableDefaultModel,
  selectedModels,
  availableModelsToAdd,
  onAddModel,
  onRemoveModel,
  evaluationUseAgentDefaultModel,
  modelRowExtra,
}: EvaluationModelPickerCardProps) {
  const { t_i18n } = useFormatter();
  const effectiveDefault = editing ? useAgentDefaultModel : evaluationUseAgentDefaultModel;
  const extraCount = selectedModels.length;

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Cpu className="h-3.5 w-3.5 text-default-secondary" />
          <CardTitle className="text-base font-medium">{t_i18n("Models")}</CardTitle>
          {(effectiveDefault || extraCount > 0) && (
            <InfoLabel content={t_i18n(MODEL_NOTES.count)}>
              <Badge variant="outline" className="content-caption">
                {effectiveDefault ? t_i18n("default") : ""}{effectiveDefault && extraCount > 0 ? " + " : ""}{extraCount > 0 ? t_i18n("{count} extra", { count: extraCount }) : ""}
              </Badge>
            </InfoLabel>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between rounded-sm bg-elevation-default px-4 py-3">
          <div className="space-y-0.5">
            <p className="content-base font-medium">{t_i18n("Use agent's own model")}</p>
            <p className="content-compact text-default-secondary">{t_i18n("Each agent runs on its configured model (or platform default)")}</p>
          </div>
          {editing ? (
            // Disabled while it is the only source of models: why, on hover and focus.
            <Tip content={canDisableDefaultModel ? undefined : t_i18n(MODEL_NOTES.cannotTurnOff)} wrap="auto">
              <Switch aria-label={t_i18n("Use agent's own model")} checked={useAgentDefaultModel} onCheckedChange={onUseAgentDefaultModelChange} disabled={!canDisableDefaultModel} />
            </Tip>
          ) : (
            <Badge variant={evaluationUseAgentDefaultModel ? "cyan" : "outline"} className="content-caption">
              {evaluationUseAgentDefaultModel ? t_i18n("Enabled") : t_i18n("Disabled")}
            </Badge>
          )}
        </div>

        <div className="space-y-2">
          {editing && (
            <>
              <p className="content-compact text-default-secondary">
                {useAgentDefaultModel ? t_i18n("Add extra models to compare against each agent's default:") : t_i18n("Select models to evaluate:")}
              </p>
              {/* The list opens above the page, never under the next card. */}
              <ModelSelect
                // Keyed on the count: each addition starts an empty field again.
                key={selectedModels.length}
                aria-label={t_i18n("Search models to add...")}
                placeholder={t_i18n("Search models to add...")}
                models={availableModelsToAdd}
                value=""
                // An evaluation stores the bare model id: the provider pin is dropped.
                onValueChange={(choice) => onAddModel(parseModelChoice(choice).model)}
                emptyMessage={availableModelsToAdd.length > 0 ? t_i18n("No matching models.") : t_i18n("All models are already added.")}
              />
            </>
          )}

          {selectedModels.length > 0 ? (
            <div className="space-y-1">
              {!editing && <p className="content-compact text-default-secondary mb-1">{t_i18n("Additional models:")}</p>}
              {selectedModels.map((m) => (
                <div key={m.model} className="flex items-center gap-3 rounded-sm bg-elevation-default px-4 py-2.5 group hover:bg-elevation-hover transition-colors">
                  <Cpu className="h-4 w-4 text-accent shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="content-base font-medium font-mono-plex truncate">{m.model}</p>
                  </div>
                  {m.disabled !== undefined && (
                    <ModelActiveLabel className="shrink-0">
                      <Badge variant={m.disabled ? "outline" : "default"} className="text-[9px]">
                        {m.disabled ? t_i18n("Disabled") : t_i18n("Enabled")}
                      </Badge>
                    </ModelActiveLabel>
                  )}
                  {m.unavailable ? (
                    <UnavailableModelBadge />
                  ) : (
                    <span className="content-caption text-default-secondary shrink-0">{PROVIDER_DISPLAY_NAMES[m.provider] || m.provider}</span>
                  )}
                  {editing && (
                    <>
                      {modelRowExtra?.(m)}
                      <Tip content={t_i18n("Remove model")}>
                        <button aria-label={t_i18n("Remove model")}
                          type="button"
                          className="h-7 w-7 flex items-center justify-center rounded-sm text-default-secondary hover:text-feedback-error-primary transition-all"
                          onClick={() => onRemoveModel(m.model)}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </Tip>
                    </>
                  )}
                </div>
              ))}
            </div>
          ) : (
            editing && !useAgentDefaultModel && (
              <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
                <Cpu className="h-6 w-6 text-default-secondary mx-auto mb-2" />
                <p className="content-compact text-default-secondary">{t_i18n("No models added yet. Search and add models above.")}</p>
              </div>
            )
          )}
          {!editing && selectedModels.length === 0 && !evaluationUseAgentDefaultModel && (
            <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
              <Cpu className="h-6 w-6 text-default-secondary mx-auto mb-2" />
              <p className="content-compact text-default-secondary">{t_i18n("No models configured.")}</p>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
