// SPDX-License-Identifier: Apache-2.0

/**
 * The visual language every CTEM Cases screen shares (dev-docs/ctem-cases.md
 * section 7.0): one DS Chip severity per state and per priority, owners as the
 * DS Avatar, "Unassigned" in secondary text, never a warning colour.
 */
import { Avatar, Chip } from "@filigran/design-system";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemCasePriority, CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { formatList } from "@/lib/format-list";

type ChipSeverity = "neutral" | "info" | "low" | "medium" | "high" | "critical";

/** One colour per state, everywhere (7.0): alert, info, success, neutral. */
const CASE_STATE_SEVERITY: Record<CtemCaseState, ChipSeverity> = {
  needs_attention: "medium",
  in_progress: "info",
  resolved: "low",
  accepted_risk: "neutral",
};

/** English labels, translated where drawn. */
export const CASE_STATE_LABEL: Record<CtemCaseState, string> = {
  needs_attention: "Needs attention",
  in_progress: "In progress",
  resolved: "Resolved",
  accepted_risk: "Accepted risk",
};

const CASE_PRIORITY_SEVERITY: Record<CtemCasePriority, ChipSeverity> = {
  critical: "critical",
  high: "high",
  medium: "medium",
  low: "low",
};

export const CASE_PRIORITY_LABEL: Record<CtemCasePriority, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** Higher first: the sort value of a priority. */
export const CASE_PRIORITY_RANK: Record<CtemCasePriority, number> = { critical: 4, high: 3, medium: 2, low: 1 };

export function CaseStateChip({ state }: { state: CtemCaseState }) {
  const { t_i18n } = useFormatter();
  return <Chip label={t_i18n(CASE_STATE_LABEL[state])} severity={CASE_STATE_SEVERITY[state]} className="shrink-0" />;
}

export function CasePriorityChip({ priority }: { priority: CtemCasePriority }) {
  const { t_i18n } = useFormatter();
  return <Chip label={t_i18n(CASE_PRIORITY_LABEL[priority])} severity={CASE_PRIORITY_SEVERITY[priority]} className="shrink-0" />;
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
  if (products.length === 0) return { label: t_i18n("No open exposure on your stack"), unread: false };
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
