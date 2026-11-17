// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Both triggers are tab stops, so an explanation reaches the keyboard as well as the mouse;
 * inside a routed data table row the row is the one tab stop, so they add none (#12070).
 */
const FOCUSABLE = "cursor-help rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus";

/**
 * The "i" beside a field's label that explains it in place: `label` names the
 * icon for assistive tech ("What each run mode does"), `content` is the tooltip.
 * The design system `Icon` takes no focus or role of its own, so a span is the
 * named, focusable trigger and the 16 px glyph inside it is decorative.
 */
export function InfoTip({
  content,
  label,
  className,
}: {
  content: React.ReactNode;
  label: string;
  className?: string;
}) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={content}>
      <span
        tabIndex={tabIndex}
        role="img"
        aria-label={label}
        className={cn("inline-flex shrink-0 text-default-secondary hover:text-default-primary", FOCUSABLE, className)}
      >
        <Icon name="info" size={16} />
      </span>
    </Tip>
  );
}

/**
 * A term that explains itself on hover or focus: a column header, a headline's
 * name, a metric's name. For a label that is not itself the term, use `InfoTip`.
 */
export function InfoLabel({
  content,
  className,
  children,
}: {
  content: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={content}>
      <span tabIndex={tabIndex} className={cn(FOCUSABLE, className)}>
        {children}
      </span>
    </Tip>
  );
}
