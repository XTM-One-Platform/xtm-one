// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, MailCheck, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardDescription } from "@/components/ui/card";
import { useForgotPassword, useAuthConfig } from "@/hooks/api/useAuthApi";
import { usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

export default function ForgotPassword() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();
  const forgot = useForgotPassword();
  const { data: authConfig } = useAuthConfig();
  const hasSso = authConfig?.oidc_enabled || authConfig?.saml_enabled;

  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!isValidEmail(email.trim())) {
      setError(t_i18n("Please enter a valid email address"));
      return;
    }
    try {
      await forgot.mutateAsync({ email: email.trim() });
      setSent(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t_i18n("Something went wrong"));
    }
  };

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
            {sent ? t_i18n("Check your email") : t_i18n("Reset your password")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {sent ? (
            <div className="space-y-4">
              <div className="flex items-start gap-2.5 rounded-sm bg-accent/5 px-4 py-3">
                <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                <p className="content-base text-accent">
                  {t_i18n("If an account exists for {email}, we've sent a password reset link. Check your inbox and spam folder.", { email: email.trim() })}
                </p>
              </div>
              <Button variant="outline" className="w-full gap-2" onClick={() => navigate("/login")}>
                <ArrowLeft className="h-4 w-4" /> {t_i18n("Back to sign in")}
              </Button>
            </div>
          ) : (
            <form onSubmit={handleSubmit} noValidate className="space-y-4">
              <p className="content-base text-default-secondary">
                {t_i18n("Enter your account email and we'll send you a link to reset your password.")}
              </p>
              {hasSso && (
                <p className="content-compact text-default-secondary/80">
                  {t_i18n("Note: If your account uses SSO, password reset is not available. Sign in via your identity provider instead.")}
                </p>
              )}
              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Email")}</label>
                <Input aria-label={t_i18n("Email")}
                  type="text"
                  inputMode="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => { setEmail(e.target.value); setError(""); }}
                  placeholder="user@mail.com"
                  aria-invalid={Boolean(error) || undefined}
                  aria-describedby={error ? "forgot-email-error" : undefined}
                />
                {error && (
                  <p id="forgot-email-error" className="mt-1.5 flex items-center gap-1.5 content-compact text-feedback-error-primary">
                    <AlertCircle className="h-3 w-3 shrink-0" />
                    {error}
                  </p>
                )}
              </div>

              <Button type="submit" className="w-full" disabled={forgot.isPending}>
                {forgot.isPending ? (
                  <span className="flex items-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    {t_i18n("Sending...")}
                  </span>
                ) : (
                  t_i18n("Send reset link")
                )}
              </Button>

              <button
                type="button"
                onClick={() => navigate("/login")}
                className="flex w-full items-center justify-center gap-1.5 content-base text-default-secondary hover:text-default-primary"
              >
                <ArrowLeft className="h-3.5 w-3.5" /> {t_i18n("Back to sign in")}
              </button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
