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
 * "New fleet": create a fleet by hand, its members included.
 *
 * One small form over the existing fleet routes (`useCreateFleet`): a name and
 * a description; the workspace it works in (exactly one: an existing one
 * the user may manage, or a new one named after the fleet); the orchestrator
 * and the home members, picked among the agents the user may manage that have
 * no home fleet yet; connected agents (any agent the user sees, each with its
 * collaboration instruction and context inclusion); and which home members
 * other fleets may reach (the orchestrator always). The form refuses what it
 * can see is wrong before sending, the API refuses the rest and the dialog
 * shows why; on save the fleet page opens.
 *
 * Every field is a `SettingsField` (frontend-design-system.md, Forms): the
 * label above at 14 px, one help line at 12 px, an info bubble where the
 * field needs an explanation, the error in place of the help.
 */
import { useMemo, useState } from "react";
import { Plus, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Textarea, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { Input } from "@/components/ui/input";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useAgents } from "@/hooks/api/useAgentApi";
import { FleetLeftBehindError, useCreateFleet, type FleetContextInclusion } from "@/hooks/api/useFleetApi";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { canManageEntity } from "@/lib/utils";

import { CONTEXT_INCLUSION_FIELD, CONTEXT_INCLUSIONS } from "./context-inclusion";
import {
  EMPTY_FLEET_FORM,
  describeFleetRefusal,
  homeMemberCandidates,
  homeMembers,
  toNewFleet,
  validateFleetForm,
  type NewFleetForm,
} from "./new-fleet-form";

/** Picker value for "create a workspace named after the fleet". */
const NEW_WORKSPACE = "__new__";

export function NewFleetDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { user, isAdmin } = useAuth();
  const { data: agents } = useAgents();
  const { data: workspaces } = useWorkspaces({ enabled: open, throwOnError: false });
  const createFleet = useCreateFleet();
  const [form, setForm] = useState<NewFleetForm>(EMPTY_FLEET_FORM);
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<{ message: string; detail: string | null } | null>(null);

  const update = (patch: Partial<NewFleetForm>) => {
    setServerError(null);
    setForm((prev) => ({ ...prev, ...patch }));
  };

  // MultiSelect emits one toggle per change, several in one tick when a
  // selection is cleared: each toggle applies to the latest state, never to
  // the `form` this render closed over.
  const toggle = (key: "memberIds" | "exposedIds", id: string) => {
    setServerError(null);
    setForm((prev) => ({
      ...prev,
      [key]: prev[key].includes(id) ? prev[key].filter((v) => v !== id) : [...prev[key], id],
    }));
  };

  const candidates = useMemo(
    () => homeMemberCandidates(agents ?? [], (a) => canManageEntity(a, user?.id, isAdmin)),
    [agents, user?.id, isAdmin],
  );
  const agentName = useMemo(() => new Map((agents ?? []).map((a) => [a.id, a.name])), [agents]);
  const home = homeMembers(form);
  const connectable = useMemo(
    () => (agents ?? []).filter((a) => !home.includes(a.id) && !form.connected.some((c) => c.agent_id === a.id)),
    [agents, home, form.connected],
  );
  const manageableWorkspaces = useMemo(
    () => (workspaces ?? []).filter((w) => w.can_manage && !w.is_archived),
    [workspaces],
  );
  const errors = validateFleetForm(form, t_i18n);
  const shownErrors = submitted ? errors : {};

  const close = (next: boolean) => {
    if (!next) {
      setForm(EMPTY_FLEET_FORM);
      setSubmitted(false);
      setServerError(null);
    }
    onOpenChange(next);
  };

  const save = async () => {
    setSubmitted(true);
    if (Object.keys(errors).length > 0) {
      // Whatever submitted the form, focus lands on the first field to fix.
      if (errors.name) document.getElementById("new-fleet-name")?.focus();
      return;
    }
    try {
      const fleet = await createFleet.mutateAsync(toNewFleet(form));
      close(false);
      navigate(`/fleets/${fleet.id}`, { state: { backTo: "/fleets", backLabel: "Back to Fleets" } });
    } catch (err) {
      if (err instanceof FleetLeftBehindError) {
        // The undo failed too: never a silent half-made fleet.
        setServerError({
          message: t_i18n("The fleet {name} was created but not completed, and could not be removed: delete it before trying again.", { name: err.fleet.name }),
          detail: err.message,
        });
        return;
      }
      setServerError(describeFleetRefusal(err, t_i18n));
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t_i18n("New fleet")}</DialogTitle>
          <DialogDescription>
            {t_i18n("A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept.")}
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
            id="new-fleet-name"
            required
            label={t_i18n("Name")}
            helper={t_i18n("Shown on the fleet page, in the fleet list and to the agents the fleet works with.")}
            info={t_i18n("The fleet's display name, up to 200 characters. Example: CEO Office. Read by people in the fleet list and by agents when they hand work to the fleet. Required: a fleet without a name is not created.")}
            error={shownErrors.name}
          >
            <Input
              id="new-fleet-name"
              aria-label={t_i18n("Name")}
              aria-required
              autoFocus
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              placeholder={t_i18n("e.g. CEO Office")}
              maxLength={200}
              aria-invalid={!!shownErrors.name}
              aria-describedby={settingsFieldHintId("new-fleet-name")}
            />
          </SettingsField>

          <SettingsField
            id="new-fleet-description"
            label={t_i18n("Description (optional)")}
            helper={t_i18n("What the fleet is for, in a sentence or two, shown on the fleet page.")}
            info={t_i18n("A sentence or two on the fleet's purpose. Example: Prepares the CEO's week and follows up every open loop. Read by people on the fleet page. Left empty, the page shows no description.")}
          >
            <Textarea
              id="new-fleet-description"
              value={form.description}
              onChange={(e) => update({ description: e.target.value })}
              aria-describedby={settingsFieldHintId("new-fleet-description")}
              rows={2}
            />
          </SettingsField>

          <SettingsField
            id="new-fleet-workspace"
            label={t_i18n("Workspace")}
            helper={t_i18n("Where the fleet works: its work items, shared state and knowledge are kept there. Created with the fleet unless you pick one.")}
            info={t_i18n("The workspace that holds everything the fleet's agents produce. Example: an existing Security operations workspace you manage. Read by every member's runs. Left on the default, a new workspace named after the fleet is created.")}
          >
            <SearchableSelect
              id="new-fleet-workspace"
              aria-label={t_i18n("Workspace")}
              aria-describedby={settingsFieldHintId("new-fleet-workspace")}
              options={manageableWorkspaces.map((w) => ({ value: w.id, label: w.name }))}
              pinned={[{ value: NEW_WORKSPACE, label: t_i18n("A new workspace named after the fleet") }]}
              value={form.workspaceId ?? NEW_WORKSPACE}
              onValueChange={(value) => update({ workspaceId: value === NEW_WORKSPACE ? null : value })}
            />
          </SettingsField>

          <SettingsField
            id="new-fleet-orchestrator"
            label={t_i18n("Orchestrator")}
            helper={t_i18n("Optional: the agent other fleets reach first, always exposed to them.")}
            info={t_i18n("The member that receives work from other fleets and hands it on. Example: an Assistant agent that routes requests to the desks. Read by other fleets when they delegate. Left empty, the fleet has no orchestrator.")}
          >
            <SearchableSelect
              id="new-fleet-orchestrator"
              aria-label={t_i18n("Orchestrator")}
              aria-describedby={settingsFieldHintId("new-fleet-orchestrator")}
              options={candidates.map((a) => agentSelectOption(a))}
              value={form.orchestratorId}
              onValueChange={(value) => update({ orchestratorId: value || null })}
              placeholder={t_i18n("Search agents...")}
              emptyMessage={t_i18n("No agent without a fleet that you can manage")}
              clearable
            />
          </SettingsField>

          <SettingsField
            id="new-fleet-members"
            label={t_i18n("Members")}
            helper={t_i18n("An agent has one home fleet: only agents you manage that have none are listed.")}
            info={t_i18n("The agents whose home is this fleet, besides the orchestrator. Example: a Developer and a Mail Secretary agent. Their runs work in the fleet's workspace. Left empty, the fleet has only its orchestrator, or no member yet.")}
          >
            <MultiSelect
              id="new-fleet-members"
              options={candidates.filter((a) => a.id !== form.orchestratorId).map((a) => ({ value: a.id, label: a.name }))}
              selected={form.memberIds.filter((id) => id !== form.orchestratorId)}
              onToggle={(id) => toggle("memberIds", id)}
              placeholder={t_i18n("Home members")}
              aria-label={t_i18n("Members")}
              aria-describedby={settingsFieldHintId("new-fleet-members")}
              searchPlaceholder={t_i18n("Search agents...")}
              emptyOptionsLabel={t_i18n("No agent without a fleet that you can manage")}
              noMatchLabel={t_i18n("No match")}
            />
          </SettingsField>

          <SettingsField
            id="new-fleet-connect"
            label={t_i18n("Connected agents")}
            helper={t_i18n("Agents outside the fleet that it works with, each with when and how.")}
            info={t_i18n("Agents of other fleets, or with no fleet, that this fleet may call. Example: a UX Director agent consulted on every screen change. Read by the fleet's members when they delegate. Left empty, the fleet works only with its own members.")}
            error={shownErrors.connected}
          >
            <div className="space-y-1.5">
            {form.connected.map((row, index) => {
              const rowId = `new-fleet-connect-${row.agent_id}`;
              return (
              <div key={row.agent_id} className="space-y-3 rounded-sm border border-elevation-subtle-soft p-2">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate content-base font-medium text-default-primary">{agentName.get(row.agent_id) ?? row.agent_id}</span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={t_i18n("Remove {name}", { name: agentName.get(row.agent_id) ?? row.agent_id })}
                    onClick={() => update({ connected: form.connected.filter((_, i) => i !== index) })}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
                <SettingsField
                  id={`${rowId}-context`}
                  label={t_i18n(CONTEXT_INCLUSION_FIELD)}
                  helper={t_i18n("What this agent receives from the conversation with each task.")}
                  info={t_i18n("How much of the current conversation the fleet passes to this agent with each task. Example: A summary, for a reviewer that needs the background but not every message. Read by the fleet's members when they hand the task over. Left on Nothing, the default, the agent sees only the task.")}
                >
                  <SearchableSelect
                    id={`${rowId}-context`}
                    aria-labelledby={settingsFieldLabelId(`${rowId}-context`)}
                    aria-describedby={settingsFieldHintId(`${rowId}-context`)}
                    options={CONTEXT_INCLUSIONS.map((option) => ({
                      value: option.value,
                      label: t_i18n(option.label),
                      description: t_i18n(option.description),
                    }))}
                    sort={false}
                    searchable={false}
                    value={row.context_inclusion}
                    onValueChange={(value) =>
                      update({
                        connected: form.connected.map((c, i) =>
                          i === index ? { ...c, context_inclusion: value as FleetContextInclusion } : c,
                        ),
                      })
                    }
                  />
                </SettingsField>
                <SettingsField
                  id={`${rowId}-instruction`}
                  label={t_i18n("Collaboration instruction")}
                  helper={t_i18n("When and how the fleet works with this agent (optional)")}
                  info={t_i18n("A standing note telling the fleet when to call this agent and what to expect from it, up to 4000 characters. Example: Ask for a review before any screen change ships. Read by the fleet's members each time they decide whether to delegate. Left empty, members call the agent from its own description alone.")}
                >
                  <Input
                    id={`${rowId}-instruction`}
                    aria-labelledby={settingsFieldLabelId(`${rowId}-instruction`)}
                    aria-describedby={settingsFieldHintId(`${rowId}-instruction`)}
                    value={row.collaboration_instruction}
                    maxLength={4000}
                    placeholder={t_i18n("e.g. Ask for a review before any screen change ships.")}
                    onChange={(e) =>
                      update({
                        connected: form.connected.map((c, i) =>
                          i === index ? { ...c, collaboration_instruction: e.target.value } : c,
                        ),
                      })
                    }
                  />
                </SettingsField>
              </div>
              );
            })}
            <SearchableSelect
              id="new-fleet-connect"
              aria-label={t_i18n("Connect an agent")}
              aria-describedby={settingsFieldHintId("new-fleet-connect")}
              options={connectable.map((a) => agentSelectOption(a))}
              value={null}
              onValueChange={(value) =>
                value &&
                update({
                  connected: [...form.connected, { agent_id: value, collaboration_instruction: "", context_inclusion: "none" }],
                })
              }
              placeholder={t_i18n("Connect an agent...")}
            />
            </div>
          </SettingsField>

          {home.length > 0 && (
            <SettingsField
              id="new-fleet-exposed"
              label={t_i18n("Exposed to other fleets")}
              helper={t_i18n("Other fleets reach the orchestrator, and the members you add here.")}
              info={t_i18n("The home members other fleets may call directly. Example: a Developer agent other fleets ask for code changes. Read by other fleets when they delegate. Left empty, only the orchestrator is reachable.")}
            >
              <MultiSelect
                id="new-fleet-exposed"
                options={home.filter((id) => id !== form.orchestratorId).map((id) => ({ value: id, label: agentName.get(id) ?? id }))}
                selected={form.exposedIds}
                onToggle={(id) => toggle("exposedIds", id)}
                placeholder={t_i18n("Only the orchestrator")}
                aria-label={t_i18n("Exposed to other fleets")}
                aria-describedby={settingsFieldHintId("new-fleet-exposed")}
                searchPlaceholder={t_i18n("Search agents...")}
                emptyOptionsLabel={t_i18n("Only the orchestrator")}
                noMatchLabel={t_i18n("No match")}
              />
            </SettingsField>
          )}

          {serverError && <RefusalAlert message={serverError.message} detail={serverError.detail} />}

          {/* The dialog scrolls on a short screen: the actions stay in view and
              cover the dialog's bottom padding, so nothing scrolls under them. */}
          <div className="sticky -bottom-6 -mb-6 flex justify-end gap-2 bg-elevation-default pb-6 pt-2">
            <Button type="button" variant="secondary" onClick={() => close(false)}>
              {t_i18n("Cancel")}
            </Button>
            <Button type="submit" className="gap-1.5" disabled={createFleet.isPending} aria-busy={createFleet.isPending || undefined}>
              {createFleet.isPending && <Spinner size="sm" tone="inherit" />}{!createFleet.isPending && <Plus className="h-4 w-4" />}
              {t_i18n("Create fleet")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
