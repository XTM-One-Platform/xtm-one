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
 * The user's own tool approval posture, on Profile > Tool
 * approvals beside the list of tools that ask first.
 *
 * Two layers live here, most permissive first:
 *
 * 1. "Allow every tool for me" — the wholesale mode. One switch instead of a
 *    "Yes, always" per tool; while it is on, none of this user's runs or chats
 *    wait for their approval and the per-tool list below is moot (kept, not
 *    deleted, so switching back restores the previous posture exactly).
 * 2. Standing per-tool approvals — every "Yes, always" the user clicked lands
 *    here, and this is the only place they can take one back. Without a
 *    visible list the grant would be a one-way door.
 *
 * Deliberately scoped to the signed-in user. There is no administrator view of
 * everyone's approvals; an admin who needs to act on someone else's does it by
 * impersonating them, which renders this same section.
 */

import { useState } from "react";
import { Building2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { hitlState } from "@/lib/hitl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { cn, formatShortDate } from "@/lib/utils";
import { usePendingApprovalsByTool } from "@/hooks/api/useAssignmentApi";
import {
  useRevokeToolApproval,
  useSetApprovalMode,
  useToolApprovals,
} from "@/hooks/api/useToolApprovals";
import { ApproveByToolDialog } from "@/pages/ApproveByToolDialog";
import { Spinner } from "@filigran/design-system";

export function ToolApprovalsRow() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data, isLoading } = useToolApprovals();
  const revoke = useRevokeToolApproval();
  const setMode = useSetApprovalMode();

  const preferences = data?.preferences ?? [];
  const invalidated = data?.recently_invalidated ?? [];
  const allowAll = data?.approve_all_tools ?? false;
  const platformOff = data?.tool_approvals_enabled === false;
  /* Approvals are opt-in: until an agent asks before running tools nothing
     asks this person, and the card says so before offering any control. */
  const notInUse = !!data && !platformOff && data.approvals_in_use === false;

  /* The switch only reaches calls yet to be gated: the gate has already run for
     anything sitting paused, so without this offer "allow all" does least at
     the moment it is worth most (FR-043).
     Queried whenever the gate is live rather than only while the switch is off:
     the count is what the switch offers to clear, and it is also what the line
     below reports afterwards — tying it to `!allowAll` left that line unable to
     load its own number after a reload. */
  const [sweepOpen, setSweepOpen] = useState(false);
  const { data: waiting, isLoading: waitingLoading } = usePendingApprovalsByTool(
    !platformOff,
    undefined,
    true,
  );
  const waitingRuns =
    (waiting?.tools ?? []).reduce((n, t) => n + t.run_count, 0) +
    (waiting?.mixed_run_count ?? 0);

  const handleAllowAll = async (next: boolean) => {
    if (next) {
      const ok = await confirm({
        title: t_i18n("Allow every tool to run without asking you?"),
        description: t_i18n(
          "None of your conversations or scheduled runs will wait for your approval any more, tools that delete data or act outside the platform included. The tools you always allow are kept and apply again when you turn this off.",
        ),
        confirmLabel: t_i18n("Allow every tool"),
        variant: "warning",
      });
      if (!ok) return;
    }
    setMode.mutate(next, {
      onSuccess: () => {
        toast({
          title: next
            ? t_i18n("Every tool now runs without asking you")
            : t_i18n("Tools ask for your approval again"),
          variant: "success",
        });
        /* Offered, never applied: a run restarting with no explicit action is
           surprising, and dismissing this leaves the switch on (FR-039a). */
        if (next && waitingRuns > 0) setSweepOpen(true);
      },
      onError: (err) =>
        toast({
          title: t_i18n("Could not update your approval mode"),
          description: err.message,
          variant: "error",
        }),
    });
  };

  const handleRevoke = (id: string, toolKey: string) => {
    revoke.mutate(id, {
      onSuccess: () =>
        toast({
          title: t_i18n("Approval revoked"),
          description: t_i18n("{tool} will ask you again next time.", { tool: toolKey }),
          variant: "success",
        }),
      onError: (err) =>
        toast({
          title: t_i18n("Could not revoke approval"),
          description: err.message,
          variant: "error",
        }),
    });
  };

  return (
    <Card>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <span
            className={cn(
              "flex size-8 shrink-0 items-center justify-center rounded-sm border",
              notInUse
                ? "border-elevation-subtle-soft bg-elevation-hover"
                : allowAll || platformOff
                ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/10"
                : "border-feedback-success-primary/25 bg-feedback-success-primary/10",
            )}
          >
            <HitlIcon
              state={notInUse ? "topic" : hitlState(!(allowAll || platformOff))}
              className="size-4"
            />
          </span>
          {t_i18n("Your tool approvals")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n(
            "An agent set to ask before running tools waits for your approval before a tool call that is not known to be read-only. Choose here what may run without asking you. It changes nothing for anyone else.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">

      {notInUse && (
        <div
          className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-secondary"
          data-testid="personal-approvals-not-in-use"
        >
          {t_i18n(
            "No agent on this platform asks before running tools right now, so nothing asks you. Your choices below apply once one does.",
          )}
        </div>
      )}

      {/* An administrator's platform-wide suspend switch outranks everything in
          this section, so when it is on, say so — otherwise "why does nothing
          ever ask me?" has no answer here. */}
      {platformOff && (
        <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2 content-compact text-feedback-warning-tertiary">
          {t_i18n(
            "An administrator has suspended approvals for the whole platform, so nothing asks anyone right now. Your choices below apply again when approvals are resumed.",
          )}
        </div>
      )}

      {/* Allow-all mode — the wholesale form of "Yes, always". */}
      <div
        className={cn(
          "flex items-center justify-between gap-3 rounded-sm border px-3 py-2.5 transition-colors",
          allowAll
            ? "border-feedback-alert-primary/40 bg-feedback-alert-primary/[0.07]"
            : "border-elevation-subtle-soft bg-elevation-hover",
        )}
      >
        <div className="min-w-0">
          <p className="content-base font-medium">{t_i18n("Allow every tool for me")}</p>
          <p className="content-compact text-default-secondary">
            {allowAll
              ? t_i18n(
                  "Nothing asks you, in chat or on your scheduled runs. Turn this off to go back to the tools you always allow.",
                )
              : t_i18n(
                  "Skip approvals for every tool, instead of allowing them one by one.",
                )}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {setMode.isPending && (
            <Spinner size="sm" tone="inherit" className="text-default-secondary" />
          )}
          <Switch
            checked={allowAll}
            onCheckedChange={handleAllowAll}
            disabled={setMode.isPending || isLoading}
            aria-label={t_i18n("Allow every tool for me")}
          />
        </div>
      </div>

      {/* Standing per-tool approvals — moot while allow-all is on, so they
          collapse into one line rather than listing controls that do nothing. */}
      {isLoading ? (
        <div className="flex items-center gap-2 content-compact text-default-secondary">
          <Spinner size="sm" tone="inherit" />
          {t_i18n("Loading…")}
        </div>
      ) : allowAll ? (
        preferences.length > 0 && (
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "{count, plural, one {# tool you always allow is kept} other {# tools you always allow are kept}} and will apply again when you turn Allow every tool for me off.",
              { count: preferences.length },
            )}
          </p>
        )
      ) : (
        <div className="space-y-1.5">
          <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">
            {t_i18n("Tools you always allow")}
          </p>
          {preferences.length === 0 ? (
            <p className="content-compact text-default-secondary">
              {t_i18n(
                "None yet. Use Always allow for me in the list of tools below, or answer Yes, always when an agent asks you.",
              )}
            </p>
          ) : (
            <ul className="space-y-1.5">
              {preferences.map((preference) => (
                <li
                  key={preference.id}
                  className="group flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono-plex content-compact">
                      {preference.tool_key}
                    </p>
                    <p className="flex items-center gap-1.5 text-[11px] text-default-secondary">
                      <span className="truncate">{preference.tool_source}</span>
                      {preference.granted_at && (
                        <span className="shrink-0">
                          · {formatShortDate(preference.granted_at)}
                        </span>
                      )}
                      {preference.granted_by_admin_id && (
                        <span className="inline-flex shrink-0 items-center gap-1 text-feedback-warning-primary">
                          <Building2 className="size-3" />
                          {t_i18n("granted by an administrator on your behalf")}
                        </span>
                      )}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 shrink-0 content-compact opacity-60 group-hover:opacity-100"
                    disabled={revoke.isPending}
                    onClick={() =>
                      handleRevoke(preference.id, preference.tool_key)
                    }
                  >
                    <RotateCcw className="mr-1 size-3" />
                    {t_i18n("Ask me again")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Dismissing the offer leaves these paused, and nothing else on this
          page would say so — the per-tool list is moot while allow-all is on. */}
      {allowAll && waitingRuns > 0 && (
        <div className="flex items-center justify-between gap-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2">
          <p className="min-w-0 flex-1 content-compact text-feedback-warning-primary">
            {t_i18n(
              "{n, plural, one {# of your runs was} other {# of your runs were}} already waiting when you turned this on, so {n, plural, one {it is} other {they are}} still paused.",
              { n: waitingRuns },
            )}
          </p>
          <Button
            size="sm"
            variant="outline"
            className="h-6 shrink-0 border-feedback-alert-primary/40 px-2 text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
            onClick={() => setSweepOpen(true)}
          >
            {t_i18n("Answer by tool…")}
          </Button>
        </div>
      )}

      <ApproveByToolDialog
        open={sweepOpen}
        onOpenChange={setSweepOpen}
        data={waiting}
        isLoading={waitingLoading}
        ownOnly
        approveOnly
      />

      {!allowAll && invalidated.length > 0 && (
        <div className="space-y-1.5 pt-1">
          <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">
            {t_i18n("Stopped applying")}
          </p>
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "These tools changed after you allowed them, so they ask you again. Answer Yes, always the next time one asks, or allow it again below.",
            )}
          </p>
          <ul className="space-y-1.5">
            {invalidated.map((entry) => (
              <li
                key={entry.id}
                className="rounded-sm border border-dashed border-elevation-subtle-soft px-3 py-2"
              >
                <p className="truncate font-mono-plex content-compact">{entry.tool_key}</p>
                <p className="text-[11px] text-default-secondary">
                  {t_i18n("the tool's parameters changed")}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}
      </CardContent>
    </Card>
  );
}
