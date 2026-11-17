// SPDX-License-Identifier: Apache-2.0

import { DisclosureSummary } from "@/components/ui/disclosure";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * The refusal of a form's save, under its fields: the plain sentence, and the
 * server's own detail behind a "Details" disclosure when it says more. The one
 * refusal box of the fleet dialogs (new fleet, fork, settings), never re-drawn
 * by a page (`option-card.test.ts` fails a copy of its class string).
 */
export function RefusalAlert({
  message,
  detail,
  className,
}: {
  message: string;
  /** The server's wording, shown when it differs from `message`. */
  detail?: string | null;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="alert"
      className={cn(
        "rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 px-3 py-2 content-compact",
        className,
      )}
    >
      <p>{message}</p>
      {detail && detail !== message && (
        <details className="mt-1 text-default-secondary">
          <DisclosureSummary>{t_i18n("Details")}</DisclosureSummary>
          <p className="mt-1 break-words">{detail}</p>
        </details>
      )}
    </div>
  );
}
