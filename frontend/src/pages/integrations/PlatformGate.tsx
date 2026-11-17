/**
 * A connected platform's approval posture, where the connection is.
 *
 * The state has two halves and they answer different questions, which is why
 * this never collapses to one boolean:
 *
 *   Can its chatbot ask?  A fact the platform reports about itself
 *                         (``supports_approval_prompts``). Only it knows.
 *   If it cannot, then?   A policy only an operator can set (FR-022).
 *
 * "Whitelisting a chatbot" is that policy set to ``skip`` (FR-047). It is
 * deliberately NOT a whitelist scoped to one caller: whether a tool gates stays
 * answerable from the platform-wide list alone, without inspecting who is
 * asking, and a per-chatbot list would make that false.
 *
 * Both surfaces here read the same registration, joined on ``integration_id``,
 * so the list and the detail page cannot disagree.
 */

import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  useSetPlatformApproval,
  type PlatformRegistration,
} from "@/hooks/api/usePlatformApi";
import { useApprovalsInUse } from "@/hooks/api/useToolApprovals";
import { usePlatformFor } from "@/pages/integrations/usePlatformFor";


function useGateWords() {
  const { t_i18n } = useFormatter();
  return (reg: PlatformRegistration) => {
    // `gates_tools` is the server's resolved answer, and the only one that
    // matches what the gate does: `platform_gates_tools()` puts an
    // administrator's explicit choice above the declared capability, so a
    // platform that supports prompts but was set to `skip` does NOT gate.
    // Testing `supports_approval_prompts` first inverted that and painted a
    // green shield over tools running unattended.
    if (reg.gates_tools && reg.supports_approval_prompts === true) {
      return {
        tone: "gated" as const,
        label: t_i18n("Its chatbot asks for approval in the conversation"),
      };
    }
    if (!reg.gates_tools) {
      return {
        tone: "open" as const,
        label: t_i18n(
          "Its chatbot cannot ask for approval, and its tools are set to run anyway",
        ),
      };
    }
    return {
      tone: reg.supports_approval_prompts === null ? "unknown" : "gated",
      label:
        reg.supports_approval_prompts === null
          ? t_i18n(
              "It has never said whether its chatbot can ask for approval, so its tools gate: the safe reading, not a known state",
            )
          : t_i18n(
              "Its chatbot cannot ask for approval, so its tools gate and its agents explain themselves instead of acting",
            ),
    } as { tone: "gated" | "unknown"; label: string };
  };
}

/** Compact indicator for the integrations list and its rows, while approvals
 *  are in use (an agent asks before running tools). */
export function PlatformGateIcon({ integrationId }: { integrationId: string }) {
  const reg = usePlatformFor(integrationId);
  const words = useGateWords();
  const inUse = useApprovalsInUse();
  if (!reg || !inUse) return null;

  const { tone, label } = words(reg);
  return <HitlIcon state={tone} label={label} />;
}

/**
 * The same state on the detail page, with the policy changeable in place.
 *
 * Offered only when the platform cannot ask: while it can, the policy decides
 * nothing and a control that decides nothing is worse than no control.
 */
export function PlatformGateControl({ integrationId }: { integrationId: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const reg = usePlatformFor(integrationId);
  const words = useGateWords();
  const setApproval = useSetPlatformApproval();

  if (!reg) return null;

  const { tone, label } = words(reg);
  // Offered whenever the platform is not gating, whatever the reason. Keyed on
  // the capability alone, an admin who set `skip` lost the control that would
  // put the gate back — the one case where it is most needed.
  const canDecide = reg.supports_approval_prompts !== true || !reg.gates_tools;
  const skipping = reg.approval_policy === "skip";

  const apply = async () => {
    if (!skipping) {
      const ok = await confirm({
        title: t_i18n("Let {platform} tools run without approval?", {
          platform: reg.platform_title,
        }),
        description: t_i18n(
          "Every tool this platform contributes will run unattended, for every user, including the ones that change or send things. Nobody is asked because its chatbot cannot ask. This is recorded in the audit log and can be undone here.",
        ),
        confirmLabel: t_i18n("Let them run"),
        variant: "warning",
      });
      if (!ok) return;
    }
    setApproval.mutate(
      { id: reg.id, policy: skipping ? "require" : "skip" },
      {
        onError: (err) =>
          toast({
            title: t_i18n("Could not change the policy"),
            description: err.message,
            variant: "error",
          }),
      },
    );
  };

  return (
    <div className="flex items-start gap-2.5 rounded-sm bg-elevation-hover p-3">
      {ConfirmEl}
      <HitlIcon state={tone} className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <p className="content-compact font-medium">{label}</p>
        <p className="mt-0.5 text-[11px] text-default-secondary">
          {reg.approval_policy_set_by === "admin"
            ? t_i18n("Policy set by an administrator")
            : reg.approval_policy_set_by === "deployment"
              ? t_i18n("Policy from this deployment's configuration")
              : t_i18n("Policy left at its default")}
        </p>
      </div>
      {isAdmin && canDecide && (
        <Button
          size="sm"
          variant="outline"
          className="h-7 shrink-0 text-[11px]"
          disabled={setApproval.isPending}
          aria-busy={setApproval.isPending}
          onClick={apply}
        >
          {setApproval.isPending && <Spinner size="sm" tone="inherit" />}
          {skipping ? t_i18n("Require approval again") : t_i18n("Let its tools run")}
        </Button>
      )}
    </div>
  );
}
