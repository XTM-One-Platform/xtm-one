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
 * "Waiting for you" (#842, S5; dev-docs/human-gates.md section 4): every open
 * decision of the reader in one list, whatever asked it (a run's question, a
 * tool approval, a flow, a work item), with the same columns. It is the one
 * records list of the "Waiting for me" view (search, Kind filter, sort, total,
 * Columns, page size; UX round 1 on #1081, B1 and B2). A row opens the shared
 * `DecisionCard` in the centred dialog (B3) and answers there through the one
 * answer path (`POST /decisions/{id}/answer`, surface `inbox`); the source
 * stays a link under the card.
 *
 * Rendered only while the read switch is on (`enabled`): otherwise nothing,
 * and the per-engine views stay the reader's.
 */
import { useState, type ReactNode } from "react";
import { Icon, type IconName } from "@filigran/design-system";
import { Link } from "react-router-dom";

import { DecisionCard, type DecisionAnswer, type DecisionView } from "@/components/decisions/DecisionCard";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAnswerDecision, useWaitingDecisions, type WaitingDecision } from "@/hooks/api/useDecisionApi";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** The source of a row as the From column names it. Catalog keys. @i18n-keys */
const SOURCE_LABELS = {
  hitl: "Run",
  tool_approval: "Tool approval",
  flow_ask: "Flow",
  work_item: "Work item",
} as const satisfies Record<WaitingDecision["kind"], string>;

const SOURCE_ICONS: Record<WaitingDecision["kind"], IconName> = {
  hitl: "play",
  tool_approval: "shield-check",
  flow_ask: "waypoints",
  work_item: "list-checks",
};

/** A row as the shared card reads it. Pure: tests read it. */
export function waitingDecisionView(row: WaitingDecision): DecisionView {
  const ask = row.ask ?? {};
  return {
    question: ask.question?.trim() || row.question,
    questionMarkdown: row.kind === "hitl",
    context: (ask.context ?? "").split("\n"),
    options: ask.options ?? [],
    recommendation: ask.recommendation ?? null,
    deadline: row.deadline ?? ask.deadline ?? null,
    links: ask.links ?? [],
    asker: row.agent,
  };
}

/** The From cell's words: the source kind, then its name when there is one. */
export function sourceText(row: WaitingDecision, t_i18n: Translate): string {
  const kind = t_i18n(SOURCE_LABELS[row.kind]);
  return row.origin ? `${kind}: ${row.origin}` : kind;
}

/** The From cell: the kind icon, then the source words cut with a DS tooltip. Shared with the fleet Approval tab (UX r1 B1 of #1497). */
export function DecisionSourceCell({ row }: { row: WaitingDecision }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon name={SOURCE_ICONS[row.kind]} size={16} className="shrink-0 text-default-secondary" aria-hidden />
      <TruncatedText text={sourceText(row, t_i18n)} />
    </span>
  );
}

/** The first row of the list, where focus lands when the answered row has left it (A1). */
function firstRow(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-testid="waiting-for-you-row"]');
}

function DecisionDialog({ row, onClose }: { row: WaitingDecision; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const answer = useAnswerDecision();
  // The answer resumes an agent: with AI off the card is read-only and says why (FR-018).
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();

  const send = async ({ option, text }: DecisionAnswer) => {
    if (aiOff || (!option && !text)) return;
    try {
      await answer.mutateAsync({ id: row.id, option, text });
      toast({ title: t_i18n("Answer sent"), description: t_i18n("The agent has your answer and goes on."), variant: "success" });
      onClose();
    } catch (e) {
      // The backend detail is untranslated: log it, show a translated hint.
      console.error("decision answer failed", e);
      toast({
        title: t_i18n("Failed to send the answer"),
        description: t_i18n("Try again, or reload the item if it changed."),
        variant: "error",
      });
    }
  };

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      {/* The shared centred dialog, as the work items open (Design standards s.2, UX r1 B3 on #1081). */}
      <DialogContent className="max-w-2xl" returnFocusFallback={firstRow}>
        <DialogHeader>
          <DialogTitle className="pr-8 text-base leading-snug">{sourceText(row, t_i18n)}</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-3" data-testid="waiting-for-you-panel">
          <DecisionCard
            view={waitingDecisionView(row)}
            canAnswer={!aiOff}
            readOnlyNote={aiReason ?? undefined}
            pending={answer.isPending}
            onAnswer={(a) => void send(a)}
            testId="waiting-decision"
          />
          {/* The run, flow or item stays a link under the card. */}
          <Link
            to={row.link}
            className="inline-flex items-center gap-1 rounded-sm content-compact text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            data-testid="waiting-decision-source-link"
          >
            <Icon name="external-link" size={16} aria-hidden />
            {t_i18n("Open the source")}
          </Link>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

/** Kind filter choices, in the From column's order. */
const KINDS = Object.keys(SOURCE_LABELS) as WaitingDecision["kind"][];

/**
 * @param toolbarLead the view switch of the host page, first on the toolbar row, so the
 *   reader leaves this view from the list's own toolbar (no row between banner and list).
 */
export function WaitingForYou({ toolbarLead }: { toolbarLead?: ReactNode } = {}) {
  const { t_i18n, intl } = useFormatter();
  const waiting = useWaitingDecisions();
  const [openId, setOpenId] = useState<string | null>(null);

  // The read switch off: nothing here, the per-engine views stay the reader's.
  if (waiting.data && !waiting.data.enabled) return null;
  const rows = waiting.data?.items ?? [];
  const open = rows.find((row) => row.id === openId) ?? null;

  const columns: DataTableColumn<WaitingDecision>[] = [
    {
      key: "decision",
      header: "Decision",
      cell: (row) => <TruncatedText text={row.question} />,
      csv: (row) => row.question,
      sortValue: (row) => row.question.toLowerCase(),
    },
    {
      key: "from",
      header: "From",
      width: 220,
      hideable: true,
      cell: (row) => <DecisionSourceCell row={row} />,
      csv: (row) => sourceText(row, t_i18n),
      sortValue: (row) => sourceText(row, t_i18n).toLowerCase(),
    },
    {
      key: "agent",
      header: "Agent",
      width: 180,
      hideBelow: "4xl",
      hideable: true,
      cell: (row) => (row.agent ? <TruncatedText text={row.agent} /> : null),
      csv: (row) => row.agent ?? "",
      sortValue: (row) => row.agent?.toLowerCase() ?? null,
    },
    {
      key: "asked",
      header: "Asked",
      width: 140,
      hideable: true,
      cell: (row) => (row.asked_at ? formatShortDateTimeIntl(intl, row.asked_at) : ""),
      csv: (row) => row.asked_at ?? "",
      sortValue: (row) => row.asked_at,
    },
    {
      key: "answer-by",
      header: "Answer by",
      width: 140,
      hideable: true,
      cell: (row) => (row.deadline ? formatShortDateTimeIntl(intl, row.deadline) : ""),
      csv: (row) => row.deadline ?? "",
      sortValue: (row) => row.deadline,
    },
  ];

  return (
    <div data-testid="waiting-for-you">
      {/* The one records list of the view: no heading row above its toolbar (s.4); with no
          sort chosen the rows keep the server's order, soonest deadline first, then oldest asked. */}
      <DataTable<WaitingDecision>
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        storageKey="waiting-for-you"
        layout="fixed"
        toolbarLead={toolbarLead}
        searchFields={(row) => [row.question, sourceText(row, t_i18n), row.agent]}
        searchPlaceholder={t_i18n("Search decisions...")}
        filters={[
          {
            key: "kind",
            label: "Kind",
            values: (row) => [row.kind],
            options: KINDS.map((kind) => ({ value: kind, label: t_i18n(SOURCE_LABELS[kind]) })),
          },
        ]}
        exportName="waiting-for-you"
        isLoading={waiting.isLoading}
        isError={waiting.isError}
        errorLabel={t_i18n("Could not load what is waiting for you.")}
        onRetry={() => void waiting.refetch()}
        emptyLabel={t_i18n("Nothing is waiting for you")}
        rowTestId="waiting-for-you-row"
        onRowClick={(row) => setOpenId(row.id)}
      />
      {/* The answer closes its own row only: it resolves once every invalidated list is read
          again, after the answered row has left and the reader may have opened the next one. */}
      {open && (
        <DecisionDialog
          key={open.id}
          row={open}
          onClose={() => setOpenId((current) => (current === open.id ? null : current))}
        />
      )}
    </div>
  );
}
