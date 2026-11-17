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
 * "Duplicate skill": fork any skill the reader sees, a built-in one included,
 * into an editable personal copy (`POST /skills/{id}/fork`, #9174).
 *
 * The form: the copy's name (default `Copy of` the source's name) and,
 * optionally, a fleet the reader manages whose member agents using the source
 * are re-bound to the copy. The copy starts its own version history at v1; on
 * save the copy's page opens and a toast says what was re-bound and what was
 * left on the source.
 */
import { useMemo, useState } from "react";
import { CopyPlus } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useForkSkill } from "@/hooks/api/useSkillApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import type { AgentSkill } from "@/types";

import {
  defaultForkSkillName,
  describeSkillForkRefusal,
  initialForkSkillForm,
  skillForkSummary,
  toForkSkillRequest,
  type ForkSkillForm,
} from "./fork-skill-form";
import { Spinner } from "@filigran/design-system";

/** Picker value for "re-bind no fleet". */
const NO_FLEET = "__none__";

export function ForkSkillDialog({
  skill,
  isBuiltin,
  open,
  onOpenChange,
}: {
  skill: AgentSkill;
  isBuiltin: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const fork = useForkSkill(skill.id);
  // The fork route sits behind the AI gate: with AI off the button is visibly unavailable.
  const aiOff = useAiActionDisabled();
  const { data: fleets } = useFleets({ enabled: open, optional: true });
  const defaultName = defaultForkSkillName(skill, t_i18n);
  const [form, setForm] = useState<ForkSkillForm>(() => initialForkSkillForm(skill, t_i18n));
  const [serverError, setServerError] = useState<{ message: string; detail: string | null } | null>(null);

  // Only a fleet the reader may edit can have its agents re-bound; a built-in one is read-only.
  const manageableFleets = useMemo(
    () => (fleets ?? []).filter((f) => f.can_manage && !f.is_builtin && !f.is_archived),
    [fleets],
  );

  const update = (patch: Partial<ForkSkillForm>) => {
    setServerError(null);
    setForm((prev) => ({ ...prev, ...patch }));
  };

  const close = (next: boolean) => {
    if (!next) {
      setForm(initialForkSkillForm(skill, t_i18n));
      setServerError(null);
    }
    onOpenChange(next);
  };

  const save = async () => {
    if (aiOff) return;
    try {
      const result = await fork.mutateAsync(toForkSkillRequest(form, defaultName));
      const summary = skillForkSummary(result, t_i18n);
      toast({ title: summary.title, description: summary.lines.join(" "), variant: summary.variant });
      close(false);
      navigate(`/skills/${result.skill.id}`);
    } catch (err) {
      setServerError(describeSkillForkRefusal(err, t_i18n));
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t_i18n("Duplicate skill")}</DialogTitle>
          <DialogDescription>
            {isBuiltin
              ? t_i18n("A built-in skill is read-only. Its copy is yours to edit, with its own version history, and the platform never changes it.")
              : t_i18n("The copy is yours to edit, with its own version history. The original skill is not changed.")}
          </DialogDescription>
        </DialogHeader>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="fork-skill-name" className="content-base">
              {t_i18n("Name")} <span className="font-normal text-default-secondary">({t_i18n("Optional")})</span>
            </Label>
            <Input
              id="fork-skill-name"
              aria-label={t_i18n("Name")}
              aria-describedby="fork-skill-name-help"
              autoFocus
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              maxLength={200}
            />
            <p id="fork-skill-name-help" className="content-compact text-default-secondary">
              {t_i18n("Left empty, the copy is named {name}.", { name: defaultName })}
            </p>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <Label htmlFor="fork-skill-fleet" className="content-base">
                {t_i18n("Use the copy in a fleet")}{" "}
                <span className="font-normal text-default-secondary">({t_i18n("Optional")})</span>
              </Label>
              <InfoTip
                label={t_i18n("What using the copy in a fleet does")}
                content={t_i18n(
                  "Pick a fleet you manage: its member agents that use the original skill switch to the copy. Agents outside the fleet, and fleet agents you cannot edit, stay on the original. The original skill itself is never changed.",
                )}
              />
            </div>
            <SearchableSelect
              id="fork-skill-fleet"
              aria-label={t_i18n("Use the copy in a fleet")}
              aria-describedby="fork-skill-fleet-help"
              options={manageableFleets.map((f) => ({ value: f.id, label: f.name }))}
              pinned={[{ value: NO_FLEET, label: t_i18n("No fleet: keep every agent on the original skill") }]}
              value={form.repointFleetId ?? NO_FLEET}
              onValueChange={(value) => update({ repointFleetId: value === NO_FLEET ? null : value })}
            />
            <p id="fork-skill-fleet-help" className="content-compact text-default-secondary">
              {t_i18n("The fleet's agents that use the original skill switch to the copy. Agents you cannot edit keep the original.")}
            </p>
          </div>

          {serverError && <RefusalAlert message={serverError.message} detail={serverError.detail} />}

          <div className="sticky -bottom-6 -mb-6 flex justify-end gap-2 bg-elevation-default pb-6 pt-2">
            <Button type="button" variant="ghost" onClick={() => close(false)}>
              {t_i18n("Cancel")}
            </Button>
            <AiDisabledTip>
              <Button type="submit" className="gap-1.5" disabled={fork.isPending || aiOff} aria-busy={fork.isPending || undefined}>
                {fork.isPending && <Spinner size="sm" tone="inherit" />}{!fork.isPending && <CopyPlus className="h-4 w-4" />}
                {t_i18n("Duplicate")}
              </Button>
            </AiDisabledTip>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
