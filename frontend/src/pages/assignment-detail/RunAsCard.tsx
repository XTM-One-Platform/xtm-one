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

import { UserCog, Users, EyeOff } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Assignment } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

// ---------------------------------------------------------------------------
// RunAsCard — "run as" identity for company-managed assignments (admin only)
//
// Company-managed agents have no single user, so their assignments execute as
// the admin owner by default. Admins can override the execution identity here
// so the run uses a specific user's connected integrations (incl. per-user
// template bindings), LLM model preference and memory. Only rendered for
// company-managed assignments, and only editable by admins (canManage already
// resolves to admin-only for company-managed entities).
// ---------------------------------------------------------------------------

const OWNER_VALUE = "__owner__";

interface RunAsCardProps {
  assignment: Assignment;
  /** The parent agent's hide_runs default (assignment.hide_runs inherits it). */
  agentHideRuns?: boolean;
  canManage: boolean;
  /** The run-as identity alone is editable although `canManage` is false: a
   *  seeded opt-in assignment of a built-in agent, for an admin. */
  canChooseRunAs?: boolean;
  onUpdate: () => void;
}

export function RunAsCard({
  assignment,
  agentHideRuns = false,
  canManage,
  canChooseRunAs = false,
  onUpdate,
}: RunAsCardProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const aiOff = useAiActionDisabled();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  // The user list endpoint is admin-only. Gate the fetch on
  // ``is_company_managed && canManage`` — NOT ``canManage`` alone — because
  // ``canManage`` is also true for a user's OWN private agent
  // (``!agent.is_company_managed || isAdmin``), so a non-admin owner viewing
  // their own assignment would otherwise trigger a 403 on ``GET /users`` and
  // crash the whole page via the query ErrorBoundary. This card only renders
  // for company-managed assignments anyway (see the early return below), so
  // the user list is only ever needed when an admin manages one.
  const canPickRunAs = canManage || canChooseRunAs;
  const { data: users } = useUsers(assignment.is_company_managed && canPickRunAs);

  // Company-managed only — for private agents the owner is always the runner.
  if (!assignment.is_company_managed) return null;

  const currentValue = assignment.run_as_user_id ?? OWNER_VALUE;
  const selectedUser = users?.find((u) => u.id === assignment.run_as_user_id);

  const userLabel = (u: { full_name: string; email: string }) =>
    u.full_name?.trim() ? `${u.full_name} (${u.email})` : u.email;

  // The owner the assignment defaults to (the agent owner — the admin for a
  // company-managed agent). Surfaced in the default option so it doesn't look
  // like a duplicate of the same user further down the list.
  const owner = users?.find((u) => u.id === assignment.user_id);
  const defaultLabel = owner
    ? t_i18n("Default, run as owner: {owner}", { owner: userLabel(owner) })
    : t_i18n("Default: run as owner");

  const handleChange = async (value: string) => {
    const runAs = value === OWNER_VALUE ? null : value;
    // Its runs use the per-user integrations of whom they act as.
    if (!(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { run_as_user_id: runAs } }))) return;
    update.mutate(
      { id: assignment.id, run_as_user_id: runAs },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Run-as identity updated"), variant: "success" });
          onUpdate();
        },
      },
    );
  };

  const runAsAll = assignment.run_as_all_users;
  const isSchedule = assignment.trigger_type === "schedule";
  // A per-user fan-out is always private per user; otherwise the assignment's
  // own override falls back to the agent's hide_runs default.
  const effectiveHide = runAsAll || (assignment.hide_runs ?? agentHideRuns);

  const saveFlag = async (
    patch: { run_as_all_users?: boolean; hide_runs?: boolean | null },
  ) => {
    if (
      patch.run_as_all_users !== undefined
      && !(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { run_as_all_users: patch.run_as_all_users } }))
    ) {
      return;
    }
    update.mutate(
      { id: assignment.id, ...patch },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Assignment updated"), variant: "success" });
          onUpdate();
        },
      },
    );
  };

  return (
    <Card>
      {consentElement}
      <CardHeader className="p-4 pb-2">
        <CardTitle className="content-base flex items-center gap-2">
          <UserCog className="h-4 w-4 text-accent" /> {t_i18n("Run As")}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0 space-y-3">
        {/* Mode: fan out to one run per user (scheduled triggers). The primary
            choice — mutually exclusive with picking a single run-as identity. */}
        {isSchedule && (
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="content-compact font-medium flex items-center gap-1.5">
                <Users className="h-3.5 w-3.5 text-accent" /> {t_i18n("Run for all users")}
              </p>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Each scheduled run fans out to one run per active user, scoped to that user's own access. Implies private runs.")}
              </p>
            </div>
            <Switch aria-label={t_i18n("Run for all users")}
              checked={runAsAll}
              onCheckedChange={(v) => saveFlag({ run_as_all_users: v })}
              disabled={!canManage || update.isPending || aiOff}
            />
          </div>
        )}

        {/* Single run-as identity — only when NOT fanning out (otherwise each
            run executes as its own user, so a single identity doesn't apply). */}
        {runAsAll ? (
          <p className="text-[11px] text-default-secondary border-t border-elevation-subtle-soft pt-3">
            {t_i18n("Each scheduled run executes as the user it is generated for: there is no single run-as identity. Manual and follow-up runs always execute as the user who triggers them.")}
          </p>
        ) : (
          <div className={cn("space-y-2", isSchedule && "border-t border-elevation-subtle-soft pt-3")}>
            <p className="text-[11px] text-default-secondary">
              {t_i18n("Choose which user's identity automatic executions (schedule, webhook, integration events) run under: their connected integrations, model preference and memory are used. Manual and follow-up runs always execute as the user who triggers them. Defaults to the admin owner.")}
            </p>
            {canPickRunAs ? (
              <SearchableSelect
                aria-label={t_i18n("Run As")}
                options={(users ?? []).map((u) =>
                  userSelectOption(u, u.id === assignment.user_id ? { description: `${u.email} · ${t_i18n("owner")}` } : {}),
                )}
                pinned={[{ value: OWNER_VALUE, label: defaultLabel }]}
                value={currentValue}
                onValueChange={handleChange}
                disabled={update.isPending || aiOff}
                placeholder={t_i18n("Search users...")}
              />
            ) : (
              <div className="flex items-center gap-2 rounded-sm bg-elevation-hover px-3 py-2.5">
                <Users className="h-3.5 w-3.5 text-default-secondary" />
                <span className="content-compact">
                  {selectedUser
                    ? userLabel(selectedUser)
                    : assignment.run_as_user_id
                      ? t_i18n("A specific user")
                      : defaultLabel}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Run privacy */}
        <div className="flex items-start justify-between gap-3 border-t border-elevation-subtle-soft pt-3">
          <div className="min-w-0">
            <p className="content-compact font-medium flex items-center gap-1.5">
              <EyeOff className="h-3.5 w-3.5 text-accent" /> {t_i18n("Hide runs from other users")}
            </p>
            <p className="text-[11px] text-default-secondary">
              {runAsAll
                ? t_i18n("Always on while running for all users - each run stays private to its user.")
                : t_i18n("Each run is private to the user it executed as, instead of visible to everyone. Lets non-admins trigger it (it only ever produces their own report).")}
            </p>
          </div>
          <Switch
            checked={effectiveHide}
            onCheckedChange={(v) =>
              // Persist null (inherit the agent default) when the chosen value
              // matches it, so the assignment keeps following future changes to
              // the agent's hide_runs default instead of pinning an override.
              saveFlag({ hide_runs: v === agentHideRuns ? null : v })
            }
            disabled={!canManage || runAsAll || update.isPending || aiOff}
          />
        </div>
      </CardContent>
    </Card>
  );
}
