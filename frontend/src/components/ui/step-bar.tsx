// SPDX-License-Identifier: Apache-2.0

/**
 * The step bar of a guided flow (UX round 1 B1 on #1092): one thin segment per
 * step, each a button with the step's name as its tooltip and accessible name,
 * the current one marked `aria-current="step"`. The bar stays thin visually;
 * the `py-2` padding gives each control a real hit area (WCAG 2.5.8).
 *
 * The one step bar of the app: the new agent wizard and the CTEM loop setup
 * render it, and `step-bar.test.ts` fails on a second `aria-current="step"`
 * bar written outside this file.
 */
import { useFormatter } from "@/i18n";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** How a segment reads: the current step, a step already passed or settled, or one still ahead. */
export type StepBarState = "current" | "settled" | "todo";

export interface StepBarStep {
  key: string;
  /** The step's name, already translated. */
  title: string;
  state: StepBarState;
  /** A step that cannot be reached yet. */
  disabled?: boolean;
}

/** The fill of a segment by state. */
export const STEP_BAR_FILL: Record<StepBarState, string> = {
  current: "bg-accent",
  settled: "bg-accent/60",
  todo: "bg-elevation-hover",
};

export function StepBar({
  steps,
  onSelect,
  ariaLabel,
  testIdPrefix,
  className,
}: {
  steps: readonly StepBarStep[];
  onSelect: (index: number) => void;
  /** The list's accessible name. */
  ariaLabel: string;
  /** `<prefix>-<key>` on each segment button. */
  testIdPrefix?: string;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <ol className={cn("flex items-center gap-2", className)} aria-label={ariaLabel}>
      {steps.map((step, i) => (
        <li key={step.key} className="flex-1">
          <Tip content={step.title}>
            <button
              type="button"
              aria-label={t_i18n("Step {current} of {total}: {title}", { current: i + 1, total: steps.length, title: step.title })}
              aria-current={step.state === "current" ? "step" : undefined}
              disabled={step.disabled}
              onClick={() => onSelect(i)}
              className="w-full py-2 disabled:cursor-not-allowed"
              data-testid={testIdPrefix ? `${testIdPrefix}-${step.key}` : undefined}
            >
              <span className={cn("block h-1.5 w-full rounded-full transition-colors", STEP_BAR_FILL[step.state])} />
            </button>
          </Tip>
        </li>
      ))}
    </ol>
  );
}
