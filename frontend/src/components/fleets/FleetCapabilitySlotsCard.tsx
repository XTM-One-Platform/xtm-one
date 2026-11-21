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
 */
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

export function FleetCapabilitySlotsCard({ fleet }: { fleet: Fleet }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, error } = useFleetCapabilitySlots(fleet.id);
  if (isLoading || error || !data || data.members.length === 0) return null;
  return (
    <Card data-testid="fleet-capability-slots">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 content-base">
          <Icon name="network" size={16} className="text-accent" aria-hidden /> {t_i18n("Capabilities")}
        </CardTitle>
        <p className="content-caption text-default-secondary">
          {fleet.is_builtin
            ? t_i18n("Connect your tools to each member. Your bindings are kept by every release; the members and their handovers stay the product's.")
            : t_i18n("Connect your tools to each member.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.members.map((member) => (
          <MemberSlots key={member.member_id} fleetId={fleet.id} member={member} />
        ))}
      </CardContent>
    </Card>
  );
}

function MemberSlots({ fleetId, member }: { fleetId: string; member: FleetMemberSlots }) {
  const { t_i18n } = useFormatter();
  return (
    <section aria-label={member.name} className="rounded-md border border-elevation-subtle p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <Link to={`/agents/${member.agent_id}`} className="content-compact font-medium hover:underline">
          {member.name || t_i18n("Unknown agent")}
        </Link>
        {member.role_label && <span className="content-caption text-default-secondary">{member.role_label}</span>}
      </div>
      <ul className="mt-2 space-y-2">
        {member.slots.map((slot) => (
          <SlotRow key={slot.capability} fleetId={fleetId} member={member} slot={slot} />
        ))}
      </ul>
    </section>
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
      <span className="w-40 shrink-0 truncate" title={slot.label}>{t_text(slot.label)}</span>
      <Badge variant={slot.state === "filled" ? "success" : "outline"} className="shrink-0">
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
