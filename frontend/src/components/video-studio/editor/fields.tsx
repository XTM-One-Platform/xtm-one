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
 * The inspector's fields: one look for every setting of a scene or a
 * video. A field left empty is REMOVED from the spec (`onChange(undefined)`),
 * so the template's value or the default applies again - the placeholder says
 * which. A field the video's template locks is shown, disabled, with why.
 *
 * The library's `Input` puts `className` on its wrapper (the field, then its
 * "N/max" counter when it has a `maxLength`), never on the input element: a height
 * there does not shrink the field, it lets the field and its counter spill over
 * whatever comes next. So no input here is given a height, and a short code (a
 * `#rrggbb`) is cut to its length while typing rather than counted.
 */
import { useId, useState, type ReactNode } from "react";
import { ColorPicker, Slider } from "@filigran/design-system";
import { ChevronDown, Lock } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export function Section({
  title,
  icon: Icon,
  children,
  defaultOpen = true,
  actions,
}: {
  title: string;
  icon?: React.ElementType;
  children: ReactNode;
  defaultOpen?: boolean;
  actions?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="border-b border-elevation-subtle-soft last:border-b-0">
      <div className="flex items-center gap-1 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left content-compact font-semibold uppercase tracking-wide text-default-secondary hover:text-default-primary"
        >
          {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-accent" />}
          <span className="truncate">{title}</span>
          <ChevronDown className={cn("ml-auto h-3.5 w-3.5 shrink-0 transition-transform", !open && "-rotate-90")} />
        </button>
        {actions}
      </div>
      {open && <div className="space-y-3 px-3 pb-3">{children}</div>}
    </section>
  );
}

function LockedNote({ locked }: { locked?: boolean }) {
  const { t_i18n } = useFormatter();
  if (!locked) return null;
  return (
    <Tip content={t_i18n("The video's template keeps this setting")}>
      <Lock className="h-3 w-3 shrink-0 text-default-secondary" aria-label={t_i18n("Locked by the template")} />
    </Tip>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  locked,
  children,
  inline = false,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  locked?: boolean;
  children: ReactNode;
  inline?: boolean;
}) {
  const heading = (
    <div className="flex min-w-0 items-center gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      <LockedNote locked={locked} />
    </div>
  );
  const help = hint && <p className="text-[11px] leading-snug text-default-secondary">{hint}</p>;
  if (inline) {
    // The help line goes under the row, full width: as a third flex child it would squeeze the label and push the control off its column.
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-3">
          {heading}
          {children}
        </div>
        {help}
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      {heading}
      {children}
      {help}
    </div>
  );
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  maxLength,
  hint,
  locked,
  disabled,
}: {
  label: string;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
  placeholder?: string;
  maxLength?: number;
  hint?: string;
  locked?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} htmlFor={id} locked={locked}>
      <Input aria-label={label}
        id={id}
        value={value ?? ""}
        placeholder={placeholder}
        maxLength={maxLength}
        disabled={disabled || locked}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
      />
    </Field>
  );
}

export function TextAreaField({
  label,
  value,
  onChange,
  placeholder,
  maxLength,
  rows = 3,
  hint,
  disabled,
}: {
  label: string;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
  placeholder?: string;
  maxLength?: number;
  rows?: number;
  hint?: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <textarea
        id={id}
        rows={rows}
        value={value ?? ""}
        placeholder={placeholder}
        maxLength={maxLength}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
        className="w-full resize-y rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-2 content-compact leading-relaxed placeholder:text-default-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-60"
      />
    </Field>
  );
}

/** A number with a slider: the slider for the feel, the box for the exact value. */
export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step = 0.1,
  unit,
  fallback,
  hint,
  locked,
  disabled,
}: {
  label: string;
  value: number | undefined;
  onChange: (value: number | undefined) => void;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  /** What applies when the field is empty (the default or the template's). */
  fallback?: number;
  hint?: string;
  locked?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  const shown = value ?? fallback ?? min;
  const off = disabled || locked;
  const clamp = (n: number) => Math.min(max, Math.max(min, n));
  return (
    <Field label={label} hint={hint} htmlFor={id} locked={locked}>
      <div className="flex items-center gap-2">
        <Slider
          min={min}
          max={max}
          step={step}
          value={[shown]}
          disabled={off}
          aria-label={label}
          onValueChange={([next]) => onChange(clamp(next))}
          wrapperClassName="min-w-0 flex-1"
        />
        <div className="flex w-[84px] shrink-0 items-center gap-1">
          <Input aria-label={label}
            id={id}
            type="number"
            min={min}
            max={max}
            step={step}
            value={value ?? ""}
            placeholder={fallback !== undefined ? String(fallback) : undefined}
            disabled={off}
            onChange={(event) => {
              const raw = event.target.value;
              if (raw === "") onChange(undefined);
              else if (Number.isFinite(Number(raw))) onChange(clamp(Number(raw)));
            }}
          />
          {unit && <span className="content-caption text-default-secondary">{unit}</span>}
        </div>
      </div>
    </Field>
  );
}

interface ChoiceOption<T extends string> {
  value: T;
  label: string;
}

/** A picker option cannot hold an empty value: the "unset" choice is spelled with this sentinel. */
const UNSET = "__unset__";

export function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  unsetLabel,
  hint,
  locked,
  disabled,
}: {
  label: string;
  value: T | undefined;
  options: ChoiceOption<T>[];
  onChange: (value: T | undefined) => void;
  /** Offer "unset" (the default / the template's) under this label. */
  unsetLabel?: string;
  hint?: string;
  locked?: boolean;
  disabled?: boolean;
}) {
  // The list leaves out the option the unset choice stands for (the default), so a
  // spec that names that default outright (`frame.style: "window"`) matches no row:
  // the field would read empty. It reads the unset choice instead, which is what plays.
  const offered = value !== undefined && options.some((option) => option.value === value);
  const shown = offered ? value : unsetLabel ? UNSET : value;
  return (
    <Field label={label} hint={hint} locked={locked}>
      <SearchableSelect
        aria-label={label}
        searchable="auto"
        sort={false}
        options={options}
        pinned={unsetLabel ? [{ value: UNSET, label: unsetLabel }] : []}
        value={shown}
        onValueChange={(next) => onChange(next === UNSET ? undefined : (next as T))}
        disabled={disabled || locked}
      />
    </Field>
  );
}

export function SwitchField({
  label,
  checked,
  onChange,
  hint,
  locked,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  hint?: string;
  locked?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} htmlFor={id} locked={locked} inline>
      <Switch id={id} checked={checked} disabled={disabled || locked} onCheckedChange={onChange} />
    </Field>
  );
}

/** A `#rrggbb` colour: a swatch to pick, a box to type. */
export function ColorField({
  label,
  value,
  onChange,
  placeholder,
  locked,
  disabled,
}: {
  label: string;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
  placeholder?: string;
  locked?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  const off = disabled || locked;
  return (
    <Field label={label} htmlFor={id} locked={locked}>
      {/* The design system's ColorPicker: swatch, panel and hex field in one control; empty removes the value. */}
      <ColorPicker
        id={id}
        aria-label={label}
        value={value ?? ""}
        placeholder={placeholder}
        disabled={off}
        className="w-full"
        onValueChange={(next) => onChange(next === "" ? undefined : next.slice(0, 7))}
      />
    </Field>
  );
}
