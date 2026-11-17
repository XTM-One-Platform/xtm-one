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
 * The "From chat, estimated" series of the ROI views (#10544 S5; design
 * dev-docs/roi-outcomes.md section 11): the deliverables an agent finished in
 * a conversation (decks, reports, videos, documents), counted on the low side
 * with its own lines. Its counts and figures are the ledger's chat totals of the
 * scope (`figures`, #12077): the Chat line of By source, which the hero sums, so
 * the card and the hero never differ; a fleet's chat is the chat the ledger
 * stamps with that fleet when the deliverable is written.
 * One card on the Value page, the fleet ROI view and the agent's Value tab, each
 * with its own scope, attributed to the agent and the person. A line is read and
 * corrected ("Adjust") by its person and by administrators only (Sam, 15 Nov
 * 2026); a non-administrator's organization read is the totals and the kinds,
 * never a line. A side read: a failed read stays in the card's own error line.
 * Reading it never starts a run nor calls a model.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CompactFigure } from "@/components/ui/compact-figure";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { Tip, TIP_WRAP_FOCUS_RING } from "@/components/ui/tooltip";
import {
  useCorrectRoiChatLine,
  useRoiChatSeries,
  type RoiChatKind,
  type RoiChatLine,
  type RoiChatScope,
  type RoiChatSeries,
} from "@/hooks/api/useRoiChatApi";
import type { RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";

/** The label of each kind, read through `t_i18n`. @i18n-keys */
const KIND_LABELS = {
  presentation: "Deck",
  report: "Report",
  video: "Video",
  file: "Document",
} as const satisfies Record<RoiChatKind, string>;

/** The state of a line, read through `t_i18n`. @i18n-keys */
const STATUS_LABELS = {
  pending: "Waiting for use",
  counted: "Counted",
  corrected: "Corrected",
} as const;

type LineStatus = keyof typeof STATUS_LABELS;

/** The most hours a correction sets (`roi_chat_series.CORRECTION_MAX_MIN`, a working month). */
const CORRECTION_MAX_HOURS = 720;

export function lineStatus(line: RoiChatLine): LineStatus {
  if (!line.counted) return "pending";
  return line.adjusted_min !== null ? "corrected" : "counted";
}

/** Hours typed by a person, as the minutes the API stores; `null` when out of range. */
export function hoursToMinutes(input: string): number | null {
  const raw = input.trim().replace(",", ".");
  if (raw === "") return null;
  const hours = Number(raw);
  if (!Number.isFinite(hours) || hours < 0 || hours > CORRECTION_MAX_HOURS) return null;
  return Math.round(hours * 60 * 10) / 10;
}

function useLineSize() {
  const { t_i18n } = useFormatter();
  return (line: RoiChatLine) => {
    const count = line.size;
    if (line.kind === "presentation") return t_i18n("{count, plural, one {# slide} other {# slides}}", { count });
    if (line.kind === "video") return t_i18n("{count, plural, one {# minute of video} other {# minutes of video}}", { count });
    return t_i18n("{count, plural, one {# page} other {# pages}}", { count });
  };
}

/**
 * A free-text cell cut with an ellipsis (UX round 1 of #10681, B2): when the
 * text is wider than its cap, the design-system tooltip names it whole, on
 * hover and on keyboard focus (the span becomes a tab stop with the shared
 * ring); when it fits, no tooltip and no tab stop. No native `title`.
 * Measured again when the column or the text changes.
 *
 * UX round 2 of #10681, B2: the span is held through a callback ref, so the
 * span that is mounted is the one measured and observed. `Tip` without content
 * renders its child bare and with content inside a `TooltipTrigger`, so the
 * first `cut` remounts the span; a measurement bound to the first node then
 * read the detached one (0 wide), set `cut` back to false and the tooltip
 * never stayed.
 */
export function CutText({ text, className }: { text: string; className?: string }) {
  const [node, setNode] = useState<HTMLSpanElement | null>(null);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    if (!node) return;
    const measure = () => {
      if (node.isConnected) setCut(node.scrollWidth > node.clientWidth + 1);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node, text]);
  return (
    <Tip content={cut ? text : undefined}>
      <span
        ref={setNode}
        tabIndex={cut ? 0 : undefined}
        className={cn("truncate", cut && TIP_WRAP_FOCUS_RING, className)}
      >
        {text}
      </span>
    </Tip>
  );
}

function useColumns(currency: string): DataTableColumn<RoiChatLine>[] {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat(currency);
  const size = useLineSize();
  return useMemo<DataTableColumn<RoiChatLine>[]>(
    () => [
      {
        key: "deliverable",
        header: "Deliverable",
        cell: (line) => {
          const title = line.title || t_i18n(KIND_LABELS[line.kind]);
          return (
            <span className="flex min-w-0 max-w-[10rem] flex-col">
              <CutText text={title} className="font-medium" />
              <span className="truncate content-compact text-default-secondary">
                {t_i18n(KIND_LABELS[line.kind])} · {size(line)}
              </span>
            </span>
          );
        },
        sortValue: (line) => line.title ?? "",
        csv: (line) => line.title ?? "",
      },
      {
        key: "agent",
        header: "Agent",
        hideBelow: "3xl",
        hideable: true,
        cell: (line) => {
          const name = line.agent_name ?? t_i18n("Unknown");
          return <CutText text={name} className="block max-w-[9rem]" />;
        },
        sortValue: (line) => line.agent_name ?? "",
        csv: (line) => line.agent_name ?? "",
      },
      {
        key: "person",
        header: "Person",
        hideBelow: "4xl",
        hideable: true,
        cell: (line) => {
          const name = line.user_name ?? t_i18n("Unknown");
          return <CutText text={name} className="block max-w-[9rem]" />;
        },
        sortValue: (line) => line.user_name ?? "",
        csv: (line) => line.user_name ?? "",
      },
      {
        key: "status",
        header: "Status",
        cell: (line) => {
          const status = lineStatus(line);
          return (
            <Badge variant={status === "pending" ? "outline" : "secondary"} className="whitespace-nowrap">
              {t_i18n(STATUS_LABELS[status])}
            </Badge>
          );
        },
        sortValue: (line) => lineStatus(line),
        csv: (line) => lineStatus(line),
      },
      {
        key: "hours",
        header: "Hours saved",
        align: "right",
        cell: (line) => <CompactFigure exact={f.exactCount(line.hours)}>{f.count(line.hours)}</CompactFigure>,
        sortValue: (line) => line.hours,
        csv: (line) => line.hours,
      },
      {
        key: "value",
        header: "Value",
        align: "right",
        cell: (line) => <CompactFigure exact={f.exactMoney(line.value)}>{f.money(line.value)}</CompactFigure>,
        sortValue: (line) => line.value ?? -1,
        csv: (line) => line.value ?? "",
      },
    ],
    [t_i18n, f, size],
  );
}

/** The correction of one line: hours counted, or back to the estimate. */
function AdjustDialog({ line, onClose }: { line: RoiChatLine; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const f = useNumberFormat();
  const correct = useCorrectRoiChatLine();
  const ids = useId();
  const field = useRef<HTMLInputElement | null>(null);
  const [hours, setHours] = useState(String(Math.round((line.minutes / 60) * 100) / 100));
  const [error, setError] = useState<string | null>(null);
  const estimate = line.estimate;
  const asHours = (minutes: number) => f.exactCount(Math.round((minutes / 60) * 100) / 100);

  const send = (minutes: number | null) =>
    correct.mutate(
      { id: line.id, minutes },
      {
        onSuccess: () => {
          toast({ title: t_i18n(minutes === null ? "Back to the estimate" : "Hours saved updated"), variant: "success" });
          onClose();
        },
        onError: (failure) =>
          toast({ title: t_i18n("Could not update the hours"), description: failure instanceof Error ? failure.message : undefined, variant: "error" }),
      },
    );

  const save = () => {
    const minutes = hoursToMinutes(hours);
    if (minutes === null) {
      setError(t_i18n("Enter a number of hours between 0 and {max}.", { max: CORRECTION_MAX_HOURS }));
      field.current?.focus();
      return;
    }
    setError(null);
    send(minutes);
  };

  const fieldId = `${ids}-hours`;
  const helpId = `${ids}-help`;
  const errorId = `${ids}-error`;
  const busy = correct.isPending;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md" data-testid="roi-chat-adjust">
        <DialogHeader>
          <DialogTitle>{t_i18n("Adjust the hours saved")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Estimated {estimate} h: {low} h by hand at the low bound, minus {subtracted} h for the time spent in the conversation, at most {cap} h.", {
              estimate: asHours(estimate.counted_min),
              low: asHours(estimate.manual_min_low),
              subtracted: asHours(estimate.subtracted_min),
              cap: asHours(estimate.cap_min),
            })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor={fieldId} id={`${fieldId}-label`}>
            {t_i18n("Hours saved")}
          </Label>
          <Input
            id={fieldId}
            aria-labelledby={`${fieldId}-label`}
            ref={field}
            type="number"
            inputMode="decimal"
            min={0}
            max={CORRECTION_MAX_HOURS}
            step="any"
            className="w-32"
            value={hours}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : helpId}
            onChange={(event) => {
              setHours(event.target.value);
              if (error && hoursToMinutes(event.target.value) !== null) setError(null);
            }}
          />
          {error ? (
            <p id={errorId} className="content-compact text-feedback-error-primary">{error}</p>
          ) : (
            <p id={helpId} className="content-compact text-default-secondary">
              {t_i18n("Your figure replaces the estimate for everyone who reads it. Nothing waits on it.")}
            </p>
          )}
        </div>
        <DialogFooter className="gap-2">
          {line.adjusted_min !== null && (
            <Button type="button" variant="outline" onClick={() => send(null)} disabled={busy}>
              {t_i18n("Back to the estimate")}
            </Button>
          )}
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <Button type="button" onClick={save} disabled={busy} aria-busy={busy || undefined}>
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The organization's deliverables by kind, without any line: what a non-administrator reads of the organization. */
function KindTotals({ byKind }: { byKind: RoiChatSeries["by_kind"] }) {
  const { t_i18n } = useFormatter();
  const kinds = (Object.keys(KIND_LABELS) as RoiChatKind[]).filter((kind) => (byKind[kind]?.deliverables ?? 0) > 0);
  return (
    <div className="space-y-2" data-testid="roi-chat-kinds">
      {kinds.length > 0 && (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 content-compact">
          {kinds.map((kind) => (
            <div key={kind} className="flex items-baseline gap-2">
              <dt className="text-default-secondary">{t_i18n(KIND_LABELS[kind])}</dt>
              <dd className="font-semibold">{byKind[kind].deliverables}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="content-compact text-default-secondary">
        {t_i18n("Each deliverable is listed only to the person who made it and to administrators.")}
      </p>
    </div>
  );
}

interface RoiChatSeriesCardProps {
  scope: RoiChatScope;
  window: RoiWindow;
  /** The prefix of the table's URL keys, unique on the page. */
  urlKey: string;
}

export function RoiChatSeriesCard({ scope, window, urlKey }: RoiChatSeriesCardProps) {
  const { t_i18n } = useFormatter();
  const series = useRoiChatSeries(scope, window);
  const currency = series.data?.currency ?? "USD";
  const f = useNumberFormat(currency);
  const columns = useColumns(currency);
  const [adjusting, setAdjusting] = useState<RoiChatLine | null>(null);
  const lines = series.data?.lines ?? [];
  const filters = useMemo(
    () => [
      {
        key: "kind",
        label: "Kind",
        values: (line: RoiChatLine) => [line.kind],
        options: (Object.keys(KIND_LABELS) as RoiChatKind[]).map((kind) => ({ value: kind, label: t_i18n(KIND_LABELS[kind]) })),
      },
      {
        key: "status",
        label: "Status",
        values: (line: RoiChatLine) => [lineStatus(line)],
        options: (Object.keys(STATUS_LABELS) as LineStatus[]).map((status) => ({ value: status, label: t_i18n(STATUS_LABELS[status]) })),
      },
    ],
    [t_i18n],
  );

  // The ledger's chat totals when answered (one source of truth), else the lines' own.
  const ledger = series.data?.figures?.totals;
  const own = series.data?.totals;
  const totals = own && {
    deliverables: ledger?.deliverables ?? own.deliverables,
    pending: own.pending,
    hours: ledger?.hours ?? own.hours,
    value: ledger?.value ?? own.value,
  };
  return (
    <Card className="space-y-3 p-4" data-testid="roi-chat-series">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h2 className="flex items-center gap-2 content-base font-semibold">
            {t_i18n("From chat, estimated")}
            <InfoTip
              label={t_i18n("How this is computed")}
              content={t_i18n("Decks, reports, videos and documents an agent finished in a chat, counted once used or 7 days after the last edit. Manual time (low bound) minus the chat time plus 25%. Same figure as the Chat (estimated) line of How this is computed.")}
            />
          </h2>
          {totals && (
            <p className="content-compact text-default-secondary" data-testid="roi-chat-totals">
              {t_i18n("{count, plural, one {# deliverable counted} other {# deliverables counted}}, {pending, plural, one {# waiting for use} other {# waiting for use}}", {
                count: totals.deliverables,
                pending: totals.pending,
              })}
            </p>
          )}
        </div>
        {totals && totals.deliverables > 0 && (
          <div className="flex items-baseline gap-4 content-base" data-testid="roi-chat-figures">
            <span>
              <CompactFigure exact={f.exactCount(totals.hours)} className="font-semibold">{f.count(totals.hours)}</CompactFigure>{" "}
              <span className="content-compact text-default-secondary">{t_i18n("Hours saved")}</span>
            </span>
            <span>
              <CompactFigure exact={f.exactMoney(totals.value)} className="font-semibold">{f.money(totals.value)}</CompactFigure>{" "}
              <span className="content-compact text-default-secondary">{t_i18n("Value")}</span>
            </span>
          </div>
        )}
      </div>
      {series.isError ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
          {t_i18n("The chat estimate could not be read.")}
          <Button type="button" variant="outline" size="sm" onClick={() => series.refetch()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      ) : series.data?.aggregate_only ? (
        <KindTotals byKind={series.data.by_kind} />
      ) : (
        <DataTable
          urlKey={urlKey}
          storageKey="roi-chat"
          rows={lines}
          columns={columns}
          rowKey={(line) => line.id}
          defaultSort={{ key: "hours", direction: "desc" }}
          searchFields={(line) => [line.title, line.agent_name, line.user_name]}
          searchPlaceholder={t_i18n("Search deliverables...")}
          filters={filters}
          isLoading={series.isLoading}
          emptyLabel={t_i18n("Nothing from chat in this period.")}
          rowActions={(line) =>
            line.counted && line.can_adjust ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={t_i18n("Adjust {name}", { name: line.title || t_i18n(KIND_LABELS[line.kind]) })}
                onClick={() => setAdjusting(line)}
              >
                {t_i18n("Adjust")}
              </Button>
            ) : null
          }
          rowTestId="roi-chat-row"
        />
      )}
      {adjusting && <AdjustDialog line={adjusting} onClose={() => setAdjusting(null)} />}
    </Card>
  );
}
