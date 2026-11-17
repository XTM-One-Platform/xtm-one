// SPDX-License-Identifier: Apache-2.0

/**
 * The second-factor code step.
 *
 * Shared by every entry point that can end up holding a challenge token instead
 * of a session: local login, the SSO callback return, and the password-reset
 * completion. Extracted rather than duplicated because the three differ only in
 * how they *acquired* the challenge — the exchange itself, the recovery-code
 * fallback, and the error handling are identical, and a copy would inevitably
 * drift on the security-relevant half.
 *
 * Callers render this INSTEAD of their own form, never beside it: whatever
 * proved the first factor is already behind us, and leaving the original form
 * on screen would offer a way to sidestep the code being asked for.
 */

import { useState } from "react";
import { AlertCircle, ArrowLeft, KeyRound, Smartphone } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useVerifyMfa } from "@/hooks/api/useAuthApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export function MfaCodeStep({
  mfaToken,
  email,
  onVerified,
  onCancel,
}: {
  /** The challenge token handed back by login, the SSO callback, or the reset. */
  mfaToken: string;
  /** Shown in the prompt when known. Absent on the SSO return — the user never
   *  typed one — in which case the wording falls back to a generic line. */
  email?: string;
  onVerified: () => void;
  /** Omit to hide the escape hatch (the reset flow has nothing to go back to). */
  onCancel?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const verifyMfa = useVerifyMfa();
  const [code, setCode] = useState("");
  const [useRecoveryCode, setUseRecoveryCode] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    const trimmed = code.trim();
    if (!trimmed) {
      setError(
        useRecoveryCode
          ? t_i18n("Enter a recovery code")
          : t_i18n("Enter the 6-digit code from your authenticator app"),
      );
      return;
    }

    try {
      await verifyMfa.mutateAsync({ mfa_token: mfaToken, code: trimmed });
      onVerified();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setCode("");
    }
  };

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <div className="flex items-start gap-3 rounded-sm bg-accent/5 px-4 py-3">
        {useRecoveryCode ? (
          <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        ) : (
          <Smartphone className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        )}
        <p className="content-base text-default-secondary">
          {useRecoveryCode
            ? t_i18n("Enter one of the recovery codes you saved when you enabled two-factor authentication.")
            : email
              ? t_i18n("Open your authenticator app and enter the 6-digit code for {email}.", { email })
              : t_i18n("Open your authenticator app and enter the 6-digit code to finish signing in.")}
        </p>
      </div>

      <div className="space-y-2">
        <label className="content-base font-medium text-default-secondary">
          {useRecoveryCode ? t_i18n("Recovery code") : t_i18n("Verification code")}
        </label>
        <Input aria-label={useRecoveryCode ? t_i18n("Recovery code") : t_i18n("Verification code")}
          // `one-time-code` is what lets iOS/Android offer the code from the
          // notification, and password managers autofill it.
          autoComplete="one-time-code"
          autoFocus
          inputMode={useRecoveryCode ? "text" : "numeric"}
          value={code}
          onChange={(e) => { setCode(e.target.value); if (error) setError(""); }}
          placeholder={useRecoveryCode ? "XXXX-XXXX-XXXX-XXXX" : "123456"}
          className={cn(!useRecoveryCode && "text-center text-lg tracking-[0.4em] font-mono-plex")}
        />
      </div>

      {error && (
        <div className="flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3 animate-in fade-in slide-in-from-top-2 duration-300">
          <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
          <p className="content-base text-feedback-error-primary">{error}</p>
        </div>
      )}

      <Button type="submit" className="w-full" disabled={verifyMfa.isPending}>
        {verifyMfa.isPending ? (
          <span className="flex items-center gap-2">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            {t_i18n("Verifying...")}
          </span>
        ) : (
          t_i18n("Verify")
        )}
      </Button>

      <div className={cn("flex items-center content-compact", onCancel ? "justify-between" : "justify-end")}>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="flex items-center gap-1.5 text-default-secondary hover:text-default-primary hover:underline"
          >
            <ArrowLeft className="h-3 w-3" />
            {t_i18n("Back to sign in")}
          </button>
        )}
        <button
          type="button"
          onClick={() => { setUseRecoveryCode(!useRecoveryCode); setCode(""); setError(""); }}
          className="text-accent hover:underline"
        >
          {useRecoveryCode
            ? t_i18n("Use your authenticator app")
            : t_i18n("Use a recovery code")}
        </button>
      </div>
    </form>
  );
}
