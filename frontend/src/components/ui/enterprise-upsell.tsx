// SPDX-License-Identifier: Apache-2.0

import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Bot, ExternalLink, KeyRound, Link2, MessageSquare, Sparkles, Workflow, Zap,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { invalidateApprovalUsage } from "@/hooks/api/useToolApprovals";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useAuth } from "@/hooks/useAuth";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import {
  EDITION_BADGES, useAgenticAIEnabled, useAiEnabled, useEdition, useIsXtmOne, usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { api } from "@/lib/api";
import { FILIGRAN_CONTACT_URL, XTM_ONE_TRIAL_URL } from "@/lib/constants";
import { SETTINGS_AI_ENABLEMENT_PATH, SETTINGS_MODELS_PATH } from "@/lib/settings-routes";
import { cn } from "@/lib/utils";

/**
 * Everything the platform says about Community Edition, in one module.
 *
 * The XTM Platform model has two editions and one licence answer: Enterprise
 * Edition (a Filigran license, or a connected Enterprise Edition OpenCTI /
 * OpenAEV / OpenCRQ) turns every agentic capability on; Community Edition
 * runs the platform with AI off. So every surface that meets a locked
 * affordance says the same thing and offers the same three ways forward —
 * start a trial, talk to Filigran, or (an administrator) paste a license —
 * through the components below. There is deliberately no comparison table
 * and no "next tier": there is nothing between the two editions to compare.
 */

/* ── Outbound CTAs ─────────────────────────────────────────────────────────── */

const openExternal = (url: string) => window.open(url, "_blank", "noopener,noreferrer");

/**
 * Start free trial (primary) + Contact sales — the two Filigran paths.
 * `withContactSales={false}` keeps the trial alone, for a card whose footer
 * carries one call to action (`AiLockedFooter`); "Contact sales" then stays in
 * the notice and the dialog.
 */
export function EnterpriseCtaButtons({
  size = "default",
  className,
  withContactSales = true,
}: {
  size?: "sm" | "default" | "lg";
  className?: string;
  withContactSales?: boolean;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <Button size={size} className="gap-1.5" onClick={() => openExternal(XTM_ONE_TRIAL_URL)}>
        <Sparkles className="h-3.5 w-3.5" />
        {t_i18n("Start free trial")}
      </Button>
      {withContactSales && (
        <Button size={size} variant="outline" className="gap-1.5" onClick={() => openExternal(FILIGRAN_CONTACT_URL)}>
          <ExternalLink className="h-3.5 w-3.5" />
          {t_i18n("Contact sales")}
        </Button>
      )}
    </div>
  );
}

/* ── Locked AI surface footer ──────────────────────────────────────────────── */

/**
 * The bottom line of an AI card that cannot run: the reason in one sentence
 * and at most one way forward, chosen by `useAiLockReason()` (one reason per
 * surface, AI off before the license, the license before the model). An
 * administrator gets the action; anyone else gets the sentence alone, never a
 * disabled button. Renders nothing when AI can run. The spec is section 6 of
 * dev-docs/ctem-open-source.md.
 *
 * `layout="stacked"` puts the sentence on its own line and the actions on ONE
 * row under it, for a narrow slot (the command center hero at 1024) where the
 * inline flow wraps each action onto a row of its own.
 */
export function AiLockedFooter({
  reason,
  className,
  layout = "inline",
}: {
  reason: AiLockReason | null;
  className?: string;
  layout?: "inline" | "stacked";
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const disabledReason = useAiDisabledReason();
  const [open, setOpen] = useState(false);
  if (!reason) return null;

  const sentence = (() => {
    switch (reason) {
      case "group_off":
        return t_i18n("AI is disabled for your group. Ask an administrator if you need it.");
      case "ai_off":
        // The shared platform sentence; the hook answers null when the config
        // still says AI is on and only the backend status knows it is off.
        return disabledReason ?? t_i18n(
          "AI is disabled on this platform. An administrator can enable it in Settings → Platform.",
        );
      case "unlicensed":
        return isAdmin
          ? t_i18n("AI features run with Enterprise Edition.")
          : t_i18n("Ask your administrator to activate a license.");
      case "no_model":
        return isAdmin
          ? null
          : t_i18n("No default model is configured. Contact your administrator to configure it.");
    }
  })();

  const action = (() => {
    if (!isAdmin) return null;
    switch (reason) {
      case "ai_off":
        return (
          <Button asChild size="sm" variant="outline">
            <Link to={SETTINGS_AI_ENABLEMENT_PATH}>{t_i18n("Turn AI on")}</Link>
          </Button>
        );
      case "unlicensed":
        return (
          <>
            <EnterpriseCtaButtons size="sm" withContactSales={false} />
            <button
              type="button"
              onClick={() => setOpen(true)}
              className="content-compact text-accent underline-offset-2 hover:underline rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {t_i18n("Activate License")}
            </button>
          </>
        );
      case "no_model":
        return (
          <Button asChild size="sm" variant="outline">
            <Link to={SETTINGS_MODELS_PATH}>{t_i18n("Configure a model")}</Link>
          </Button>
        );
      default:
        return null;
    }
  })();

  const stacked = layout === "stacked";
  return (
    <div
      className={cn(
        stacked ? "flex flex-col items-start gap-2" : "flex flex-wrap items-center gap-x-3 gap-y-2",
        className,
      )}
      data-testid="ai-locked-footer"
      data-reason={reason}
      data-layout={layout}
    >
      {sentence && <p className="content-compact text-default-secondary">{sentence}</p>}
      {stacked && action ? (
        <div className="flex flex-nowrap items-center gap-3 [&>div]:flex-nowrap" data-testid="ai-locked-footer-actions">
          {action}
        </div>
      ) : (
        action
      )}
      {reason === "unlicensed" && isAdmin && <EnterpriseUpsellDialog open={open} onOpenChange={setOpen} />}
    </div>
  );
}

/* ── License activation form ───────────────────────────────────────────────── */

/**
 * Paste-a-PEM form, for an administrator with a license in hand. Saves
 * through the same `/settings/general-assistant` mutation as the Settings
 * page; the parent surface refreshes once `platform-config` refetches and
 * `useAgenticAIEnabled()` flips.
 */
export function LicenseActivationForm({ compact = false }: { compact?: boolean }) {
  const { t_i18n } = useFormatter();
  const [pem, setPem] = useState("");
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const qc = useQueryClient();

  const saveLicense = useMutation({
    // Invalid PEMs render inline (the destructive banner under the
    // textarea); suppress the global MutationCache toast in ``main.tsx`` to
    // avoid duplicating the same message in a global toast.
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (license: string) =>
      api.put("/settings/general-assistant", { enterprise_license: license }),
    onMutate: () => setError(null),
    onSuccess: () => {
      setPem("");
      if (textareaRef.current) textareaRef.current.value = "";
      qc.refetchQueries({ queryKey: ["platform-config"] });
      invalidateApprovalUsage(qc);
    },
    onError: (err: Error) => {
      setError(err.message || t_i18n("Failed to validate license"));
    },
  });

  return (
    <div className={cn("space-y-3", !compact && "pt-3 border-t border-elevation-subtle-soft")}>
      <div className="flex items-center gap-2 content-compact text-default-secondary">
        <KeyRound className="h-3.5 w-3.5 text-accent shrink-0" />
        <span>{t_i18n("Already have a license? Paste it below to activate.")}</span>
      </div>
      <textarea
        ref={textareaRef}
        defaultValue={pem}
        onChange={(e) => {
          setPem(e.target.value);
          if (error) setError(null);
        }}
        placeholder={`-----BEGIN CERTIFICATE-----\n${t_i18n("Paste your Filigran license PEM...")}\n-----END CERTIFICATE-----`}
        className="w-full h-28 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus resize-none"
      />
      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 p-2 content-base text-feedback-error-primary"
        >
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <span className="leading-relaxed">{error}</span>
        </div>
      )}
      <Button
        className="w-full"
        onClick={() => {
          const val = textareaRef.current?.value || pem;
          if (!val.trim()) {
            setError(t_i18n("Paste a license PEM certificate before activating."));
            return;
          }
          saveLicense.mutate(val);
        }}
        disabled={saveLicense.isPending}
      >
        {saveLicense.isPending ? t_i18n("Validating...") : t_i18n("Activate License")}
      </Button>
    </div>
  );
}

/* ── What Enterprise Edition unlocks ───────────────────────────────────────── */

const UNLOCKS = [
  { icon: MessageSquare, label: "Agentic chat across OpenCTI, OpenAEV and OpenCRQ" },
  { icon: Bot, label: "Autonomous agents, assignments and the full Agent Studio" },
  { icon: Workflow, label: "Flows, knowledge bases, skills, MCP servers, A2A and every integration" },
  { icon: Zap, label: "An included agentic allowance from day one, growing with acts packs" },
] as const;

/**
 * The body every Enterprise upsell surface shows: one title, what the edition
 * unlocks, how it is activated, the two Filigran CTAs and — for an admin — the
 * activation form, so a license in hand can be pasted where the refusal
 * happens. `requirement` names what the person was trying to do.
 */
export function EnterpriseUpsellBody({ requirement }: { requirement?: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();

  return (
    <DialogContent className="max-w-lg">
      <DialogHeader>
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
            <Sparkles className="h-5 w-5 text-accent" />
          </div>
          <div className="space-y-1">
            <DialogTitle className="text-base">
              {isXtmOne
                ? t_i18n("Activate Enterprise Edition to unlock AI")
                : t_i18n("A valid license is required")}
            </DialogTitle>
            <DialogDescription className="content-base leading-relaxed">
              {requirement ?? (isXtmOne
                ? t_i18n("AI is not activated on this Community Edition deployment. Everything is already installed: Enterprise Edition is one license key away, never a redeploy.")
                : t_i18n("Paste a valid Filigran license to use AI on this platform."))}
            </DialogDescription>
          </div>
        </div>
      </DialogHeader>

      {isXtmOne && (
        <div className="space-y-4 pt-1">
          <ul className="space-y-2">
            {UNLOCKS.map(({ icon: Icon, label }) => (
              <li key={label} className="flex items-start gap-2.5 content-base">
                <Icon className="h-4 w-4 shrink-0 mt-0.5 text-accent" />
                <span className="text-default-secondary">{t_i18n(label)}</span>
              </li>
            ))}
          </ul>

          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-2">
            <p className="content-caption font-medium uppercase tracking-wider text-default-secondary">
              {t_i18n("Two ways to activate")}
            </p>
            <div className="flex items-start gap-2.5 content-compact">
              <Link2 className="h-3.5 w-3.5 shrink-0 mt-0.5 text-accent" />
              <span className="text-default-secondary">
                {t_i18n("Connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform: this deployment inherits Enterprise Edition with the included agentic allowance.")}
              </span>
            </div>
            <div className="flex items-start gap-2.5 content-compact">
              <KeyRound className="h-3.5 w-3.5 shrink-0 mt-0.5 text-accent" />
              <span className="text-default-secondary">
                {t_i18n("Activate a Filigran license: it carries your own agentic capacity, sized to your usage.")}
              </span>
            </div>
          </div>
        </div>
      )}

      <EnterpriseCtaButtons className="pt-2 [&>button]:flex-1" />

      {isAdmin && <LicenseActivationForm />}
    </DialogContent>
  );
}

/**
 * The upsell dialog on its own, driven by state. Opened by the edition badge,
 * the ⌘K palette and any surface that has no child to click-capture.
 */
export function EnterpriseUpsellDialog({
  open,
  onOpenChange,
  requirement,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  requirement?: string;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <EnterpriseUpsellBody requirement={requirement} />
    </Dialog>
  );
}

/* ── Tooltip ───────────────────────────────────────────────────────────────── */

/**
 * Wraps a control with the shared explanation when AI is not licensed, and
 * renders it untouched otherwise — `Tip` passes children through on empty
 * content. `wrap` defaults to true because the wrapped control is normally
 * natively disabled, and a disabled element swallows the hover.
 */
export function EnterpriseLockedTip({
  children,
  side = "top",
  wrap = true,
  wrapClassName,
}: {
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  wrap?: boolean;
  wrapClassName?: string;
}) {
  const reason = useEnterpriseLockedReason();
  return (
    <Tip content={reason ?? undefined} side={side} wrap={wrap && !!reason} wrapClassName={wrapClassName}>
      {children}
    </Tip>
  );
}

/* ── Dashboard notice ──────────────────────────────────────────────────────── */

/**
 * The Community Edition notice for the dashboard notices region and the AI
 * settings sections: what is off, why, and the three ways forward. Renders
 * nothing when AI is licensed — and it is the ONLY notice a Community Edition
 * deployment should show about AI: the "no default model" / "no reasoning
 * provider" warnings are suppressed while it is up, because configuring a
 * provider changes nothing until Enterprise Edition is activated.
 *
 * Also nothing while the administrator's AI switch is off: no licence
 * enables AI that is switched off, so the notice would promise an unlock that
 * activation does not deliver — the AI-off surfaces explain that state.
 */
export function EnterpriseUpsellNotice({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const reason = useEnterpriseLockedReason();
  const isXtmOne = useIsXtmOne();
  const aiEnabled = useAiEnabled();
  const [open, setOpen] = useState(false);
  if (!reason || !isXtmOne || !aiEnabled) return null;
  return (
    <>
      <DashboardNotice
        variant="info"
        icon={Sparkles}
        title={t_i18n("AI is not activated - this deployment runs Community Edition")}
        className={className}
        actions={
          <>
            <NoticeAction variant="info" onClick={() => openExternal(XTM_ONE_TRIAL_URL)}>
              <Sparkles className="h-3 w-3" /> {t_i18n("Start free trial")}
            </NoticeAction>
            <NoticeAction variant="info" onClick={() => openExternal(FILIGRAN_CONTACT_URL)}>
              <ExternalLink className="h-3 w-3" /> {t_i18n("Contact sales")}
            </NoticeAction>
            {isAdmin && (
              <NoticeAction variant="info" onClick={() => setOpen(true)}>
                <KeyRound className="h-3 w-3" /> {t_i18n("Activate")}
              </NoticeAction>
            )}
          </>
        }
      >
        {t_i18n("Agents, chat and automations unlock with Enterprise Edition: connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or activate a Filigran license. Everything is already installed - nothing to redeploy.")}
      </DashboardNotice>
      <EnterpriseUpsellDialog open={open} onOpenChange={setOpen} />
    </>
  );
}

/* ── Edition badge ─────────────────────────────────────────────────────────── */

/** Copilot mode's licence state, the one style of each (#9448). */
const COPILOT_LICENCE_BADGES = {
  licensed: { label: "Licensed", variant: "cyan", className: "" },
  community: { label: "Community", variant: "outline", className: "text-feedback-warning-primary border-feedback-alert-primary/30" },
} as const;

/**
 * The XTM One edition badge — the single surface for "which edition is this
 * deployment on". Every edition badge in the app goes through this component
 * so the label, the colors and the click behaviour never drift between the
 * CTEM command center, the Settings license card and the Quotas banner.
 *
 * On Community Edition the badge is a button: clicking it opens the upsell
 * dialog, for every user (only the activation form inside is admin-gated).
 * On Enterprise Edition it is a plain label carrying the caller's tooltip.
 *
 * Copilot mode reports an empty ``edition``: there the badge answers the
 * licence state instead, "Licensed" or "Community" (#9448, design
 * dev-docs/roi-outcomes.md 6.1 C1), a plain label in its own style. Never an
 * edition badge drawn by hand (`pages/settings/edition-badge-gate.test.ts`).
 */
export function EditionBadge({
  className,
  tooltip,
  variant = "outline",
  icon,
}: {
  /** Extra classes on the ``Badge`` itself (callers set the font size). */
  className?: string;
  /** Richer context for the hover (the license customer / type, for instance). */
  tooltip?: ReactNode;
  variant?: "default" | "outline";
  /** Optional leading icon rendered inside the badge, before the label. */
  icon?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const edition = useEdition();
  const licensed = useAgenticAIEnabled();
  const loaded = usePlatformConfig() != null;
  const [open, setOpen] = useState(false);
  // Nothing until the platform config is read: never a "Community" flash.
  if (!loaded) return null;
  if (edition === "") {
    const state = COPILOT_LICENCE_BADGES[licensed ? "licensed" : "community"];
    const label = (
      <Badge variant={state.variant} className={cn(state.className, className)}>
        {icon}
        {t_i18n(state.label)}
      </Badge>
    );
    return tooltip ? <Tip content={tooltip}>{label}</Tip> : label;
  }

  const badge = EDITION_BADGES[edition];
  const el = (
    <Badge variant={variant} className={cn(badge.className, className)}>
      {icon}
      {t_i18n(badge.label)}
    </Badge>
  );

  if (edition === "enterprise") {
    return tooltip ? <Tip content={tooltip}>{el}</Tip> : el;
  }

  const hint = t_i18n("Click to see what Enterprise Edition unlocks and how to activate it.");
  return (
    <>
      <Tip
        content={
          tooltip ? (
            <span className="block space-y-1">
              <span className="block">{tooltip}</span>
              <span className="block text-default-secondary">{hint}</span>
            </span>
          ) : (
            hint
          )
        }
      >
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex items-center rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        >
          <Badge
            variant={variant}
            className={cn(badge.className, "cursor-pointer transition-opacity hover:opacity-80", className)}
          >
            {icon}
            {t_i18n(badge.label)}
          </Badge>
        </button>
      </Tip>
      <EnterpriseUpsellDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
