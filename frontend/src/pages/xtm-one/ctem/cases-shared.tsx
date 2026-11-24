// SPDX-License-Identifier: Apache-2.0

/**
 * The visual language every CTEM Cases screen shares (dev-docs/ctem-cases.md
 * section 7.0): one hot tone per state and per priority (the shared outlined
 * ToneBadge, the hot hues of Work items and Tasks, #1267), owners as the DS Avatar,
 * "Unassigned" in secondary text, never a warning colour.
 */
import { Avatar, Icon } from "@filigran/design-system";
import { type HotTone, ToneBadge } from "@/components/ui/tone-badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemCasePriority, CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { formatList } from "@/lib/format-list";

/**
 * One colour per status, everywhere (7.0, #1267, #1503), from the six hot tones:
 * new orange, waiting for decision red (a person is the blocker), the three
 * statuses agents work in (analyzing, remediating, validating) blue, mitigated,
 * watching yellow (still watched, with the DS eye icon), resolved green,
 * accepted risk neutral.
 */
const CASE_STATE_TONE: Record<CtemCaseState, HotTone> = {
  new: "orange",
  analyzing: "blue",
  waiting_decision: "red",
  remediating: "blue",
  validating: "blue",
  mitigated_watching: "yellow",
  resolved: "green",
  accepted_risk: "neutral",
};

/** English labels, translated where drawn. */
export const CASE_STATE_LABEL: Record<CtemCaseState, string> = {
  new: "New",
  analyzing: "Analyzing",
  waiting_decision: "Waiting for decision",
  remediating: "Remediating",
  validating: "Validating",
  mitigated_watching: "Mitigated, watching",
  resolved: "Resolved",
  accepted_risk: "Accepted risk",
};

/** One colour per priority: critical red, high orange, medium yellow, low green. */
const CASE_PRIORITY_TONE: Record<CtemCasePriority, HotTone> = {
  critical: "red",
  high: "orange",
  medium: "yellow",
  low: "green",
};

export const CASE_PRIORITY_LABEL: Record<CtemCasePriority, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** Higher first: the sort value of a priority. */
export const CASE_PRIORITY_RANK: Record<CtemCasePriority, number> = { critical: 4, high: 3, medium: 2, low: 1 };

/**
 * The DS icon a state carries inside its badge, before the label, so the
 * badge keeps the left edge and baseline of its siblings (UX round 2 of #857,
 * B1).
 */
const CASE_STATE_ICON: Partial<Record<CtemCaseState, "eye">> = { mitigated_watching: "eye" };

export function CaseStateChip({ state }: { state: CtemCaseState }) {
  const { t_i18n } = useFormatter();
  const icon = CASE_STATE_ICON[state];
  return (
    <ToneBadge tone={CASE_STATE_TONE[state]} icon={icon ? <Icon name={icon} size={16} aria-hidden /> : undefined}>
      {t_i18n(CASE_STATE_LABEL[state])}
    </ToneBadge>
  );
}

export function CasePriorityChip({ priority }: { priority: CtemCasePriority }) {
  const { t_i18n } = useFormatter();
  return <ToneBadge tone={CASE_PRIORITY_TONE[priority]}>{t_i18n(CASE_PRIORITY_LABEL[priority])}</ToneBadge>;
}

/**
 * The sentence of an empty Cases list or board (#12139): "No open exposure"
 * is an all-clear only when every product answered the read. When the list
 * carries `details_unavailable`, the sentence names the products that could
 * not be read instead (by display name, with an ICU plural), and `unread`
 * lets the board draw a warning icon.
 */
export function useCasesEmptyLabel(detailsUnavailable: readonly string[] | undefined): { label: string; unread: boolean } {
  const { t_i18n, intl } = useFormatter();
  const products = detailsUnavailable ?? [];
  if (products.length === 0) return { label: t_i18n("No open exposure on your software"), unread: false };
  // The API sends product ids ("openaev"): show their display names.
  const names = products.map((p) => CTEM_PRODUCT_INFO[p]?.name ?? p);
  return {
    label: t_i18n(
      "{count, plural, one {{products} could not be read, so open exposures there may be missing.} other {{products} could not be read, so open exposures there may be missing.}}",
      { products: formatList(names, intl.locale), count: names.length },
    ),
    unread: true,
  };
}

/** The owner as the DS Avatar and their name, or "Unassigned" in secondary text. */
export function CaseOwner({ name }: { name: string | null | undefined }) {
  const { t_i18n } = useFormatter();
  if (!name) return <span className="text-default-secondary">{t_i18n("Unassigned")}</span>;
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Avatar name={name} size="sm" />
      <TruncatedText text={name} />
    </span>
  );
}
