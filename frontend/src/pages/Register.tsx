// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Shield, AlertCircle, UserCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardDescription } from "@/components/ui/card";
import { useValidateInvitation, useRegisterWithInvitation } from "@/hooks/api/useInvitationsApi";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { validatePassword, MIN_PASSWORD_LENGTH } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import { usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
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

export default function Register() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token");

  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();

  const { data: validation, isLoading: validating } = useValidateInvitation(token);
  const register = useRegisterWithInvitation();
  const { data: authConfig } = useAuthConfig();
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [fieldErrors, setFieldErrors] = useState<{ password?: string; fullName?: string }>({});
  const [formError, setFormError] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const validate = (): boolean => {
    const errors: typeof fieldErrors = {};
    if (!password) errors.password = t_i18n("Password is required");
    else {
      const pwErr = validatePassword(password, minPasswordLength);
      if (pwErr) errors.password = pwErr;
    }
    if (!fullName.trim()) errors.fullName = t_i18n("Full name is required");
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const clearFieldError = (key: keyof typeof fieldErrors) => {
    if (fieldErrors[key]) setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
    if (formError) setFormError("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setFormError("");
    if (!token || !validate()) return;
    try {
      const registered = await register.mutateAsync({ token, password, full_name: fullName });
      // A registered platform's invitation continues on that platform.
      const next = safeHttpUrl(registered.redirect_url);
      if (next) window.location.assign(next);
      else navigate("/");
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : t_i18n("Something went wrong");
      setFormError(message);
    }
  };

  const isInvalid = !token || (!validating && (!validation || !validation.valid));
  const role = validation?.role;

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
            {isInvalid ? t_i18n("Invitation") : t_i18n("Complete your registration")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {validating && (
            <div className="flex items-center justify-center py-8">
              <span className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            </div>
          )}

          {!validating && isInvalid && (
            <div className="space-y-4">
              <div className="flex items-start gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-error-primary" />
                <p className="content-base text-feedback-error-primary">
                  {t_i18n("This invitation link is invalid, has expired, or has already been used.")}
                </p>
              </div>
              <Button variant="outline" className="w-full" onClick={() => navigate("/login")}>
                {t_i18n("Go to sign in")}
              </Button>
            </div>
          )}

          {!validating && !isInvalid && (
            <form onSubmit={handleSubmit} noValidate className="space-y-4">
              {/* Role badge (read-only — set by the inviting admin) */}
              <div className="flex items-center gap-2.5 rounded-sm bg-accent/5 px-4 py-3">
                {role === "admin" ? (
                  <Shield className="h-4 w-4 shrink-0 text-accent" />
                ) : (
                  <UserCircle className="h-4 w-4 shrink-0 text-accent" />
                )}
                <p className="content-base text-accent">
                  {t_i18n("You'll be registered as")}{" "}
                  <span className="font-semibold">
                    {role === "admin" ? t_i18n("an Administrator") : t_i18n("a User")}
                  </span>
                </p>
              </div>

              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Full Name")}</label>
                <Input aria-label={t_i18n("Full Name")}
                  value={fullName}
                  onChange={(e) => { setFullName(e.target.value); clearFieldError("fullName"); }}
                  placeholder={t_i18n("Your full name")}
                  aria-invalid={(submitted && Boolean(fieldErrors.fullName)) || undefined}
                  aria-describedby={submitted && fieldErrors.fullName ? "register-full-name-error" : undefined}
                />
                <FieldError id="register-full-name-error" message={submitted ? fieldErrors.fullName : undefined} />
              </div>

              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Email")}</label>
                <Input aria-label={t_i18n("Email")}
                  type="text"
                  value={validation?.email ?? ""}
                  readOnly
                  disabled
                  className="opacity-70"
                />
                <p className="text-[11px] text-default-secondary">{t_i18n("This invitation is bound to this email and can't be changed.")}</p>
              </div>

              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Password")}</label>
                <Input aria-label={t_i18n("Password")}
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => { setPassword(e.target.value); clearFieldError("password"); }}
                  placeholder="••••••••"
                  aria-invalid={(submitted && Boolean(fieldErrors.password)) || undefined}
                  aria-describedby={submitted && fieldErrors.password ? "register-password-error" : undefined}
                />
                <FieldError id="register-password-error" message={submitted ? fieldErrors.password : undefined} />
              </div>

              {formError && (
                <div className="flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3 animate-in fade-in slide-in-from-top-2 duration-300">
                  <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
                  <p className="content-base text-feedback-error-primary">{formError}</p>
                </div>
              )}

              <Button type="submit" className="w-full" disabled={register.isPending}>
                {register.isPending ? (
                  <span className="flex items-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    {t_i18n("Creating account...")}
                  </span>
                ) : (
                  t_i18n("Create Account")
                )}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
