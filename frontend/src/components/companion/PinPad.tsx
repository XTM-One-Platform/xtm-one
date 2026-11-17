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

import { useEffect } from "react";
import { Delete } from "lucide-react";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "back"] as const;

/**
 * A phone keypad for a PIN: the digits typed show as dots, never as digits.
 * A hardware keyboard works too (digits, Backspace, Enter).
 */
export function PinPad({
  value,
  onChange,
  onSubmit,
  minDigits,
  maxDigits,
  disabled = false,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  minDigits: number;
  maxDigits: number;
  disabled?: boolean;
  /** What the dots stand for, for screen readers ("PIN"). */
  label: string;
}) {
  const { t_i18n } = useFormatter();

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (disabled || event.metaKey || event.ctrlKey || event.altKey) return;
      if (/^[0-9]$/.test(event.key)) {
        if (value.length < maxDigits) onChange(value + event.key);
      } else if (event.key === "Backspace") {
        onChange(value.slice(0, -1));
      } else if (event.key === "Enter") {
        if (value.length >= minDigits) onSubmit();
      } else {
        return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [disabled, maxDigits, minDigits, onChange, onSubmit, value]);

  const slots = Math.max(minDigits, Math.min(maxDigits, value.length + (value.length >= minDigits ? 1 : 0)));

  return (
    <div className="flex flex-col items-center gap-6">
      <div
        role="status"
        aria-label={t_i18n("{label}: {count, plural, one {# digit} other {# digits}} entered", {
          label,
          count: value.length,
        })}
        className="flex h-4 items-center gap-3"
      >
        {Array.from({ length: slots }, (_, index) => (
          <span
            key={index}
            aria-hidden="true"
            className={cn(
              "h-3 w-3 rounded-full border border-accent transition-colors",
              index < value.length ? "bg-accent" : "bg-transparent",
            )}
          />
        ))}
      </div>
      <div className="grid grid-cols-3 gap-3">
        {KEYS.map((key, index) =>
          key === "" ? (
            <span key={index} aria-hidden="true" />
          ) : key === "back" ? (
            <button
              key={index}
              type="button"
              disabled={disabled || value.length === 0}
              onClick={() => onChange(value.slice(0, -1))}
              aria-label={t_i18n("Delete the last digit")}
              className="flex h-16 w-16 items-center justify-center rounded-full text-default-secondary transition-colors hover:bg-elevation-hover active:bg-elevation-hover disabled:opacity-40"
            >
              <Delete className="h-6 w-6" aria-hidden="true" />
            </button>
          ) : (
            <button
              key={index}
              type="button"
              disabled={disabled || value.length >= maxDigits}
              onClick={() => onChange(value + key)}
              className="flex h-16 w-16 items-center justify-center rounded-full bg-elevation-highlight text-2xl font-medium text-default-primary transition-colors hover:bg-elevation-hover active:bg-elevation-hover disabled:opacity-40"
            >
              {key}
            </button>
          ),
        )}
      </div>
    </div>
  );
}
