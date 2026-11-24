// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Exposure: "Proposed changes" (CTEM-30, #937; UX
 * round 1 B2 on #941). The field changes proposed outside the form (the loop
 * trace's "Propose scope threshold" on a watchlisted signal, a chat proposal)
 * wait here for an administrator: each row names the field, the current and
 * the proposed value and why; Accept saves the next profile version, Reject
 * drops it. Nothing changes until then. Shown only when one is pending.
 * Tracked organization drafts have their own review page and are not listed.
 */
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { SettingsCard } from "@/components/ui/settings-form";
import {
  type OrganizationContextFieldProposal,
  useDecideOrganizationContextProposal,
  useOrganizationContextProposals,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";

/** The fields a proposal may name outside the form contract labels. @i18n-keys */
const PROPOSED_FIELD_LABEL = {
  "prioritization_policy.scope_min_criticality": "Scope threshold",
} as const;
const PROPOSED_FIELD_LABELS: Readonly<Record<string, string>> = PROPOSED_FIELD_LABEL;

/** The fields whose value is a criticality level. */
const LEVEL_FIELDS = new Set(["prioritization_policy.scope_min_criticality"]);

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="list-checks" size={16} aria-hidden />
    </span>
  );
}

export function OrganizationProposalsCard() {
  const { t_i18n } = useFormatter();
  const { data } = useOrganizationContextProposals();
  const decide = useDecideOrganizationContextProposal();
  const rows = (data ?? []).filter((p) => p.kind === "fields" && p.status === "pending");
  if (rows.length === 0) return null;

  const value = (entry: OrganizationContextFieldProposal, raw: unknown): string => {
    if (raw == null || raw === "") return t_i18n("Not set");
    if (LEVEL_FIELDS.has(entry.field) && typeof raw === "string" && raw in CRITICALITY_LABELS) {
      return t_i18n(CRITICALITY_LABELS[raw as keyof typeof CRITICALITY_LABELS]);
    }
    return typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean" ? String(raw) : JSON.stringify(raw);
  };
  const fieldLabel = (field: string) => (PROPOSED_FIELD_LABELS[field] ? t_i18n(PROPOSED_FIELD_LABELS[field]) : field);

  return (
    <SettingsCard
      data-testid="settings.organization.proposals"
      icon={CardIcon}
      title={t_i18n("Proposed changes")}
      meta={t_i18n("{count, plural, one {# waits for a decision} other {# wait for a decision}}", { count: rows.length })}
      description={t_i18n("Changes proposed outside this form. Nothing changes until you accept one.")}
    >
      <ul className="divide-y divide-[var(--border-elevation-subtle)]">
        {rows.map((proposal) => (
          <li key={proposal.id} className="flex min-w-0 flex-col gap-2 py-3 first:pt-0" data-testid="settings.organization.proposal">
            {proposal.proposals.map((entry) => (
              <div key={entry.field} className="min-w-0 content-compact">
                <div className="font-medium">{fieldLabel(entry.field)}</div>
                <div className="text-default-secondary">
                  {t_i18n("From {current} to {proposed}", { current: value(entry, entry.current), proposed: value(entry, entry.value) })}
                </div>
                {entry.basis && <div className="content-compact text-default-secondary">{entry.basis}</div>}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                disabled={decide.isPending}
                onClick={() => decide.mutate({ id: proposal.id, accept: proposal.proposals.map((entry) => entry.field) })}
              >
                {t_i18n("Accept")}
              </Button>
              <Button size="sm" variant="secondary" disabled={decide.isPending} onClick={() => decide.mutate({ id: proposal.id, accept: [] })}>
                {t_i18n("Reject")}
              </Button>
              {decide.isError && decide.variables?.id === proposal.id && (
                <span className="content-compact text-feedback-error-primary" role="alert">
                  {t_i18n("The decision could not be saved")}
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </SettingsCard>
  );
}
