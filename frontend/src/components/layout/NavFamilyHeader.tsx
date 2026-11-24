// SPDX-License-Identifier: Apache-2.0

/**
 * The header of a page that belongs to a nav family (#9301 N3, UX r2 B1 and
 * r3 A1/A2): the family's name as the one h1, the member page's help in its
 * "i", then ONE `DetailTabs` row of the family's members, each tab a link to
 * its own route. Never a second tab row under it: a member with tabs of its
 * own (Analytics' Model usage / Outcomes) is flattened into the family.
 *
 * The members come from the resolved nav model (`useNavFamily`), so the row
 * lists exactly what the reader's menu lists. A reader who sees one member
 * only (a group manager on Model usage) gets the page's own title and no tab
 * row. Detail routes (`/observability/traces/:id`) do not render this: they light the
 * family in the menu and keep their own tabs.
 *
 * Every member page renders it first in the same root (`space-y-6`, no own
 * padding) and it keeps the Settings spacing (`space-y-6`), so switching tabs
 * never moves the header (UX r1 B1/B2 on #10409; guarded in nav-model.test.ts).
 */
import type { ReactNode } from "react";
import { useFormatter } from "@/i18n";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useNavFamily } from "./use-nav-group";

export function NavFamilyHeader({
  to,
  title,
  infoLabel,
  info,
  actions,
}: {
  /** The member page's route, as the family declares it. */
  to: string;
  /** The page's own title, shown when the reader sees no family. */
  title: string;
  infoLabel: string;
  info: ReactNode;
  actions?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const family = useNavFamily(to);
  const members = family?.family ?? [];
  return (
    <div className="space-y-6">
      <ListPageHeader title={t_i18n(family ? family.label : title)} infoLabel={infoLabel} info={info} actions={actions} />
      {members.length > 1 && (
        <DetailTabs
          tabs={members.map((member) => ({ key: member.to, label: member.label, glyph: member.icon, to: member.to }))}
          active={to}
        />
      )}
    </div>
  );
}
