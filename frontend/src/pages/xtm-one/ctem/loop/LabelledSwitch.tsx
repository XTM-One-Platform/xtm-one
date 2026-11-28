// SPDX-License-Identifier: Apache-2.0

/**
 * The one labelled switch of a source's opened configuration (#2740, UX round 1 B3): its label sits right next to
 * the switch at the row's end, in every section (an integration's "Provisioned", the scheduled pass, "Use on this
 * path"), and an optional muted note takes the row's start.
 */
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

export function LabelledSwitch({
  label,
  note,
  checked,
  disabled,
  onCheckedChange,
  testId,
  ariaLabel,
  className,
}: {
  label: string;
  note?: string;
  checked: boolean;
  disabled: boolean;
  onCheckedChange: (on: boolean) => void;
  testId: string;
  /** The switch's accessible name when the visible label alone does not say what it turns (an integration's name). */
  ariaLabel?: string;
  className?: string;
}) {
  return (
    <span className={cn("flex min-w-0 items-center justify-between gap-3", className)} data-testid={`${testId}-row`}>
      <span className="min-w-0 text-default-secondary">{note}</span>
      <label className="flex shrink-0 items-center gap-2">
        <span>{label}</span>
        <Switch className="shrink-0" checked={checked} disabled={disabled} onCheckedChange={onCheckedChange} aria-label={ariaLabel ?? label} data-testid={testId} />
      </label>
    </span>
  );
}
