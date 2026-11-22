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
 * tool approval, a flow, a work item), with the same columns. A row opens the
 * shared `DecisionCard` in a side panel and answers there through the one
 * answer path (`POST /decisions/{id}/answer`, surface `inbox`); the source
 * stays a link under the card.
 *
 * Rendered only while the read switch is on (`enabled`): otherwise nothing,
 * and the per-engine views stay the reader's.
 */
import { useState } from "react";
import { Icon, type IconName } from "@filigran/design-system";
import { Link } from "react-router-dom";

import { DecisionCard, type DecisionAnswer, type DecisionView } from "@/components/decisions/DecisionCard";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SlideOver } from "@/components/ui/slide-over";
import { useToast } from "@/components/ui/toast";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAnswerDecision, useWaitingDecisions, type WaitingDecision } from "@/hooks/api/useDecisionApi";
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

function DecisionPanel({ row, onClose }: { row: WaitingDecision; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const answer = useAnswerDecision();

  const send = async ({ option, text }: DecisionAnswer) => {
    if (!option && !text) return;
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
    <SlideOver open onClose={onClose} label={t_i18n("Waiting for you")} width="w-[520px]">
      <div className="flex items-center justify-between border-b border-elevation-subtle px-5 py-3">
        <h2 className="content-base font-semibold">{t_i18n("Waiting for you")}</h2>
        <Button type="button" variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={onClose} autoFocus>
          <Icon name="x" size={16} aria-hidden />
        </Button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-5" data-testid="waiting-for-you-panel">
        <DecisionCard
          view={waitingDecisionView(row)}
          canAnswer
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
      </div>
    </SlideOver>
  );
}

export function WaitingForYou() {
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
    },
    {
      key: "from",
      header: "From",
      width: 220,
      cell: (row) => (
        <span className="flex min-w-0 items-center gap-1.5">
          <Icon name={SOURCE_ICONS[row.kind]} size={16} className="shrink-0 text-default-secondary" aria-hidden />
          <TruncatedText text={sourceText(row, t_i18n)} />
        </span>
      ),
      csv: (row) => sourceText(row, t_i18n),
    },
    {
      key: "agent",
      header: "Agent",
      width: 180,
      hideBelow: "4xl",
      cell: (row) => (row.agent ? <TruncatedText text={row.agent} /> : null),
      csv: (row) => row.agent ?? "",
    },
    {
      key: "asked",
      header: "Asked",
      width: 140,
      cell: (row) => (row.asked_at ? formatShortDateTimeIntl(intl, row.asked_at) : ""),
      csv: (row) => row.asked_at ?? "",
    },
    {
      key: "answer-by",
      header: "Answer by",
      width: 140,
      cell: (row) => (row.deadline ? formatShortDateTimeIntl(intl, row.deadline) : ""),
      csv: (row) => row.deadline ?? "",
    },
  ];

  return (
    <section className="space-y-2" aria-labelledby="waiting-for-you-heading" data-testid="waiting-for-you">
      <h2 id="waiting-for-you-heading" className="content-base font-semibold text-default-primary">
        {t_i18n("Decisions waiting for you")}
      </h2>
      <DataTable<WaitingDecision>
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        paginate={false}
        layout="fixed"
        noFilters="one served list, ordered by the server: soonest deadline first, then oldest asked"
        isLoading={waiting.isLoading}
        isError={waiting.isError}
        errorLabel={t_i18n("Could not load what is waiting for you.")}
        onRetry={() => void waiting.refetch()}
        emptyLabel={t_i18n("Nothing is waiting for you")}
        rowTestId="waiting-for-you-row"
        onRowClick={(row) => setOpenId(row.id)}
      />
      {open && <DecisionPanel key={open.id} row={open} onClose={() => setOpenId(null)} />}
    </section>
  );
}
