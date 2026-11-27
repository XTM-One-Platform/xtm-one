// SPDX-License-Identifier: Apache-2.0

/**
 * The email row of the Profile account card, with its change flow.
 *
 * The other Filigran platforms find a person's account by this address, so a
 * new one is proven before it is written: the person gives it with their
 * current password, then types the code sent to it. When the platform would
 * refuse the change (an address the identity provider owns, the ADMIN_EMAIL
 * account, no mail server), the row is read-only and says why.
 */

import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { useConfirmEmailChange, useRequestEmailChange } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import type { EmailChangeBlocked } from "@/types";

type Step = "idle" | "address" | "code";

export function EmailRow({ email, blocked, impersonated }: {
  email: string;
  /** Why the change is refused (`email_change_blocked` on `/auth/me`), or null. */
  blocked?: EmailChangeBlocked | null;
  /** An administrator acting as this person: the change is refused, so it is not offered. */
  impersonated: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const requestChange = useRequestEmailChange();
  const confirmChange = useConfirmEmailChange();
  const [step, setStep] = useState<Step>("idle");
  const [newEmail, setNewEmail] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");

  const reset = () => {
    setStep("idle");
    setNewEmail("");
    setCurrentPassword("");
    setCode("");
    setError("");
  };

  const sendCode = () => {
    setError("");
    requestChange.mutate(
      { email: newEmail.trim(), current_password: currentPassword },
      {
        onSuccess: () => {
          setCurrentPassword("");
          setCode("");
          setStep("code");
        },
        onError: (err: Error) => setError(err.message || t_i18n("Could not send the code")),
      },
    );
  };

  const confirm = () => {
    setError("");
    confirmChange.mutate(
      { code: code.trim() },
      {
        onSuccess: (data) => {
          toast({ title: t_i18n("Email changed"), description: t_i18n("Your account now uses {email}.", { email: data.email }), variant: "success" });
          reset();
        },
        onError: (err: Error) => setError(err.message || t_i18n("This code is wrong or has expired")),
      },
    );
  };

  const canChange = !blocked && !impersonated;
  const blockedReason: Record<EmailChangeBlocked, string> = {
    identity_provider: t_i18n("Your identity provider manages your email: change it there."),
    platform_admin: t_i18n("This account's email is set by ADMIN_EMAIL: it cannot be changed here."),
    no_email: t_i18n("The platform sends no email: ask an administrator to change your email."),
  };

  return (
    <div className="px-5 py-3.5" data-testid="profile.email-row">
      <div className="flex items-center gap-3">
        <Icon name="mail" size={16} className="text-accent shrink-0" aria-hidden />
        <span className="content-base font-medium shrink-0 w-36" data-testid="profile.info-row-label">{t_i18n("Email")}</span>
        <div className="flex-1 min-w-0">
          <span className="content-base">{email}</span>
          {blocked && <p className="content-compact text-default-secondary">{blockedReason[blocked]}</p>}
        </div>
        {canChange && step === "idle" && (
          <Button variant="secondary" className="gap-1.5" data-testid="profile.email-change" onClick={() => setStep("address")}>
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Change")}
          </Button>
        )}
      </div>

      {step === "address" && (
        <div className="mt-3 space-y-3 max-w-sm pl-7">
          <p className="content-compact text-default-secondary">
            {t_i18n("The other platforms find your account by this address: a code sent to the new one proves it is yours.")}
          </p>
          <div className="space-y-1">
            <Label id="profile-new-email-label" htmlFor="profile-new-email">{t_i18n("New email")}</Label>
            <Input id="profile-new-email" aria-labelledby="profile-new-email-label" type="email" autoComplete="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} autoFocus />
          </div>
          <div className="space-y-1">
            <Label id="profile-email-password-label" htmlFor="profile-email-password">{t_i18n("Current Password")}</Label>
            <Input id="profile-email-password" aria-labelledby="profile-email-password-label" type="password" autoComplete="current-password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} placeholder={t_i18n("Enter current password")} />
          </div>
          {error && <p className="content-compact text-feedback-error-primary" role="alert">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={reset}>{t_i18n("Cancel")}</Button>
            <Button onClick={sendCode} disabled={!newEmail.trim() || !currentPassword || requestChange.isPending} aria-busy={requestChange.isPending || undefined}>
              {requestChange.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Send code")}
            </Button>
          </div>
        </div>
      )}

      {step === "code" && (
        <div className="mt-3 space-y-3 max-w-sm pl-7">
          <p className="content-compact text-default-secondary">
            {t_i18n("If {email} can be used, a 6-digit code was sent to it. It works once, within 10 minutes.", { email: newEmail.trim() })}
          </p>
          <div className="space-y-1">
            <Label id="profile-email-code-label" htmlFor="profile-email-code">{t_i18n("Code")}</Label>
            <Input id="profile-email-code" aria-labelledby="profile-email-code-label" inputMode="numeric" autoComplete="one-time-code" maxLength={32} value={code} onChange={(e) => setCode(e.target.value)} autoFocus />
          </div>
          {error && <p className="content-compact text-feedback-error-primary" role="alert">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={reset}>{t_i18n("Cancel")}</Button>
            <Button variant="secondary" onClick={() => { setError(""); setStep("address"); }}>{t_i18n("Ask for a new code")}</Button>
            <Button onClick={confirm} disabled={!code.trim() || confirmChange.isPending} aria-busy={confirmChange.isPending || undefined}>
              {confirmChange.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Confirm")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
