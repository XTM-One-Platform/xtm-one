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
 * "Duplicate fleet": fork any fleet the reader sees, a built-in one included,
 * into an editable personal copy (`POST /fleets/{id}/fork`, #9174).
 *
 * The form: the copy's name (default "Copy of {name}", localized), the
 * workspace it works in (a new one named after the copy, or one the reader
 * manages), and what happens to the members: every member becomes an
 * editable copy, or the orchestrator is copied and the other members are
 * linked as connected agents (an agent has one home fleet). The dialog says
 * up front that every copied automation is created switched off, so a fork
 * starts no run; on save the fork's page opens and a toast lists what was
 * left out.
 *
 * Every field is a `SettingsField` (frontend-design-system.md, Forms): the
 * label above at 14 px, one help line at 12 px, an info bubble on each field.
 */
import { useMemo, useState, type KeyboardEvent } from "react";
import { CopyPlus, Link2, Users } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { OptionCard } from "@/components/ui/option-card";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useForkFleet, type Fleet, type FleetForkMembers } from "@/hooks/api/useFleetApi";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";

import { describeForkRefusal, forkSummary, initialForkForm, toForkRequest, type ForkFleetForm } from "./fork-fleet-form";
import { Spinner } from "@filigran/design-system";

/** Picker value for "create a workspace named after the copy". */
const NEW_WORKSPACE = "__new__";

const MEMBER_MODES: { value: FleetForkMembers; icon: React.ElementType; title: string; description: string }[] = [
  {
    value: "copy",
    icon: Users,
    title: "Copy every member",
    description: "Each member agent becomes an editable copy that you own. Handovers between members point at the copies.",
  },
  {
    value: "keep",
    icon: Link2,
    title: "Keep the members, copy the orchestrator",
    description: "The orchestrator is copied; the other members stay where they are and are connected to the copy.",
  },
];

export function ForkFleetDialog({
  fleet,
  open,
  onOpenChange,
}: {
  fleet: Fleet;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const fork = useForkFleet(fleet.id);
  // The fork route sits behind the AI gate: with AI off the button is visibly unavailable.
  const aiOff = useAiActionDisabled();
  const { data: workspaces } = useWorkspaces({ enabled: open, throwOnError: false });
  const [form, setForm] = useState<ForkFleetForm>(() => initialForkForm(fleet, t_i18n));
  const [serverError, setServerError] = useState<{ message: string; detail: string | null } | null>(null);

  const manageableWorkspaces = useMemo(
    () => (workspaces ?? []).filter((w) => w.can_manage && !w.is_archived),
    [workspaces],
  );
  // `keep` needs an orchestrator to copy: without one only `copy` is offered.
  const keepAllowed = !!fleet.orchestrator_agent_id;

  const update = (patch: Partial<ForkFleetForm>) => {
    setServerError(null);
    setForm((prev) => ({ ...prev, ...patch }));
  };

  const close = (next: boolean) => {
    if (!next) {
      setForm(initialForkForm(fleet, t_i18n));
      setServerError(null);
    }
    onOpenChange(next);
  };

  const save = async () => {
    if (aiOff) return;
    try {
      const result = await fork.mutateAsync(toForkRequest(form));
      const summary = forkSummary(result, t_i18n);
      toast({ title: summary.title, description: summary.lines.join(" "), variant: summary.variant });
      close(false);
      navigate(`/fleets/${result.fleet.id}`, { state: { backTo: "/fleets", backLabel: "Back to Fleets" } });
    } catch (err) {
      setServerError(describeForkRefusal(err, t_i18n));
    }
  };

  // Arrow keys move the choice, as in a native radio group.
  const onModeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.key) || !keepAllowed) return;
    event.preventDefault();
    const next: FleetForkMembers = form.members === "copy" ? "keep" : "copy";
    update({ members: next });
    document.getElementById(`fork-fleet-members-${next}`)?.focus();
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t_i18n("Duplicate fleet")}</DialogTitle>
          <DialogDescription>
            {fleet.is_builtin
              ? t_i18n("A built-in fleet is read-only. Its copy is yours to edit, and the platform never changes it.")
              : t_i18n("The copy is yours to edit. The original fleet is not changed.")}
          </DialogDescription>
        </DialogHeader>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <SettingsField
            id="fork-fleet-name"
            label={t_i18n("Name")}
            helper={t_i18n("The copy's name; left empty, the copy is named after the original fleet.")}
            info={t_i18n("The display name of the copy, up to 200 characters. Example: Copy of CEO Office. Read by people in the fleet list and by agents when they hand work to the copy. Left empty, the copy is named after the original fleet.")}
          >
            <Input
              id="fork-fleet-name"
              aria-label={t_i18n("Name")}
              aria-describedby={settingsFieldHintId("fork-fleet-name")}
              autoFocus
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              maxLength={200}
            />
          </SettingsField>

          <SettingsField
            id="fork-fleet-workspace"
            label={t_i18n("Workspace")}
            helper={t_i18n("Where the copy works: its work items, shared state and knowledge are kept there.")}
            info={t_i18n("The workspace that holds everything the copy's agents produce. Example: an existing Security operations workspace you manage. Read by every member's runs. Left on the default, a new workspace named after the copy is created.")}
          >
            <SearchableSelect
              id="fork-fleet-workspace"
              aria-label={t_i18n("Workspace")}
              aria-describedby={settingsFieldHintId("fork-fleet-workspace")}
              options={manageableWorkspaces.map((w) => ({ value: w.id, label: w.name }))}
              pinned={[{ value: NEW_WORKSPACE, label: t_i18n("A new workspace named after the fleet") }]}
              value={form.workspaceId ?? NEW_WORKSPACE}
              onValueChange={(value) => update({ workspaceId: value === NEW_WORKSPACE ? null : value })}
            />
          </SettingsField>

          <div className="space-y-1.5">
            {/* The radio group's name: the Label type (14 px), not a form label of one control. */}
            <div className="flex items-center gap-1.5">
              <span id="fork-fleet-members-label" className="block content-base font-medium leading-none text-default-primary">{t_i18n("Members")}</span>
              <InfoTip
                content={
                  <div className="max-w-xs space-y-1 content-compact">
                    {t_i18n("What the copy does with the original fleet's agents. Example: keep the members to reuse the same desks under a new orchestrator. Read once, when the copy is created. An agent has one home fleet, so a kept member stays in the original fleet.")}
                  </div>
                }
                label={t_i18n("About this field")}
              />
            </div>
            <div role="radiogroup" aria-labelledby="fork-fleet-members-label" className="grid gap-2" onKeyDown={onModeKeyDown}>
              {MEMBER_MODES.map((mode) => {
                const disabled = mode.value === "keep" && !keepAllowed;
                const selected = form.members === mode.value;
                return (
                  <OptionCard
                    key={mode.value}
                    id={`fork-fleet-members-${mode.value}`}
                    selected={selected}
                    tabIndex={selected ? 0 : -1}
                    disabled={disabled}
                    onSelect={() => update({ members: mode.value })}
                    icon={mode.icon}
                    title={t_i18n(mode.title)}
                    description={disabled ? t_i18n("This fleet has no orchestrator to copy.") : t_i18n(mode.description)}
                  />
                );
              })}
            </div>
          </div>

          <p className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-secondary">
            {t_i18n("Every automation is copied switched off, so the copy starts no run until you turn one on.")}
          </p>

          {serverError && <RefusalAlert message={serverError.message} detail={serverError.detail} />}

          <div className="sticky -bottom-6 -mb-6 flex justify-end gap-2 bg-elevation-default pb-6 pt-2">
            <Button type="button" variant="secondary" onClick={() => close(false)}>
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
