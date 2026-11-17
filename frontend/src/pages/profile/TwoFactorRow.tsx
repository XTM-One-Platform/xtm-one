// SPDX-License-Identifier: Apache-2.0

/**
 * TOTP two-factor enrollment and management.
 *
 * Rendered as a row inside the Profile → Overview account card, next to the
 * password row it complements. Lives in its own file rather than inline in
 * `Profile.tsx` because enrollment is a small state machine — idle → enrolling
 * → showing recovery codes → idle — and folding four screens' worth of that
 * into an already 1800-line page would bury it.
 *
 * The one rule the whole component is built around: **the recovery codes are
 * readable exactly once**, in the response to `enable` (or a regeneration).
 * Every interaction that would drop them from view therefore requires an
 * explicit "I saved these" acknowledgement — there is no way to fetch them
 * again, and a user who clicks past this screen has a working second factor
 * with no way back in if they lose the device.
 */

import { useState } from "react";
import { ShieldCheck, Smartphone, Copy, Check, AlertTriangle, KeyRound, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import {
  useSetupMfa,
  useEnableMfa,
  useDisableMfa,
  useRegenerateRecoveryCodes,
} from "@/hooks/api/useAuthApi";
import type { MfaSetup, MfaState } from "@/types";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { downloadRecoveryCodesPdf } from "@/lib/recovery-codes-pdf";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

/** Below this many unused codes, nudge the user to regenerate. */
const LOW_RECOVERY_CODES = 3;

function RecoveryCodeList({
  codes,
  email,
  onAcknowledge,
}: {
  codes: string[];
  email: string;
  onAcknowledge: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const platformName = usePlatformName();
  const [copied, setCopied] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);

  // Built in the browser, not fetched: the server keeps only hashes and returns
  // the plaintext once, so by the time a download request could reach it the
  // codes are gone. See lib/recovery-codes-pdf.ts.
  const downloadPdf = () => {
    downloadRecoveryCodesPdf(codes, email, {
      title: t_i18n("Two-factor recovery codes"),
      account: t_i18n("Account"),
      generated: t_i18n("Generated"),
      warning: t_i18n("Each code signs you in once if you lose your authenticator device. Keep this sheet somewhere safe."),
      footer: t_i18n("These codes cannot be displayed again. Generate a new set to replace them."),
    }, platformName);
  };

  const copyAll = async () => {
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
    } catch {
      // Clipboard access can be denied (insecure origin, browser policy). The
      // codes are on screen either way, so this is a nudge, not a failure.
      toast({ title: t_i18n("Could not copy: select and copy the codes manually"), variant: "error" });
    }
  };

  return (
    <div className="mt-3 space-y-3 pl-7">
      <div className="flex items-start gap-2.5 rounded-sm bg-feedback-alert-primary/10 px-3 py-2.5">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
        <p className="content-compact text-feedback-warning-primary">
          {t_i18n("Save these recovery codes somewhere safe. Each one signs you in once if you lose your device. This is the only time they can be displayed.")}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 font-mono-plex content-compact">
        {codes.map((code) => (
          <span key={code} className="select-all tracking-wider">{code}</span>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" className="h-7 content-compact gap-1.5" onClick={copyAll}>
          {copied ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />}
          {copied ? t_i18n("Copied") : t_i18n("Copy all")}
        </Button>
        <Button variant="ghost" size="sm" className="h-7 content-compact gap-1.5" onClick={downloadPdf}>
          <Download className="h-3 w-3" />
          {t_i18n("Download PDF")}
        </Button>
        <div className="flex items-center gap-2">
          <Switch
            id="mfa-codes-acknowledged"
            checked={acknowledged}
            onCheckedChange={setAcknowledged}
          />
          <label
            htmlFor="mfa-codes-acknowledged"
            className="cursor-pointer content-compact text-default-secondary"
          >
            {t_i18n("I have saved these codes")}
          </label>
        </div>
        <Button size="sm" className="h-7 content-compact" disabled={!acknowledged} onClick={onAcknowledge}>
          {t_i18n("Done")}
        </Button>
      </div>
    </div>
  );
}

function EnrollmentForm({
  setup,
  onCancel,
  onEnabled,
}: {
  setup: MfaSetup;
  onCancel: () => void;
  onEnabled: (codes: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const enable = useEnableMfa();
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [showSecret, setShowSecret] = useState(false);

  const submit = () => {
    setError("");
    enable.mutate(
      { code: code.trim() },
      {
        onSuccess: (data) => onEnabled(data.recovery_codes),
        onError: (err: Error) => setError(err.message || t_i18n("Invalid verification code. Please try again.")),
      },
    );
  };

  return (
    <div className="mt-3 space-y-4 pl-7">
      <p className="max-w-lg content-compact text-default-secondary">
        {t_i18n("Scan this QR code with Google Authenticator, Microsoft Authenticator, KeePassXC, 1Password, Bitwarden, or any other authenticator app, then enter the 6-digit code it shows.")}
      </p>

      <div className="flex flex-wrap items-start gap-5">
        {/* Rendered server-side as an SVG data URL, so this works offline and
            on air-gapped deployments — no third-party QR service is contacted. */}
        <img
          src={setup.qr_code}
          alt={t_i18n("QR code for two-factor enrollment")}
          className="h-40 w-40 rounded-sm bg-elevation-default p-2"
        />

        <div className="space-y-2">
          <p className="content-caption uppercase tracking-wider text-default-secondary font-medium">
            {t_i18n("Can't scan?")}
          </p>
          {showSecret ? (
            <div className="space-y-1.5">
              <code className="block select-all rounded-sm border border-elevation-subtle-soft bg-elevation-default px-2 py-1.5 font-mono-plex content-compact tracking-wider">
                {setup.secret}
              </code>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("{algorithm}, {digits} digits, {period}s", {
                  algorithm: setup.algorithm,
                  digits: setup.digits,
                  period: setup.period,
                })}
              </p>
            </div>
          ) : (
            <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => setShowSecret(true)}>
              {t_i18n("Enter the key manually")}
            </Button>
          )}
        </div>
      </div>

      <div className="max-w-xs space-y-1">
        <Label htmlFor="two-factor-setup-code">
          {t_i18n("Verification code")}
        </Label>
        <Input id="two-factor-setup-code" aria-label={t_i18n("Verification code")}
          autoComplete="one-time-code"
          inputMode="numeric"
          value={code}
          onChange={(e) => { setCode(e.target.value); if (error) setError(""); }}
          onKeyDown={(e) => { if (e.key === "Enter" && code.trim()) submit(); }}
          placeholder="123456"
          className="h-8 text-center font-mono-plex content-base tracking-[0.3em]"
        />
      </div>

      {error && <p className="content-compact text-feedback-error-primary">{error}</p>}

      <div className="flex gap-2">
        <Button size="sm" className="h-7 content-compact" onClick={submit} disabled={!code.trim() || enable.isPending} aria-busy={enable.isPending || undefined}>
          {enable.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Enable two-factor")}
        </Button>
        <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={onCancel}>
          {t_i18n("Cancel")}
        </Button>
      </div>
    </div>
  );
}

function DisableForm({
  needsPassword,
  onCancel,
  onDisabled,
}: {
  needsPassword: boolean;
  onCancel: () => void;
  onDisabled: () => void;
}) {
  const { t_i18n } = useFormatter();
  const disable = useDisableMfa();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    setError("");
    disable.mutate(
      // Omit the field entirely on SSO-only accounts rather than sending an
      // empty string, so the request says "there is no password here" instead
      // of "the password is blank".
      { ...(needsPassword ? { password } : {}), code: code.trim() },
      {
        onSuccess: onDisabled,
        onError: (err: Error) => setError(err.message || t_i18n("Could not disable two-factor authentication")),
      },
    );
  };

  return (
    <div className="mt-3 max-w-sm space-y-3 pl-7">
      <p className="content-compact text-default-secondary">
        {needsPassword
          ? t_i18n("Confirm with your password and a current code. Both are required, so neither an unlocked browser nor the device alone can remove your second factor.")
          : t_i18n("Confirm with a current code from your authenticator app.")}
      </p>
      {needsPassword && (
        <div className="space-y-1">
          <Label htmlFor="two-factor-disable-password">{t_i18n("Password")}</Label>
          <Input id="two-factor-disable-password" aria-label={t_i18n("Password")} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className="h-8 content-base" />
        </div>
      )}
      <div className="space-y-1">
        <Label htmlFor="two-factor-disable-code">{t_i18n("Verification or recovery code")}</Label>
        <Input id="two-factor-disable-code" aria-label={t_i18n("Verification or recovery code")} autoComplete="one-time-code" value={code} onChange={(e) => { setCode(e.target.value); if (error) setError(""); }} placeholder="123456" className="h-8 font-mono-plex content-base" />
      </div>
      {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
      <div className="flex gap-2">
        <Button
          variant="destructive"
          size="sm"
          className="h-7 content-compact"
          onClick={submit}
          disabled={(needsPassword && !password) || !code.trim() || disable.isPending} aria-busy={disable.isPending || undefined}>
          {disable.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Disable two-factor")}
        </Button>
        <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={onCancel}>{t_i18n("Cancel")}</Button>
      </div>
    </div>
  );
}

function RegenerateForm({ onCancel, onRegenerated }: { onCancel: () => void; onRegenerated: (codes: string[]) => void }) {
  const { t_i18n } = useFormatter();
  const regenerate = useRegenerateRecoveryCodes();
  const [code, setCode] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    setError("");
    regenerate.mutate(
      { code: code.trim() },
      {
        onSuccess: (data) => onRegenerated(data.recovery_codes),
        onError: (err: Error) => setError(err.message || t_i18n("Invalid verification code. Please try again.")),
      },
    );
  };

  return (
    <div className="mt-3 max-w-xs space-y-3 pl-7">
      <p className="content-compact text-default-secondary">
        {t_i18n("Generating a new set invalidates your current recovery codes. Enter a code from your authenticator app to confirm.")}
      </p>
      <div className="space-y-1">
        <Label htmlFor="two-factor-regenerate-code">{t_i18n("Verification code")}</Label>
        <Input id="two-factor-regenerate-code" aria-label={t_i18n("Verification code")}
          autoComplete="one-time-code"
          inputMode="numeric"
          value={code}
          onChange={(e) => { setCode(e.target.value); if (error) setError(""); }}
          onKeyDown={(e) => { if (e.key === "Enter" && code.trim()) submit(); }}
          placeholder="123456"
          className="h-8 text-center font-mono-plex content-base tracking-[0.3em]"
        />
      </div>
      {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
      <div className="flex gap-2">
        <Button size="sm" className="h-7 content-compact" onClick={submit} disabled={!code.trim() || regenerate.isPending} aria-busy={regenerate.isPending || undefined}>
          {regenerate.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Generate new codes")}
        </Button>
        <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={onCancel}>{t_i18n("Cancel")}</Button>
      </div>
    </div>
  );
}

type Mode = "idle" | "enrolling" | "codes" | "disabling" | "regenerating";

export function TwoFactorRow({ mfa, email }: { mfa: MfaState | undefined; email: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const setup = useSetupMfa();

  const [mode, setMode] = useState<Mode>("idle");
  const [setupData, setSetupData] = useState<MfaSetup | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);

  // An older backend build omits the block entirely; treat that as
  // "off and unavailable" rather than rendering a control that would 404.
  if (!mfa?.available) return null;

  const enabled = mfa.enabled;
  const remaining = mfa.recovery_codes_remaining;

  const startEnrollment = () => {
    setup.mutate(undefined, {
      onSuccess: (data) => { setSetupData(data); setMode("enrolling"); },
      onError: (err: Error) => toast({ title: err.message || t_i18n("Could not start enrollment"), variant: "error" }),
    });
  };

  const reset = () => { setMode("idle"); setSetupData(null); setRecoveryCodes([]); };

  return (
    <div className="px-5 py-3.5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <ShieldCheck className={enabled ? "h-4 w-4 shrink-0 text-feedback-success-primary" : "h-4 w-4 shrink-0 text-accent"} />
          <span className="w-36 shrink-0 content-base font-medium" data-testid="profile.two-factor-label">
            {t_i18n("Two-factor")}
          </span>
          <Badge variant={enabled ? "success" : "outline"} className="content-caption">
            {enabled ? t_i18n("Enabled") : t_i18n("Not enabled")}
          </Badge>
          {enabled && remaining <= LOW_RECOVERY_CODES && (
            <span className="flex items-center gap-1.5 content-caption text-feedback-warning-primary">
              <AlertTriangle className="h-3 w-3" />
              {t_i18n("{count, plural, one {# recovery code left} other {# recovery codes left}}", { count: remaining })}
            </span>
          )}
        </div>

        {mode === "idle" && !enabled && (
          <Button variant="secondary" className="gap-1.5" data-testid="profile.two-factor-setup" onClick={startEnrollment} disabled={setup.isPending} aria-busy={setup.isPending || undefined}>
            {setup.isPending && <Spinner size="sm" tone="inherit" />}{!setup.isPending && <Smartphone className="h-3 w-3" />}
            {t_i18n("Set up")}
          </Button>
        )}
        {mode === "idle" && enabled && (
          <div className="flex gap-2" data-testid="profile.two-factor-actions">
            <Button variant="secondary" className="gap-1.5" onClick={() => setMode("regenerating")}>
              <KeyRound className="h-3 w-3" />
              {t_i18n("Recovery codes")}
            </Button>
            <Button variant="ghost" className="text-feedback-error-primary hover:text-feedback-error-primary" onClick={() => setMode("disabling")}>
              {t_i18n("Disable")}
            </Button>
          </div>
        )}
      </div>

      {mode === "idle" && !enabled && (
        <p className="mt-1.5 pl-7 content-compact text-default-secondary">
          {t_i18n("Require a code from your authenticator app in addition to your password when signing in.")}
        </p>
      )}

      {mode === "enrolling" && setupData && (
        <EnrollmentForm
          setup={setupData}
          onCancel={reset}
          onEnabled={(codes) => {
            setRecoveryCodes(codes);
            setSetupData(null);
            setMode("codes");
            toast({ title: t_i18n("Two-factor authentication enabled"), variant: "success" });
          }}
        />
      )}

      {mode === "codes" && (
        <RecoveryCodeList codes={recoveryCodes} email={email} onAcknowledge={reset} />
      )}

      {mode === "disabling" && (
        <DisableForm
          // Older backend builds omit the field; defaulting to "ask" keeps the
          // form strict for the one rolling-deploy interval where it is missing.
          needsPassword={mfa.requires_password_to_disable !== false}
          onCancel={reset}
          onDisabled={() => {
            reset();
            toast({ title: t_i18n("Two-factor authentication disabled"), variant: "success" });
          }}
        />
      )}

      {mode === "regenerating" && (
        <RegenerateForm
          onCancel={reset}
          onRegenerated={(codes) => { setRecoveryCodes(codes); setMode("codes"); }}
        />
      )}
    </div>
  );
}
