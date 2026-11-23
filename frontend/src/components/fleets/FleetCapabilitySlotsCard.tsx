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
 * The capabilities a fleet's home members need (#914 slice 4), on the fleet
 * page's Members view, the CTEM loop setup dialog and the fleet setup wizard.
 *
 * One row per capability type (#1201): capability icon and label, how many
 * members use it, its status (Connected, Partial, Not bound, None
 * connected), the fleet choice with its provider logos and the picker that
 * writes it on every member slot of that type
 * (`PUT /fleets/{id}/capabilities/{capability}`). A built-in option ("XTM One
 * inventory (built in)", "In XTM One only") is offered first and fills the
 * slot without an integration; notification takes several channels at once
 * and its built-in option stays on. A row expands to its members, each with
 * its own picker (`PUT /fleets/{id}/members/{member}/slots/{capability}`): a
 * member whose set differs from the fleet choice reads "Custom" and can be
 * reset. A type change asks before it replaces a custom member's choice.
 *
 * On a built-in fleet a binding is the customer layer: kept by every release,
 * while the fleet's members, handovers and gates stay locked. The card is not
 * shown when no member's role declares a slot. Pure logic:
 * `capability-slots-table.ts`. `inSetup` keeps every action inside the wizard
 * (no collapse, no link out, "Connect one" in a new tab).
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { InfoTip } from "@/components/ui/info-tip";
import { MultiSelect } from "@/components/ui/multi-select";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { XtmOneEmblem } from "@/components/ui/xtm-one-emblem";
import {
  useFleetCapabilitySlots,
  useSetFleetCapability,
  useSetFleetMemberSlot,
  type Fleet,
  type FleetCapabilityType,
  type FleetCapabilityTypeMember,
  type FleetSlotIntegration,
} from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import {
  BLOCKED_REASON_LINES,
  BLOCKED_REASON_SHORT,
  CAPABILITIES_OPEN_BY_DEFAULT,
  MEMBERS_COLUMN_WIDTH,
  MULTI_CHANNEL_CAPABILITIES,
  NO_INTEGRATION_VALUE,
  ROW_ACTIONS_WIDTH,
  SECONDARY_COLUMNS_HIDE_BELOW,
  STATUS_COLUMN_WIDTH,
  TYPE_STATE_LABELS,
  TYPE_STATE_VARIANTS,
  blockedFixPath,
  builtinValue,
  capabilityIcon,
  customMembers,
  firstBlocked,
  idsForPick,
  integrationIds,
  isBuiltinValue,
  multiChannelSelection,
  overwritableCustomMembers,
  pickerValue,
  toggledIds,
  typeCounts,
  typeEditable,
  typePickerValue,
  typeSearchFields,
  typeStateRank,
  unsetMembers,
} from "./capability-slots-table";
import { FleetFoldHeader } from "./FleetFoldHeader";
import { fleetRefusalMessage } from "./fleet-refusals";

/** Ten rows a page: the Members table stays one short scroll away (no nested scroll box). */
const SLOTS_PAGE_SIZE = 10;

export function FleetCapabilitySlotsCard({
  fleet,
  inSetup = false,
}: {
  fleet: Fleet;
  /**
   * Inside the setup wizard (#1151, UX r1 B2 and A1, r2 B4): always expanded, no collapse,
   * nothing links out of the wizard in the same tab, and "Connect one" opens /integrations
   * in a new tab with the slots read again when this tab regains focus.
   */
  inSetup?: boolean;
}) {
  const { t_i18n, t_text } = useFormatter();
  const { toast } = useToast();
  const { data, isLoading, error, refetch } = useFleetCapabilitySlots(fleet.id);
  const [expanded, setOpen] = useState(CAPABILITIES_OPEN_BY_DEFAULT);
  const [openTypes, setOpenTypes] = useState<ReadonlySet<string>>(new Set());
  const open = inSetup || expanded;
  const setType = useSetFleetCapability(fleet.id);
  const setMember = useSetFleetMemberSlot(fleet.id);
  const [confirmElement, confirm] = useConfirm();
  const navigate = useNavigate();
  if (isLoading || error || !data) return null;
  const types = (data.types ?? []).filter((type) => type.members.length > 0);
  if (types.length === 0) return null;
  const { connected, total } = typeCounts(types);
  const busy = setType.isPending || setMember.isPending;
  const refused = (err: Error) =>
    toast({ title: t_i18n("The binding could not be changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
  const help = fleet.is_builtin
    ? t_i18n("Choose the tools of each capability once for the whole fleet; a member can keep its own. Your choices are kept by every release; the members and their handovers stay the product's.")
    : t_i18n("Choose the tools of each capability once for the whole fleet; a member can keep its own.");
  const integrationName = (integration: { name: string; provider: string }) => integration.name || integration.provider;
  const connectOne = () => {
    if (!inSetup) {
      navigate("/integrations");
      return;
    }
    window.addEventListener("focus", () => void refetch(), { once: true });
    window.open("/integrations", "_blank", "noopener,noreferrer");
  };
  const toggleType = (type: FleetCapabilityType) =>
    setOpenTypes((current) => {
      const next = new Set(current);
      if (next.has(type.capability)) next.delete(type.capability);
      else next.add(type.capability);
      return next;
    });

  // The fleet choice: every editable member of the type, a custom one only once the person confirms.
  const chooseForType = async (type: FleetCapabilityType, ids: string[]) => {
    const custom = overwritableCustomMembers(type);
    const overrideCustom =
      custom.length > 0 &&
      (await confirm({
        title: t_i18n("Replace the custom choices?"),
        description: t_i18n(
          "{count, plural, one {# member has its own choice for {capability}. Replace it with the fleet choice?} other {# members have their own choice for {capability}. Replace them with the fleet choice?}}",
          { count: custom.length, capability: t_text(type.label) },
        ),
        confirmLabel: t_i18n("Replace"),
        cancelLabel: t_i18n("Keep their choice"),
        variant: "warning",
      }));
    setType.mutate({ capability: type.capability, integrationIds: ids, overrideCustom }, { onError: refused });
  };
  const chooseForMember = (type: FleetCapabilityType, member: FleetCapabilityTypeMember, ids: string[]) =>
    setMember.mutate({ memberId: member.member_id, capability: type.capability, integrationIds: ids }, { onError: refused });

  const builtinChip = (type: FleetCapabilityType) =>
    type.builtin && (
      <span
        className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-sm bg-elevation-hover px-1.5 py-0.5"
        data-testid={`type-builtin-${type.capability}`}
      >
        {/* What XTM One serves wears its emblem, never the capability icon (#1373). */}
        <XtmOneEmblem />
        <TruncatedText text={t_text(type.builtin.label)} />
      </span>
    );
  const integrationChips = (integrations: readonly FleetSlotIntegration[], testId: string) =>
    integrations.map((integration) => (
      <span
        key={integration.integration_id}
        className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-sm bg-elevation-hover px-1.5 py-0.5"
        data-testid={testId}
      >
        <ProviderLogo provider={integration.provider} className="h-4 w-4 shrink-0" decorative />
        <TruncatedText text={integrationName(integration)} />
      </span>
    ));

  /**
   * The picker of a type or of one member: a multi-select for a multi-channel capability
   * (the built-in option always on), else one choice with the built-in option pinned first.
   * Every picker is the same 240 px box with no control outside it (UX r1 B1 of #1209): a
   * member picker offers "No integration" as an option instead of a clear button beside it.
   */
  const picker = (
    type: FleetCapabilityType,
    bound: readonly FleetSlotIntegration[],
    onPick: (ids: string[]) => void,
    label: string,
    forMember: boolean,
  ) => {
    const builtin = builtinValue(type);
    const builtinLabel = type.builtin ? t_text(type.builtin.label) : "";
    if (MULTI_CHANNEL_CAPABILITIES.has(type.capability)) {
      const current = integrationIds(bound);
      const channel = (integration: FleetSlotIntegration) => ({
        value: integration.integration_id,
        label: integrationName(integration),
        icon: <ProviderLogo provider={integration.provider} className="h-4 w-4 shrink-0" decorative />,
      });
      // The always-on built-in ("In XTM One") is the first row, with the XTM One emblem, checked
      // and locked; the external channels add to it, each with its provider logo (#1373). With
      // nothing else chosen the field reads the built-in option, never "Choose an integration".
      return (
        <MultiSelect
          aria-label={label}
          options={[
            ...(builtin ? [{ value: builtin, label: builtinLabel, icon: <XtmOneEmblem />, locked: true }] : []),
            ...bound.filter((integration) => !type.candidates.some((c) => c.integration_id === integration.integration_id)).map(channel),
            ...type.candidates.map(channel),
          ]}
          sort={false}
          selected={multiChannelSelection(type, bound)}
          onToggle={(value) => {
            if (!isBuiltinValue(value)) onPick(toggledIds(current, value));
          }}
          disabled={busy}
          placeholder={t_i18n("Choose an integration")}
          searchPlaceholder={t_i18n("Search integrations...")}
          emptyOptionsLabel={t_i18n("No integration connected")}
          noMatchLabel={t_i18n("No match")}
          maxVisibleChips="fit"
          className="w-[240px]"
        />
      );
    }
    const options: SelectOption[] = type.candidates.map((candidate) => ({
      value: candidate.integration_id,
      label: integrationName(candidate),
      keywords: [candidate.provider],
      icon: <ProviderLogo provider={candidate.provider} className="h-4 w-4" decorative />,
    }));
    const pinned: SelectOption[] = builtin
      ? [{ value: builtin, label: builtinLabel, icon: <XtmOneEmblem /> }]
      : forMember && bound.length > 0
        ? [{ value: NO_INTEGRATION_VALUE, label: t_i18n("No integration") }]
        : [];
    return (
      <SearchableSelect
        aria-label={label}
        options={options}
        pinned={pinned}
        searchable="auto"
        value={forMember ? pickerValue(type, bound) : typePickerValue(type)}
        disabled={busy}
        placeholder={t_i18n("Choose an integration")}
        onValueChange={(value) => onPick(idsForPick(value))}
        className="w-[240px]"
      />
    );
  };

  const columns: DataTableColumn<FleetCapabilityType>[] = [
    {
      key: "capability",
      header: "Capability",
      sortValue: (type) => t_text(type.label),
      csv: (type) => t_text(type.label),
      cell: (type) => (
        <span className="flex min-w-0 items-center gap-2">
          <Icon
            name={openTypes.has(type.capability) ? "chevron-down" : "chevron-right"}
            size={16}
            className="shrink-0 text-default-secondary"
            aria-hidden
          />
          <Icon name={capabilityIcon(type.capability)} size={16} className="shrink-0 text-default-secondary" aria-hidden />
          <TruncatedText text={t_text(type.label)} />
        </span>
      ),
    },
    {
      key: "members",
      header: "Members",
      width: MEMBERS_COLUMN_WIDTH,
      hideable: true,
      // The opened row lists the members: the count steps aside below 1280 px of table (UX r2 B1 of #1209).
      hideBelow: SECONDARY_COLUMNS_HIDE_BELOW,
      sortValue: (type) => type.members.length,
      csv: (type) => String(type.members.length),
      cell: (type) => (
        <span className="whitespace-nowrap text-default-secondary">
          {t_i18n("{count, plural, one {# member} other {# members}}", { count: type.members.length })}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      width: STATUS_COLUMN_WIDTH,
      sortValue: typeStateRank,
      csv: (type) => t_i18n(TYPE_STATE_LABELS[type.state]),
      cell: (type) => (
        <Badge variant={TYPE_STATE_VARIANTS[type.state]} className="whitespace-nowrap">
          {t_i18n(TYPE_STATE_LABELS[type.state])}
        </Badge>
      ),
    },
    {
      key: "integration",
      header: "Integration",
      hideable: true,
      // Steps aside below 1280 px of table, and in the setup dialog (UX r2 B1 of #1209).
      hideBelow: SECONDARY_COLUMNS_HIDE_BELOW,
      csv: (type) => [type.builtin ? t_text(type.builtin.label) : "", ...type.choice.map(integrationName)].filter(Boolean).join(", "),
      cell: (type) => (
        <span className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid={`type-choice-${type.capability}`}>
          {builtinChip(type)}
          {integrationChips(type.choice, `type-bound-${type.capability}`)}
          {/* A Not usable type names its first blocked integration here, muted, one line: the rest
              sit behind "+N" and its tip, the anatomy of the row's button (UX r2 B4 of #1376). */}
          {type.state === "not_usable" && blockedChip(type)}
          {unsetMembers(type).length > 0 && type.choice.length > 0 && (
            <Badge variant="warning" className="whitespace-nowrap" data-testid={`type-unset-${type.capability}`}>
              {t_i18n("{count, plural, one {# member not set} other {# members not set}}", { count: unsetMembers(type).length })}
            </Badge>
          )}
          {customMembers(type).length > 0 && (
            <Badge variant="secondary" className="whitespace-nowrap">
              {t_i18n("Custom")}
            </Badge>
          )}
        </span>
      ),
    },
  ];

  // The type picker: its last column, which never opens the row.
  const typeAction = (type: FleetCapabilityType) => {
    if (!typeEditable(type)) return null;
    if (type.candidates.length > 0 || type.builtin)
      return picker(
        type,
        type.choice,
        (ids) => void chooseForType(type, ids),
        t_i18n("Fleet choice for {capability}", { capability: t_text(type.label) }),
        false,
      );
    // A connected integration of the type the members cannot take is named with why and how to
    // fix it, never hidden behind "Connect one" (#1373).
    const blocked = firstBlocked(type);
    if (blocked) return blockedAction(type, blocked);
    // "Connect one" leaves for /integrations: a new tab inside the setup flows (UX r1 B3 of #1209).
    if (inSetup)
      return (
        <Button type="button" variant="outline" className="w-[240px] gap-1.5" onClick={connectOne}>
          {t_i18n("Connect one")}
          <Icon name="external-link" size={16} aria-hidden />
        </Button>
      );
    // A button of the picker's 36 px height, never a link in a row cell (#12070, UX r1 B1 of #1133).
    return (
      <Button type="button" variant="outline" className="w-[240px]" onClick={connectOne}>
        {t_i18n("Connect one")}
      </Button>
    );
  };

  // "YouTrack · Not company-managed": the integration and why the members cannot take it (UX r1 B2 of #1376).
  const blockedLabel = (blocked: NonNullable<ReturnType<typeof firstBlocked>>) =>
    t_i18n("{name} · {reason}", { name: integrationName(blocked), reason: t_i18n(BLOCKED_REASON_SHORT[blocked.reason]) });

  // The Integration cell of a Not usable row: the first blocked integration cut with its tip, then
  // "+N" whose tip lists the others, so the row keeps the table's one-line height (UX r2 B4 of #1376).
  const blockedChip = (type: FleetCapabilityType) => {
    const [first, ...others] = type.blocked ?? [];
    if (!first) return null;
    const rest = others.map((item) => blockedLabel(item)).join("\n");
    return (
      <span className="inline-flex min-w-0 max-w-full items-center gap-1.5 text-default-secondary" data-testid={`type-blocked-chip-${type.capability}`}>
        <ProviderLogo provider={first.provider} className="h-4 w-4 shrink-0" decorative />
        <TruncatedText text={blockedLabel(first)} />
        {others.length > 0 && (
          <Tip content={<span className="whitespace-pre-line">{rest}</span>}>
            <span
              tabIndex={0}
              aria-label={rest}
              className={cn("shrink-0 whitespace-nowrap", TIP_WRAP_FOCUS_RING)}
              data-testid={`type-blocked-chip-more-${type.capability}`}
            >
              {t_i18n("+{count}", { count: others.length })}
            </span>
          </Tip>
        )}
      </span>
    );
  };

  // A Not usable row (#1373): the integration, why the members cannot take it, and a button to its page.
  const blockedAction = (type: FleetCapabilityType, blocked: NonNullable<ReturnType<typeof firstBlocked>>) => {
    const others = (type.blocked ?? []).slice(1);
    // Every blocked integration, one line each, in the tip: the row names the first (UX r1 B2 of #1376).
    const why = [blocked, ...others]
      .map((item) => t_i18n(BLOCKED_REASON_LINES[item.reason], { name: integrationName(item) }))
      .join("\n");
    const label = blockedLabel(blocked);
    const open = () => {
      const path = blockedFixPath(blocked);
      if (!inSetup) {
        navigate(path);
        return;
      }
      window.addEventListener("focus", () => void refetch(), { once: true });
      window.open(path, "_blank", "noopener,noreferrer");
    };
    return (
      <span className="inline-flex w-[240px] items-center gap-1" data-testid={`type-blocked-${type.capability}`}>
        <Button
          type="button"
          variant="outline"
          // The Button wraps its children in a span: it must shrink too, or the label runs past the row (#1376 r2).
          className="min-w-0 flex-1 gap-1.5 [&>span]:min-w-0 [&>span]:max-w-full"
          onClick={open}
          aria-label={why}
          data-testid={`type-blocked-open-${type.capability}`}
        >
          <ProviderLogo provider={blocked.provider} className="h-4 w-4 shrink-0" decorative />
          {/* The integration named in words, never by its 16 px logo alone (UX r1 B2 of #1376). */}
          <TruncatedText text={label} />
          {others.length > 0 && (
            <span className="shrink-0 text-default-secondary" data-testid={`type-blocked-more-${type.capability}`}>
              {t_i18n("+{count}", { count: others.length })}
            </span>
          )}
          {inSetup && <Icon name="external-link" size={16} className="shrink-0" aria-hidden />}
        </Button>
        <InfoTip label={why} content={<span className="whitespace-pre-line">{why}</span>} />
      </span>
    );
  };

  // The members of an opened type, each pre-filled with the fleet choice, overridable and resettable.
  const membersPanel = (type: FleetCapabilityType) => {
    if (!openTypes.has(type.capability)) return null;
    return (
      // The opened row pads 16 px a side, the row's actions cell 12 px: -mr-1 puts every member
      // picker on the type picker's right edge (UX r1 B1 of #1209).
      <ul className="-mr-1 space-y-2" data-testid={`type-members-${type.capability}`} aria-label={t_i18n("Members")}>
        {type.members.map((member) => (
          <li key={member.member_id} className="flex min-w-0 items-center gap-3" data-testid="fleet-capability-member-row">
            <span className="block min-w-0 flex-1">
              {inSetup ? (
                <TruncatedText text={member.name || t_i18n("Unknown agent")} className="max-w-[240px] font-medium" />
              ) : (
                <Link to={`/agents/${member.agent_id}`} className="block max-w-[240px] truncate font-medium hover:underline">
                  {member.name || t_i18n("Unknown agent")}
                </Link>
              )}
              {member.role_label && (
                <TruncatedText text={member.role_label} className="max-w-[240px] content-caption text-default-secondary" />
              )}
            </span>
            {member.custom && (
              <Badge variant="secondary" className="whitespace-nowrap">
                {t_i18n("Custom")}
              </Badge>
            )}
            {member.can_edit ? (
              <>
                {member.custom && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => chooseForMember(type, member, integrationIds(type.choice))}
                  >
                    {t_i18n("Reset to fleet choice")}
                  </Button>
                )}
                {(type.candidates.length > 0 || type.builtin) &&
                  picker(
                    type,
                    member.bound,
                    (ids) => chooseForMember(type, member, ids),
                    t_i18n("{capability} for {member}", { capability: t_text(type.label), member: member.name }),
                    true,
                  )}
              </>
            ) : (
              <span className="flex min-w-0 flex-wrap items-center gap-1.5">{integrationChips(member.bound, "member-bound")}</span>
            )}
          </li>
        ))}
      </ul>
    );
  };

  return (
    <Card data-testid="fleet-capability-slots">
      <CardContent className="space-y-3 p-4">
        {/* The shared fleet section header (UX r1 B4 of #994; one header for every fold, UX r1 B1 of #1393). */}
        <FleetFoldHeader
          icon="network"
          title={t_i18n("Capabilities")}
          titleExtra={<InfoTip label={t_i18n("What capability slots are")} content={help} />}
          summary={
            <span className="min-w-0 content-compact text-default-secondary" data-testid="fleet-capability-slots-summary">
              {t_i18n("{total, plural, one {{connected} of # capability connected} other {{connected} of # capabilities connected}}", {
                connected,
                total,
              })}
            </span>
          }
          fold={inSetup ? undefined : { open, onToggle: () => setOpen((value) => !value), controls: "fleet-capability-slots-rows" }}
        />
        {open && (
          <div id="fleet-capability-slots-rows">
            <DataTable
              rows={types}
              columns={columns}
              rowKey={(type) => type.capability}
              onRowClick={toggleType}
              expandedRow={membersPanel}
              rowActions={typeAction}
              rowActionsWidth={ROW_ACTIONS_WIDTH}
              // Fixed columns: at 1080 the flexible ones truncate instead of pushing the picker out of the card.
              layout="fixed"
              defaultSort={{ key: "status", direction: "asc" }}
              searchFields={(type) => typeSearchFields(type, t_text(type.label))}
              searchPlaceholder={t_i18n("Search capabilities, members, integrations...")}
              filters={[
                {
                  key: "status",
                  label: "Status",
                  values: (type) => [type.state],
                  // An ordinal scale: what is left to bind first.
                  ordered: true,
                  options: (["not_bound", "partial", "not_usable", "none_connected", "connected"] as const).map((state) => ({
                    value: state,
                    label: t_i18n(TYPE_STATE_LABELS[state]),
                  })),
                },
              ]}
              defaultPageSize={SLOTS_PAGE_SIZE}
              emptyLabel={t_i18n("No capability slot")}
              noResultsLabel={t_i18n("No slot matches the search and filters")}
              framed={false}
              rowTestId="fleet-capability-slot-row"
            />
          </div>
        )}
        {confirmElement}
      </CardContent>
    </Card>
  );
}
