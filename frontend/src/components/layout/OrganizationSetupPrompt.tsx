// SPDX-License-Identifier: Apache-2.0

/**
 * The first-connection setup proposal (#9173 S3, versioned by #1087).
 *
 * At install, and on the first administrator sign-in after an upgrade that
 * carries a new setup version (`ctem-loop-1`), a centred dialog proposes the
 * setup. The server decides (GET /organization-context/setup-prompt):
 * administrators only, XTM One mode only, once per instance per version. Any
 * answer (Dismiss, Later, Set up now) is recorded server side and the dialog
 * never comes back for that version, for anyone. The close cross, Escape and
 * an overlay click record nothing: they only close it for this page, and a
 * reload or a new sign-in proposes it again until an answer is recorded.
 * Reset onboarding proposes it anew: the dialog then opens again in the same
 * page session, whatever was answered or closed before.
 *
 * "Set up now" opens the guided CTEM loop setup (`CtemLoopSetupDialog`): six
 * steps, each saved on its own, nothing switched on before the last one.
 */
import { useState } from "react";
import { Building2, Gauge, Network, Sparkles } from "lucide-react";

import { CtemLoopSetupDialog } from "@/components/fleets/CtemLoopSetupDialog";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useAnswerOrganizationSetupPrompt,
  useOrganizationSetupPrompt,
  useOrganizationSetupPromptResets,
  type OrganizationSetupAnswer,
} from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

export { ORGANIZATION_SETUP_PATH } from "./organization-setup-path";

/** @i18n-keys */
const WHY_TEXTS = [
  "The loop ranks every signal against your primary assets, software and scope threshold.",
  "Connect your modules and security stack, choose which actions wait for a person, then preview the runs.",
  "Each step saves on its own and can be skipped; nothing runs until the last step.",
] as const;

/** Each line of WHY_TEXTS with its icon. */
const WHY = [
  { icon: Gauge, text: WHY_TEXTS[0] },
  { icon: Network, text: WHY_TEXTS[1] },
  { icon: Sparkles, text: WHY_TEXTS[2] },
] as const;

export function OrganizationSetupPrompt() {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const isXtmOne = useIsXtmOne();
  const isAdmin = user?.role === "admin";
  const prompt = useOrganizationSetupPrompt(Boolean(isAdmin && isXtmOne && !user?.impersonating));
  const answer = useAnswerOrganizationSetupPrompt();
  const [guided, setGuided] = useState(false);
  const resets = useOrganizationSetupPromptResets();
  // The Reset onboarding count at which the dialog was answered or closed.
  const [answeredAtReset, setAnsweredAtReset] = useState<number | null>(null);
  const [closedAtReset, setClosedAtReset] = useState<number | null>(null);

  // An answer, or the close cross / Escape, closes the dialog until the next
  // Reset onboarding (Settings > Platform) in this page session; a refetch of the
  // setup-prompt query with the same proposal never reopens it.
  const answered = answeredAtReset === resets;
  const closed = closedAtReset === resets;
  const open = Boolean(prompt.data?.propose) && !answered && !closed;
  const reply = (outcome: OrganizationSetupAnswer) =>
    answer.mutate(outcome, {
      onSuccess: () => setAnsweredAtReset(resets),
      onSettled: () => {
        if (outcome === "started") setGuided(true);
      },
    });

  if (guided) return <CtemLoopSetupDialog open onOpenChange={(next) => !next && setGuided(false)} />;
  if (!open) return null;
  return (
    <Dialog open onOpenChange={(next) => !next && !answer.isPending && setClosedAtReset(resets)}>
      <DialogContent size="md" data-testid="organization-setup-prompt">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Building2 className="h-4 w-4 text-accent" aria-hidden />
            {t_i18n("Set up the CTEM loop")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("A guided setup takes you from your organization profile to the first case in the command center, in about fifteen minutes.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <ul className="space-y-3">
            {WHY.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-start gap-3 content-base">
                <Icon className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden />
                <span>{t_i18n(text)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-4 content-compact text-default-secondary">
            {t_i18n("This is proposed once for this version. You will find it later on the command center and the CTEM loop fleet page.")}
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" className="mr-auto" disabled={answer.isPending} onClick={() => reply("dismissed")}>
            {t_i18n("Dismiss")}
          </Button>
          <Button variant="secondary" disabled={answer.isPending} onClick={() => reply("later")}>
            {t_i18n("Later")}
          </Button>
          <Button disabled={answer.isPending} aria-busy={answer.isPending || undefined} onClick={() => reply("started")}>
            {t_i18n("Set up now")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
