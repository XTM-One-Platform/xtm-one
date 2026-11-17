// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactNode } from "react";
import { Sparkles } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Card, CardContent } from "@/components/ui/card";
import { BackButton } from "@/components/ui/back-button";
import {
  EnterpriseCtaButtons, EnterpriseUpsellBody, LicenseActivationForm,
} from "@/components/ui/enterprise-upsell";
import { useAgenticAIEnabled, useChatSendEnabled, useIsXtmOne } from "@/hooks/usePlatformConfig";
import { AiActionGate, AiDisabledNotice } from "@/components/ui/ai-disabled";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";

/**
 * Licence gates on individual affordances.
 *
 * Under the XTM Platform model there is ONE licence question — is this
 * deployment on Enterprise Edition — so the "agentic" gate and the "chat" gate
 * below are the same gate under two names, kept because their call sites read
 * differently (a creation button, the A2A surfaces). Both open the same
 * `EnterpriseUpsellBody`, which says what Enterprise Edition unlocks and how it
 * is activated; only the first sentence names what the person was trying to do.
 */

interface AgenticGateDialogProps {
  requiresAi?: boolean;
  children: ReactNode;
  feature?: string;
}

/**
 * What the caller was trying to do, as the sentence's subject.
 *
 * Translated here rather than at each call site: the English prose they pass is
 * already a message id. One that arrives translated is not a key, and comes
 * back unchanged.
 */
function useGateSubject(feature?: string): string {
  const { t_i18n } = useFormatter();
  return feature ? t_i18n(feature) : t_i18n("This feature");
}

/** What the gate is missing: Enterprise Edition (XTM One) or a valid license. */
function useEnterpriseRequirement(feature?: string): string {
  const { t_i18n } = useFormatter();
  const isXtmOne = useIsXtmOne();
  const subject = useGateSubject(feature);
  // One message, subject included. Joining fragments in code would put a space
  // between subject and predicate, which is wrong in Japanese.
  return isXtmOne
    ? t_i18n(
        "{subject} is an Enterprise Edition capability. AI is not activated on this Community Edition deployment: connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or activate a Filigran license.",
        { subject },
      )
    : t_i18n(
        "{subject} requires a valid license. Contact Filigran to enable it or request a trial license.",
        { subject },
      );
}

/**
 * The upsell dialog on its own, driven by state instead of by intercepting a
 * click on a child.
 *
 * `AgenticGateDialog` wraps its children in a click-capturing span, which works
 * for buttons but not for a surface driven by the keyboard — the ⌘K palette
 * runs its selection on Enter, where no click ever happens. That surface opens
 * this directly.
 */
export function AgenticGateDialogControlled({
  open, onOpenChange, feature,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  feature?: string;
}) {
  const requirement = useEnterpriseRequirement(feature);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <EnterpriseUpsellBody requirement={requirement} />
    </Dialog>
  );
}

/**
 * `AgenticGateDialog` for surfaces gated on agentic CHAT — A2A. The same
 * licence, read through `useChatSendEnabled` because chat additionally goes
 * dark when an administrator switches AI off, which those surfaces follow.
 */
export function ChatGateDialog({
  children,
  feature,
}: {
  children: ReactNode;
  feature?: string;
}) {
  const offered = useChatSendEnabled();
  const aiDisabled = useAiActionDisabled();
  const requirement = useEnterpriseRequirement(feature);
  const [open, setOpen] = useState(false);

  /* AI off wins over the licence gate, for the reason it does below. */
  if (aiDisabled) return <AiActionGate>{children}</AiActionGate>;
  if (offered) return <>{children}</>;

  return (
    <>
      <span
        onClickCapture={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen(true);
        }}
        className="contents"
      >
        {children}
      </span>

      <Dialog open={open} onOpenChange={setOpen}>
        <EnterpriseUpsellBody requirement={requirement} />
      </Dialog>
    </>
  );
}

export function AgenticGateDialog({
  children,
  feature,
  /**
   * Whether the wrapped action needs AI. Defaults to `true` so a call site must
   * *declare* itself exempt: the AI branch below disables everything it
   * wraps, and a few callers — variables, custom tools, enabling an
   * integration — are things the AI-off mode promises to keep working.
   */
  requiresAi = true,
}: AgenticGateDialogProps) {
  const enabled = useAgenticAIEnabled();
  const aiDisabled = useAiActionDisabled();
  const requirement = useEnterpriseRequirement(feature);
  const [open, setOpen] = useState(false);

  /* AI off wins over the licence gate: no licence enables AI here (FR-031), so
     the upsell below would send an administrator to buy what they already have. */
  if (requiresAi && aiDisabled) return <AiActionGate>{children}</AiActionGate>;
  if (enabled) return <>{children}</>;

  return (
    <>
      <span
        onClickCapture={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen(true);
        }}
        className="contents"
      >
        {children}
      </span>

      <Dialog open={open} onOpenChange={setOpen}>
        <EnterpriseUpsellBody requirement={requirement} />
      </Dialog>
    </>
  );
}

/**
 * Full-page gate for the creation wizards (`/agents/new`, `/knowledge/new`,
 * ...): the same message as the dialog, with room to breathe, and a way back.
 */
export function AgenticGatePage({ feature }: { feature: string }) {
  const { isAdmin } = useAuth();
  const { t_i18n } = useFormatter();
  const isXtmOne = useIsXtmOne();
  const aiDisabled = useAiActionDisabled();
  const requirement = useEnterpriseRequirement(feature);

  /* AI off wins over the licence wall, same as the dialog: the licence copy and
     its CTAs are wrong when the platform simply has AI switched off (FR-031).
     Callers must reach this branch — see the `|| aiDisabled` on the creation
     wizards. */
  if (aiDisabled) {
    return (
      <div className="space-y-8">
        <BackButton to={-1} label={t_i18n("Back")} />
        <div className="flex items-center justify-center min-h-[50vh]">
          <Card className="max-w-lg w-full border-feedback-alert-primary/20">
            <CardContent className="py-8 space-y-6 text-center">
              <div className="flex items-center justify-center">
                <div className="flex h-14 w-14 items-center justify-center rounded-full bg-feedback-alert-primary/10">
                  <Sparkles className="h-7 w-7 text-feedback-warning-primary" />
                </div>
              </div>
              <h2 className="text-xl font-bold font-sans-geologica">
                {t_i18n("AI is switched off on this platform")}
              </h2>
              <AiDisabledNotice className="text-center" />
            </CardContent>
          </Card>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <BackButton to={-1} label={t_i18n("Back")} />
      <div className="flex items-center justify-center min-h-[50vh]">
        <Card className="max-w-lg w-full border-accent/20">
          <CardContent className="py-8 space-y-6">
            <div className="text-center space-y-3">
              <div className="flex items-center justify-center">
                <div className="flex h-14 w-14 items-center justify-center rounded-full bg-accent/10">
                  <Sparkles className="h-7 w-7 text-accent" />
                </div>
              </div>
              <h2 className="text-xl font-bold font-sans-geologica">
                {isXtmOne
                  ? t_i18n("Activate Enterprise Edition to unlock AI")
                  : t_i18n("A valid license is required")}
              </h2>
              <p className="content-base text-default-secondary max-w-sm mx-auto">
                {requirement}
              </p>
            </div>

            <EnterpriseCtaButtons className="[&>button]:flex-1" />

            {isAdmin && <LicenseActivationForm />}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/**
 * "this is gated" on a catalog tile, told rather than inferred.
 *
 * The caller knows which gate the thing answers to; this component cannot.
 * Inferring the licence here put an EE badge on tiles that were available
 * with AI switched off, and the two disagree.
 */
export function EEBadge({ unavailable }: { unavailable: boolean }) {
  if (!unavailable) return null;
  return (
    <span className="ml-auto inline-flex items-center justify-center rounded-sm border border-feedback-alert-primary/40 bg-feedback-alert-primary/10 px-1 py-px content-caption font-semibold leading-none text-feedback-warning-tertiary">
      EE
    </span>
  );
}

/**
 * EE badge for use inside Create/New buttons: dark text on the solid amber fill, which keeps one
 * colour in both themes (`text-on-feedback-fill`, 10.2:1; the warning text on it read 1.5:1 dark,
 * 2.92:1 light).
 */
export function AgenticEEBadgeForButton() {
  const enabled = useAgenticAIEnabled();
  if (enabled) return null;
  return (
    <span className="inline-flex items-center justify-center rounded-sm border border-feedback-alert-primary bg-feedback-alert-primary px-1 py-px content-caption font-semibold leading-none text-on-feedback-fill">
      EE
    </span>
  );
}
