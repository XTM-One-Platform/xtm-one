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
 * "Review actions" (#10025, slice B of #10000, design round 3 frame M2): the
 * approval gates the platform proposes for a fleet that has no catalog yet,
 * computed from its members' write tools (`GET /fleets/{id}/approval-gates`,
 * no AI, no quota). Accepting stores them as the fleet's catalog
 * (`PUT /fleets/{id}/approval-gates`); from then on the level acts on them.
 *
 * Rows hold on one line at 1024 in fr (UX round 3 C1): fixed layout, the
 * action cell truncates with its full text in the title. Each row is one plain
 * translated action and the integration it acts in, with its mark (UX round 1
 * B1, B2 on #10034); the tool names stay in the stored gate, not on the page.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { useToast } from "@/components/ui/toast";
import { useFleetApprovalGates, useSetFleetApprovalGates, type FleetApprovalGate } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { gateActionLabel, IMPACT_LABELS, integrationName } from "./fleet-autonomy";
import { moduleLabel } from "./fleet-about-showcase";
import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetGateMark } from "./FleetModuleMark";

export function FleetGateReview({ fleetId }: { fleetId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data, isLoading, isError, refetch } = useFleetApprovalGates(fleetId);
  const save = useSetFleetApprovalGates(fleetId);
  const [selected, setSelected] = useState<string[]>([]);

  const proposals = data?.proposals ?? [];
  const editable = !!data?.editable;
  /** Where a proposal acts: the integration's name, else the module's. */
  const where = (gate: FleetApprovalGate) => {
    if (gate.provider) return integrationName(gate.provider);
    const label = moduleLabel(gate.module, {});
    return label.translate ? t_i18n(label.text) : label.text;
  };
  const whereKey = (gate: FleetApprovalGate) => gate.provider || gate.module;
  const whereOptions = [...new Map(proposals.map((g) => [whereKey(g), where(g)])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1]))
    .map(([value, label]) => ({ value, label }));

  const accept = async (gates: FleetApprovalGate[]) => {
    if (gates.length === 0) return;
    const keys = new Set(gates.map((g) => g.key));
    const kept = (data?.catalog ?? []).filter((g) => !keys.has(g.key));
    try {
      await save.mutateAsync([...kept, ...gates]);
      setSelected([]);
      toast({
        title: t_i18n("{count, plural, =0 {No action changes} one {# action now waits for approval} other {# actions now wait for approval}}", { count: gates.length }),
        variant: "success",
      });
    } catch (err) {
      // A 422 is a validation refusal in English (UX round 4 B1 on #10034): never shown raw.
      const invalid = (err as { status?: number } | null)?.status === 422;
      toast({
        title: t_i18n("The approval gates were not changed"),
        description: invalid
          ? t_i18n("These approval gates could not be stored. Reload the page and try again.")
          : fleetRefusalMessage(err, t_i18n),
        variant: "error",
      });
    }
  };

  const columns: DataTableColumn<FleetApprovalGate>[] = [
    {
      key: "action",
      header: "Action",
      cell: (gate) => {
        const label = gateActionLabel(gate, t_i18n);
        return (
          <span className="block truncate" title={label}>
            {label}
          </span>
        );
      },
      sortValue: (gate) => gateActionLabel(gate, t_i18n),
    },
    {
      key: "integration",
      header: "Integration",
      hideable: true,
      width: 160,
      cell: (gate) => (
        <span className="inline-flex min-w-0 items-center gap-1.5 whitespace-nowrap">
          <FleetGateMark gate={gate} />
          <span className="truncate">{where(gate)}</span>
        </span>
      ),
      sortValue: (gate) => where(gate),
    },
    {
      key: "impact",
      header: "Impact",
      hideable: true,
      width: 140,
      cell: (gate) => <span className="whitespace-nowrap">{t_i18n(IMPACT_LABELS[gate.impact])}</span>,
      sortValue: (gate) => ["low", "high", "destructive"].indexOf(gate.impact),
    },
  ];

  return (
    <div className="space-y-2" data-testid="fleet-gate-review">
      <p className="content-compact text-default-secondary">
        {t_i18n("Actions this fleet's members can take outside XTM One, proposed from their configuration. Accepting one makes it wait for approval at the levels its impact sets.")}
      </p>
      {data && data.existing_approvals.length > 0 && (
        <ul className="content-compact text-default-secondary" data-testid="fleet-gate-review-existing">
          {data.existing_approvals.map((a) => (
            <li key={`${a.member}:${a.approval}`}>
              <span className="font-medium text-default-primary">{a.member}</span>: {a.approval}
            </li>
          ))}
        </ul>
      )}
      {!editable && data && (
        <p className="content-compact text-default-secondary" data-testid="fleet-gate-review-read-only">
          {t_i18n("Only a manager of this fleet can accept approval gates.")}
        </p>
      )}
      <DataTable
        rows={proposals}
        columns={columns}
        rowKey={(gate) => gate.key}
        layout="fixed"
        searchFields={(gate) => [gateActionLabel(gate, t_i18n), where(gate)]}
        searchPlaceholder={t_i18n("Search actions")}
        filters={[
          {
            key: "integration",
            label: "Integration",
            values: (gate) => [whereKey(gate)],
            options: whereOptions,
          },
          {
            key: "impact",
            label: "Impact",
            values: (gate) => [gate.impact],
            options: [...new Set(proposals.map((g) => g.impact))].map((i) => ({ value: i, label: t_i18n(IMPACT_LABELS[i]) })),
          },
        ]}
        defaultPageSize={25}
        storageKey="fleet-gate-review"
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No write action found: the members of this fleet have no integration they can write to.")}
        selection={
          editable
            ? {
                selected,
                onChange: setSelected,
                actions: (
                  <Button
                    size="sm"
                    className={TOOLBAR_CONTROL_HEIGHT}
                    onClick={() => void accept(proposals.filter((g) => selected.includes(g.key)))}
                    disabled={save.isPending}
                  >
                    {t_i18n("Accept selected ({count})", { count: selected.length })}
                  </Button>
                ),
              }
            : undefined
        }
        rowActions={
          editable
            ? (gate) => (
                <Button variant="ghost" size="sm" onClick={() => void accept([gate])} disabled={save.isPending}>
                  {t_i18n("Accept")}
                </Button>
              )
            : undefined
        }
        toolbar={
          editable && proposals.length > 0 ? (
            <Button size="sm" variant="outline" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => void accept(proposals)} disabled={save.isPending}>
              {t_i18n("Accept all ({count})", { count: proposals.length })}
            </Button>
          ) : undefined
        }
        rowTestId="fleet-gate-review-row"
      />
    </div>
  );
}
