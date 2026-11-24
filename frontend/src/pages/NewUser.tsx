// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  UserPlus, Shield, Eye, EyeOff, KeyRound, Globe,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useCreateUser, useAuthConfig } from "@/hooks/api/useAuthApi";
import { cn, validatePassword, MIN_PASSWORD_LENGTH } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export default function NewUser() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const create = useCreateUser();
  const { data: authConfig } = useAuthConfig();
  const { toast } = useToast();
  // Live ``min_password_length`` from the backend; keeps the placeholder
  // and inline validation copy in sync with the server-side policy
  // (falls back to ``MIN_PASSWORD_LENGTH`` while the auth-config query
  // is in flight on first paint).
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [accountType, setAccountType] = useState<"local" | "sso">("local");
  const [showPassword, setShowPassword] = useState(false);

  const hasSSOEnabled = authConfig?.oidc_enabled || authConfig?.saml_enabled;

  // Compute the policy result once per render and reuse it for the submit
  // gate AND the inline error message below.  Calling ``validatePassword``
  // three times per render (gate + condition + display) duplicates work
  // and — more importantly — risks the gate disagreeing with the
  // user-visible error string if the validator's branches ever drift.
  // The local-account branch is the only one that consults this; SSO
  // pre-provisioning has no password input at all.
  const pwError = accountType === "local"
    ? validatePassword(password, minPasswordLength)
    : null;

  const handleCreate = () => {
    if (!email.trim() || !fullName.trim()) return;
    if (accountType === "local" && !password) return;

    create.mutate(
      {
        email,
        full_name: fullName,
        password: accountType === "local" ? password : undefined,
        role: isAdmin ? "admin" : "user",
        account_type: accountType,
      },
      {
        onSuccess: (newUser) => {
          toast({ title: t_i18n("User created"), variant: "success" });
          navigate(`/users-and-groups/users/${newUser.id}`);
        },
      },
    );
  };

  const canSubmit =
    email.trim() &&
    fullName.trim() &&
    (accountType === "sso" || pwError === null);

  return (
    <div className="space-y-6">
      <BackButton to="/users-and-groups" label="Back to Users" />
      <div className="flex items-center gap-3">
        <UserPlus className="h-6 w-6 text-accent" />
        <h1 className="text-2xl font-bold">{t_i18n("New User")}</h1>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          {/* Account type selector */}
          <div className="space-y-1.5">
            <Label id="new-user-account-type">{t_i18n("Account Type")}</Label>
            <div role="group" aria-labelledby="new-user-account-type" className="flex gap-2">
              <button
                type="button"
                aria-pressed={accountType === "local"}
                className={cn(
                  "flex-1 flex items-center gap-2 rounded-sm border p-3 text-left transition-all",
                  accountType === "local"
                    ? "border-accent/30 bg-accent/5"
                    : "border-elevation-subtle-soft bg-elevation-hover hover:bg-elevation-highlight",
                )}
                onClick={() => setAccountType("local")}
              >
                <KeyRound className={cn("h-4 w-4", accountType === "local" ? "text-accent" : "text-default-secondary")} />
                <div>
                  <p className={cn("content-base font-medium", accountType === "local" ? "text-default-primary" : "text-default-secondary")}>{t_i18n("Local Account")}</p>
                  <p className="text-[11px] text-default-secondary">{t_i18n("Create with email & password")}</p>
                </div>
              </button>
              <button
                type="button"
                aria-pressed={accountType === "sso"}
                className={cn(
                  "flex-1 flex items-center gap-2 rounded-sm border p-3 text-left transition-all",
                  accountType === "sso"
                    ? "border-accent/30 bg-accent/5"
                    : "border-elevation-subtle-soft bg-elevation-hover hover:bg-elevation-highlight",
                  !hasSSOEnabled && "opacity-50 cursor-not-allowed",
                )}
                onClick={() => hasSSOEnabled && setAccountType("sso")}
              >
                <Globe className={cn("h-4 w-4", accountType === "sso" ? "text-accent" : "text-default-secondary")} />
                <div>
                  <p className={cn("content-base font-medium", accountType === "sso" ? "text-default-primary" : "text-default-secondary")}>{t_i18n("SSO Pre-provision")}</p>
                  <p className="text-[11px] text-default-secondary">
                    {hasSSOEnabled ? t_i18n("Pre-create account for SSO user") : t_i18n("Enable SSO in Settings first")}
                  </p>
                </div>
              </button>
            </div>
          </div>

          {/* Email */}
          <div className="space-y-1.5">
            <Label htmlFor="new-user-email">{t_i18n("Email")}</Label>
            <Input id="new-user-email" aria-label={t_i18n("Email")}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@company.com"
            />
          </div>

          {/* Full name */}
          <div className="space-y-1.5">
            <Label htmlFor="new-user-full-name">{t_i18n("Full Name")}</Label>
            <Input id="new-user-full-name" aria-label={t_i18n("Full Name")}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="John Doe"
            />
          </div>

          {/* Password (local accounts only) */}
          {accountType === "local" && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="new-user-password">{t_i18n("Password")}</Label>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 content-compact"
                  onClick={() => setShowPassword(!showPassword)}
                >
                  {showPassword ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
                  {showPassword ? t_i18n("Hide") : t_i18n("Show")}
                </Button>
              </div>
              <Input id="new-user-password" aria-label={t_i18n("Password")}
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t_i18n("At least {min} characters, with a letter and a number", { min: minPasswordLength })}
              />
              {password.length > 0 && pwError && (
                <p className="text-[11px] text-feedback-error-primary">{pwError}</p>
              )}
            </div>
          )}

          {/* SSO info banner */}
          {accountType === "sso" && (
            <div className="rounded-sm bg-accent/5 p-3">
              <p className="content-compact text-accent">
                {t_i18n("This user will be pre-provisioned without a password. When they sign in through SSO for the first time, their account will be linked automatically by email. Their role and profile will already be set.")}
              </p>
            </div>
          )}

          {/* Role */}
          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-accent" />
              <div>
                <p className="content-base font-medium">{t_i18n("Administrator")}</p>
                <p className="text-[11px] text-default-secondary">{t_i18n("Grant admin access to manage the platform")}</p>
              </div>
            </div>
            <Switch aria-label={t_i18n("Administrator")} checked={isAdmin} onCheckedChange={setIsAdmin} />
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/users-and-groups")}>{t_i18n("Cancel")}</Button>
            <Button onClick={handleCreate} disabled={!canSubmit || create.isPending}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create User")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
