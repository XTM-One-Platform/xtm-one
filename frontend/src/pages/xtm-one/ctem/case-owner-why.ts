// SPDX-License-Identifier: Apache-2.0

import type { CtemCaseOwnerBasis } from "@/hooks/api/useCtemCasesApi";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * The sentences explaining why a case has its owner (pure, #2571): the reason
 * worded from the `owner_basis` data, then, on a fallback, where owners are set.
 */
export function ownerBasisSentences(basis: CtemCaseOwnerBasis, t: Translate): string[] {
  const fallbackHint = t("Set an owner on the asset groups in Inventory to route cases to the teams that fix them.");
  switch (basis.kind) {
    case "asset_group":
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
