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
 * The mobile companion's PIN lock (`lib/app-lock.ts`).
 *
 * On a phone, nothing of the app mounts until the session is unlocked: the
 * gate renders the lock screen INSTEAD of its children, so no page, no query
 * and no stream runs behind it. The server enforces the same (every request
 * of a locked companion session answers 423); this gate only follows it:
 *
 * - the app locks each time it opens (`POST /app-lock/session`);
 * - any 423 (`APP_LOCKED_EVENT`, fired by the API client and on a chat stream's `app_locked` frame)
 *   brings the lock screen back;
 * - the app locks itself after `RELOCK_AFTER_HIDDEN_MS` out of sight, and as
 *   the page goes away.
 *
 * On a computer the gate renders its children and nothing else.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, LockKeyhole } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  APP_LOCK_KEY,
  lockCompanionSession,
  useAppLockStatus,
  useSetAppPin,
  useUnlockApp,
} from "@/hooks/api/useAppLockApi";
import { signOut } from "@/hooks/useAuth";
import { useCompanionDevice } from "@/hooks/useCompanionDevice";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import {
  APP_LOCKED_EVENT,
  APP_PIN_SETUP_REQUIRED_CODE,
  RELOCK_AFTER_HIDDEN_MS,
  pinProblem,
  wrongPinAttemptsLeft,
} from "@/lib/app-lock";
import type { AppLockStatus } from "@/types";
import { AppLockContext, type AppLockControls } from "./app-lock-context";
import { PinPad } from "./PinPad";
import { PIN_PROBLEMS } from "./pin-problems";

export function AppLockGate({ children }: { children: ReactNode }) {
  const phone = useCompanionDevice();
  if (!phone) return <>{children}</>;
  return <CompanionLockGate>{children}</CompanionLockGate>;
}

function CompanionLockGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { data: status } = useAppLockStatus();
  const [choosing, setChoosing] = useState(false);

  const markLocked = useCallback(
    (setupRequired = false) =>
      qc.setQueryData<AppLockStatus>(APP_LOCK_KEY, (old) =>
        old ? { ...old, locked: true, setup_required: old.setup_required || setupRequired } : old,
      ),
    [qc],
  );

  // A request was refused as locked: show the lock screen, then read the truth.
  useEffect(() => {
    const onLocked = (event: Event) => {
      const code = (event as CustomEvent<{ code?: string }>).detail?.code;
      markLocked(code === APP_PIN_SETUP_REQUIRED_CODE);
      void qc.invalidateQueries({ queryKey: APP_LOCK_KEY });
    };
    window.addEventListener(APP_LOCKED_EVENT, onLocked);
    return () => window.removeEventListener(APP_LOCKED_EVENT, onLocked);
  }, [markLocked, qc]);

  const lockNow = useCallback(() => {
    markLocked();
    lockCompanionSession().catch(() => {
      // The server's own unlock window still runs out.
    });
  }, [markLocked]);

  // Out of sight too long, or the page going away: lock.
  const applies = !!status?.applies && !!status.companion;
  useEffect(() => {
    if (!applies) return;
    let hiddenAt: number | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
        // Timers of a hidden page may be frozen: the check on return covers that.
        timer = setTimeout(lockNow, RELOCK_AFTER_HIDDEN_MS);
        return;
      }
      if (timer) clearTimeout(timer);
      timer = null;
      if (hiddenAt !== null && Date.now() - hiddenAt >= RELOCK_AFTER_HIDDEN_MS) lockNow();
      hiddenAt = null;
    };
    const onPageHide = () => {
      lockCompanionSession().catch(() => {});
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [applies, lockNow]);

  const controls = useMemo<AppLockControls | null>(
    () => (status ? { status, lockNow, choosePin: () => setChoosing(true) } : null),
    [lockNow, status],
  );

  if (!status || !controls) return <LockBackdrop />;
  if (status.setup_required) return <ChoosePinScreen status={status} required />;
  if (status.locked) return <UnlockScreen status={status} />;
  if (choosing && !status.has_pin) {
    return <ChoosePinScreen status={status} onDone={() => setChoosing(false)} />;
  }
  return <AppLockContext.Provider value={controls}>{children}</AppLockContext.Provider>;
}

/** The screen before the lock's state is known: nothing of the app. */
function LockBackdrop({ children }: { children?: ReactNode }) {
  return (
    <div
      className="flex min-h-dvh flex-col items-center justify-center bg-elevation-default px-6"
      style={{ paddingTop: "env(safe-area-inset-top, 0px)", paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
    >
      {children}
    </div>
  );
}

function LockHeading({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-col items-center gap-3 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10">
        <LockKeyhole className="h-6 w-6 text-accent" aria-hidden="true" />
      </span>
      <h1 className="title-xs text-default-primary">{title}</h1>
      <p className="content-compact max-w-xs text-default-secondary">{body}</p>
    </div>
  );
}

function LockError({ message }: { message: string }) {
  return (
    <div role="alert" className="flex max-w-xs items-center gap-2 rounded-sm bg-feedback-error-primary/10 px-3 py-2">
      <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" aria-hidden="true" />
      <p className="content-compact text-feedback-error-primary">{message}</p>
    </div>
  );
}

function UnlockScreen({ status }: { status: AppLockStatus }) {
  const { t_i18n } = useFormatter();
  const qc = useQueryClient();
  // Not `useAuth`: its profile query is refused while locked.
  const logout = () => signOut(qc);
  const name = usePlatformName("XTM One");
  const unlock = useUnlockApp();
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");

  const submit = useCallback(() => {
    if (unlock.isPending || pin.length < status.min_digits) return;
    unlock.mutate(pin, {
      onError: (err) => {
        setPin("");
        const left = wrongPinAttemptsLeft((err as { detail?: unknown }).detail);
        setError(
          left === null
            ? t_i18n("The PIN could not be checked. Try again.")
            : t_i18n(
                "Wrong PIN. {count, plural, one {# attempt left} other {# attempts left}} before you are signed out.",
                { count: left },
              ),
        );
      },
    });
  }, [pin, status.min_digits, t_i18n, unlock]);

  return (
    <LockBackdrop>
      <div className="flex w-full max-w-sm flex-col items-center gap-6">
        <LockHeading title={t_i18n("Enter your PIN")} body={t_i18n("{name} is locked on this phone.", { name })} />
        <PinPad
          label={t_i18n("PIN")}
          value={pin}
          onChange={(next) => {
            setPin(next);
            if (error) setError("");
          }}
          onSubmit={submit}
          minDigits={status.min_digits}
          maxDigits={status.max_digits}
          disabled={unlock.isPending}
        />
        {error && <LockError message={error} />}
        <Button className="min-h-11 w-full max-w-xs" disabled={unlock.isPending || pin.length < status.min_digits} onClick={submit}>
          {t_i18n("Unlock")}
        </Button>
        <div className="flex flex-col items-center gap-1 text-center">
          <p className="content-compact text-default-secondary">
            {t_i18n("Forgot your PIN? Sign out, then change it in your profile on a computer.")}
          </p>
          <Button variant="ghost" className="min-h-11" onClick={logout}>
            {t_i18n("Sign out")}
          </Button>
        </div>
      </div>
    </LockBackdrop>
  );
}

function ChoosePinScreen({
  status,
  required = false,
  onDone,
}: {
  status: AppLockStatus;
  /** The organization requires a PIN: there is no way around this screen. */
  required?: boolean;
  onDone?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const qc = useQueryClient();
  // Not `useAuth`: its profile query is refused while locked.
  const logout = () => signOut(qc);
  const name = usePlatformName("XTM One");
  const setAppPin = useSetAppPin();
  const [first, setFirst] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const { min_digits: min, max_digits: max } = status;

  const submit = useCallback(() => {
    if (setAppPin.isPending) return;
    if (first === null) {
      const problem = pinProblem(pin, min, max);
      if (problem) {
        setError(t_i18n(PIN_PROBLEMS[problem], { min, max }));
        return;
      }
      setFirst(pin);
      setPin("");
      return;
    }
    if (pin !== first) {
      setFirst(null);
      setPin("");
      setError(t_i18n("The two PINs are not the same. Choose your PIN again."));
      return;
    }
    setAppPin.mutate(
      { pin },
      {
        onSuccess: () => onDone?.(),
        onError: (err) => {
          setFirst(null);
          setPin("");
          setError(err instanceof Error && err.message ? err.message : t_i18n("The PIN could not be saved. Try again."));
        },
      },
    );
  }, [first, max, min, onDone, pin, setAppPin, t_i18n]);

  return (
    <LockBackdrop>
      <div className="flex w-full max-w-sm flex-col items-center gap-6">
        <LockHeading
          title={first === null ? t_i18n("Choose a PIN") : t_i18n("Enter the same PIN again")}
          body={
            first !== null
              ? t_i18n("{min} to {max} digits.", { min, max })
              : required
                ? t_i18n("Your organization requires a PIN to use {name} on this phone: {min} to {max} digits.", { name, min, max })
                : t_i18n("Nothing of {name} shows on this phone until the PIN is entered: {min} to {max} digits.", { name, min, max })
          }
        />
        <PinPad
          label={t_i18n("PIN")}
          value={pin}
          onChange={(next) => {
            setPin(next);
            if (error) setError("");
          }}
          onSubmit={submit}
          minDigits={min}
          maxDigits={max}
          disabled={setAppPin.isPending}
        />
        {error && <LockError message={error} />}
        <Button className="min-h-11 w-full max-w-xs" disabled={setAppPin.isPending || pin.length < min} onClick={submit}>
          {first === null ? t_i18n("Continue") : t_i18n("Set PIN")}
        </Button>
        {required ? (
          <Button variant="ghost" className="min-h-11" onClick={logout}>
            {t_i18n("Sign out")}
          </Button>
        ) : (
          <Button variant="ghost" className="min-h-11" onClick={onDone}>
            {t_i18n("Cancel")}
          </Button>
        )}
      </div>
    </LockBackdrop>
  );
}
