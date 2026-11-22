/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The capability slots of a fleet's home members (#914, slice 4), on the
 * fleet page's Members view: each role's slots (ticketing, notification,
 * EDR/XDR, scanner...) filled, empty or unavailable, and binding a connected
 * integration to a slot in one click (`GET /fleets/{id}/capability-slots`,
 * `POST|DELETE /fleets/{id}/members/{member}/slots/{capability}`).
 *
 * On a built-in fleet a binding is the customer layer: kept by every release,
 * while the fleet's members, handovers and gates stay locked. The card is not
 * shown when no member's role declares a slot.
 *
 * Capabilities first (#1128): the card opens expanded on the shared DataTable,
 * one row per member x slot (capability icon and label, member and role,
 * status, bound integrations with their provider logo, the bind action),
 * searchable, filtered by status and capability, Not bound first, paged by
 * the table so the Members table below stays close. Pure logic:
 * `capability-slots-table.ts`. Also embedded by the CTEM loop setup dialog:
 * its props stay `{ fleet }`.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { InfoTip } from "@/components/ui/info-tip";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useBindFleetSlot, useFleetCapabilitySlots, useUnbindFleetSlot, type Fleet } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import {
  CAPABILITIES_OPEN_BY_DEFAULT,
  SLOT_STATE_LABELS,
  SLOT_STATE_VARIANTS,
  capabilityIcon,
  capabilityOptions,
  capabilitySlotRows,
  slotCounts,
  slotRowSearchFields,
  slotStateRank,
  type CapabilitySlotRow,
} from "./capability-slots-table";
import { fleetRefusalMessage } from "./fleet-refusals";

/** Ten rows a page: the Members table stays one short scroll away (no nested scroll box). */
const SLOTS_PAGE_SIZE = 10;

export function FleetCapabilitySlotsCard({ fleet }: { fleet: Fleet }) {
  const { t_i18n, t_text } = useFormatter();
  const { toast } = useToast();
  const { data, isLoading, error } = useFleetCapabilitySlots(fleet.id);
  const [open, setOpen] = useState(CAPABILITIES_OPEN_BY_DEFAULT);
  const bind = useBindFleetSlot(fleet.id);
  const unbind = useUnbindFleetSlot(fleet.id);
  if (isLoading || error || !data || data.members.length === 0) return null;
  const rows = capabilitySlotRows(data.members);
  if (rows.length === 0) return null;
  const { connected, total } = slotCounts(data.members);
  const busy = bind.isPending || unbind.isPending;
  const refused = (err: Error) =>
    toast({ title: t_i18n("The binding could not be changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
  const help = fleet.is_builtin
    ? t_i18n("Connect your tools to each member. Your bindings are kept by every release; the members and their handovers stay the product's.")
    : t_i18n("Connect your tools to each member.");
  const integrationName = (integration: { name: string; provider: string }) => integration.name || integration.provider;

  const columns: DataTableColumn<CapabilitySlotRow>[] = [
    {
      key: "capability",
      header: "Capability",
      sortValue: (row) => t_text(row.slot.label),
      csv: (row) => t_text(row.slot.label),
      cell: (row) => (
        <span className="flex min-w-0 items-center gap-2">
          <Icon name={capabilityIcon(row.slot.capability)} size={16} className="shrink-0 text-default-secondary" aria-hidden />
          <span className="truncate">{t_text(row.slot.label)}</span>
        </span>
      ),
    },
    {
      key: "member",
      header: "Member",
      sortValue: (row) => row.member.name,
      csv: (row) => row.member.name,
      cell: (row) => (
        <span className="block min-w-0">
          <span className="block max-w-[240px] truncate font-medium">{row.member.name || t_i18n("Unknown agent")}</span>
          {row.member.role_label && <span className="block max-w-[240px] truncate content-caption text-default-secondary">{row.member.role_label}</span>}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortValue: slotStateRank,
      csv: (row) => t_i18n(SLOT_STATE_LABELS[row.slot.state]),
      cell: (row) => (
        <Badge variant={SLOT_STATE_VARIANTS[row.slot.state]} className="whitespace-nowrap">
          {t_i18n(SLOT_STATE_LABELS[row.slot.state])}
        </Badge>
      ),
    },
    {
      key: "integration",
      header: "Integration",
      hideable: true,
      interactive: true,
      csv: (row) => row.slot.bound.map(integrationName).join(", "),
      cell: (row) => (
        <span className="flex min-w-0 flex-wrap items-center gap-1.5">
          {row.slot.bound.map((integration) => (
            <span
              key={integration.integration_id}
              className="group inline-flex max-w-[220px] items-center gap-1.5 rounded-sm bg-elevation-hover px-1.5 py-0.5"
              data-testid={`slot-bound-${row.key}`}
            >
              <ProviderLogo provider={integration.provider} className="h-4 w-4 shrink-0" decorative />
              <span className="truncate" title={integrationName(integration)}>
                {integrationName(integration)}
              </span>
              {row.member.can_edit && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                  disabled={busy}
                  aria-label={t_i18n("Unbind {name}", { name: integrationName(integration) })}
                  onClick={() =>
                    unbind.mutate(
                      { memberId: row.member.member_id, capability: row.slot.capability, integrationId: integration.integration_id },
                      { onError: refused },
                    )
                  }
                >
                  <Icon name="x" size={16} aria-hidden />
                </Button>
              )}
            </span>
          ))}
        </span>
      ),
    },
  ];

  // The bind action of a row: its last column, which never opens the row (the member's agent).
  const bindAction = (row: CapabilitySlotRow) => {
        if (!row.member.can_edit) return null;
        if (row.slot.candidates.length > 0)
          return (
            <SearchableSelect
              aria-label={t_i18n("Bind an integration to {slot}", { slot: t_text(row.slot.label) })}
              options={row.slot.candidates.map((candidate) => ({
                value: candidate.integration_id,
                label: integrationName(candidate),
                keywords: [candidate.provider],
                icon: <ProviderLogo provider={candidate.provider} className="h-4 w-4" decorative />,
              }))}
              searchable="auto"
              value=""
              disabled={busy}
              placeholder={t_i18n("Bind an integration")}
              onValueChange={(integrationId) => {
                if (integrationId)
                  bind.mutate({ memberId: row.member.member_id, capability: row.slot.capability, integrationId }, { onError: refused });
              }}
              className="w-[200px]"
            />
          );
        if (row.slot.state === "unavailable")
          return (
            <Link to="/integrations" className="content-caption text-accent">
              {t_i18n("Connect one")}
            </Link>
          );
        return null;
  };

  return (
    <Card data-testid="fleet-capability-slots">
      <CardContent className="space-y-3 p-4">
        {/* The Fleet defaults bar's header: icon, 12 px title, one-line summary (UX r1 B4 of #994). */}
        <div className="flex items-start gap-2">
          <Icon name="network" size={16} className="shrink-0 text-default-secondary" aria-hidden />
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2">
            <h3 className="flex shrink-0 items-center gap-1 whitespace-nowrap content-compact font-medium">
              {t_i18n("Capabilities")}
              <InfoTip label={t_i18n("What capability slots are")} content={help} />
            </h3>
            <span className="min-w-0 content-compact text-default-secondary" data-testid="fleet-capability-slots-summary">
              {t_i18n("{total, plural, one {{connected} of # slot connected} other {{connected} of # slots connected}}", { connected, total })}
            </span>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="ml-auto shrink-0"
            aria-expanded={open}
            aria-controls="fleet-capability-slots-rows"
            onClick={() => setOpen((value) => !value)}
          >
            {open ? t_i18n("Hide slots") : t_i18n("Show slots")}
          </Button>
        </div>
        {open && (
          <div id="fleet-capability-slots-rows">
            <DataTable
              rows={rows}
              columns={columns}
              rowKey={(row) => row.key}
              rowHref={(row) => `/agents/${row.member.agent_id}`}
              rowActions={bindAction}
              rowActionsWidth={216}
              defaultSort={{ key: "status", direction: "asc" }}
              searchFields={(row) => slotRowSearchFields(row, t_text(row.slot.label))}
              searchPlaceholder={t_i18n("Search capabilities, members, integrations...")}
              filters={[
                {
                  key: "status",
                  label: "Status",
                  values: (row) => [row.slot.state],
                  // An ordinal scale: what is left to bind first.
                  ordered: true,
                  options: (["empty", "unavailable", "filled"] as const).map((state) => ({ value: state, label: t_i18n(SLOT_STATE_LABELS[state]) })),
                },
                {
                  key: "capability",
                  label: "Capability",
                  values: (row) => [row.slot.capability],
                  options: capabilityOptions(rows).map((option) => ({ value: option.value, label: t_text(option.label) })),
                },
              ]}
              defaultPageSize={SLOTS_PAGE_SIZE}
              emptyLabel={t_i18n("No capability slot")}
              noResultsLabel={t_i18n("No slot matches the search and filters")}
              framed={false}
              rowLines={2}
              rowTestId="fleet-capability-slot-row"
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
