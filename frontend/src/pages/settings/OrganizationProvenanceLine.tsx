// SPDX-License-Identifier: Apache-2.0

/**
 * Where a profile field's value came from (#1773): the provenance the current version
 * keeps for it (an agent's accepted proposal: its source and basis, and when the value
 * was confirmed unchanged). Nothing is drawn when the version holds none for the field.
 */

import { useFormatter } from "@/i18n";
import { provenanceOf } from "@/lib/organization-reference";

export function OrganizationProvenanceLine({ sources, field }: { sources: Record<string, unknown> | null | undefined; field: string }) {
  const { t_i18n } = useFormatter();
  const provenance = provenanceOf(sources, field);
  if (!provenance) return null;
  const confirmed = provenance.confirmed_at ? provenance.confirmed_at.slice(0, 10) : "";
  return (
    <p className="content-compact text-default-secondary break-words" data-testid={`settings.organization.provenance.${field}`}>
      {confirmed
        ? t_i18n("Confirmed on {date} by {source}: {basis}", { date: confirmed, source: provenance.source || t_i18n("an agent"), basis: provenance.basis || t_i18n("no basis given") })
        : t_i18n("Set by {source}: {basis}", { source: provenance.source || t_i18n("an agent"), basis: provenance.basis || t_i18n("no basis given") })}
    </p>
  );
}
