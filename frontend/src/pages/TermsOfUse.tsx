// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { ScrollText, ExternalLink, AlertTriangle, LogOut, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/hooks/useAuth";
import { usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
import { useAcceptTerms, useDeclineTerms } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import type { TermsOfUseState } from "@/types";

/**
 * Blocking Terms of Use consent screen.
 *
 * Terms of Use are accepted ONCE for the whole platform (by an admin, on behalf
 * of the organisation), not per-user. `TermsGate` renders this in place of the
 * entire authenticated app — no sidebar, no layout, no route content — until the
 * platform has accepted the currently published version. The server enforces the
 * same gate on every API call (`app.api.deps.enforce_terms_of_use`), so this is
 * the presentation half of the contract, not the security boundary.
 *
 * Only admins can accept (`terms.can_accept`); every other user sees a
 * "waiting for an administrator" state and can only sign out. This is why a
 * regular user landing here is never shown an accept button — they cannot bind
 * the whole organisation.
 *
 * The document itself is NOT embedded. https://filigran.io/ai-terms/ serves a
 * PDF from S3 behind a 301 — an iframe of that is not a dependable render
 * target, and the text is not ours to inline. The admin opens it in a new tab
 * and confirms here. The checkbox is deliberate friction: it forces a distinct,
 * affirmative act before the accept button becomes usable.
 *
 * Visual style follows `LicenseRequired` — the platform's existing full-screen
 * lockdown surface — so the gate reads as native rather than bolted on.
 */
export default function TermsOfUse({ terms }: { terms: TermsOfUseState }) {
  const { t_i18n } = useFormatter();
  const { logout, isAdmin } = useAuth();
  const platformName = usePlatformName();
  const logoUrl = usePlatformLogoUrl();

  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const acceptTerms = useAcceptTerms();
  const declineTerms = useDeclineTerms();

  /* `can_accept` mirrors the admin check server-side. During a rolling deploy
   * (or with a cached `["me"]` payload) `/auth/me` may come from a backend
   * build that predates the field — fall back to the authenticated role so an
   * admin is never locked out of accepting; the server enforces admin-only on
   * POST /auth/terms/accept regardless of what this renders. */
  const canAccept = terms.can_accept ?? isAdmin;

  const handleAccept = () => {
    if (!confirmed) return;
    setError(null);
    acceptTerms.mutate(
      { version: terms.version },
      {
        onError: (err: Error) =>
          setError(err.message || t_i18n("Failed to record your acceptance")),
      },
    );
  };

  /* Declining is audit-only server-side, so sign out regardless of whether the
   * call succeeds — a network failure must not trap the user on a screen whose
   * only other action is one they just refused. */
  const handleDecline = () => {
    declineTerms.mutate(undefined, {
      onSettled: () => logout(),
    });
  };

  /* Non-admins never see an accept option, so their exit is a plain sign-out —
   * NOT a decline: recording "Declined Terms of Use" in the audit trail for a
   * user who was never offered the choice would pollute the compliance record. */
  const handleSignOut = () => {
    logout();
  };

  const busy = acceptTerms.isPending || declineTerms.isPending;
  /* The platform previously accepted an older version — the document changed, so
   * say so instead of showing first-acceptance copy. */
  const isRevision = Boolean(
    terms.accepted_version && terms.accepted_version !== terms.version,
  );
  const platformLabel = platformName || t_i18n("this platform");

  return (
    <div className="min-h-screen bg-elevation-default flex items-center justify-center p-6">
      <div className="w-full max-w-lg space-y-8 animate-fade-in">
        <div className="text-center space-y-4">
          <img src={logoUrl} alt={platformName || "Platform"} className="h-10 mx-auto" />
          <div className="flex items-center justify-center gap-2 text-accent">
            <ScrollText className="h-6 w-6" />
            <h1 className="text-xl font-bold font-sans-geologica">
              {isRevision
                ? t_i18n("Updated Terms of Use")
                : t_i18n("Terms of Use")}
            </h1>
          </div>
          {canAccept && (
            <p className="content-base text-default-secondary max-w-sm mx-auto">
              {isRevision
                ? t_i18n("The Terms of Use have been updated. Review and accept the new version to continue using {name}.", { name: platformLabel })
                : t_i18n("Before your organization starts using {name}, please review and accept the Terms of Use on its behalf.", { name: platformLabel })}
            </p>
          )}
        </div>

        <Card className="border-accent/20">
          <CardContent className="py-6 space-y-5">
            <div className="flex items-center justify-between">
              <span className="content-base text-default-secondary">{t_i18n("Version")}</span>
              <Badge variant="outline" className="content-caption text-accent border-accent/30">
                {terms.version}
              </Badge>
            </div>

            <Button
              variant="outline"
              className="w-full gap-2"
              onClick={() =>
                window.open(terms.url, "_blank", "noopener,noreferrer")
              }
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t_i18n("Read the Terms of Use")}
            </Button>

            {canAccept ? (
              <>
                <div className="flex items-start gap-3 rounded-sm bg-accent/[0.04] border border-accent/10 p-3">
                  <Switch
                    id="tou-confirmed"
                    checked={confirmed}
                    onCheckedChange={setConfirmed}
                    disabled={busy}
                    className="mt-0.5 shrink-0"
                  />
                  <label
                    htmlFor="tou-confirmed"
                    className="cursor-pointer content-base leading-relaxed"
                  >
                    {t_i18n("I have read and accept the Terms of Use on behalf of my organization.")}
                  </label>
                </div>

                {error && (
                  <div className="flex items-start gap-2 rounded-sm bg-feedback-error-primary/[0.06] border border-feedback-error-primary/20 p-3">
                    <AlertTriangle className="h-4 w-4 text-feedback-error-primary mt-0.5 shrink-0" />
                    <p className="content-compact text-feedback-error-primary">{error}</p>
                  </div>
                )}

                <div className="flex gap-2">
                  <Button
                    className="flex-1 gap-1.5"
                    onClick={handleAccept}
                    disabled={!confirmed || busy}
                  >
                    <Check className="h-3.5 w-3.5" />
                    {acceptTerms.isPending
                      ? t_i18n("Recording…")
                      : t_i18n("Accept and continue")}
                  </Button>
                  <Button
                    variant="outline"
                    className="gap-1.5"
                    onClick={handleDecline}
                    disabled={busy}
                  >
                    <LogOut className="h-3.5 w-3.5" />
                    {t_i18n("Decline and sign out")}
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="flex items-start gap-2 rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-3">
                  <AlertTriangle className="h-4 w-4 text-feedback-warning-primary mt-0.5 shrink-0" />
                  <p className="content-base leading-relaxed text-default-secondary">
                    {t_i18n("An administrator must review and accept the Terms of Use on behalf of {name} before anyone can use the platform. Please contact your administrator, then sign in again.", { name: platformLabel })}
                  </p>
                </div>
                <Button
                  variant="outline"
                  className="w-full gap-1.5"
                  onClick={handleSignOut}
                  disabled={busy}
                >
                  <LogOut className="h-3.5 w-3.5" />
                  {t_i18n("Sign out")}
                </Button>
              </>
            )}
          </CardContent>
        </Card>

        <p className="text-center content-compact text-default-secondary">
          {t_i18n("Acceptance is recorded once for the whole platform, with the date and the accepting administrator, in the audit log.")}
        </p>
      </div>
    </div>
  );
}
