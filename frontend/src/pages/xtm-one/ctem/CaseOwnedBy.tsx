// SPDX-License-Identifier: Apache-2.0

/**
 * The fleet that runs a case (#1390): "Owned by <fleet>" linked to the fleet
 * page, or "No active fleet owns this case" in secondary text when the API
 * names no owner or an owner whose case automation is off. The owner is the
 * one `GET /ctem-cases/{id}` computes (`owner_fleet`), never guessed here.
 *
 * The linked label and its icon read in the product primary in every state: the
 * design-system Link paints a visited link in the tonic accent (turquoise), which
 * turned the header label green next to the purple chips once the fleet page had
 * been opened (#1559). Hover keeps the library's brand tertiary.
 */
import { Link as RouterLink } from "react-router-dom";
import { Icon, Link } from "@filigran/design-system";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemCaseOwnerFleet } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { ownerFleetId, type LoopFleet } from "./case-loop-reason";

export function CaseOwnedBy({
  owner,
  fleets,
  caseId,
}: {
  owner: CtemCaseOwnerFleet | null | undefined;
  fleets: readonly LoopFleet[] | undefined;
  /** The case the fleet page links back to. */
  caseId: string;
}) {
  const { t_i18n } = useFormatter();
  // Absent on an API older than #1396: draw nothing rather than a false "no owner".
  if (owner === undefined) return null;
  if (!owner || !owner.active) {
    return (
      <span className="content-caption text-default-secondary" data-testid="ctem-case-owner-fleet">
        {t_i18n("No active fleet owns this case")}
      </span>
    );
  }
  const fleetId = ownerFleetId(owner.slug, fleets);
  const label = t_i18n("Owned by {name}", { name: owner.name });
  return (
    <span className="flex min-w-0 items-center gap-1 content-caption text-default-primary" data-testid="ctem-case-owner-fleet">
      <Icon name="network" size={16} className={fleetId ? "shrink-0 text-filigran-brand-primary" : "shrink-0"} aria-hidden />
      {fleetId ? (
        <Link asChild size="compact" className="min-w-0 visited:text-filigran-brand-primary">
          <RouterLink to={`/fleets/${fleetId}`} state={{ backTo: `/ctem/cases/${caseId}`, backLabel: "Back to Case" }}>
            <TruncatedText text={label} />
          </RouterLink>
        </Link>
      ) : (
        <TruncatedText text={label} />
      )}
    </span>
  );
}
