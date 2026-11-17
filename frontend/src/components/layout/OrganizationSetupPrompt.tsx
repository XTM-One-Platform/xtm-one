// SPDX-License-Identifier: Apache-2.0

/**
 * The first-login organization setup proposal (#9173, S3).
 *
 * The first time an administrator signs in to an XTM One platform whose
 * organization profile was never saved, a centred dialog proposes the setup.
 * The server decides (GET /organization-context/setup-prompt): administrators
 * only, XTM One mode only, once per instance. Any answer is recorded server
 * side and the dialog never comes back, for anyone; the setup stays reachable
 * from Settings > Organization.
 *
 * "Set up now" opens Settings > Organization in edit mode with the Draft menu
 * (From the platforms, With AI): nothing is saved before the form's Save.
 */
import { Building2, Gauge, Network, Sparkles } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAnswerOrganizationSetupPrompt, useOrganizationSetupPrompt, type OrganizationSetupAnswer } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

/** Settings > Organization in edit mode, the Draft menu open: the place "Set up now" lands on. */
export const ORGANIZATION_SETUP_PATH = "/settings/organization?setup=1";

/** @i18n-keys */
const WHY_TEXTS = [
  "Agents rank, scope and estimate against your sector, regions, crown jewels and risk appetite.",
  "Fleets read it before they act: each card of the profile names the fleets and agents that use it.",
  "Start from a draft of your connected platforms, or with AI; nothing is saved until you review and save.",
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
  const navigate = useNavigate();
  const prompt = useOrganizationSetupPrompt(Boolean(isAdmin && isXtmOne && !user?.impersonating));
  const answer = useAnswerOrganizationSetupPrompt();

  const open = Boolean(prompt.data?.propose) && !answer.isSuccess;
  const reply = (outcome: OrganizationSetupAnswer) =>
    answer.mutate(outcome, {
      onSettled: () => {
        if (outcome === "started") navigate(ORGANIZATION_SETUP_PATH);
      },
    });

  if (!open) return null;
  return (
    <Dialog open onOpenChange={(next) => !next && !answer.isPending && reply("later")}>
      <DialogContent size="md" data-testid="organization-setup-prompt">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Building2 className="h-4 w-4 text-accent" aria-hidden />
            {t_i18n("Set up your organization profile")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("Every agent and fleet reads this profile before it acts. It takes a few minutes and you can change it at any time.")}
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
            {t_i18n("This is proposed once. You will find it later in Settings > Organization.")}
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
