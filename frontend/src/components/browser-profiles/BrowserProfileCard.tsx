/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * One browser profile: its application, its saved session as
 * the last probe read it, the sign-in and a pending MFA prompt answered in
 * place. Credentials are never shown: only whether each one is set.
 */
import { useState } from "react";
import { AlertTriangle, FlaskConical, Globe, KeyRound, LogIn, LogOut, Pencil, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { Input } from "@/components/ui/input";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SharedIcon } from "@/components/ui/shared-badge";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import {
  useCheckBrowserProfileSession,
  useMfaScreenshot,
  useSignInBrowserProfile,
  useSignOutBrowserProfile,
  useSubmitMfaCode,
  useTestBrowserProfileSignIn,
} from "@/hooks/api/useBrowserProfilesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn, isEntityShared, timeAgo } from "@/lib/utils";
import type { BrowserProfile, BrowserSignInState } from "@/types";

import { SESSION_STATUS_LABELS, SESSION_VARIANTS } from "./browser-profile-format";

/** How a credential read from a vault is named, read by `t_i18n(CREDENTIAL_LABELS[key])`. @i18n-keys */
const CREDENTIAL_LABELS = {
  username: "Username",
  password: "Password",
  totp_secret: "TOTP secret",
} as const;

function errorText(err: unknown): string | undefined {
  return err instanceof Error && err.message ? err.message : undefined;
}

function MfaPrompt({ profile }: { profile: BrowserProfile }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const submit = useSubmitMfaCode();
  const [code, setCode] = useState("");
  const challenge = profile.mfa_challenge;
  const screenshot = useMfaScreenshot(profile);
  if (!challenge) return null;
  const send = async () => {
    try {
      const state = await submit.mutateAsync({ challengeId: challenge.challenge_id, code: code.trim() });
      setCode("");
      toast({
        title:
          state.status === "active"
            ? t_i18n("Signed in")
            : state.status === "unverified"
              ? t_i18n("Signed in, not checked")
              : t_i18n("Code sent"),
        description: state.message,
        variant: state.status === "failed" ? "error" : state.status === "unverified" ? "info" : "success",
      });
    } catch (err) {
      toast({ title: t_i18n("The code was not accepted"), description: errorText(err), variant: "error" });
    }
  };
  return (
    <div className="space-y-2 rounded-sm border border-accent/30 bg-accent/5 p-3">
      <p className="flex items-center gap-1.5 content-compact font-medium text-accent">
        <ShieldCheck className="h-3.5 w-3.5" />
        {challenge.kind === "code"
          ? t_i18n("The environment asks for a verification code")
          : t_i18n("Approve the sign-in on your device")}
      </p>
      {screenshot && <img src={screenshot} alt={t_i18n("The sign-in page")} className="w-full rounded-sm border border-elevation-subtle" />}
      <p className="content-compact text-default-secondary">
        {t_i18n(
          "The sign-in runs in the platform's browser: signing in to the application in a tab of your own does not sign this profile in.",
        )}
      </p>
      {challenge.kind === "code" ? (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          <Input
            value={code}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder={t_i18n("Code")}
            onChange={(event) => setCode(event.target.value)}
            aria-label={t_i18n("Verification code")}
          />
          <Button type="submit" disabled={!code.trim() || submit.isPending}>
            {t_i18n("Send")}
          </Button>
        </form>
      ) : (
        <p className="content-compact text-default-secondary">
          {challenge.kind === "push_number"
            ? t_i18n("Enter the number shown above in your authenticator app: the sign-in continues by itself once you approve.")
            : t_i18n("The browser continues by itself once you approve.")}
        </p>
      )}
    </div>
  );
}

export function BrowserProfileCard({
  profile,
  onEdit,
  onDelete,
}: {
  profile: BrowserProfile;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const signIn = useSignInBrowserProfile();
  const signOut = useSignOutBrowserProfile();
  const check = useCheckBrowserProfileSession();
  const testSignIn = useTestBrowserProfileSignIn();
  const [dryRun, setDryRun] = useState<BrowserSignInState | null>(null);
  const busy = profile.session_status === "signing_in" || signIn.isPending;
  const probe = profile.last_probe_result;
  const runSignIn = async () => {
    try {
      const state: BrowserSignInState = await signIn.mutateAsync(profile.id);
      if (state.status === "active") toast({ title: t_i18n("Signed in"), description: state.message, variant: "success" });
      else if (state.status === "unverified")
        toast({ title: t_i18n("Signed in, not checked"), description: state.message, variant: "info" });
      else if (state.status === "failed") toast({ title: t_i18n("Sign-in failed"), description: state.message, variant: "error" });
    } catch (err) {
      toast({ title: t_i18n("Sign-in failed"), description: errorText(err), variant: "error" });
    }
  };
  const runCheck = async () => {
    try {
      const checked = await check.mutateAsync(profile.id);
      const result = checked.last_probe_result;
      if (result?.http_status != null) {
        toast({ title: t_i18n("The session could not be checked"), description: result.reason, variant: "info" });
        return;
      }
      toast({
        title: result?.authenticated ? t_i18n("Signed in") : t_i18n("Not signed in"),
        description: result ? `${result.url} - ${result.reason}` : undefined,
        variant: result?.authenticated ? "success" : "error",
      });
    } catch (err) {
      toast({ title: t_i18n("The session was not checked"), description: errorText(err), variant: "error" });
    }
  };
  const runTestSignIn = async () => {
    setDryRun(null);
    try {
      const state = await testSignIn.mutateAsync(profile.id);
      setDryRun(state);
      toast({
        title:
          state.status === "active"
            ? t_i18n("Dry run succeeded")
            : state.status === "unverified"
              ? t_i18n("Dry run: signed in, not checked")
              : t_i18n("Dry run did not finish"),
        description: state.message,
        variant: state.status === "active" ? "success" : state.status === "unverified" ? "info" : "error",
      });
    } catch (err) {
      toast({ title: t_i18n("The sign-in test failed"), description: errorText(err), variant: "error" });
    }
  };
  const menu: OverflowMenuItem[] = [];
  if (profile.can_manage) {
    menu.push({ id: "edit", label: t_i18n("Edit"), icon: Pencil, onSelect: onEdit });
    if (profile.session_status !== "none") {
      menu.push({ id: "sign-out", label: t_i18n("Forget the session"), icon: LogOut, onSelect: () => signOut.mutate(profile.id) });
    }
    menu.push({ id: "delete", label: t_i18n("Delete"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: onDelete });
  }
  return (
    <Card data-testid="browser-profiles.card">
      <CardContent className="space-y-3 p-4">
        <div className="flex items-start gap-2">
          <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0 flex-1">
            <p className="truncate content-base font-semibold">{profile.name}</p>
            <p className="flex items-center gap-1 truncate content-compact text-default-secondary">
              <Globe className="h-3 w-3 shrink-0" />
              {profile.start_url}
            </p>
          </div>
          {isEntityShared(profile, user?.id) && <SharedIcon />}
          {profile.is_company_managed && <CompanyManagedIcon />}
          {menu.length > 0 && <OverflowMenu items={menu} label={t_i18n("Profile actions")} />}
        </div>
        {profile.description && <p className="line-clamp-2 content-compact text-default-secondary">{profile.description}</p>}
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={SESSION_VARIANTS[profile.session_status]}>{t_i18n(SESSION_STATUS_LABELS[profile.session_status])}</Badge>
          {profile.username_hint && <Badge variant="outline">{profile.username_hint}</Badge>}
          {profile.has_password && <Badge variant="outline">{t_i18n("Password")}</Badge>}
          {(profile.totp_secret_status === "valid" || profile.totp_secret_status === "vault") && (
            <Badge variant="outline">{t_i18n("TOTP")}</Badge>
          )}
          {profile.vault_credentials.length > 0 && (
            <Tip content={t_i18n("Read from a secret vault each time the browser signs in: {credentials}.", { credentials: profile.vault_credentials.map((key) => t_i18n(CREDENTIAL_LABELS[key])).join(", ") })}>
              <Badge variant="outline" className="gap-1">
                <KeyRound className="h-3 w-3 text-accent" aria-hidden />
                {t_i18n("From a vault")}
              </Badge>
            </Tip>
          )}
          {profile.totp_secret_status === "invalid" && (
            <Tip content={t_i18n("The TOTP secret is not a base32 key or an otpauth:// URI; MFA codes cannot be computed.")}>
              <Badge variant="destructive" className="gap-1">
                <AlertTriangle className="h-3 w-3" />
                {t_i18n("TOTP invalid")}
              </Badge>
            </Tip>
          )}
          {profile.renews_by_itself && (
            <Tip content={t_i18n("The platform renews this session by itself when it expires (credentials that sign in without a person, credentials and a valid TOTP secret, or a saved identity-provider session).")}>
              <Badge variant="outline">{t_i18n("Renews by itself")}</Badge>
            </Tip>
          )}
          {profile.allowed_hosts.length > 0 && (
            <Tip content={profile.allowed_hosts.join(", ")}>
              <Badge variant="outline">{t_i18n("{count} private hosts", { count: profile.allowed_hosts.length })}</Badge>
            </Tip>
          )}
        </div>
        {profile.apps.length > 0 && (
          <ul aria-label={t_i18n("Also signs in")} className="space-y-0.5 content-compact text-default-secondary">
            {profile.apps.map((app) => (
              <li key={app.url} className="break-all">
                {t_i18n("Also signs in: {name} - {url}", { name: app.name, url: app.url })}
              </li>
            ))}
          </ul>
        )}
        <VaultSecretsNotice status={profile.vault_secrets} fieldLabels={{ credentials: t_i18n("Sign-in credentials") }} />
        {profile.last_sign_in_message && (
          <p className={cn("content-compact", profile.session_status === "failed" ? "text-feedback-error-primary" : "text-default-secondary")}>
            {profile.last_sign_in_message}
          </p>
        )}
        {probe && profile.last_probe_at && (
          <p className="break-all content-compact text-default-secondary">
            {probe.authenticated
              ? t_i18n("Checked {when}: the start URL opens the application.", { when: timeAgo(profile.last_probe_at) })
              : probe.http_status != null
                ? t_i18n("Checked {when}: the application did not answer ({reason})", { when: timeAgo(profile.last_probe_at), reason: probe.reason })
                : t_i18n("Checked {when}: the start URL lands on {url}", { when: timeAgo(profile.last_probe_at), url: probe.url || "-" })}
          </p>
        )}
        {profile.session_saved_at && (
          <p className="content-compact text-default-secondary">{t_i18n("Session saved {when}", { when: timeAgo(profile.session_saved_at) })}</p>
        )}
        <MfaPrompt profile={profile} />
        <div className="flex flex-wrap gap-2">
          {(profile.has_username || profile.has_password || profile.login_url) && (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => void runSignIn()}>
              <LogIn className="h-3.5 w-3.5" />
              {busy ? t_i18n("Signing in...") : profile.session_status === "active" ? t_i18n("Sign in again") : t_i18n("Sign in")}
            </Button>
          )}
          {profile.session_saved_at && (
            <Button variant="ghost" size="sm" className="gap-1.5" disabled={busy || check.isPending} onClick={() => void runCheck()}>
              <RefreshCw className={cn("h-3.5 w-3.5", check.isPending && "animate-spin")} />
              {check.isPending ? t_i18n("Checking...") : t_i18n("Check the session")}
            </Button>
          )}
          {profile.can_manage && (profile.has_username || profile.has_password || profile.login_url) && (
            <Tip content={t_i18n("Run the whole sign-in once without saving anything, to see where it stalls.")}>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5"
                disabled={busy || testSignIn.isPending}
                onClick={() => void runTestSignIn()}
              >
                <FlaskConical className={cn("h-3.5 w-3.5", testSignIn.isPending && "animate-pulse")} />
                {testSignIn.isPending ? t_i18n("Testing...") : t_i18n("Test sign-in")}
              </Button>
            </Tip>
          )}
        </div>
        {dryRun && (
          <div
            className={cn(
              "space-y-1 rounded-sm border p-3 content-compact",
              dryRun.status === "active"
                ? "border-feedback-success-primary/30"
                : dryRun.status === "unverified"
                  ? "border-elevation-subtle"
                  : "border-feedback-error-primary/30",
            )}
          >
            <p className="font-medium">
              {dryRun.status === "active"
                ? t_i18n("Dry run: signed in")
                : dryRun.status === "unverified"
                  ? t_i18n("Dry run: signed in, not checked")
                  : t_i18n("Dry run: did not finish")}
            </p>
            <p className="text-default-secondary">{dryRun.message}</p>
            {dryRun.stage && (
              <p className="text-default-secondary">
                {t_i18n("Stage: {stage}", { stage: dryRun.stage })}
                {dryRun.last_url ? ` - ${dryRun.last_url}` : ""}
              </p>
            )}
            {dryRun.trace && dryRun.trace.length > 0 && (
              <p className="break-all text-default-secondary">
                {t_i18n("Steps")}: {dryRun.trace.map((step) => step.decision).join(" -> ")}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
