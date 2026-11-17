// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
/**
 * The "About" dialog: the running version and the platform blurb. Opened from
 * the profile menu (`Header`), the left navigation keeping to the navigation
 * as in OpenCTI and OpenAEV.
 */
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { COMMIT_SHORT_SHA, APP_VERSION } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import {
  usePlatformConfig, useIsXtmOne, usePlatformName, usePlatformLogoUrl,
} from "@/hooks/usePlatformConfig";

export function AboutDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const isXtm = useIsXtmOne();
  const platformName = usePlatformName();
  const platformConfig = usePlatformConfig();
  const logoSrc = usePlatformLogoUrl();
  const logoAlt = platformName;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <img src={logoSrc} alt={logoAlt} className="h-8 shrink-0" />
            <div>
              <DialogTitle className="sr-only">{platformName}</DialogTitle>
              <p className="content-compact text-default-secondary mt-0.5">{t_i18n("Version")} {platformConfig?.platform_version ?? APP_VERSION} ({COMMIT_SHORT_SHA})</p>
            </div>
          </div>
        </DialogHeader>
        <DialogDescription asChild>
          <div className="space-y-4 content-base leading-relaxed text-default-secondary">
            {isXtm ? (
              <>
                <p>
                  {platformName} is the open source, AI-native cockpit for threat-informed defense, built by{" "}
                  <a href="https://filigran.io" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">Filigran</a>.
                  It connects OpenCTI (cyber threat intelligence) and OpenAEV (adversarial exposure validation) into a single conversational experience to operationalize Continuous Threat Exposure Management (CTEM).
                </p>
                <p>
                  Ask questions in natural language to analyze threats, build dashboards and hunting packages, design adversary emulation scenarios mapped to MITRE ATT&CK, and convert indicators into detection rules for your SIEM, EDR, or XDR. Extend it with autonomous agents, MCP tool servers, custom tools, and vectorized knowledge bases — all running with your own AI provider and your own data.
                </p>
              </>
            ) : (
              <>
                <p>
                  {platformName} is an autonomous agentic assistant platform built by{" "}
                  <a href="https://filigran.io" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">Filigran</a>.
                  Configure AI agents with personas, knowledge bases, skills, and event-driven assignments that act on your behalf — triaging emails, answering questions, enforcing conventions, drafting replies, and more.
                </p>
                <p>
                  The platform connects to email, GitHub, Slack, Teams, WhatsApp, Telegram, and dozens of other services. Extend it further with MCP tool servers, custom tools, and vectorized knowledge bases for RAG — all backed by multiple LLM providers with encrypted credentials and fine-grained access control.
                </p>
              </>
            )}
          </div>
        </DialogDescription>
        <div className="pt-3 border-t border-elevation-subtle-soft flex items-center gap-3">
          <a
            href="https://github.com/XTM-One-Platform/xtm-one"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 rounded-sm px-3 py-2 content-base text-accent hover:bg-accent/10 transition-colors"
          >
            <Icon name="external-link" size={16} />
            GitHub
          </a>
          <a
            href="https://filigran.io"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 rounded-sm px-3 py-2 content-base text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors"
          >
            <Icon name="external-link" size={16} />
            filigran.io
          </a>
        </div>
      </DialogContent>
    </Dialog>
  );
}
