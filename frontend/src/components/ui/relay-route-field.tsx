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

import { Icon } from "@filigran/design-system";
/**
 * "Route through": where an integration's or a remote MCP server's requests
 * leave from - directly from the platform, or from a relay group's
 * network. One picker for every resource that can be routed; a flow HTTP node
 * gets the same choice from the node catalog (`relay_groups` source).
 *
 * The options are the groups the viewer may route through, plus the group the
 * resource is already bound to (an admin editing a colleague's resource keeps
 * seeing it). A routed resource never falls back to direct: when its group has
 * no relay online, or when the service's address is plainly outside the
 * group's list or every relay's own list, its requests fail, which the picker
 * says up front.
 */
import { useId } from "react";
import { Link } from "react-router-dom";

import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useGroupRelays, useRelayGroups, useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { groupAllows, parseDestination, relayMayAllow } from "@/lib/relay-destinations";
import { cn } from "@/lib/utils";

const DIRECT = "__direct__";

interface RelayRouteFieldProps {
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  disabled?: boolean;
  /** A resource that cannot be routed (a stdio MCP server), with the reason. */
  unavailableReason?: string;
  className?: string;
  /** Renders the value as text instead of a picker (a read-only view). */
  readOnly?: boolean;
  /** The service URL the resource calls, to warn when no list admits it. */
  destination?: string | null;
  /** The resource is reached only through a relay group: "Direct" is not offered. */
  required?: boolean;
}

export function RelayRouteField({
  value,
  onChange,
  disabled,
  unavailableReason,
  className,
  readOnly,
  destination,
  required = false,
}: RelayRouteFieldProps) {
  const { t_i18n } = useFormatter();
  const baseId = useId();
  const pickerId = `${baseId}-picker`;
  const offlineId = `${baseId}-offline`;
  const destinationId = `${baseId}-destination`;
  const noGroupId = `${baseId}-no-group`;
  const relaysEnabled = useRelaysEnabled();
  // Unknown while `/auth/config` loads: neither the picker nor "turned off".
  const relaysOn = relaysEnabled === true;
  const relaysOff = relaysEnabled === false;
  const bound = value ? [value] : [];
  const { data: library } = useRelayGroups({ enabled: relaysOn });
  const { data: all } = useRelayGroups({ scope: "all", enabled: relaysOn && !!value });
  const groups = withBoundEntities(library, all, bound);
  const dest = parseDestination(destination);
  const { data: relays } = useGroupRelays(relaysOn && dest ? value ?? undefined : undefined);

  // Nothing to say on a platform without relays, unless something is still
  // routed (the platform refuses its requests until relays are back on).
  if (!relaysOn && !value) return null;

  const current = groups?.find((g) => g.id === value);
  const noneOnline = current && current.relays_online === 0;
  const target = dest ? `${dest.host}:${dest.port}` : "";
  const groupRefuses = !!current && !!dest && !groupAllows(current.allowed_destinations, dest);
  // Only relays that connected have told the platform their list.
  const reported = (relays ?? []).filter((relay) => relay.protocol_version !== null);
  const relaysRefuse = !!dest && !groupRefuses && reported.length > 0
    && reported.every((relay) => !relayMayAllow(relay.declared_destinations, dest));
  const destinationWarning = groupRefuses ? (
    <p id={destinationId} className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
      <Icon name="triangle-alert" size={16} className="shrink-0" />
      {t_i18n("The group's allowed destinations do not include {target}: its requests are refused before they reach a relay.", { target })}
    </p>
  ) : relaysRefuse ? (
    <p id={destinationId} className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
      <Icon name="triangle-alert" size={16} className="shrink-0" />
      {t_i18n("No relay of this group lists {target} in its own allowed destinations: add it to the relay's relay.toml.", { target })}
    </p>
  ) : null;

  if (readOnly || !relaysOn) {
    return (
      <div className={cn("space-y-1", className)}>
        <p className="content-compact font-medium text-default-secondary">{t_i18n("Route through")}</p>
        <p className="flex items-center gap-1.5 content-base">
          <Icon name="network" size={16} className="text-accent" />
          {value
            ? current
              ? <Link to={`/connections/relays/${current.id}`} className="hover:text-accent">{current.name}</Link>
              : t_i18n("A relay group")
            : t_i18n("Direct (from the platform)")}
        </p>
        {relaysOff && value && (
          <p className="content-compact text-feedback-warning-primary">
            {t_i18n("Relays are turned off on this platform: requests routed through a relay group are refused until they are back on.")}
          </p>
        )}
        {relaysOn && destinationWarning}
      </div>
    );
  }

  const noGroup = !!groups && groups.length === 0;
  // The picker announces its help line and every warning drawn under it.
  const describedBy = [
    settingsFieldHintId(pickerId),
    noneOnline && offlineId,
    destinationWarning && destinationId,
    noGroup && noGroupId,
  ].filter(Boolean).join(" ");
  const help = unavailableReason
    ? unavailableReason
    : value
      ? t_i18n("Requests leave from the group's network through one of its relays, never directly from the platform.")
      : required
        ? <span className="text-feedback-warning-primary">{t_i18n("This service is reached only through a relay group installed in its network: choose one.")}</span>
        : t_i18n("Choose a relay group to reach a service inside a network the platform cannot reach.");

  return (
    <div className={cn("space-y-1.5", className)}>
      <SettingsField
        id={pickerId}
        label={t_i18n("Route through")}
        helper={help}
        info={t_i18n("Where this service's requests leave from. Direct: from the platform itself. A relay group: from a relay installed in the group's network, for a service the platform cannot reach. Only the groups shared with you are listed.")}
      >
        <SearchableSelect
          id={pickerId}
          aria-labelledby={settingsFieldLabelId(pickerId)}
          aria-describedby={describedBy}
          options={(groups ?? []).map((group) => ({
            value: group.id,
            label: group.zone ? `${group.name} (${group.zone})` : group.name,
            description: t_i18n("{online}/{total} online", { online: group.relays_online, total: group.relays_total }),
            icon: (
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  group.relays_online > 0 ? "bg-feedback-success-primary" : "bg-[var(--text-default-secondary)]",
                )}
              />
            ),
          }))}
          pinned={required ? [] : [{ value: DIRECT, label: t_i18n("Direct (from the platform)") }]}
          value={value || (required ? "" : DIRECT)}
          placeholder={required ? t_i18n("Choose a relay group") : undefined}
          onValueChange={(next) => onChange(next === DIRECT ? null : next)}
          disabled={disabled || !!unavailableReason || groups === undefined}
        />
      </SettingsField>
      {noneOnline && (
        <p id={offlineId} className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
          <Icon name="triangle-alert" size={16} className="shrink-0" />
          {t_i18n("No relay of this group is online: its requests fail until one connects.")}
        </p>
      )}
      {destinationWarning}
      {noGroup && (
        <p id={noGroupId} className="content-compact text-default-secondary">
          {t_i18n("No relay group is shared with you yet.")}{" "}
          <Link to="/connections/relays" className="text-accent hover:underline">{t_i18n("Relays")}</Link>
        </p>
      )}
    </div>
  );
}
