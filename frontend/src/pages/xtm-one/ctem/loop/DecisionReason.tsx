// SPDX-License-Identifier: Apache-2.0

/**
 * The why of a loop decision, the same component everywhere it shows
 * (dev-docs/ctem-loop-visibility.md section 3): the state in words as a DS
 * Chip, one sentence built from the decision record (never stored in
 * English, never written by a model at read time), and the inputs the rule
 * read. No decision yet reads "Waiting for reconcile"; a reason code this
 * version does not know keeps its chip and says so, never an empty cell.
 */
import { Chip } from "@filigran/design-system";
import type { CtemDecisionRecord, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";

type ChipSeverity = "neutral" | "info" | "low" | "medium" | "high" | "critical";

/** English labels of the signal states, translated where drawn. */
export const SIGNAL_STATE_LABEL: Record<CtemSignalState, string> = {
  new: "New",
  confirmed: "Confirmed",
  to_validate: "To validate",
  discarded: "Discarded",
  watchlist: "Watchlist",
  case_opened: "Case opened",
};

const SIGNAL_STATE_SEVERITY: Record<CtemSignalState, ChipSeverity> = {
  new: "info",
  confirmed: "info",
  to_validate: "medium",
  discarded: "neutral",
  watchlist: "medium",
  case_opened: "high",
};

/**
 * The sentence of each reason code the backend writes, with its ICU params.
 * Each backend item that adds a code (CTEM-02 reconcile and scope gate, CTEM-03
 * cases) adds its sentence here and in the four catalogs.
 */
const REASON_SENTENCE: Record<string, string> = {};

export function SignalStateChip({ state }: { state: CtemSignalState }) {
  const { t_i18n } = useFormatter();
  return <Chip label={t_i18n(SIGNAL_STATE_LABEL[state] ?? state)} severity={SIGNAL_STATE_SEVERITY[state] ?? "neutral"} className="shrink-0" />;
}

/** The sentence of a decision record, or the waiting and unknown-code wordings. */
export function decisionSentence(
  decision: CtemDecisionRecord | null | undefined,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): string {
  if (!decision?.reason_code) return t_i18n("Waiting for reconcile");
  const sentence = REASON_SENTENCE[decision.reason_code];
  return sentence ? t_i18n(sentence, decision.reason_params ?? {}) : t_i18n("Reason not available in this version");
}

export function DecisionReason({
  state,
  decision,
  showInputs = false,
}: {
  state: CtemSignalState;
  decision: CtemDecisionRecord | null | undefined;
  /** The loop trace lists the inputs; a row keeps the chip and the sentence. */
  showInputs?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const inputs = decision?.inputs ?? [];
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid="ctem-decision-reason">
      <div className="flex min-w-0 items-center gap-2">
        <SignalStateChip state={state} />
        <span className="content-compact text-default-secondary truncate">{decisionSentence(decision, t_i18n)}</span>
      </div>
      {showInputs && inputs.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption">
          {inputs.map((input) => (
            <div key={input.name} className="contents">
              <dt className="text-default-secondary">{input.name}</dt>
              <dd className="min-w-0 truncate">
                {String(input.value ?? "--")}
                {input.source ? <span className="text-default-secondary"> ({input.source})</span> : null}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
