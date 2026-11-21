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
 * Compact (UX round 1 of #994, B3/B4): one bar styled like Fleet defaults,
 * a one-line summary of the connected slots, expanded on click into one row
 * per member with its slots inline, so the Members table stays on the first
 * screen.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import {
  useBindFleetSlot,
  useFleetCapabilitySlots,
  useUnbindFleetSlot,
  type Fleet,
  type FleetCapabilitySlot,
  type FleetMemberSlots,
} from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { fleetRefusalMessage } from "./fleet-refusals";

/** The words of a slot's state, short and plain (13bis.10). @i18n-keys */
const SLOT_STATE_LABELS = {
  filled: "Connected",
  empty: "Not bound",
  unavailable: "None connected",
} as const;

/** A slot that can be bound but is not reads as a warning, apart from "None connected" (UX r1 A1). */
const SLOT_STATE_VARIANTS = {
  filled: "success",
  empty: "warning",
  unavailable: "outline",
} as const;

export function FleetCapabilitySlotsCard({ fleet }: { fleet: Fleet }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, error } = useFleetCapabilitySlots(fleet.id);
  const [open, setOpen] = useState(false);
  if (isLoading || error || !data || data.members.length === 0) return null;
  const slots = data.members.flatMap((member) => member.slots);
  const connected = slots.filter((slot) => slot.state === "filled").length;
  const help = fleet.is_builtin
    ? t_i18n("Connect your tools to each member. Your bindings are kept by every release; the members and their handovers stay the product's.")
    : t_i18n("Connect your tools to each member.");
  return (
    <Card data-testid="fleet-capability-slots">
      <CardContent className="space-y-3 p-4">
        {/* The Fleet defaults bar's header: icon, 12 px title, one-line summary (UX r1 B4). */}
        <div className="flex items-start gap-2">
          <Icon name="network" size={16} className="shrink-0 text-default-secondary" aria-hidden />
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2">
            <h3 className="flex shrink-0 items-center gap-1 whitespace-nowrap content-compact font-medium">
              {t_i18n("Capabilities")}
              <InfoTip label={t_i18n("What capability slots are")} content={help} />
            </h3>
            <span className="min-w-0 content-compact text-default-secondary" data-testid="fleet-capability-slots-summary">
              {t_i18n("{total, plural, one {{connected} of # slot connected} other {{connected} of # slots connected}}", { connected, total: slots.length })}
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
          <ul id="fleet-capability-slots-rows" className="divide-y divide-elevation-subtle">
            {data.members.map((member) => (
              <MemberSlots key={member.member_id} fleetId={fleet.id} member={member} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** One row per member: name and role on the left, its slots inline across the width. */
function MemberSlots({ fleetId, member }: { fleetId: string; member: FleetMemberSlots }) {
  const { t_i18n } = useFormatter();
  return (
    <li aria-label={member.name} className="grid gap-2 py-2 first:pt-0 last:pb-0 md:grid-cols-[220px_minmax(0,1fr)]">
      <div className="min-w-0">
        <Link to={`/agents/${member.agent_id}`} className="block truncate content-compact font-medium hover:underline">
          {member.name || t_i18n("Unknown agent")}
        </Link>
        {member.role_label && <span className="block truncate content-caption text-default-secondary">{member.role_label}</span>}
      </div>
      <ul className="flex min-w-0 flex-wrap gap-x-4 gap-y-2">
        {member.slots.map((slot) => (
          <SlotRow key={slot.capability} fleetId={fleetId} member={member} slot={slot} />
        ))}
      </ul>
    </li>
  );
}

function SlotRow({ fleetId, member, slot }: { fleetId: string; member: FleetMemberSlots; slot: FleetCapabilitySlot }) {
  // The slot's label comes from the API: translated when the catalog holds it, shown as is otherwise.
  const { t_i18n, t_text } = useFormatter();
  const { toast } = useToast();
  const bind = useBindFleetSlot(fleetId);
  const unbind = useUnbindFleetSlot(fleetId);
  const busy = bind.isPending || unbind.isPending;
  const refused = (error: Error) =>
    toast({ title: t_i18n("The binding could not be changed"), description: fleetRefusalMessage(error, t_i18n), variant: "error" });

  return (
    <li className="flex flex-wrap items-center gap-2 content-compact" data-testid={`slot-${member.member_id}-${slot.capability}`}>
      <span className="shrink-0">{t_text(slot.label)}</span>
      <Badge variant={SLOT_STATE_VARIANTS[slot.state]} className="shrink-0">
        {t_i18n(SLOT_STATE_LABELS[slot.state])}
      </Badge>
      {slot.bound.map((integration) => (
        <span key={integration.integration_id} className="inline-flex max-w-[220px] items-center gap-1 rounded-sm bg-elevation-hover px-1.5 py-0.5">
          <span className="truncate" title={integration.name || integration.provider}>
            {integration.name || integration.provider}
          </span>
          {member.can_edit && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              disabled={busy}
              aria-label={t_i18n("Unbind {name}", { name: integration.name || integration.provider })}
              onClick={() =>
                unbind.mutate(
                  { memberId: member.member_id, capability: slot.capability, integrationId: integration.integration_id },
                  { onError: refused },
                )
              }
            >
              <Icon name="x" size={16} aria-hidden />
            </Button>
          )}
        </span>
      ))}
      {member.can_edit && slot.candidates.length > 0 && (
        <SearchableSelect
          aria-label={t_i18n("Bind an integration to {slot}", { slot: t_text(slot.label) })}
          options={slot.candidates.map((candidate) => ({
            value: candidate.integration_id,
            label: candidate.name || candidate.provider,
            keywords: [candidate.provider],
          }))}
          searchable="auto"
          value=""
          disabled={busy}
          placeholder={t_i18n("Bind an integration")}
          onValueChange={(integrationId) => {
            if (integrationId) bind.mutate({ memberId: member.member_id, capability: slot.capability, integrationId }, { onError: refused });
          }}
          className="w-[200px] shrink-0"
        />
      )}
      {slot.state === "unavailable" && member.can_edit && (
        <Link to="/integrations" className="content-caption text-accent hover:underline">
          {t_i18n("Connect one")}
        </Link>
      )}
    </li>
  );
}
