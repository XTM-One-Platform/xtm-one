// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import {
  Sparkles,
  BrainCircuit,
  Bot,
  Workflow,
  ExternalLink,
  KeyRound,
  ArrowRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { usePlatformConfig } from "@/hooks/usePlatformConfig";
import { useNavigate } from "react-router-dom";
import { LicenseActivationDialog } from "@/components/ui/license-activation-dialog";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { PlatformSplash } from "@/components/ui/platform-splash";
import { FILIGRAN_CONTACT_URL, XTM_ONE_TRIAL_URL } from "@/lib/constants";
import { withBasePath } from "@/lib/base-path";

const VALUE_PROPS = [
  {
    icon: Bot,
    title: "Autonomous Agents",
    desc: "Persona-driven agents that triage, investigate, draft, and act on your behalf: across email, chat, tickets and your security stack.",
  },
  {
    icon: Workflow,
    title: "End-to-End CTEM Pipeline",
    desc: "Unify OpenCTI and OpenAEV into a single continuous threat exposure management loop: discovery to validation to mobilization.",
  },
  {
    icon: BrainCircuit,
    title: "Multi-LLM, Multi-Tool",
    desc: "Anthropic, OpenAI, Gemini, Mistral, Bedrock, OpenRouter and more: wired into MCP servers, knowledge bases and custom tools.",
  },
];

const PLATFORM_INFO: Record<string, { name: string; logo: string }> = {
  opencti: { name: "OpenCTI", logo: withBasePath("/logos/opencti.svg") },
  openaev: { name: "OpenAEV", logo: withBasePath("/logos/openaev.svg") },
  openbas: { name: "OpenAEV", logo: withBasePath("/logos/openaev.svg") },
  opencrq: { name: "OpenCRQ", logo: withBasePath("/logos/opencrq.svg") },
};

/**
 * Community Edition splash shown ONCE on the XTM One home page when:
 *   - The deployment is XTM One
 *   - No Filigran license is installed
 *   - No registered platform has an Enterprise license either
 *   - At least one Community Edition platform is registered (OpenCTI
 *     and/or OpenAEV) — i.e. ``edition === "community"`` and
 *     ``active_registration_count > 0``. When no platform is registered
 *     yet the home page renders ``WelcomeLanding`` instead.
 *
 * Only the home page renders this splash — the rest of the app stays
 * navigable so users can explore configuration and (existing)
 * conversation history. Chat *write* surfaces (composer, "new
 * conversation", suggestions) are disabled via ``useChatSendEnabled``
 * and the backend mirrors the gate with ``require_chat_enabled``. After
 * the splash, the `EnterpriseUpsellNotice` in the dashboard notices region
 * keeps the upgrade path one click away.
 *
 * Visual style mirrors ``LicenseRequired`` (the Copilot-mode lockdown)
 * and ``WelcomeLanding`` (the CTEM hero) so the lockdown feels native
 * rather than bolted on.
 */
export function CtemTrialSplash() {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const pc = usePlatformConfig();
  const [activateOpen, setActivateOpen] = useState(false);
  const { data: registrations } = usePlatformRegistrations();
  const updateProfile = useUpdateProfile();

  // Enter the command center: mark the splash seen so it never shows again for
  // this user (the CE numeric posture is available without a license).
  const enterCommandCenter = () => updateProfile.mutate({ ctem_splash_seen: true });

  // Detect connected CE platforms so we can name them in the hero copy
  // ("Your OpenCTI is connected — unlock the AI brain"). Falls back to a
  // generic CTA when the registrations call is still loading.
  const connectedPlatforms = (registrations ?? [])
    .filter((r) => r.status === "active")
    .map((r) => PLATFORM_INFO[r.platform_identifier] ?? null)
    .filter((p): p is { name: string; logo: string } => p !== null);

  const connectedNames =
    connectedPlatforms.length > 0
      ? Array.from(new Set(connectedPlatforms.map((p) => p.name)))
      : [];

  return (
    <PlatformSplash
      logo={<img src={withBasePath("/xtp-one.svg")} alt="XTM One" className="h-16" />}
      badge={
        <Badge variant="outline" className="content-caption text-accent border-accent/30 gap-1.5">
          <Sparkles className="h-3 w-3" />
          {t_i18n("Enterprise Agentic AI for CTEM")}
        </Badge>
      }
      headline={
        connectedNames.length > 0
          ? intl.formatMessage(
              { id: "Unlock the AI brain for your {names} deployment" },
              { names: <span className="text-gradient-accent">{connectedNames.join(" & ")}</span> },
            )
          : intl.formatMessage(
              { id: "Activate {product} to power your CTEM with Agentic AI" },
              { product: <span className="text-gradient-accent">XTM One</span> },
            )
      }
      description={intl.formatMessage(
        { id: "You're running {edition} platforms. Start a free trial or contact our team to activate Enterprise Agentic AI: autonomous agents, continuous threat exposure management, and a unified ops center across your stack." },
        { edition: <span className="text-default-primary font-medium">{t_i18n("Community Edition")}</span> },
      )}
      actions={
        <>
          <Button
            size="lg"
            className="gap-2 bg-accent text-negative-primary font-medium hover:bg-accent/90 shadow-[0_0_25px_color-mix(in_srgb,_var(--color-accent)_calc(0.3_*_100%),_transparent)] hover:shadow-[0_0_35px_color-mix(in_srgb,_var(--color-accent)_calc(0.45_*_100%),_transparent)] transition-all duration-300"
            onClick={enterCommandCenter}
            disabled={updateProfile.isPending}
          >
            <ArrowRight className="h-4 w-4" />
            {t_i18n("Explore the command center")}
          </Button>
          <Button
            size="lg"
            variant="outline"
            className="gap-2"
            onClick={() => window.open(XTM_ONE_TRIAL_URL, "_blank", "noopener,noreferrer")}
          >
            <Sparkles className="h-4 w-4" />
            {t_i18n("Start free trial")}
          </Button>
          <Button
            size="lg"
            variant="outline"
            className="gap-2"
            onClick={() => window.open(FILIGRAN_CONTACT_URL, "_blank", "noopener,noreferrer")}
          >
            <ExternalLink className="h-4 w-4" />
            {t_i18n("Contact sales")}
          </Button>
          {isAdmin && (
            <Button
              size="lg"
              variant="ghost"
              className="gap-2 text-default-secondary"
              onClick={() => setActivateOpen(true)}
            >
              <KeyRound className="h-4 w-4" />
              {t_i18n("Activate license")}
            </Button>
          )}
        </>
      }
      hero={
        connectedPlatforms.length > 0 ? (
          <div className="flex flex-wrap items-center justify-center gap-3 pt-4 border-t border-elevation-subtle-soft mt-4 w-full">
            <span className="content-caption uppercase tracking-wider text-default-secondary font-medium">
              {t_i18n("Connected")}
            </span>
            {Array.from(new Map(connectedPlatforms.map((p) => [p.name, p])).values()).map((p) => (
              <div key={p.name} className="flex items-center gap-1.5 px-2 py-1 rounded-sm layer-1 bg-elevation-default border border-elevation-subtle-soft">
                <img src={p.logo} alt={p.name} className="h-3.5 w-3.5" />
                <span className="content-compact text-default-secondary">{p.name}</span>
                <Badge variant="outline" className="text-[8px] py-0 px-1.5 h-3.5 ml-1 text-default-secondary border-elevation-subtle">
                  CE
                </Badge>
              </div>
            ))}
          </div>
        ) : null
      }
      valueProps={VALUE_PROPS}
      footer={
        <>
          {isAdmin && (
            <p className="content-compact text-default-secondary max-w-md mx-auto">
              {t_i18n("As an admin you can still configure users, groups, intents, logs and settings to pre-provision the platform. Activate a license to unlock the full agentic experience.")}
            </p>
          )}
          {!isAdmin && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Ask your administrator to activate a license, or")}{" "}
              <button
                type="button"
                className="text-accent hover:underline"
                onClick={() => navigate("/profile")}
              >
                {t_i18n("update your profile")}
              </button>
              {" "}{t_i18n("while you wait.")}
            </p>
          )}
        </>
      }
    >
      <LicenseActivationDialog
        open={activateOpen}
        onOpenChange={setActivateOpen}
        platformName={pc?.platform_name || "XTM One"}
      />
    </PlatformSplash>
  );
}
