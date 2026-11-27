// SPDX-License-Identifier: Apache-2.0

/**
 * Why a CTEM case has its owner (#2571): the info button next to the owner in the
 * case header. The backend answers the routing basis as data (`owner_basis`,
 * ctem_case_owner.owner_details) and this file words it in the reader's language,
 * never the sentence stored on the owner event. The other asset group owners who
 * contribute are listed by name. A fallback (instance default, CTEM decider, kept)
 * says where owners are set, so a wrong routing is diagnosable and fixable.
 */
import { Icon } from "@filigran/design-system";
import { InfoPopover } from "@/components/ui/info-popover";
import type { CtemCaseOwnerBasis, CtemCaseOwnerContributor } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { ownerBasisSentences } from "./case-owner-why";

/** The info button next to a case owner; nothing when there is nothing to explain. */
export function CaseOwnerWhy({
  basis,
  contributors,
  personName,
}: {
  basis: CtemCaseOwnerBasis | null | undefined;
  contributors: CtemCaseOwnerContributor[] | undefined;
  personName: (id: string) => string | null;
}) {
  const { t_i18n } = useFormatter();
  const others = contributors ?? [];
  if (!basis && others.length === 0) return null;
  return (
    <InfoPopover
      label={t_i18n("Why this owner")}
      icon={<Icon name="info" size={16} />}
      heading={t_i18n("Why this owner")}
      width={320}
      align="start"
      triggerClassName="inline-flex shrink-0 items-center justify-center text-default-secondary transition-colors hover:text-accent"
      popoverClassName="p-3"
      triggerTestId="ctem-case-owner-why"
      popoverTestId="ctem-case-owner-why-popover"
    >
      <div className="space-y-2 content-compact text-default-secondary">
        {basis && ownerBasisSentences(basis, t_i18n).map((s) => <p key={s}>{s}</p>)}
        {others.length > 0 && (
          <div className="space-y-1">
            <p className="font-medium text-default-primary">{t_i18n("Also involved")}</p>
            <ul className="space-y-0.5">
              {others.map((c) => {
                const name = personName(c.user_id) ?? t_i18n("A person you cannot see");
                return (
                  <li key={c.user_id} data-testid="ctem-case-owner-contributor">
                    {c.group ? t_i18n("{name}, owner of the asset group {group}", { name, group: c.group }) : name}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    </InfoPopover>
  );
}
