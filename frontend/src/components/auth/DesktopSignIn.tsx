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
 * The two halves of single sign-on for XTM One Desktop.
 *
 * SSO cannot run inside the Desktop app's webview: identity providers refuse
 * or degrade embedded browsers, and the webview shares no session with the
 * browser the person is already signed in to. So the flow is split:
 *
 *   - `BrowserLoginStep` renders INSIDE the app after the person picked an SSO
 *     provider: the shell has opened the system browser on the instance's SSO
 *     route, and this step waits for the `desktop-browser-login` event that
 *     carries the session back (or a failure), with a way to re-open the
 *     browser and a way to give up;
 *   - `DesktopHandoffStep` renders IN THE BROWSER when the SSO callback lands
 *     on `/login#token=...&desktop_login=<id>`: it names the machine that is
 *     asking (read from the platform, never from the URL) and, on the person's
 *     confirmation, hands the session to the app. The browser itself is not
 *     signed in: the person asked to sign in on the desktop, not in this tab.
 */
import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowLeft, ExternalLink, Laptop, MonitorCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useCompleteDesktopLogin,
  useDeclineDesktopLogin,
  useDesktopLoginHandoff,
} from "@/hooks/api/useDesktopLoginApi";
import {
  DESKTOP_BROWSER_LOGIN_EVENT,
  DESKTOP_PRODUCT_NAME,
  beginDesktopBrowserLogin,
  cancelDesktopBrowserLogin,
  desktopListen,
} from "@/lib/desktop";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { DesktopBrowserLoginOutcome, DesktopBrowserLoginProvider } from "@/types";
import { Spinner } from "@filigran/design-system";

/* ────────────────────────────────────────────────────────────────────────── */
/*  Inside the Desktop app: waiting for the browser                           */
/* ────────────────────────────────────────────────────────────────────────── */

export function BrowserLoginStep({
  provider,
  providerLabel,
  onSignedIn,
  onCancel,
}: {
  provider: DesktopBrowserLoginProvider;
  providerLabel: string;
  /** The session the browser handed back - the caller stores it and lands the user. */
  onSignedIn: (accessToken: string) => void;
  onCancel: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const loginIdRef = useRef<string | null>(null);

  // One handoff per attempt: start it, listen for its outcome, and drop the
  // listener (and the pending login) when the step goes away.
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    setError(null);

    desktopListen<DesktopBrowserLoginOutcome>(DESKTOP_BROWSER_LOGIN_EVENT, (outcome) => {
      if (cancelled || outcome.login_id !== loginIdRef.current) return;
      if (outcome.status === "completed") {
        loginIdRef.current = null;
        onSignedIn(outcome.access_token);
        return;
      }
      setError(
        outcome.reason === "timeout"
          ? t_i18n("The sign-in was not completed in time. Open the browser again to retry.")
          : t_i18n("The sign-in could not be completed. Open the browser again to retry."),
      );
    }).then((stop) => {
      if (cancelled) stop();
      else unlisten = stop;
    });

    beginDesktopBrowserLogin(provider)
      .then((started) => {
        if (!cancelled) loginIdRef.current = started.login_id;
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });

    return () => {
      cancelled = true;
      unlisten?.();
      const pending = loginIdRef.current;
      loginIdRef.current = null;
      if (pending) void cancelDesktopBrowserLogin(pending).catch(() => {});
    };
    // `attempt` re-runs the whole handoff when the person asks to retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, attempt]);

  return (
    <div className="space-y-5 text-center" data-testid="desktop-browser-login">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-sm bg-accent/10">
        {error ? (
          <AlertCircle className="h-6 w-6 text-feedback-warning-primary" />
        ) : (
          <ExternalLink className="h-6 w-6 text-accent" />
        )}
      </div>
      <div className="space-y-1.5">
        <p className="content-base font-semibold">{t_i18n("Continue in your browser")}</p>
        <p className="content-compact leading-relaxed text-default-secondary">
          {t_i18n(
            "Finish signing in with {provider} in the browser window that just opened, then confirm that you are signing in to {product} on this machine. You will be signed in here automatically.",
            { provider: providerLabel, product: DESKTOP_PRODUCT_NAME },
          )}
        </p>
      </div>
      {error ? (
        <p className="rounded-sm bg-feedback-alert-primary/10 px-3 py-2 content-compact text-feedback-warning-tertiary">{error}</p>
      ) : (
        <p className="flex items-center justify-center gap-2 content-compact text-default-secondary">
          <Spinner size="sm" tone="inherit" />
          {t_i18n("Waiting for the browser...")}
        </p>
      )}
      <div className="flex items-center justify-center gap-2">
        <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={() => setAttempt((n) => n + 1)}>
          <ExternalLink className="h-3.5 w-3.5" />
          {t_i18n("Open the browser again")}
        </Button>
        <Button type="button" variant="ghost" size="sm" className="gap-1.5" onClick={onCancel}>
          <ArrowLeft className="h-3.5 w-3.5" />
          {t_i18n("Back")}
        </Button>
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────────── */
/*  In the browser: hand the session to the app                               */
/* ────────────────────────────────────────────────────────────────────────── */

export function DesktopHandoffStep({
  loginId,
  token,
  onDone,
}: {
  loginId: string;
  /** The session to hand over - held in memory by the caller, never read from storage here. `null` while the second factor is still owed. */
  token: string | null;
  /** The person declined, or the handoff finished - the caller decides what the tab shows next. */
  onDone: (outcome: "completed" | "declined") => void;
}) {
  const { t_i18n } = useFormatter();
  const handoff = useDesktopLoginHandoff(loginId);
  const complete = useCompleteDesktopLogin();
  const decline = useDeclineDesktopLogin();
  const [actionError, setActionError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const info = handoff.data ?? null;
  const busy = complete.isPending || decline.isPending;
  const error =
    actionError ??
    (handoff.isError
      ? (handoff.error as Error | null)?.message || t_i18n("This sign-in request has expired.")
      : null);

  const confirm = () => {
    if (!token) {
      setActionError(t_i18n("No session to hand over. Sign in again from the Desktop app."));
      return;
    }
    setActionError(null);
    complete.mutate(
      { loginId, token },
      {
        onSuccess: () => {
          setDone(true);
          onDone("completed");
        },
        onError: (e) => setActionError(e instanceof Error ? e.message : String(e)),
      },
    );
  };

  // "Not my machine": the request is dropped server-side (and the refusal
  // audited) when this tab holds a session to say so with; the tab leaves the
  // step either way - refusing must never depend on the platform answering.
  const refuse = () => {
    if (token) {
      decline.mutate({ loginId, token }, { onSettled: () => onDone("declined") });
    } else {
      onDone("declined");
    }
  };

  const platformLabel = info ? RUNNER_PLATFORM_CONFIG[info.platform]?.label ?? info.platform : "";

  if (done) {
    return (
      <div className="space-y-4 text-center" data-testid="desktop-handoff-done">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-sm bg-feedback-success-primary/10">
          <MonitorCheck className="h-6 w-6 text-feedback-success-primary" />
        </div>
        <p className="content-base font-semibold">{t_i18n("You are signed in on {product}", { product: DESKTOP_PRODUCT_NAME })}</p>
        <p className="content-compact leading-relaxed text-default-secondary">
          {t_i18n("Return to the app - it signs in on its own. You can close this tab; this browser was not signed in.")}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5 text-center" data-testid="desktop-handoff">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-sm bg-accent/10">
        <Laptop className="h-6 w-6 text-accent" />
      </div>
      <div className="space-y-1.5">
        <p className="content-base font-semibold">{t_i18n("Sign in to {product}?", { product: DESKTOP_PRODUCT_NAME })}</p>
        {info ? (
          <>
            <p className="content-compact leading-relaxed text-default-secondary">
              {t_i18n("The {product} app on {machine} ({platform}) asked to sign in with your account. Continue only if that is your machine.", {
                product: DESKTOP_PRODUCT_NAME,
                machine: info.runner_name,
                platform: platformLabel,
              })}
            </p>
            <p className="content-compact text-default-secondary/80" data-testid="desktop-handoff-owner">
              {info.enrolled_by
                ? t_i18n("Enrolled by {name}. Only that person can sign in there.", { name: info.enrolled_by })
                : t_i18n("A company-managed machine: any account can sign in there.")}
            </p>
          </>
        ) : error ? null : (
          <p className="flex items-center justify-center gap-2 content-compact text-default-secondary">
            <Spinner size="sm" tone="inherit" /> {t_i18n("Checking the request...")}
          </p>
        )}
      </div>
      {error && (
        <p className="flex items-start gap-2 rounded-sm bg-feedback-error-primary/10 px-3 py-2 text-left content-compact text-feedback-error-primary">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
        </p>
      )}
      <div className="flex items-center justify-center gap-2">
        <Button type="button" size="sm" className="gap-1.5" disabled={busy || !info} onClick={confirm} data-testid="desktop-handoff-confirm" aria-busy={busy || undefined}>
          {busy && <Spinner size="sm" tone="inherit" />}{!busy && <MonitorCheck className="h-3.5 w-3.5" />}
          {t_i18n("Yes, sign me in there")}
        </Button>
        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={refuse}>
          {t_i18n("Not my machine")}
        </Button>
      </div>
    </div>
  );
}
