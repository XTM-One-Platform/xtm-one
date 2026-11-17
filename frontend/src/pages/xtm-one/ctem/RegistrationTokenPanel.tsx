// SPDX-License-Identifier: Apache-2.0

/**
 * The registration token an admin copies into OpenCTI / OpenAEV / OpenCRQ.
 *
 * Only a token of the operator's own (`state === "configured"`) is ever
 * presented as the value to paste. The published docker-compose fallback
 * authenticates nobody and cannot connect a new platform, and an unset token
 * disables registration: both say instead that a real token must be set on
 * XTM One and on every platform, with the command that generates one.
 */
import { useState } from "react";
import { AlertTriangle, Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import type { RegistrationTokenInfo } from "@/types";

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };
  return (
    <Tip content={label}>
      <Button variant="ghost" size="sm" className="h-7 w-7 p-0 shrink-0 hover:bg-accent/10" onClick={copy} aria-label={label}>
        {copied ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
      </Button>
    </Tip>
  );
}

export function RegistrationTokenPanel({ info, isLoading }: { info: RegistrationTokenInfo | undefined; isLoading: boolean }) {
  const { t_i18n } = useFormatter();
  const platformName = usePlatformName("XTM One");

  if (info?.state === "configured" && info.registration_token) {
    return (
      <div className="space-y-1.5">
        <div className="rounded-sm border border-accent/15 bg-accent/[0.04] p-3 flex items-center gap-2.5">
          <span className="content-caption font-medium text-default-secondary uppercase tracking-wider shrink-0">
            {t_i18n("Registration token")}
          </span>
          <code className="content-compact font-mono-plex text-accent truncate flex-1">{info.registration_token}</code>
          <CopyButton value={info.registration_token} label={t_i18n("Copy token")} />
        </div>
        <p className="content-caption text-default-secondary">
          {t_i18n("Use this same value as the registration token of every connected OpenCTI, OpenAEV or OpenCRQ platform.")}
        </p>
      </div>
    );
  }

  if (!info) {
    return (
      <p className="text-[11px] text-default-secondary">
        {isLoading ? t_i18n("Loading the registration token...") : t_i18n("The registration token could not be loaded.")}
      </p>
    );
  }

  return (
    <div className="rounded-sm bg-warning-box p-3 space-y-2">
      <div className="flex items-start gap-2.5">
        <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="content-compact font-medium text-feedback-warning-primary">
            {info.state === "published_default"
              ? t_i18n("The published default registration token is in use")
              : t_i18n("Platform registration is disabled")}
          </p>
          <p className="content-compact text-feedback-warning-primary leading-relaxed">
            {info.state === "published_default"
              ? t_i18n("PLATFORM_REGISTRATION_TOKEN is set to a public default value anyone can use: it cannot connect a new platform, so never copy it into one.")
              : t_i18n("PLATFORM_REGISTRATION_TOKEN is not set on {platform}, so no platform can register.", { platform: platformName })}{" "}
            {t_i18n("Generate a random token, set it as PLATFORM_REGISTRATION_TOKEN on {platform} and as the registration token of every OpenCTI, OpenAEV or OpenCRQ platform, then restart them.", { platform: platformName })}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2 rounded-sm bg-elevation-default px-2.5 py-1.5">
        <code className="text-[11px] font-mono-plex text-default-secondary truncate flex-1">{info.generate_command}</code>
        <CopyButton value={info.generate_command} label={t_i18n("Copy command")} />
      </div>
    </div>
  );
}
