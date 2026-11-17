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
 * One tool's approval state and what can be done about it.
 *
 * Two different decisions, kept on two different screens:
 *
 *   personal — "Always allow for me": one person's own standing approval,
 *              on their profile. It reaches nobody else.
 *   platform — "Let it run without asking": the platform's rule, for every
 *              person and every agent, in Settings > Access and security > Tool approvals.
 *
 * The row is shared so both lists render a tool the same way; the `scope`
 * decides which of the two controls it carries, never both.
 */

import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useExemptReason, useGateReason } from "@/hooks/useHitlWords";
import {
  useAddToWhitelist,
  useRemoveFromWhitelist,
  type ToolGateEntry,
} from "@/hooks/api/useHitlWhitelist";
import {
  useGrantToolApproval,
  useRevokeToolApproval,
} from "@/hooks/api/useToolApprovals";
import { hitlState } from "@/lib/hitl";
import { cn } from "@/lib/utils";

export type ToolApprovalScope = "platform" | "personal";

/** The control one list offers for a tool, without any layout. */
function ToolGateActions({
  entry,
  approvedId,
  scope,
  suspended,
}: {
  entry: ToolGateEntry;
  /** The caller's standing approval for this tool, when they hold one. */
  approvedId?: string;
  scope: ToolApprovalScope;
  /**
   * Nothing this control changes would apply right now (approvals suspended,
   * or the platform rules could not be read). Offered as disabled rather than
   * hidden: the reader is looking for it, and its absence would read as a
   * missing permission rather than as a switch somebody flipped.
   */
  suspended?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const grant = useGrantToolApproval();
  const revoke = useRevokeToolApproval();
  const add = useAddToWhitelist();
  const remove = useRemoveFromWhitelist();

  const busy =
    suspended ||
    grant.isPending ||
    revoke.isPending ||
    add.isPending ||
    remove.isPending;
  /* Everything with a live definition, which is everything the catalogue can
     name from something connected. Only a tool harvested from run history that
     nothing still claims has no schema to fingerprint against — the API refuses
     those, and there is no reason to offer a control whose only outcome is that
     refusal. */
  const canSelfApprove = entry.source !== "other";

  const fail = (err: Error) =>
    toast({
      title: t_i18n("Could not apply that"),
      description: err.message,
      variant: "error",
    });

  /* Letting a tool run without asking is the dangerous direction — it stops
     the tool asking anyone, on every agent — so it is confirmed. Making it ask
     again is the safe direction and is not.

     "Ask first" is offered whatever the exemption's layer: the endpoint clears
     an admin addition, and for a shipped default or a platform read it records
     a durable override that survives the upgrade which would otherwise exempt
     the tool again. */
  const applyPlatform = async () => {
    if (!entry.gates) {
      remove.mutate(entry.tool_key, { onError: fail });
      return;
    }
    /* Going back to the default is not the same decision as making an
       exemption, even though one endpoint serves both: the tool is read-only
       and exempt by default, somebody here overrode that, and undoing it
       returns it to the default rather than pinning it exempt forever. */
    const ok = await confirm(
      entry.regated
        ? {
            title: t_i18n("Put {tool} back to its default?", {
              tool: entry.tool_key,
            }),
            description: t_i18n(
              "It is read-only, so by default it runs without asking. Going back to the default means it follows what the product ships again, including a later release that changes it.",
            ),
            confirmLabel: t_i18n("Back to the default"),
            // Still the un-gating direction: the tool stops asking again.
            variant: "warning" as const,
          }
        : {
            title: t_i18n("Let {tool} run without asking anyone?", {
              tool: entry.tool_key,
            }),
            description: t_i18n(
              "No agent and no person on this platform will be asked before it runs, scheduled runs nobody is watching included. The change is recorded in the audit log, and you can make it ask first again here.",
            ),
            confirmLabel: t_i18n("Let it run without asking"),
            variant: "warning",
          },
    );
    if (!ok) return;
    add.mutate({ toolKey: entry.tool_key }, { onError: fail });
  };

  if (scope === "personal") {
    /* Revoking is offered whenever the caller holds an approval — whatever the
       tool's source, and whether or not it still asks. It takes a preference
       id, so nothing about the tool has to be resolvable; hiding it would make
       an approval on a tool that no longer asks impossible to take back.
       Granting needs a schema to fingerprint against, so it is offered only
       where one can be read. */
    if (approvedId) {
      return (
        <Button
          size="sm"
          variant="ghost"
          className="h-7 shrink-0 text-[11px]"
          disabled={busy}
          aria-busy={revoke.isPending || grant.isPending}
          onClick={() => revoke.mutate(approvedId, { onError: fail })}
        >
          {(revoke.isPending || grant.isPending) && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Ask me again")}
        </Button>
      );
    }
    if (!entry.gates || !canSelfApprove) return null;
    return (
      <Button
        size="sm"
        variant="outline"
        className="h-7 shrink-0 text-[11px]"
        disabled={busy}
        aria-busy={revoke.isPending || grant.isPending}
        onClick={() => grant.mutate(entry.tool_key, { onError: fail })}
      >
        {(revoke.isPending || grant.isPending) && <Spinner size="sm" tone="inherit" />}
        {t_i18n("Always allow for me")}
      </Button>
    );
  }

  return (
    <div className="flex items-center justify-end gap-1">
      {ConfirmEl}
      {(add.isPending || remove.isPending) && <Spinner size="sm" className="shrink-0" />}
      <Button
        size="sm"
        variant="outline"
        className="h-7 shrink-0 text-[11px]"
        disabled={busy}
        aria-busy={add.isPending || remove.isPending}
        onClick={applyPlatform}
      >
        {!entry.gates
          ? t_i18n("Make it ask first")
          : entry.regated
          ? t_i18n("Back to the default")
          : t_i18n("Let it run without asking")}
      </Button>
    </div>
  );
}

export function GateRow({
  entry,
  approvedId,
  scope,
  suspended,
}: {
  entry: ToolGateEntry;
  approvedId?: string;
  scope: ToolApprovalScope;
  suspended?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const reason = useExemptReason();
  const gateReason = useGateReason();
  /* In the personal list a tool the reader always allows reads as allowed:
     that is what happens to them. The platform list describes the platform. */
  const asks = entry.gates && !(scope === "personal" && approvedId);
  const description =
    scope === "personal" && approvedId
      ? entry.gates
        ? t_i18n("You always allow it; it still asks everyone else first")
        : t_i18n("You always allow it")
      : entry.gates
      ? gateReason(entry)
      : reason(entry);

  return (
    <div
      className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2"
      data-testid="tool-approval-row"
      data-tool={entry.tool_key}
    >
      <span
        className={cn(
          "flex size-6 shrink-0 items-center justify-center rounded-sm border",
          asks
            ? "border-feedback-success-primary/25 bg-feedback-success-primary/10"
            : "border-elevation-subtle-soft bg-elevation-hover",
        )}
      >
        <HitlIcon
          state={hitlState(asks)}
          className={asks ? undefined : "text-default-secondary"}
          label={description}
        />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono-plex content-compact">{entry.tool_key}</p>
        <p className="truncate text-[11px] text-default-secondary">
          {entry.provider && (
            <span className="text-default-secondary">{entry.provider} · </span>
          )}
          {description}
        </p>
      </div>
      <ToolGateActions
        entry={entry}
        approvedId={approvedId}
        scope={scope}
        suspended={suspended}
      />
    </div>
  );
}
