// SPDX-License-Identifier: Apache-2.0

/**
 * The parts every sync preview shares (#1982 S4, UX round 1 on #2020): the list of the rows a run
 * adds, creates or removes (the first rows, then "+N more", in the dialog's one scroller, each cut
 * name named whole by the design-system tooltip), and the error of a preview (its cause, a Try
 * again for a read that failed, the reason of a refused pairing without any "try again").
 * Mounted by the Add a sync stepper (OrganizationImportDialog) and the Edit dialog (OrganizationSyncEditDialog).
 */
import { Button } from "@/components/ui/button";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";

/**
 * The reasons GET /syncs/directions and POST /syncs/outbound-preview give for a pairing without outbound
 * (backend DIRECTIONS of organization_context_outbound.py), listed so the catalogs translate them; an unknown
 * one shows as sent.
 * @i18n-keys
 */
const OUTBOUND_REASONS = {
  openaev_asset_groups: "Planned: OpenAEV asset group writes are not wired for syncs yet.",
  opencti_pirs: "Planned: OpenCTI PIR writes are not wired for syncs yet.",
  opencti_assets: "OpenCTI Systems are owned by its connectors.",
  openaev_software: "OpenAEV software inventory is read from its agents.",
  other: "This pairing reads only.",
} as const;

const KNOWN_REASONS: readonly string[] = Object.values(OUTBOUND_REASONS);

export const reasonLabel = (t_i18n: (key: string) => string, reason: string): string => (KNOWN_REASONS.includes(reason) ? t_i18n(reason) : reason);

/** Rows listed before the "+N more" line: the dialog keeps one scroller (design standards s.3). */
const SHOWN_ROWS = 8;

interface RowListProps {
  rows: string[];
  /** The number of rows the list stands for, when the rows are a sample of them; the rows' own count by default. */
  total?: number;
  testId?: string;
}

/** The first rows of a preview, each cut name named by the DS tooltip, then "+N more" for the rest. */
export function SyncRowList({ rows, total, testId }: RowListProps) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat();
  const shown = rows.slice(0, SHOWN_ROWS);
  const more = Math.max((total ?? rows.length) - shown.length, 0);
  return (
    <ul className="space-y-0.5 content-compact text-default-primary" data-testid={testId}>
      {shown.map((name, index) => (
        <li key={`${index}-${name}`} className="min-w-0">
          <TruncatedText text={name} />
        </li>
      ))}
      {more > 0 && (
        <li className="text-default-secondary" data-testid={testId ? `${testId}.more` : undefined}>
          {t_i18n("{count, plural, one {+{shown} more row} other {+{shown} more rows}}", { count: more, shown: f.count(more) })}
        </li>
      )}
    </ul>
  );
}

/** An API error as lib/api throws it: the HTTP status and the backend's detail. */
type RequestError = Error & { status?: number; detail?: unknown };

interface PreviewErrorProps {
  platform: string;
  /** The preview request failed: a refusal (422) carries the reason of the pairing; anything else can be tried again. */
  requestError?: RequestError | null;
  /** The request answered, but the platform could not be read. */
  readFailed?: boolean;
  /** The cause of that read failure, as the backend words it. */
  cause?: string | null;
  onRetry?: () => void;
  retrying?: boolean;
  testId?: string;
}

/** Why a preview could not be made, and what to do: Try again for a failed read, the reason alone for a refusal. */
export function SyncPreviewError({ platform, requestError, readFailed = false, cause, onRetry, retrying = false, testId }: PreviewErrorProps) {
  const { t_i18n } = useFormatter();
  if (!requestError && !readFailed) return null;
  const refusal = requestError?.status === 422 ? (typeof requestError.detail === "string" ? requestError.detail : requestError.message) : null;
  if (refusal)
    return (
      <div className="space-y-1" data-testid={testId ? `${testId}.refused` : undefined}>
        <p className="content-compact text-feedback-error-primary">{t_i18n("{platform} does not take this sync. Nothing will be saved.", { platform })}</p>
        <p className="content-compact text-default-secondary">{t_i18n("Reason: {reason}", { reason: reasonLabel(t_i18n, refusal) })}</p>
      </div>
    );
  return (
    <div className="space-y-2" data-testid={testId}>
      <p className="content-compact text-feedback-error-primary">
        {readFailed ? t_i18n("{platform} could not be read. Nothing will be saved.", { platform }) : t_i18n("The preview could not be made. Nothing will be saved.")}
      </p>
      {readFailed && cause && <p className="content-compact text-default-secondary">{t_i18n("Reason: {reason}", { reason: cause })}</p>}
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry} disabled={retrying} aria-busy={retrying || undefined}>
          {t_i18n("Try again")}
        </Button>
      )}
    </div>
  );
}
