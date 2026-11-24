// SPDX-License-Identifier: Apache-2.0

import { Link } from "react-router-dom";
import {
  Boxes,
  Settings as SettingsIcon,
  Sparkles,
  Users,
  Workflow,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  PlatformSplash,
  type SplashValueProp,
} from "@/components/ui/platform-splash";
import { useAuth } from "@/hooks/useAuth";
import { useAiDisabledGroup } from "@/hooks/useAiDisabledGroup";
import { usePlatformAiEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

/**
 * The home page when AI is disabled (FR-019).
 *
 * States three things: that AI is off, why, and what enabling it takes. Built on
 * `PlatformSplash` — the same shell as `CtemTrialSplash` — because one splash
 * pattern that both lockdown states fill is one to keep consistent, not two
 * (Principle X).
 *
 * The copy below is deliberately declarative and in one place: product and
 * marketing own these words, so replacing them should not mean touching layout.
 */

/** What still works — FR-021's list, so this reads as a scope, not an outage. */
const STILL_AVAILABLE: SplashValueProp[] = [
  {
    icon: Workflow,
    title: "Deterministic flows keep running",
    desc: "Flows without an AI step, their triggers and their schedules dispatch exactly as before. Integration syncs keep importing and exporting.",
  },
  {
    icon: Boxes,
    title: "Your data stays browsable",
    desc: "Agents, knowledge bases, flows, runs and traces remain readable. Nothing was deleted, and nothing needs repairing when AI is switched back on.",
  },
  {
    icon: Users,
    title: "Administration is unaffected",
    desc: "Sign-in and single sign-on, users, groups, roles, invitations, platform settings, branding, audit logs and health all work normally.",
  },
];

function AiDisabledLanding() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const platformAiOn = usePlatformAiEnabled();
  const group = useAiDisabledGroup();
  /* The platform's own switch wins: with it off, nothing a group says matters.
     With it on, AI is off for this person because of a group of theirs. */
  const byGroup = platformAiOn && group !== null;

  return (
    <PlatformSplash
      logo={
        <div className="flex h-16 w-16 items-center justify-center rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/[0.07]">
          <Sparkles className="h-7 w-7 text-feedback-warning-primary" />
        </div>
      }
      badge={
        <Badge
          variant="outline"
          className="content-caption text-feedback-warning-primary border-feedback-alert-primary/30 gap-1.5"
        >
          <Sparkles className="h-3 w-3" />
          {t_i18n("AI disabled")}
        </Badge>
      }
      headline={byGroup ? t_i18n("AI is switched off for your group") : t_i18n("AI is switched off on this platform")}
      description={
        byGroup
          ? t_i18n(
              "An administrator switched AI off for one of your groups. No AI request is made on your behalf and none of your automations run a model. Everything that does not need a model keeps working normally.",
            )
          : t_i18n(
              "This deployment runs without any model or embedding provider: no AI requests leave it, and no AI work is scheduled. Everything that does not need a model keeps working normally.",
            )
      }
      actions={
        byGroup ? (
          isAdmin ? (
            <Button size="lg" className="gap-2" asChild>
              <Link to="/users-and-groups/groups">
                <Users className="h-4 w-4" />
                {t_i18n("Review your groups")}
              </Link>
            </Button>
          ) : null
        ) : isAdmin ? (
          <Button size="lg" className="gap-2" asChild>
            <Link to="/settings">
              <SettingsIcon className="h-4 w-4" />
              {t_i18n("Enable AI in platform parameters")}
            </Link>
          </Button>
        ) : null
      }
      valueProps={STILL_AVAILABLE}
      footer={
        byGroup ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("Ask an administrator if you need AI back.")}
          </p>
        ) : isAdmin ? (
          <p className="mx-auto max-w-md content-compact text-default-secondary">
            {t_i18n(
              "Turning AI on provisions the built-in content and needs no restart. You will also need at least one model provider configured before agents can run.",
            )}
          </p>
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "Ask an administrator to enable AI in Settings → Platform. No licence is required.",
            )}
          </p>
        )
      }
    />
  );
}

export default AiDisabledLanding;
