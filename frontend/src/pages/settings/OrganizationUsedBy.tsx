// SPDX-License-Identifier: Apache-2.0

/**
 * "Used by" on a part of the organization profile (#9173, S2): the fleets
 * whose documented prerequisites need that part, then the seeded agents whose
 * persona reads it (#10063 r1 A3), both read from
 * GET /organization-context/readers (never a hand-written list). A fleet links
 * to its About view (`fleetAboutPath`, the fleet path: About is the page with no tab), an agent to its page;
 * both through the router (`<Link to>`, so the base path applies and the app
 * is not reloaded), with the shared focus ring (`BUTTON_ROLE_FOCUS_RING`,
 * offset in the surface colour) and a trailing arrow. Nothing is shown when
 * no visible fleet or agent reads the part.
 *
 * Both lines share ONE grid (#10166 UX r1 B1): labels in a max-content first
 * column, chip lists in the second, so the chips start at the same x on
 * every line of every card and at every width. Each line is a
 * `display: contents` row, so its label and list are cells of that grid.
 * Every label cell stacks both labels in one grid area, the other one
 * invisible and hidden from assistive tech, so the label column has the same
 * width on a card that shows only one of the lines.
 *
 * Vertical alignment (#10166 UX r2 B1): each label is laid out in the same
 * box as a chip (`CHIP_ROW_BOX`: the Badge's 1 px border, transparent here,
 * and its 2 px vertical padding, at the same content-base line height) and
 * the grid aligns its cells at the start, so the label text sits on the
 * text line of the first chip row on every card, at every width.
 */

import { Link } from "react-router-dom";
import { ArrowRight, Bot, Network } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { fleetAboutPath } from "@/pages/agent-detail/agent-fleet-card";
import type { OrganizationAgentReader, OrganizationFieldReader } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";

interface Props {
  /** Profile fields the part covers ("sector", "regions", "crown_jewels", ...). */
  fields: readonly string[];
  readers: Record<string, OrganizationFieldReader[]> | undefined;
  agents?: Record<string, OrganizationAgentReader[]> | undefined;
}

/** The Badge's vertical box (border + py-0.5), shared by the labels so they centre on the first chip row. */
const CHIP_ROW_BOX = "border border-transparent py-0.5";

interface Reader {
  id: string;
  name: string;
}

/** The readers of any of *fields*, once each, in the order the API lists them. */
function readersOf<T>(fields: readonly string[], byField: Record<string, T[]> | undefined, key: (row: T) => Reader): Reader[] {
  const seen = new Set<string>();
  const out: Reader[] = [];
  for (const field of fields) {
    for (const row of byField?.[field] ?? []) {
      const reader = key(row);
      if (seen.has(reader.id)) continue;
      seen.add(reader.id);
      out.push(reader);
    }
  }
  return out;
}

function ReaderChips({ items, to, label }: { items: Reader[]; to: (id: string) => string; label: string }) {
  return (
    <ul className="flex min-w-0 flex-wrap gap-1.5" aria-label={label} data-cell="chips">
      {items.map((item) => (
        <li key={item.id} className="max-w-full">
          <Badge variant="secondary" className="max-w-full font-normal content-base" title={item.name} data-box="chip-row">
            <Link
              to={to(item.id)}
              className={cn("group flex min-w-0 items-center gap-1 rounded-sm underline-offset-2 hover:text-accent hover:underline focus-visible:text-accent", BUTTON_ROLE_FOCUS_RING)}
            >
              <span className="truncate">{item.name}</span>
              <ArrowRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            </Link>
          </Badge>
        </li>
      ))}
    </ul>
  );
}

/** The label cell: both labels in one grid area, only *active* visible. */
function ReaderLabel({ active }: { active: "fleets" | "agents" }) {
  const { t_i18n } = useFormatter();
  const labels = [
    { key: "fleets", Icon: Network, text: t_i18n("Used by") },
    { key: "agents", Icon: Bot, text: t_i18n("Read by agents") },
  ] as const;
  return (
    <span className="col-start-1 grid text-default-secondary" data-cell="label">
      {labels.map(({ key, Icon, text }) => (
        <span
          key={key}
          className={cn("col-start-1 row-start-1 flex items-center gap-1", CHIP_ROW_BOX, key !== active && "invisible")}
          aria-hidden={key !== active ? "true" : undefined}
          data-label={key}
          data-box="chip-row"
        >
          <Icon className="h-4 w-4" aria-hidden="true" />
          {text}
        </span>
      ))}
    </span>
  );
}

export function OrganizationUsedBy({ fields, readers, agents }: Props) {
  const { t_i18n } = useFormatter();
  const fleets = readersOf(fields, readers, (row) => ({ id: row.fleet_id, name: row.name }));
  const agentReaders = readersOf(fields, agents, (row) => ({ id: row.agent_id, name: row.name }));
  if (!fleets.length && !agentReaders.length) return null;
  return (
    <div
      className="mb-3 grid grid-cols-[max-content_minmax(0,1fr)] items-start gap-x-3 gap-y-1.5 content-base"
      data-testid="settings.organization.used-by"
      data-layout="label-grid"
      data-align="start"
      data-fields={fields.join(" ")}
    >
      {fleets.length > 0 && (
        <div className="contents" data-row="contents" data-testid="settings.organization.used-by.fleets">
          <ReaderLabel active="fleets" />
          <ReaderChips items={fleets} to={fleetAboutPath} label={t_i18n("Fleets that read this part of the profile")} />
        </div>
      )}
      {agentReaders.length > 0 && (
        <div className="contents" data-row="contents" data-testid="settings.organization.used-by.agents">
          <ReaderLabel active="agents" />
          <ReaderChips items={agentReaders} to={(id) => `/agents/${id}`} label={t_i18n("Agents that read this part of the profile")} />
        </div>
      )}
    </div>
  );
}
