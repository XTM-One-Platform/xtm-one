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
 * Fleet settings side panel, on the fleet page.
 *
 * A side panel over the page, never a block above the graph (amendment 3):
 * name, description, icon and colour, the workspace the fleet works in
 * (exactly one), the orchestrator, the home members other fleets may reach
 * (the Fleet Card's exposed agents), visibility and sharing with the same
 * card as every other shareable entity, archive and delete. Delete never
 * deletes an agent: the members go back to the playground.
 *
 * The owner, a `config` grantee and an admin edit (`can_manage`); anyone else
 * reads the same panel with every control disabled and the reason on top.
 * Every write goes through `/api/v1/fleets` (`useUpdateFleet`,
 * `useSetFleetExposure`, `useDeleteFleet`): a membership change is never an
 * agent edit (no config version, no evaluation trigger).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Archive, ArchiveRestore, Lock, Trash2, Wallet, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { ColorPicker, Textarea, Spinner } from "@filigran/design-system";

import { BudgetLimitFields } from "@/components/budget/BudgetLimitFields";
import { OutcomesCard } from "@/components/roi/OutcomesCard";
import { Button } from "@/components/ui/button";
import { CompanyManagedSwitch } from "@/components/ui/company-managed-switch";
import { ConfirmDialog, useConfirm } from "@/components/ui/confirm-dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { IconPicker } from "@/components/ui/icon-picker";
import { Input } from "@/components/ui/input";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { MultiSelect } from "@/components/ui/multi-select";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SlideOver } from "@/components/ui/slide-over";
import { useToast } from "@/components/ui/toast";
import {
  useDeleteFleet,
  useSetFleetExposure,
  useUpdateFleet,
  type Fleet,
  type FleetMember,
  type FleetSettingsUpdate,
} from "@/hooks/api/useFleetApi";
import { useBudgetPolicy } from "@/hooks/api/useAgentApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import { MAX_ITERATIONS_DEFAULT, clampMaxIterations } from "@/lib/constants";
import { blockedLimits } from "@/lib/run-budget-limits";
import type { Agent } from "@/types";

import {
  DEFAULT_FLEET_COLOR,
  changedSettings,
  explicitExposure,
  exposureToSend,
  BUILTIN_ADMIN_REASON,
  builtinActivityEditable,
  fleetEditRights,
  toggledExposure,
} from "./fleet-edition";
import { fleetRefusalMessage } from "./fleet-refusals";
import { runDefaultsChanges, runDefaultsDraft, type RunDefaultsDraft } from "./fleet-run-defaults";

interface SettingsForm {
  name: string;
  description: string;
  icon: string;
  color: string;
  workspace_id: string;
  orchestrator_agent_id: string | null;
  is_company_managed: boolean;
  hide_activity_from_non_admins: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
}

function formOf(fleet: Fleet): SettingsForm {
  return {
    name: fleet.name,
    description: fleet.description,
    icon: fleet.icon,
    color: fleet.color,
    workspace_id: fleet.workspace_id,
    orchestrator_agent_id: fleet.orchestrator_agent_id,
    is_company_managed: fleet.is_company_managed,
    hide_activity_from_non_admins: fleet.hide_activity_from_non_admins ?? false,
    shared_group_ids: fleet.shared_group_ids ?? null,
    shared_group_permissions: fleet.shared_group_permissions ?? null,
  };
}

export function FleetSettingsPanel({
  fleet,
  members,
  agents,
  open,
  onClose,
  initialSection,
}: {
  fleet: Fleet;
  members: readonly FleetMember[];
  agents: readonly Agent[];
  open: boolean;
  onClose: () => void;
  /** Opens the panel scrolled to this section, focus on its first control (UX #9790 B1). */
  initialSection?: "outcomes";
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const { settings: rights } = fleetEditRights(fleet);
  const readOnly = !rights.canEdit;
  // A built-in fleet stays read-only, except "Hide activity from non-admins" for an administrator (#11624).
  const activityOnly = readOnly && builtinActivityEditable(fleet, isAdmin);
  const canSave = !readOnly || activityOnly;
  const updateFleet = useUpdateFleet(fleet.id);
  const setExposure = useSetFleetExposure(fleet.id);
  const deleteFleet = useDeleteFleet();
  const { data: workspaces } = useWorkspaces({ enabled: open, throwOnError: false });
  const shareGrants = useUserShareGrants(fleet);
  const [form, setForm] = useState<SettingsForm>(() => formOf(fleet));
  // Seeded from the members' own flags: the stored list also names the implicit orchestrator.
  const storedExposure = useMemo(() => explicitExposure(members), [members]);
  const [exposed, setExposed] = useState<string[]>(storedExposure);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A missing name is said under the Name field itself; `error` holds the refusals of the form as a whole.
  const [nameError, setNameError] = useState<string | null>(null);
  // The Outcomes card saves on its own: while it holds a draft, the panel's
  // Save and Close never drop it silently (UX #9545 B1).
  const [outcomesEditing, setOutcomesEditing] = useState(false);
  const [discardDialog, confirmDiscard] = useConfirm();
  const closePanel = useCallback(async () => {
    if (outcomesEditing) {
      const ok = await confirmDiscard({
        title: t_i18n("Unsaved changes"),
        description: t_i18n("The outcomes draft is not saved. Close the panel and lose it?"),
        confirmLabel: t_i18n("Discard"),
        variant: "warning",
      });
      if (!ok) return;
    }
    onClose();
  }, [outcomesEditing, confirmDiscard, onClose, t_i18n]);
  // Run defaults: the home members' budget limits and iteration cap.
  const { data: budgetPolicy } = useBudgetPolicy();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const platformMaxIterations = clampMaxIterations(gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT);
  const [runDefaults, setRunDefaults] = useState<RunDefaultsDraft>(() => runDefaultsDraft(fleet));
  const blockedBudget = useMemo(
    () => blockedLimits(runDefaults.limits, budgetPolicy, fleet.budget_config ?? null),
    [runDefaults.limits, budgetPolicy, fleet.budget_config],
  );

  // "Set the fleet default" lands on the Outcomes section, not on the fleet's name (UX #9790 B1).
  const outcomesRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open || initialSection !== "outcomes") return;
    const frame = requestAnimationFrame(() => {
      const section = outcomesRef.current;
      if (!section) return;
      section.scrollIntoView?.({ block: "start" });
      section.querySelector<HTMLElement>("button:not([disabled]), input:not([disabled]), [role='combobox']")?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [open, initialSection]);

  // Re-seed on open and whenever the stored fleet changes under a closed panel.
  useEffect(() => {
    if (!open) return;
    setForm(formOf(fleet));
    setExposed(storedExposure);
    setRunDefaults(runDefaultsDraft(fleet));
    shareGrants.reset();
    setError(null);
    setNameError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-seed on open only
  }, [open, fleet.id, fleet.updated_at, storedExposure.join(",")]);

  const update = (patch: Partial<SettingsForm>) => {
    setError(null);
    if (patch.name !== undefined) setNameError(null);
    setForm((prev) => ({ ...prev, ...patch }));
  };

  const agentById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const homeAgents = useMemo(
    () =>
      members
        .filter((m) => m.role === "member" && m.agent_id)
        .map((m) => agentById.get(m.agent_id as string) ?? ({ id: m.agent_id, name: m.name } as Agent)),
    [members, agentById],
  );
  const workspaceOptions = useMemo(() => {
    const list = (workspaces ?? []).filter((w) => (w.can_manage && !w.is_archived) || w.id === fleet.workspace_id);
    const options = list.map((w) => ({ value: w.id, label: w.name }));
    if (!options.some((o) => o.value === fleet.workspace_id)) {
      options.unshift({ value: fleet.workspace_id, label: fleet.workspace_name || fleet.workspace_id });
    }
    return options;
  }, [workspaces, fleet.workspace_id, fleet.workspace_name]);

  const exposedShown = exposed.filter((id) => id !== form.orchestrator_agent_id);

  const saveActivityOnly = async () => {
    if (form.hide_activity_from_non_admins === (fleet.hide_activity_from_non_admins ?? false)) {
      onClose();
      return;
    }
    try {
      await updateFleet.mutateAsync({ hide_activity_from_non_admins: form.hide_activity_from_non_admins });
    } catch (err) {
      setError(fleetRefusalMessage(err, t_i18n) ?? t_i18n("The fleet was not saved."));
      return;
    }
    toast({ title: t_i18n("Fleet saved"), variant: "success" });
    onClose();
  };

  const save = async () => {
    if (activityOnly) {
      await saveActivityOnly();
      return;
    }
    if (outcomesEditing) {
      setError(t_i18n("Save or cancel the outcomes draft first: the panel Save does not save it."));
      document.getElementById("fleet-roi-outcomes-card-unit")?.focus();
      return;
    }
    if (!form.name.trim()) {
      setError(null);
      setNameError(t_i18n("A fleet needs a name."));
      document.getElementById("fleet-settings-name")?.focus();
      return;
    }
    if (blockedBudget.size > 0) {
      setError(t_i18n("{count, plural, one {# limit needs an administrator} other {# limits need an administrator}}", { count: blockedBudget.size }));
      return;
    }
    const before = formOf(fleet);
    const changed = changedSettings(before as unknown as Record<string, unknown>, {
      ...form,
      name: form.name.trim(),
    } as unknown as Record<string, unknown>) as Partial<SettingsForm>;
    const body: FleetSettingsUpdate = {};
    if (changed.name !== undefined) body.name = changed.name;
    if (changed.description !== undefined) body.description = changed.description;
    if (changed.icon !== undefined) body.icon = changed.icon;
    if (changed.color !== undefined) body.color = changed.color;
    if (changed.workspace_id !== undefined) body.workspace_id = changed.workspace_id;
    if ("orchestrator_agent_id" in changed) body.orchestrator_agent_id = changed.orchestrator_agent_id ?? null;
    if (isAdmin && changed.is_company_managed !== undefined) body.is_company_managed = changed.is_company_managed;
    if (isAdmin && changed.hide_activity_from_non_admins !== undefined) {
      body.hide_activity_from_non_admins = changed.hide_activity_from_non_admins;
    }
    if (changed.shared_group_ids !== undefined) body.shared_group_ids = form.is_company_managed ? [] : (form.shared_group_ids ?? []);
    if (changed.shared_group_permissions !== undefined) body.shared_group_permissions = form.shared_group_permissions ?? {};
    const grants = shareGrants.toPayload(form.is_company_managed);
    body.shared_user_ids = grants.shared_user_ids ?? [];
    body.shared_user_permissions = grants.shared_user_permissions ?? {};
    body.access_restricted = grants.access_restricted;
    Object.assign(body, runDefaultsChanges(fleet, runDefaults));
    // Sent after the orchestrator is stored, from the explicit flags only (never the implicit orchestrator).
    const exposure = exposureToSend(
      exposedShown,
      storedExposure,
      form.orchestrator_agent_id,
      form.orchestrator_agent_id !== fleet.orchestrator_agent_id,
    );
    // An archived fleet refuses exposure writes: refuse before the fleet PUT, never half a save.
    if (fleet.is_archived && exposure !== null) {
      setError(t_i18n("This fleet is archived: restore it to edit its members."));
      return;
    }
    try {
      await updateFleet.mutateAsync(body);
    } catch (err) {
      setError(fleetRefusalMessage(err, t_i18n) ?? t_i18n("The fleet was not saved."));
      return;
    }
    // The exposure follows the stored orchestrator, so it cannot go first: when it is
    // refused (the fleet archived on the server meanwhile), say the rest was saved.
    if (exposure !== null) {
      try {
        await setExposure.mutateAsync(exposure);
      } catch (err) {
        const reason = fleetRefusalMessage(err, t_i18n);
        setError(
          reason
            ? t_i18n("The other settings were saved, but the exposure was not: {reason}", { reason })
            : t_i18n("The other settings were saved, but the exposure was not."),
        );
        return;
      }
    }
    toast({ title: t_i18n("Fleet saved"), variant: "success" });
    onClose();
  };

  const toggleArchive = async () => {
    try {
      await updateFleet.mutateAsync({ is_archived: !fleet.is_archived });
      toast({ title: fleet.is_archived ? t_i18n("Fleet restored") : t_i18n("Fleet archived"), variant: "success" });
    } catch (err) {
      setError(fleetRefusalMessage(err, t_i18n) ?? t_i18n("The fleet was not saved."));
    }
  };

  const remove = async () => {
    try {
      await deleteFleet.mutateAsync(fleet.id);
      toast({ title: t_i18n("Fleet deleted"), description: t_i18n("Its members are back in the playground."), variant: "success" });
      onClose();
      navigate("/fleets");
    } catch (err) {
      setError(fleetRefusalMessage(err, t_i18n) ?? t_i18n("The fleet was not deleted."));
    }
  };

  const saving = updateFleet.isPending || setExposure.isPending;

  return (
    <SlideOver open={open} onClose={() => void closePanel()} label={t_i18n("Fleet settings")} width="w-[480px]">
      <div className="flex items-center justify-between border-b border-elevation-subtle px-5 py-3">
        <h2 className="content-base font-semibold">{t_i18n("Fleet settings")}</h2>
        <Button type="button" variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={() => void closePanel()}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <form
        className="flex min-h-0 flex-1 flex-col"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) void save();
        }}
      >
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
          {readOnly && (
            <p role="note" className="flex items-start gap-2 rounded-sm bg-elevation-hover px-3 py-2 content-compact text-default-secondary">
              <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              {t_i18n(activityOnly ? BUILTIN_ADMIN_REASON : rights.reason)}
            </p>
          )}

          {/* Outside the read-only fieldset: the one setting an administrator keeps on a built-in fleet. */}
          {activityOnly && (
            <CompanyManagedSwitch
              icon="shield-check"
              label={t_i18n("Hide activity from non-admins")}
              description={t_i18n("Runs, traces, work items and shared state of the members and of the fleet's workspace are visible to administrators only.")}
              checked={form.hide_activity_from_non_admins}
              onCheckedChange={(checked) => update({ hide_activity_from_non_admins: checked })}
            />
          )}

          {/* `min-w-0`: a fieldset's default `min-width: min-content` would let a long option widen it past the panel. */}
          <fieldset disabled={readOnly} className="min-w-0 space-y-5">
            <SettingsField
              id="fleet-settings-name"
              required
              label={t_i18n("Name")}
              helper={t_i18n("Shown on the fleet page, in the fleet list and to the agents the fleet works with.")}
              info={t_i18n("The fleet's display name, up to 200 characters. Example: CEO Office. Read by people in the fleet list and by agents when they hand work to the fleet. Required: a fleet without a name is not created.")}
              error={nameError ?? undefined}
            >
              <Input
                id="fleet-settings-name"
                aria-label={t_i18n("Name")}
                aria-required
                aria-describedby={settingsFieldHintId("fleet-settings-name")}
                value={form.name}
                maxLength={200}
                onChange={(e) => update({ name: e.target.value })}
                aria-invalid={!!nameError}
              />
            </SettingsField>

            <SettingsField
              id="fleet-settings-description"
              label={t_i18n("Description (optional)")}
              helper={t_i18n("What the fleet is for, in a sentence or two, shown on the fleet page.")}
              info={t_i18n("A sentence or two on the fleet's purpose. Example: Prepares the CEO's week and follows up every open loop. Read by people on the fleet page. Left empty, the page shows no description.")}
            >
              <Textarea
                id="fleet-settings-description"
                aria-describedby={settingsFieldHintId("fleet-settings-description")}
                value={form.description}
                rows={3}
                maxLength={2000}
                onChange={(e) => update({ description: e.target.value })}
              />
            </SettingsField>

            {/* One label spacing and one control height (36 px: IconPicker's h-9, the ColorPicker field) on both sides.
                Input's className lands on its wrapper, so the field is sized through it. */}
            <div className="grid grid-cols-2 items-start gap-4">
              <SettingsField
                id="fleet-settings-icon"
                label={t_i18n("Icon")}
                helper={t_i18n("Shown beside the fleet's name in the fleet list and on its page.")}
                info={t_i18n("The small picture shown beside the fleet's name. Example: a shield for a security fleet. Read by people in the fleet list and on the fleet page. Left on the default, the fleet shows the default fleet icon.")}
              >
                <div id="fleet-settings-icon" role="group" aria-labelledby={settingsFieldLabelId("fleet-settings-icon")} aria-describedby={settingsFieldHintId("fleet-settings-icon")}>
                  <IconPicker value={form.icon} onChange={(icon) => update({ icon })} disabled={readOnly} />
                </div>
              </SettingsField>
              <SettingsField
                id="fleet-settings-color"
                label={t_i18n("Colour")}
                helper={t_i18n("The colour of the fleet's icon and badge.")}
                info={t_i18n("The colour of the fleet's icon and badge, picked from the palette. Example: a dark blue. Read by people in the fleet list and on the fleet page. Left on the default, the fleet uses the default fleet colour.")}
              >
                {/* The design-system ColorPicker (#10533): a hex field with its swatch and panel; a draft that
                    does not parse is kept off the form. It sets its own input id and aria-describedby after the
                    props spread (LIBRARY-FEEDBACK F46), so the SettingsField label and helper name it by id. */}
                <ColorPicker
                  aria-labelledby={`${settingsFieldLabelId("fleet-settings-color")} ${settingsFieldHintId("fleet-settings-color")}`}
                  formats={["hex"]}
                  eyeDropper={false}
                  disabled={readOnly}
                  value={/^#[0-9a-f]{6}$/i.test(form.color) ? form.color : DEFAULT_FLEET_COLOR}
                  onValueChange={(color) => {
                    if (/^#[0-9a-f]{6}$/i.test(color)) update({ color });
                  }}
                />
              </SettingsField>
            </div>

            <SettingsField
              id="fleet-settings-workspace"
              label={t_i18n("Workspace")}
              helper={t_i18n("The fleet works in this workspace: its work items, shared state and knowledge are kept there.")}
              info={t_i18n("The workspace that holds what the fleet's agents produce: work items, shared state and knowledge. Example: a Security operations workspace you manage. Read by every member's runs, from their next run. Only the workspaces you manage are listed; a fleet always works in exactly one.")}
            >
              <SearchableSelect
                id="fleet-settings-workspace"
                aria-label={t_i18n("Workspace")}
                aria-describedby={settingsFieldHintId("fleet-settings-workspace")}
                options={workspaceOptions}
                value={form.workspace_id}
                onValueChange={(value) => value && update({ workspace_id: value })}
                disabled={readOnly}
              />
            </SettingsField>

            <SettingsField
              id="fleet-settings-orchestrator"
              label={t_i18n("Orchestrator")}
              helper={t_i18n("Optional: the agent other fleets reach first, always exposed to them.")}
              info={t_i18n("The member that receives work from other fleets and hands it on. Example: an Assistant agent that routes requests to the desks. Read by other fleets when they delegate. Left empty, the fleet has no orchestrator.")}
            >
              <SearchableSelect
                id="fleet-settings-orchestrator"
                aria-label={t_i18n("Orchestrator")}
                aria-describedby={settingsFieldHintId("fleet-settings-orchestrator")}
                options={homeAgents.map((a) => agentSelectOption(a))}
                value={form.orchestrator_agent_id}
                onValueChange={(value) => update({ orchestrator_agent_id: value || null })}
                placeholder={t_i18n("Search agents...")}
                emptyMessage={t_i18n("Add a home member first")}
                clearable
                disabled={readOnly}
              />
            </SettingsField>

            <SettingsField
              id="fleet-settings-exposed"
              label={t_i18n("Exposed to other fleets")}
              helper={t_i18n("Other fleets reach the orchestrator, and the members you add here.")}
              info={t_i18n("The home members other fleets may call directly. Example: a Developer agent other fleets ask for code changes. Read by other fleets when they delegate. Left empty, only the orchestrator is reachable.")}
            >
              <MultiSelect
                id="fleet-settings-exposed"
                options={homeAgents
                  .filter((a) => a.id !== form.orchestrator_agent_id)
                  .map((a) => ({ value: a.id, label: a.name }))}
                selected={exposedShown}
                onToggle={(id) => setExposed((prev) => toggledExposure(prev, id, form.orchestrator_agent_id))}
                placeholder={t_i18n("Only the orchestrator")}
                aria-label={t_i18n("Exposed to other fleets")}
                aria-describedby={settingsFieldHintId("fleet-settings-exposed")}
                searchPlaceholder={t_i18n("Search agents...")}
                emptyOptionsLabel={t_i18n("Only the orchestrator")}
                noMatchLabel={t_i18n("No match")}
              />
            </SettingsField>

            <SettingsField
              id="fleet-settings-visibility"
              label={t_i18n("Visibility and sharing")}
              helper={t_i18n("Who sees the fleet besides you: every user, or the groups and people you share it with.")}
              info={t_i18n("Who besides you sees and uses the fleet, and with which rights. Example: shared with a Security team group as Read only. Read by the platform each time someone opens the fleets. Left unshared and not company-managed, only you see the fleet.")}
            >
              <div id="fleet-settings-visibility" role="group" aria-labelledby={settingsFieldLabelId("fleet-settings-visibility")} className="space-y-2">
                {isAdmin && (
                  <CompanyManagedSwitch
                    label={t_i18n("Company-managed fleet")}
                    description={
                      form.is_company_managed
                        ? t_i18n("Every user sees this fleet.")
                        : t_i18n("Only you and the people you share it with see this fleet.")
                    }
                    checked={form.is_company_managed}
                    onCheckedChange={(checked) => update({ is_company_managed: checked })}
                    disabled={readOnly}
                  />
                )}
                {/* Admins only, on a company-managed fleet, built-in fleets included (#11624). */}
                {isAdmin && form.is_company_managed && !activityOnly && (
                  <CompanyManagedSwitch
                    icon="shield-check"
                    label={t_i18n("Hide activity from non-admins")}
                    description={t_i18n("Runs, traces, work items and shared state of the members and of the fleet's workspace are visible to administrators only.")}
                    checked={form.hide_activity_from_non_admins}
                    onCheckedChange={(checked) => update({ hide_activity_from_non_admins: checked })}
                    disabled={readOnly}
                  />
                )}
                <PrincipalSharingCard
                  sharedGroupIds={form.shared_group_ids}
                  onChange={(ids) => update({ shared_group_ids: ids })}
                  sharedGroupPermissions={form.shared_group_permissions}
                  onPermissionsChange={(perms) => update({ shared_group_permissions: perms })}
                  userSharing={shareGrants.props}
                  entityType="fleet"
                  isCompanyManaged={form.is_company_managed}
                  disabled={readOnly}
                />
              </div>
            </SettingsField>

            <section aria-labelledby="fleet-run-defaults-title" className="space-y-3 border-t border-elevation-subtle-soft pt-4">
              <div className="space-y-1">
                <h3 id="fleet-run-defaults-title" className="flex items-center gap-1.5 content-base font-semibold">
                  <Wallet className="h-3.5 w-3.5 text-accent" aria-hidden />
                  {t_i18n("Run defaults")}
                </h3>
                <p className="content-compact text-default-secondary">
                  {t_i18n("What one run of a home member may spend when the agent sets no limit of its own. An agent's own value always wins; leave a field empty to inherit the platform budget.")}
                </p>
              </div>
              {!readOnly && budgetPolicy && !budgetPolicy.can_exceed_ceiling && (
                <p className="flex items-start gap-2 rounded-sm bg-elevation-hover px-3 py-2 content-compact text-default-secondary">
                  <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  {t_i18n("You can lower any limit below the platform budget. Raising one above it is an administrator's decision.")}
                </p>
              )}
              <BudgetLimitFields
                draft={runDefaults.limits}
                onChange={(key, value) => {
                  setError(null);
                  setRunDefaults((d) => ({ ...d, limits: { ...d.limits, [key]: value } }));
                }}
                policy={budgetPolicy}
                blocked={blockedBudget}
                disabled={readOnly}
                columns={1}
                iterations={{
                  value: runDefaults.maxIterations,
                  onChange: (value) => {
                    setError(null);
                    setRunDefaults((d) => ({ ...d, maxIterations: value }));
                  },
                  inherited: platformMaxIterations,
                  help: t_i18n("The most tool rounds one run of a home member may take when the agent sets none. The platform ceiling still applies."),
                  info: t_i18n("Unit: tool rounds per run. Example: 40. Read by the platform at each round of a home member's run. Left empty, the field inherits the value its placeholder shows."),
                }}
              />
            </section>
          </fieldset>

          {/* The fleet default unit of work, inherited by its members unless they declare their own (#9536). Saved on its own. */}
          <div ref={outcomesRef} className="scroll-mt-4">
            <OutcomesCard
              subject={{ type: "fleet", id: fleet.id }}
              structuredOutput={false}
              canManage={!readOnly}
              testId="fleet-roi-outcomes-card"
              onEditingChange={setOutcomesEditing}
              chrome="section"
            />
          </div>

          {error && <RefusalAlert message={error} />}

          {!readOnly && (
            // A flex gap, not space-y (a zero margin here), and the destructive action set apart.
            <div className="flex flex-col gap-4 border-t border-elevation-subtle pt-4">
              <Button type="button" variant="outline" className="w-full justify-start gap-2" onClick={() => void toggleArchive()} disabled={saving}>
                {fleet.is_archived ? <ArchiveRestore className="h-4 w-4" /> : <Archive className="h-4 w-4" />}
                {fleet.is_archived ? t_i18n("Restore the fleet") : t_i18n("Archive the fleet")}
              </Button>
              <Button
                type="button"
                variant="outline"
                className="w-full justify-start gap-2 text-feedback-error-primary"
                onClick={() => setConfirmDelete(true)}
                disabled={deleteFleet.isPending}
              >
                <Trash2 className="h-4 w-4" />
                {t_i18n("Delete the fleet")}
              </Button>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-elevation-subtle px-5 py-3">
          <Button type="button" variant="ghost" onClick={() => void closePanel()}>
            {canSave ? t_i18n("Cancel") : t_i18n("Close")}
          </Button>
          {canSave && (
            <Button type="submit" className="gap-1.5" disabled={saving} aria-busy={saving || undefined}>
              {saving && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Save")}
            </Button>
          )}
        </div>
      </form>

      {discardDialog}
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t_i18n("Delete this fleet?")}
        description={t_i18n("No agent is deleted: the members go back to the playground, their handovers and assignments unchanged. The workspace is kept.")}
        confirmLabel={t_i18n("Delete the fleet")}
        challenge
        onConfirm={() => void remove()}
      />
    </SlideOver>
  );
}
