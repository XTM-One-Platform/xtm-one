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
 * Create or edit a browser profile: a web application and how
 * the platform's browser signs in to it. Credentials are write-only: the
 * dialog never receives them, a blank field keeps what is stored, and "Clear"
 * removes it. Each credential is the platform's one `SecretField`: typed,
 * or picked from a secret vault (a 1Password item's password and one-time
 * password field included), read when the browser signs in. The TOTP secret
 * (base32 or an otpauth:// URI) lets every sign-in answer the MFA by itself.
 */
import { useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SecretField } from "@/components/ui/secret-field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { useSaveBrowserProfile } from "@/hooks/api/useBrowserProfilesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import type { BrowserProfile, BrowserProfileApp, BrowserProfileWrite, VaultSecretLocation } from "@/types";

import { appsFrom, appUrlInvalid, KEEP_STORAGE_KEYS, KEEPALIVE_LABELS, loginConfigFrom, loginConfigTexts, parseHosts } from "./browser-profile-format";

const SELECTOR_KEYS = [
  "username_selector",
  "password_selector",
  "submit_selector",
  "otp_selector",
  "confirm_selector",
  "signed_in_selector",
  "signed_in_url_pattern",
  KEEP_STORAGE_KEYS,
] as const;

/** The sign-in's bounds in seconds: range and default, as `profiles.TIMEOUT_BOUNDS` holds them. */
const TIMEOUT_FIELDS = {
  sign_in_timeout_seconds: { min: 30, max: 600, fallback: 120 },
  session_save_timeout_seconds: { min: 5, max: 120, fallback: 30 },
  mfa_timeout_seconds: { min: 60, max: 900, fallback: 300 },
} as const;

type TimeoutKey = keyof typeof TIMEOUT_FIELDS;

/** A new profile's keep-alive interval in minutes (`KEEPALIVE_MINUTES_DEFAULT`): every profile renews by itself. */
const KEEPALIVE_DEFAULT_MINUTES = 240;

/** The English label of each timeout. @i18n-keys */
const TIMEOUT_LABELS = {
  sign_in_timeout_seconds: "Sign-in timeout (s, without a navigation)",
  session_save_timeout_seconds: "Session save timeout (s)",
  mfa_timeout_seconds: "MFA window (s)",
} as const satisfies Record<TimeoutKey, string>;

type CredentialKey = "username" | "password" | "totp_secret";

/** Where a stored credential reads from, when it is a vault reference. */
function credentialLocation(profile: BrowserProfile | null, key: CredentialKey): VaultSecretLocation | null {
  return profile?.vault_secrets?.locations?.credentials?.find((location) => location.entry === key) ?? null;
}

export function BrowserProfileDialog({
  open,
  onOpenChange,
  profile,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profile: BrowserProfile | null;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const save = useSaveBrowserProfile();
  const [name, setName] = useState(profile?.name ?? "");
  const [startUrl, setStartUrl] = useState(profile?.start_url ?? "");
  const [loginUrl, setLoginUrl] = useState(profile?.login_url ?? "");
  const [hosts, setHosts] = useState((profile?.allowed_hosts ?? []).join(", "));
  const [apps, setApps] = useState<BrowserProfileApp[]>(() => (profile?.apps ?? []).map((app) => ({ ...app })));
  const setApp = (index: number, change: Partial<BrowserProfileApp>) =>
    setApps((current) => current.map((app, i) => (i === index ? { ...app, ...change } : app)));
  // An application row's URL error shows once the row was left or Save was pressed, never while it is typed.
  const [appsTouched, setAppsTouched] = useState<Record<number, boolean>>({});
  const [submitted, setSubmitted] = useState(false);
  const appUrlErrors = apps.map((app) => appUrlInvalid(app.url));
  const appsInvalid = appUrlErrors.some(Boolean);
  const removeApp = (index: number) => {
    setApps((current) => current.filter((_, i) => i !== index));
    setAppsTouched({});
  };
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [cleared, setCleared] = useState<Record<string, boolean>>({});
  const [keepalive, setKeepalive] = useState(String(profile?.keepalive_minutes ?? KEEPALIVE_DEFAULT_MINUTES));
  const [companyManaged, setCompanyManaged] = useState(profile?.is_company_managed ?? false);
  const [advanced, setAdvanced] = useState(false);
  const [selectors, setSelectors] = useState<Record<string, string>>(() => loginConfigTexts(profile?.login_config));
  const [timeouts, setTimeouts] = useState<Record<TimeoutKey, string>>(() => ({
    sign_in_timeout_seconds: String(profile?.sign_in_timeout_seconds ?? TIMEOUT_FIELDS.sign_in_timeout_seconds.fallback),
    session_save_timeout_seconds: String(
      profile?.session_save_timeout_seconds ?? TIMEOUT_FIELDS.session_save_timeout_seconds.fallback,
    ),
    mfa_timeout_seconds: String(profile?.mfa_timeout_seconds ?? TIMEOUT_FIELDS.mfa_timeout_seconds.fallback),
  }));
  const isAdmin = user?.role === "admin";

  const secret = (key: CredentialKey, value: string) => (cleared[key] ? "" : value ? value : undefined);
  const clear = (key: CredentialKey, on: boolean) => setCleared((current) => ({ ...current, [key]: on }));
  const stored: Record<CredentialKey, boolean> = {
    username: Boolean(profile?.has_username),
    password: Boolean(profile?.has_password),
    totp_secret: Boolean(profile?.has_totp),
  };

  const submit = async () => {
    setSubmitted(true);
    if (appsInvalid) return;
    const body: BrowserProfileWrite = {
      name: name.trim(),
      start_url: startUrl.trim(),
      login_url: loginUrl.trim() || null,
      allowed_hosts: parseHosts(hosts),
      apps: appsFrom(apps),
      login_config: loginConfigFrom(selectors),
      keepalive_minutes: Number(keepalive),
      is_company_managed: isAdmin ? companyManaged : undefined,
    };
    for (const key of Object.keys(TIMEOUT_FIELDS) as TimeoutKey[]) {
      const seconds = Number(timeouts[key]);
      if (timeouts[key].trim() && Number.isFinite(seconds)) body[key] = Math.round(seconds);
    }
    const user_ = secret("username", username);
    const pass = secret("password", password);
    const code = secret("totp_secret", totp);
    if (user_ !== undefined) body.username = user_;
    if (pass !== undefined) body.password = pass;
    if (code !== undefined) body.totp_secret = code;
    try {
      await save.mutateAsync({ id: profile?.id, body });
      toast({ title: t_i18n("Browser profile saved"), variant: "success" });
      onOpenChange(false);
    } catch (err) {
      toast({
        title: t_i18n("The profile was not saved"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Icon name="key-round" size={16} className="text-accent" />
            {profile ? t_i18n("Edit browser profile") : t_i18n("New browser profile")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "A web application the platform's browser signs in to - for the agents you give it and for Video Studio recordings. Credentials are stored encrypted and never shown again.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="bp-profile-name">{t_i18n("Name")}</Label>
            <Input aria-label={t_i18n("Name")} id="bp-profile-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="bp-profile-start">{t_i18n("Start URL")}</Label>
            <Input aria-label={t_i18n("Start URL")}
              id="bp-profile-start"
              value={startUrl}
              placeholder="https://demo.example.com/dashboard"
              onChange={(e) => setStartUrl(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="bp-profile-login">{t_i18n("Sign-in URL (optional)")}</Label>
            <Input aria-label={t_i18n("Sign-in URL (optional)")} id="bp-profile-login" value={loginUrl} onChange={(e) => setLoginUrl(e.target.value)} />
          </div>
          <SecretField
            id="bp-profile-user"
            label={t_i18n("Username")}
            concealed={false}
            autoComplete="off"
            value={username}
            onChange={setUsername}
            hasStoredValue={stored.username}
            stored={credentialLocation(profile, "username")}
            cleared={!!cleared.username}
            onClear={() => clear("username", true)}
            onUndoClear={() => clear("username", false)}
          />
          <SecretField
            id="bp-profile-password"
            label={t_i18n("Password")}
            autoComplete="off"
            value={password}
            onChange={setPassword}
            hasStoredValue={stored.password}
            stored={credentialLocation(profile, "password")}
            cleared={!!cleared.password}
            onClear={() => clear("password", true)}
            onUndoClear={() => clear("password", false)}
          />
          <SecretField
            id="bp-profile-totp"
            label={t_i18n("TOTP secret (base32 or otpauth:// URI)")}
            autoComplete="off"
            value={totp}
            onChange={setTotp}
            hasStoredValue={stored.totp_secret}
            stored={credentialLocation(profile, "totp_secret")}
            cleared={!!cleared.totp_secret}
            onClear={() => clear("totp_secret", true)}
            onUndoClear={() => clear("totp_secret", false)}
            helperText={t_i18n(
              "Or pick it from a secret vault: a 1Password item's one-time password field, or a vault secret holding the base32 key or the otpauth:// URI. It is read each time the browser signs in.",
            )}
          />
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "With a TOTP secret the sign-in answers the MFA prompt by itself. Without one, an MFA prompt waits for the code you give here or in the chat.",
            )}
          </p>
          <VaultSecretsNotice status={profile?.vault_secrets} fieldLabels={{ credentials: t_i18n("Sign-in credentials") }} />
          <div className="space-y-1.5" role="group" aria-labelledby="bp-profile-apps-label" aria-describedby="bp-profile-apps-help">
            <Label id="bp-profile-apps-label">{t_i18n("Also signs in (optional)")}</Label>
            <p id="bp-profile-apps-help" className="content-compact text-default-secondary">
              {t_i18n(
                "Other applications this profile's session opens signed in, for example through the same single sign-on. Agents find this profile by their URL and record them with it.",
              )}
            </p>
            {apps.map((app, index) => {
              const shownError = appUrlErrors[index] && (submitted || appsTouched[index]);
              const errorId = `bp-profile-app-${index}-error`;
              return (
                <div key={index} className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Input
                      className="min-w-0 flex-1"
                      aria-label={t_i18n("Application name")}
                      value={app.name}
                      placeholder={t_i18n("Application name")}
                      onChange={(e) => setApp(index, { name: e.target.value })}
                    />
                    <Input
                      className="min-w-0 flex-1"
                      aria-label={t_i18n("Application URL")}
                      value={app.url}
                      placeholder="https://app.example.com/"
                      aria-invalid={shownError || undefined}
                      aria-describedby={shownError ? errorId : undefined}
                      onBlur={() => setAppsTouched((current) => ({ ...current, [index]: true }))}
                      onChange={(e) => setApp(index, { url: e.target.value })}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="shrink-0"
                      aria-label={t_i18n("Remove application")}
                      onClick={() => removeApp(index)}
                    >
                      <Icon name="x" size={16} />
                    </Button>
                  </div>
                  {shownError && (
                    <p id={errorId} className="content-compact text-feedback-error-primary">
                      {t_i18n("Enter an http or https address with a host, for example https://app.example.com/.")}
                    </p>
                  )}
                </div>
              );
            })}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setApps((current) => [...current, { name: "", url: "" }])}
            >
              <Icon name="plus" size={16} />
              {t_i18n("Add application")}
            </Button>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="bp-profile-hosts">{t_i18n("Private-network hosts (optional)")}</Label>
            <Input aria-label={t_i18n("Private-network hosts (optional)")}
              id="bp-profile-hosts"
              value={hosts}
              placeholder="opencti.internal, sso.internal"
              onChange={(e) => setHosts(e.target.value)}
            />
            <p className="content-compact text-default-secondary">
              {t_i18n("Reached only when an administrator allows browser profiles on private networks.")}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label>{t_i18n("Keep the session alive")}</Label>
            <Select value={keepalive} onValueChange={setKeepalive}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(KEEPALIVE_LABELS).map(([minutes, label]) => (
                  <SelectItem key={minutes} value={minutes}>
                    {t_i18n(label)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm border border-elevation-subtle p-3">
              <div>
                <p className="content-base font-medium">{t_i18n("Company-wide")}</p>
                <p className="content-compact text-default-secondary">
                  {t_i18n("Everyone can use this profile - their agents and recordings - without seeing its credentials.")}
                </p>
              </div>
              <Switch aria-label={t_i18n("Company-wide")} checked={companyManaged} onCheckedChange={setCompanyManaged} />
            </div>
          )}
          <button
            type="button"
            className="flex items-center gap-1 content-base text-default-secondary hover:text-default-primary"
            onClick={() => setAdvanced((v) => !v)}
          >
            {advanced ? <Icon name="chevron-down" size={16} /> : <Icon name="chevron-right" size={16} />}
            {t_i18n("Sign-in selectors and timeouts (unusual sign-ins only)")}
          </button>
          {advanced && (
            <div className="space-y-3">
              <p className="content-compact text-default-secondary">
                {t_i18n(
                  "signed_in_selector names an element only the signed-in application shows: the surest session check. confirm_selector is a button the sign-in clicks when it shows, such as the application's single sign-on button.",
                )}
              </p>
              <p className="content-compact text-default-secondary">
                {t_i18n(
                  "keep_storage_keys lists, separated by commas, the localStorage and sessionStorage keys the application keeps its sign-in in: a recording clears the others before it starts.",
                )}
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                {SELECTOR_KEYS.map((key) => (
                  <div key={key} className="space-y-1">
                    <Label htmlFor={`bp-sel-${key}`} className="font-mono-plex text-default-secondary">
                      {key}
                    </Label>
                    <Input aria-label={key}
                      id={`bp-sel-${key}`}
                      className="font-mono-plex content-compact"
                      value={selectors[key] ?? ""}
                      onChange={(e) => setSelectors((s) => ({ ...s, [key]: e.target.value }))}
                    />
                  </div>
                ))}
              </div>
              <div className="grid items-end gap-3 sm:grid-cols-3">
                {(Object.keys(TIMEOUT_FIELDS) as TimeoutKey[]).map((key) => (
                  <div key={key} className="space-y-1">
                    <Label htmlFor={`bp-timeout-${key}`} className="text-default-secondary">
                      {t_i18n(TIMEOUT_LABELS[key])}
                    </Label>
                    <Input
                      aria-label={t_i18n(TIMEOUT_LABELS[key])}
                      id={`bp-timeout-${key}`}
                      type="number"
                      inputMode="numeric"
                      min={TIMEOUT_FIELDS[key].min}
                      max={TIMEOUT_FIELDS[key].max}
                      value={timeouts[key]}
                      onChange={(e) => setTimeouts((t) => ({ ...t, [key]: e.target.value }))}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              {t_i18n("Cancel")}
            </Button>
            <Button onClick={submit} disabled={!name.trim() || !startUrl.trim() || (submitted && appsInvalid) || save.isPending}>
              {t_i18n("Save")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
