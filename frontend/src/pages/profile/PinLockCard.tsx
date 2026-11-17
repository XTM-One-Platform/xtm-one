// SPDX-License-Identifier: Apache-2.0

/**
 * Profile > Phones > PIN lock: the mobile companion's PIN (`lib/app-lock.ts`).
 *
 * Turned on here or from the phone, it hides everything the companion shows
 * until the PIN is entered. The organization can require it
 * (`companion_pin_required`): the switch then stays on. A computer's session
 * changes the PIN without the current one - that is how a forgotten PIN is
 * reset; a phone's session needs the current one.
 */
import { useState } from "react";
import { LockKeyhole } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useAppLockStatus, useSetAppPin, useToggleAppLock } from "@/hooks/api/useAppLockApi";
import { useFormatter } from "@/i18n";
import { PIN_PROBLEMS } from "@/components/companion/pin-problems";
import { pinProblem, wrongPinAttemptsLeft } from "@/lib/app-lock";

function PinField({
  label,
  value,
  onChange,
  maxDigits,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  maxDigits: number;
  autoComplete: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="content-compact text-default-secondary">{label}</span>
      <Input
        aria-label={label}
        type="password"
        inputMode="numeric"
        autoComplete={autoComplete}
        maxLength={maxDigits}
        value={value}
        onChange={(event) => onChange(event.target.value.replace(/\D/g, ""))}
        className="h-8 w-40 font-mono-plex tracking-[0.3em] content-base"
      />
    </label>
  );
}

export function PinLockCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: status } = useAppLockStatus();
  const setPin = useSetAppPin();
  const toggle = useToggleAppLock();
  const [editing, setEditing] = useState(false);
  const [pin, setPinValue] = useState("");
  const [confirm, setConfirm] = useState("");
  const [current, setCurrent] = useState("");
  const [error, setError] = useState("");

  if (!status) return null;
  const { min_digits: min, max_digits: max } = status;
  const needsCurrent = status.companion && status.has_pin;
  const formOpen = editing || (status.required && !status.has_pin);

  const reset = () => {
    setEditing(false);
    setPinValue("");
    setConfirm("");
    setCurrent("");
    setError("");
  };

  const save = () => {
    const problem = pinProblem(pin, min, max);
    if (problem) {
      setError(t_i18n(PIN_PROBLEMS[problem], { min, max }));
      return;
    }
    if (pin !== confirm) {
      setError(t_i18n("The two PINs are not the same."));
      return;
    }
    setPin.mutate(
      { pin, currentPin: needsCurrent ? current : undefined },
      {
        onSuccess: () => {
          reset();
          toast({ title: t_i18n("PIN saved"), variant: "success" });
        },
        onError: (err) => setError(refusal(err, t_i18n("The PIN could not be saved. Try again."))),
      },
    );
  };

  // A wrong current PIN counts like a wrong unlock: say how many are left before the phone is signed out.
  const refusal = (err: unknown, fallback: string): string => {
    const left = wrongPinAttemptsLeft((err as { detail?: unknown }).detail);
    if (left !== null) {
      return t_i18n(
        "The current PIN is not right. {count, plural, one {# attempt left} other {# attempts left}} before you are signed out.",
        { count: left },
      );
    }
    return err instanceof Error && err.message ? err.message : fallback;
  };

  const onToggle = (on: boolean) => {
    setError("");
    if (on && !status.has_pin) {
      setEditing(true);
      return;
    }
    toggle.mutate(
      { enabled: on, currentPin: needsCurrent ? current : undefined },
      { onError: (err) => setError(refusal(err, t_i18n("The change could not be saved."))) },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 content-base">
            <LockKeyhole className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("PIN lock")}
            {status.required && <Badge variant="cyan">{t_i18n("Required by your organization")}</Badge>}
          </CardTitle>
          <Switch
            checked={status.applies}
            disabled={status.required || toggle.isPending}
            onCheckedChange={onToggle}
            aria-label={t_i18n("Ask for a PIN on my phones")}
          />
        </div>
        <CardDescription className="content-compact">
          {t_i18n(
            "On your phones, nothing shows and nothing is approved until your PIN is entered. While it is on, notifications carry no content and no Approve or Reject buttons.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 pt-0">
        {formOpen ? (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-4">
              {needsCurrent && (
                <PinField label={t_i18n("Current PIN")} value={current} onChange={setCurrent} maxDigits={max} autoComplete="current-password" />
              )}
              <PinField label={t_i18n("New PIN")} value={pin} onChange={setPinValue} maxDigits={max} autoComplete="new-password" />
              <PinField label={t_i18n("Confirm the PIN")} value={confirm} onChange={setConfirm} maxDigits={max} autoComplete="new-password" />
            </div>
            <p className="content-caption text-default-secondary">{t_i18n("{min} to {max} digits.", { min, max })}</p>
            <div className="flex gap-2">
              <Button size="sm" disabled={setPin.isPending || pin.length < min} onClick={save}>
                {t_i18n("Save PIN")}
              </Button>
              {!(status.required && !status.has_pin) && (
                <Button size="sm" variant="ghost" onClick={reset}>
                  {t_i18n("Cancel")}
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            {needsCurrent && status.applies && (
              <PinField label={t_i18n("Current PIN")} value={current} onChange={setCurrent} maxDigits={max} autoComplete="current-password" />
            )}
            <Button size="sm" variant="outline" className="self-end" onClick={() => setEditing(true)}>
              {status.has_pin ? t_i18n("Change PIN") : t_i18n("Choose a PIN")}
            </Button>
          </div>
        )}
        {error && (
          <p role="alert" className="content-compact text-feedback-error-primary">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
