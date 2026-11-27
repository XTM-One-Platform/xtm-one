// SPDX-License-Identifier: Apache-2.0

import type { CtemCaseOwnerBasis, CtemCaseOwnerTeam } from "@/hooks/api/useCtemCasesApi";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * The owning team of a case (#2577, shown since #2586): the team the routing
 * basis names, while the case still belongs to it (an owner set by hand clears
 * `owner_group_id`, so a stale basis never shows a team).
 */
export function owningTeam(basis: CtemCaseOwnerBasis | null | undefined, ownerGroupId: string | null | undefined): CtemCaseOwnerTeam | null {
  if (!basis || !ownerGroupId) return null;
  if (basis.kind !== "asset_group" && basis.kind !== "primary_asset") return null;
  return basis.team && basis.team.id === ownerGroupId ? basis.team : null;
}

/**
 * The sentences explaining why a case has its owner (pure, #2571): the reason
 * worded from the `owner_basis` data, then, on a fallback, where owners are set.
 * A team owner (#2586) is named as the owner, the person shown as its lead.
 */
export function ownerBasisSentences(basis: CtemCaseOwnerBasis, t: Translate, team: CtemCaseOwnerTeam | null = null): string[] {
  const fallbackHint = t("Set an owner on the asset groups in Inventory to route cases to the teams that fix them.");
  const lead = t("The person shown leads the case for the team, and every member of the team sees it as theirs.");
  switch (basis.kind) {
    case "asset_group":
      if (team) {
        return [
          basis.affected
            ? t("The team {team} owns the asset group {group}, which holds {assets} of the {affected, plural, one {# affected asset} other {# affected assets}}.", {
                team: team.name,
                group: basis.name,
                assets: basis.assets,
                affected: basis.affected,
              })
            : t("The team {team} owns the asset group {group}, which holds {assets, plural, one {# affected asset} other {# affected assets}}.", {
                team: team.name,
                group: basis.name,
                assets: basis.assets,
              }),
          lead,
        ];
      }
      return [
        basis.affected
          ? t("Owner of the asset group {group}, which holds {assets} of the {affected, plural, one {# affected asset} other {# affected assets}}.", {
              group: basis.name,
              assets: basis.assets,
              affected: basis.affected,
            })
          : t("Owner of the asset group {group}, which holds {assets, plural, one {# affected asset} other {# affected assets}}.", {
              group: basis.name,
              assets: basis.assets,
            }),
      ];
    case "primary_asset":
      if (team) {
        return [
          t("The team {team} is accountable for the primary asset {name}, which the case reaches. No affected asset group has an owner.", {
            team: team.name,
            name: basis.name,
          }),
          lead,
        ];
      }
      return [t("Accountable for the primary asset {name}, which the case reaches. No affected asset group has an owner.", { name: basis.name })];
    case "default":
      return [t("No asset group or primary asset owner was found: the instance default owner."), fallbackHint];
    case "decider": {
      const who =
        basis.source === "decide_group"
          ? t("No asset group or primary asset owner was found: a member of a group that decides CTEM.")
          : t("No asset group or primary asset owner was found: an administrator, as no group decides CTEM.");
      const first =
        basis.candidates > 1
          ? [t("The first of {count, plural, one {# candidate} other {# candidates}} by account creation.", { count: basis.candidates })]
          : [];
      return [who, ...first, fallbackHint];
    }
    case "kept":
      return [t("No owner was found: the current owner is kept."), fallbackHint];
    case "person":
      return [t("Set by a person.")];
  }
}
