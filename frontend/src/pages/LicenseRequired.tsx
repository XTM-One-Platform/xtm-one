// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import {
  KeyRound, ShieldCheck, AlertTriangle, CheckCircle2,
  Fingerprint, Copy, Check,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { usePlatformConfig, usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
import { useSaveEnterpriseLicense } from "@/hooks/api/useSettingsApi";
import { COPY_FEEDBACK_DELAY, FILIGRAN_CONTACT_URL, XTM_ONE_TRIAL_URL } from "@/lib/constants";
import { useFormatter } from "@/i18n";

/** Platform ID row with copy-to-clipboard — the ID is required to generate
 *  a Filigran license certificate, and Settings is unreachable while the
 *  platform is gated behind this splash. */
function PlatformIdRow({ instanceId }: { instanceId: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);

  if (!instanceId) return null;

  const handleCopy = () => {
    navigator.clipboard.writeText(instanceId);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5 content-compact text-default-secondary">
        <Fingerprint className="h-3.5 w-3.5 text-accent" />
        {t_i18n("Platform ID: required to generate your license certificate")}
      </div>
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex select-all truncate">
          {instanceId}
        </code>
        <Tip content={copied ? t_i18n("Copied!") : t_i18n("Copy to clipboard")}>
          <Button
            size="sm"
            variant="outline"
            className="shrink-0 h-9 w-9 p-0"
            onClick={handleCopy}
          >
            {copied ? <Check className="h-4 w-4 text-feedback-success-primary" /> : <Copy className="h-4 w-4" />}
          </Button>
        </Tip>
      </div>
    </div>
  );
}

/**
 * The Copilot-mode licence wall: the whole app sits behind a valid Filigran
 * Filigran license. Any valid license activates the platform — the retired
 * "Enable Copilot" capability flag on the certificate is not consulted
 * anymore (a license carries a quota, not a feature switch), so there is no
 * "valid but not included" state to explain.
 */
export default function LicenseRequired() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const pc = usePlatformConfig();
  const platformName = usePlatformName();
  const logoUrl = usePlatformLogoUrl();
  const [pem, setPem] = useState("");
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const instanceId = pc?.instance_id ?? "";

  const saveLicense = useSaveEnterpriseLicense();
  const handleActivate = () => {
    const val = textareaRef.current?.value || pem;
    if (!val.trim()) return;
    saveLicense.mutate(val, {
      onSuccess: () => setError(null),
      onError: (err: Error) => {
        setError(err.message || t_i18n("Failed to save license"));
      },
    });
  };

  return (
    <div className="min-h-screen bg-elevation-default flex items-center justify-center p-6">
      <div className="w-full max-w-lg space-y-8 animate-fade-in">
        <div className="text-center space-y-4">
          <img
            src={logoUrl}
            alt={platformName || "Platform"}
            className="h-10 mx-auto"
          />
          <div className="flex items-center justify-center gap-2 text-feedback-warning-primary">
            <ShieldCheck className="h-6 w-6" />
            <h1 className="text-xl font-bold font-sans-geologica">
              {t_i18n("Filigran License Required")}
            </h1>
          </div>
          <p className="content-base text-default-secondary max-w-sm mx-auto">
            {t_i18n("{name} requires a valid Filigran license.", { name: platformName || t_i18n("This platform") })}
            {!isAdmin && ` ${t_i18n("Contact your administrator to activate the platform.")}`}
          </p>
        </div>

        {isAdmin ? (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <KeyRound className="h-4 w-4 text-accent" />
                {t_i18n("Activate Filigran License")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="content-base text-default-secondary">
                {t_i18n("Paste your Filigran license PEM certificate. It carries your agentic capacity and activates every feature of the platform.")}
              </p>
              <PlatformIdRow instanceId={instanceId} />
              <textarea
                ref={textareaRef}
                defaultValue={pem}
                onChange={(e) => setPem(e.target.value)}
                onInput={(e) => setPem((e.target as HTMLTextAreaElement).value)}
                placeholder={`-----BEGIN CERTIFICATE-----\n${t_i18n("Paste your Filigran license PEM here...")}\n-----END CERTIFICATE-----`}
                className="w-full h-40 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus resize-none"
              />
              {error && (
                <div className="flex items-center gap-2 content-base text-feedback-error-primary">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  {error}
                </div>
              )}
              <div className="rounded-sm bg-accent/5 border border-accent/10 p-3 space-y-1.5">
                <div className="flex items-center gap-2 content-compact text-default-secondary">
                  <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" />
                  {t_i18n("License must be a")} <strong className="text-default-primary">Filigran</strong> {t_i18n("product license")}
                </div>
                <p className="content-compact text-default-secondary pt-1">
                  {t_i18n("Don't have a license?")}{" "}
                  <a href={FILIGRAN_CONTACT_URL} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {t_i18n("Contact Filigran")}
                  </a>{" "}
                  {t_i18n("or")}{" "}
                  <a href={XTM_ONE_TRIAL_URL} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {t_i18n("request a trial")}
                  </a>.
                </p>
              </div>
              <div className="content-compact text-default-secondary">
                {t_i18n("You can also set the")} <code className="text-accent">ENTERPRISE_LICENSE</code>{" "}
                {t_i18n("environment variable and restart the platform.")}
              </div>
              <Button
                type="button"
                onClick={handleActivate}
                disabled={saveLicense.isPending}
                className="w-full"
              >
                {saveLicense.isPending ? t_i18n("Validating...") : t_i18n("Activate")}
              </Button>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="py-8 content-base text-default-secondary space-y-4">
              <div className="text-center">
                <ShieldCheck className="h-10 w-10 mx-auto mb-3 text-default-disabled" />
                <p>{t_i18n("Waiting for an administrator to activate the license.")}</p>
                <p className="mt-1 content-compact">
                  {t_i18n("A Filigran license is required. Ask your admin to paste the license PEM in Settings or set the ENTERPRISE_LICENSE environment variable.")}
                </p>
              </div>
              <PlatformIdRow instanceId={instanceId} />
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
