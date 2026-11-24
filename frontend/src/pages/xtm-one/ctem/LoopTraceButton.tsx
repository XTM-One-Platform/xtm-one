// SPDX-License-Identifier: Apache-2.0

/**
 * The one "loop trace" action of the case page (#1679 point 6, Sam 24 Nov 2026:
 * the loop trace is a real button, never a bare text link or an icon-only
 * action). A small secondary Button with an icon and a label, rendered as a
 * router link to `/ctem/signals?signal=<id>`; its accessible name names the
 * signal's subject so a list of them reads apart.
 */
import { Link as RouterLink } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { signalsHref } from "./loop/signals-route";

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
  return (
    <Button asChild variant="outline" size="sm" className={cn("shrink-0 gap-1.5 whitespace-nowrap", className)}>
      <RouterLink
        to={signalsHref({ signal: signalId })}
        aria-label={t_i18n("See the loop trace of {subject}", { subject })}
        data-testid="ctem-case-loop-trace-button"
      >
        <Icon name="list-tree" size={16} aria-hidden />
        <span>{compact ? t_i18n("Loop trace") : t_i18n("See the loop trace")}</span>
      </RouterLink>
    </Button>
  );
}
