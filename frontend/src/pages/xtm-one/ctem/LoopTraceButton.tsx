// SPDX-License-Identifier: Apache-2.0

/**
 * The one "loop trace" action of the case page (#1679 point 6, Sam 24 Nov 2026:
 * the loop trace is a real button, never a bare text link or an icon-only
 * action). A small secondary Button with an icon and a label; its accessible
 * name names the signal's subject so a list of them reads apart.
 *
 * It opens the trace on the page that renders it (Sam, 24 Nov 2026: the case
 * stays underneath, never a jump to the Signals page): it sets the `signal`
 * parameter of the current route, read by `LoopTraceSheet`.
 */
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { useLoopTraceParam } from "./loop/LoopTraceSheet";

export function LoopTraceButton({
  signalId,
  subject,
  compact = false,
  className,
}: {
  signalId: string;
  /** The CVE or subject the trace is about, named in the accessible name. */
  subject: string;
  /** The table row form: the short label "Loop trace". */
  compact?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const trace = useLoopTraceParam();
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn("shrink-0 gap-1.5 whitespace-nowrap", className)}
      aria-label={t_i18n("See the loop trace of {subject}", { subject })}
      data-testid="ctem-case-loop-trace-button"
      onClick={() => trace.open(signalId)}
    >
      <Icon name="list-tree" size={16} aria-hidden />
      <span>{compact ? t_i18n("Loop trace") : t_i18n("See the loop trace")}</span>
    </Button>
  );
}
