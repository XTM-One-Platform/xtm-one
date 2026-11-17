// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertCircle, KeyRound, Shield } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardDescription } from "@/components/ui/card";
import { useValidatePasswordReset, useResetPassword, useAuthConfig } from "@/hooks/api/useAuthApi";
import { validatePassword, MIN_PASSWORD_LENGTH } from "@/lib/utils";
import { usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
import { MfaCodeStep } from "@/components/auth/MfaCodeStep";
import { useFormatter } from "@/i18n";

/** Inline field error displayed below an input; `id` is named by the input's `aria-describedby`. */
function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="mt-1.5 flex items-center gap-1.5 content-compact text-feedback-error-primary animate-in fade-in slide-in-from-top-1 duration-200">
      <AlertCircle className="h-3 w-3 shrink-0" />
      {message}
    </p>
  );
}

export default function ResetPassword() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token");

  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();

  const { data: validation, isLoading: validating } = useValidatePasswordReset(token);
  const reset = useResetPassword();
  const { data: authConfig } = useAuthConfig();
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldErrors, setFieldErrors] = useState<{ password?: string; confirm?: string }>({});
  const [formError, setFormError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  // The new password is set, but the account has two-factor enabled and no
  // session was issued. Without this step the reset link would be a
  // complete bypass of the second factor: whoever controls the mailbox resets
  // the password and walks in — the exact threat the factor exists to stop.
  const [mfaToken, setMfaToken] = useState<string | null>(null);

  const validate = (): boolean => {
    const errors: typeof fieldErrors = {};
    const pwErr = validatePassword(password, minPasswordLength);
    if (!password) errors.password = t_i18n("Password is required");
    else if (pwErr) errors.password = pwErr;
    if (confirm !== password) errors.confirm = t_i18n("Passwords do not match");
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setFormError("");
    if (!token || !validate()) return;
    try {
      const result = await reset.mutateAsync({ token, new_password: password });
      if (result.mfa_required && result.mfa_token) {
        // Drop the plaintext password from state — the challenge stands in for
        // it now, and the reset itself already succeeded.
        setPassword("");
        setConfirm("");
        setMfaToken(result.mfa_token);
        return;
      }
      navigate("/");
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : t_i18n("Something went wrong"));
    }
  };

  const isSsoOnly = !validating && validation?.sso_only;
  const isInvalid = !token || (!validating && !isSsoOnly && (!validation || !validation.valid));

  return (
    <div className="flex min-h-screen items-center justify-center bg-elevation-default p-4">
      <div className="pointer-events-none fixed inset-0">
        <div className="absolute left-1/2 top-1/3 h-96 w-96 -translate-x-1/2 -translate-y-1/2 rounded-full bg-elevation-highlight/5 blur-3xl" />
      </div>

      <Card className="relative z-10 w-full max-w-md border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs">
        <CardHeader className="text-center">
          <div className="mx-auto mb-6">
            <img src={logoSrc} alt={platformName} className="h-10" />
          </div>
          <CardDescription>
            {mfaToken
              ? t_i18n("Two-factor authentication")
              : isSsoOnly ? t_i18n("SSO account") : isInvalid ? t_i18n("Password reset") : t_i18n("Choose a new password")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* The password is already changed; only the session is owed. Replaces
              the form entirely so there is nothing left to resubmit. */}
          {mfaToken && (
            <MfaCodeStep
              mfaToken={mfaToken}
              email={validation?.email ?? undefined}
              onVerified={() => navigate("/")}
            />
          )}

          {!mfaToken && validating && (
            <div className="flex items-center justify-center py-8">
              <span className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            </div>
          )}

          {!mfaToken && !validating && isInvalid && (
            <div className="space-y-4">
              <div className="flex items-start gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-error-primary" />
                <p className="content-base text-feedback-error-primary">
                  {t_i18n("This reset link is invalid, has expired, or has already been used.")}
                </p>
              </div>
              <Button variant="outline" className="w-full" onClick={() => navigate("/login")}>
                {t_i18n("Go to sign in")}
              </Button>
            </div>
          )}

          {!mfaToken && !validating && isSsoOnly && (
            <div className="space-y-4">
              <div className="flex items-start gap-2.5 rounded-sm bg-feedback-alert-primary/10 px-4 py-3">
                <Shield className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
                <p className="content-base text-feedback-warning-primary">
                  {t_i18n("This account is managed by your organization's single sign-on (SSO) provider. Password reset is not available: please sign in via SSO.")}
                </p>
              </div>
              <Button variant="outline" className="w-full" onClick={() => navigate("/login")}>
                {t_i18n("Go to sign in")}
              </Button>
            </div>
          )}

          {!mfaToken && !validating && !isInvalid && !isSsoOnly && (
            <form onSubmit={handleSubmit} noValidate className="space-y-4">
              <div className="flex items-center gap-2.5 rounded-sm bg-accent/5 px-4 py-3">
                <KeyRound className="h-4 w-4 shrink-0 text-accent" />
                <p className="content-base text-accent">
                  {t_i18n("Resetting password for")} <span className="font-semibold">{validation?.email}</span>
                </p>
              </div>

              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("New Password")}</label>
                <Input aria-label={t_i18n("New Password")}
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => { setPassword(e.target.value); setFieldErrors((p) => ({ ...p, password: undefined })); setFormError(""); }}
                  placeholder="••••••••"
                  aria-invalid={(submitted && Boolean(fieldErrors.password)) || undefined}
                  aria-describedby={submitted && fieldErrors.password ? "reset-password-error" : undefined}
                />
                <FieldError id="reset-password-error" message={submitted ? fieldErrors.password : undefined} />
              </div>

              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Confirm Password")}</label>
                <Input aria-label={t_i18n("Confirm Password")}
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => { setConfirm(e.target.value); setFieldErrors((p) => ({ ...p, confirm: undefined })); }}
                  placeholder="••••••••"
                  aria-invalid={(submitted && Boolean(fieldErrors.confirm)) || undefined}
                  aria-describedby={submitted && fieldErrors.confirm ? "reset-confirm-error" : undefined}
                />
                <FieldError id="reset-confirm-error" message={submitted ? fieldErrors.confirm : undefined} />
              </div>

              {formError && (
                <div className="flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3 animate-in fade-in slide-in-from-top-2 duration-300">
                  <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
                  <p className="content-base text-feedback-error-primary">{formError}</p>
                </div>
              )}

              <Button type="submit" className="w-full" disabled={reset.isPending}>
                {reset.isPending ? (
                  <span className="flex items-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    {t_i18n("Setting password...")}
                  </span>
                ) : (
                  t_i18n("Set New Password")
                )}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
