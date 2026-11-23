// SPDX-License-Identifier: Apache-2.0

/**
 * "Opened by" on a case page (CTEM-03-UI, #892; dev-docs/ctem-loop-visibility.md
 * sections 3 and 4): the signals that opened or joined the case, each with its
 * lens, its trigger path, its source (CTEM-29-UI), the scope gate's reason in one sentence, and a
 * link to its loop trace (`?loop=signal:<id>`). Read through
 * `GET /ctem-signals?case_id=`, which applies the reader's visibility. A case
 * is opened only by a signal or split by a person or an agent (#1529): a case
 * with no signal says which, and one a source read built before the readers
 * recorded signals is labelled legacy.
 */
import { Link as RouterLink } from "react-router-dom";
import { Link } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCtemCaseSignals } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatDayIntl } from "@/lib/relative-time";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { NO_SIGNAL_ORIGIN_LABEL, noSignalOrigin } from "./case-loop-reason";
import { decisionSentence } from "./loop/DecisionReason";
import { signalProviderName } from "./loop/SignalLoopTrace";
import { signalsHref } from "./loop/signals-route";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };

export function CaseOpenedBy({ caseId, history }: { caseId: string; history?: readonly CtemCaseEvent[] }) {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError } = useCtemCaseSignals(caseId);
  const signals = data?.signals ?? [];

  return (
    <Card data-testid="ctem-case-opened-by">
      <CardHeader>
        <CardTitle>{t_i18n("Opened by")}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && !data ? (
          <Skeleton className="h-10 w-full" />
        ) : isError && !data ? (
          <p className="content-base text-default-secondary">{t_i18n("The signals of this case could not be read.")}</p>
        ) : signals.length === 0 ? (
          <p className="content-base text-default-secondary" data-testid="ctem-case-opened-by-origin">
            {t_i18n(NO_SIGNAL_ORIGIN_LABEL[noSignalOrigin(history)])}
          </p>
        ) : (
          <ul className="space-y-3">
            {signals.map((s) => (
              <li key={s.id} className="flex min-w-0 flex-col gap-0.5 content-base" data-testid="ctem-case-opened-by-signal">
                <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{t_i18n(s.trigger_path_label || s.trigger_path)}</span>
                  <span className="text-default-secondary">{t_i18n(LENS_LABEL[s.lens] ?? s.lens)}</span>
                  {/* Where the signal came from (CTEM-29-UI): the leak source, OpenCTI for a supplier report. */}
                  <span className="text-default-secondary" data-testid="ctem-case-opened-by-source">
                    {t_i18n("From {source}", { source: signalProviderName(s.source.provider) })}
                  </span>
                  <span className="min-w-0 truncate text-default-secondary" title={s.subject.label || s.subject.ref}>
                    {s.subject.label || s.subject.ref}
                  </span>
                  <Link asChild className="ml-auto whitespace-nowrap">
                    <RouterLink to={signalsHref({ signal: s.id })}>{t_i18n("See the loop trace")}</RouterLink>
                  </Link>
                </span>
                <span className="text-default-secondary">
                  {decisionSentence(s.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
