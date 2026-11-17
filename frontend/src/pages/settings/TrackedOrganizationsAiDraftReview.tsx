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
 * The review page of a tracked-organizations AI draft (#9484 S-c2,
 * dev-docs/ctem-fleets.md section 6.1): `/settings/organization/tracked/ai-draft/<id>`,
 * a page section with its own URL, never a dialog. The drafted rows sit in the
 * shared data table (the tracked organizations list's own table); each row is
 * accepted or rejected, alone or by selection, and nothing is created before
 * "Create accepted". A name already tracked is a conflict row, never created and
 * never selectable; a failed batch keeps the other batches. Every organization
 * created starts with monitoring off (section 9.7).
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Check, Network, X } from "lucide-react";

import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { type OrganizationCriticality, type TrackedDraftRow, useDecideTrackedDraft, useTrackedOrganizationsDraft } from "@/hooks/api/useOrganizationsApi";
import { useFormatter } from "@/i18n";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";
import { decideBody, failedBatches, isDecidable, linkedOpenCti } from "@/lib/tracked-organizations-ai-draft";

import { TrackedOrganizationsAiDraftDialog } from "./TrackedOrganizationsAiDraftDialog";
import { Spinner } from "@filigran/design-system";

/** @i18n-keys */
const RELATIONSHIP_LABELS = { provider: "Provider", customer: "Customer", partner: "Partner", subsidiary: "Subsidiary" } as const;

/** @i18n-keys */
const CONFIDENCE_LABELS = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" } as const;

/**
 * The batch errors the backend writes (services/organization_tracked_ai_draft.py), said in
 * the reader's language; an unknown one reads as a failed call, its own text kept for the
 * details tooltip only. @i18n-keys
 */
const BATCH_ERROR_LABELS = {
  noProposal: "The model proposed nothing for these names",
  cut: "The model's answer was cut short",
  failed: "The model call failed",
} as const;

function batchErrorLabel(error: string): (typeof BATCH_ERROR_LABELS)[keyof typeof BATCH_ERROR_LABELS] {
  if (error.startsWith("The model answered no proposal")) return BATCH_ERROR_LABELS.noProposal;
  if (error.startsWith("The model's answer was cut")) return BATCH_ERROR_LABELS.cut;
  return BATCH_ERROR_LABELS.failed;
}

const NOT_PENDING = <span className="text-default-secondary">-</span>;

const CONFIDENCE_RANK = { low: 0, medium: 1, high: 2 } as const;
const CRITICALITY_RANK: Record<OrganizationCriticality, number> = { low: 0, medium: 1, high: 2, critical: 3 };

const BACK_TO = "/settings/organization";

export default function TrackedOrganizationsAiDraftReview() {
  const { proposalId } = useParams<{ proposalId: string }>();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const draft = useTrackedOrganizationsDraft(proposalId);
  const decide = useDecideTrackedDraft();
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [rejected, setRejected] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string[]>([]);
  const [picked, setPicked] = useState<Record<string, string>>({});
  /** The names of the failed batch Retry drafts again, in a fresh run; null: the dialog is closed. */
  const [retryNames, setRetryNames] = useState<string[] | null>(null);

  const rows = draft.data?.rows ?? [];
  const decided = draft.data?.status !== undefined && draft.data.status !== "pending";
  const decidable = rows.filter(isDecidable);
  const acceptedCount = decidable.filter((row) => accepted.has(row.name)).length;
  const batches = failedBatches(rows);

  const mark = (names: readonly string[], accept: boolean) => {
    const keep = names.filter((name) => decidable.some((row) => row.name === name));
    setAccepted((prev) => {
      const next = new Set(prev);
      for (const name of keep) {
        if (accept) next.add(name);
        else next.delete(name);
      }
      return next;
    });
    setRejected((prev) => {
      const next = new Set(prev);
      for (const name of keep) {
        if (accept) next.delete(name);
        else next.add(name);
      }
      return next;
    });
    setSelected([]);
  };

  const finish = (accept: boolean) => {
    if (!proposalId) return;
    const body = accept ? decideBody(rows, accepted, picked) : { accept: [], opencti_ids: {} };
    decide.mutate(
      { proposalId, accept: body.accept, openctiIds: body.opencti_ids },
      {
        onSuccess: (result) => {
          toast({
            title: accept
              ? t_i18n("{count, plural, one {# organization created, monitoring off} other {# organizations created, monitoring off}}", { count: result.created.length })
              : t_i18n("Draft discarded"),
            variant: "success",
          });
          navigate(BACK_TO);
        },
      },
    );
  };

  const stateBadge = (row: TrackedDraftRow) => {
    if (row.status === "conflict") return <Badge variant="secondary" className="whitespace-nowrap">{t_i18n("Already tracked")}</Badge>;
    if (row.status === "error") return <Badge variant="destructive" className="whitespace-nowrap">{t_i18n("Not drafted")}</Badge>;
    if (row.status === "accepted" || accepted.has(row.name)) return <Badge variant="success" className="whitespace-nowrap">{t_i18n("Accepted")}</Badge>;
    if (row.status === "rejected" || rejected.has(row.name)) return <Badge variant="outline" className="whitespace-nowrap">{t_i18n("Rejected")}</Badge>;
    return <Badge variant="outline" className="whitespace-nowrap">{t_i18n("To review")}</Badge>;
  };

  const openctiCell = (row: TrackedDraftRow) => {
    if (!isDecidable(row)) return <span className="text-default-secondary">-</span>;
    if (row.opencti_matches.length === 0) return <span className="text-default-secondary">{t_i18n("No match")}</span>;
    return (
      <SearchableSelect
        aria-label={t_i18n("OpenCTI organization of {name}", { name: row.name })}
        className="min-w-[160px]"
        searchable="auto"
        clearable
        placeholder={t_i18n("Pick one")}
        options={row.opencti_matches.map((match) => ({ value: match.id, label: match.name }))}
        value={linkedOpenCti(row, picked)}
        onValueChange={(value) => setPicked((prev) => ({ ...prev, [row.name]: value }))}
        disabled={decided}
      />
    );
  };

  const columns: DataTableColumn<TrackedDraftRow>[] = [
    {
      key: "name",
      header: "Name",
      sortValue: (row) => row.name.toLowerCase(),
      // One row height for every state (UX round 2, B2): the name cell is as tall as the
      // 36 px OpenCTI picker of a pending row; a narrow table caps the name (B1).
      cell: (row) => (
        <div className="flex min-h-9 min-w-0 flex-col justify-center" data-testid="tracked-ai-draft.name-cell">
          <span title={row.name} className="block max-w-[160px] truncate font-medium @5xl:max-w-[200px] @7xl:max-w-[260px]">
            {row.name}
          </span>
          {/* Plain text: a conflict row opens Tracked organizations (rowHref, #12070). */}
          {row.status === "conflict" && row.already_tracked_id && (
            <span
              title={t_i18n("See it in Tracked organizations")}
              className="block max-w-[160px] truncate content-compact text-default-secondary @5xl:max-w-[200px] @7xl:max-w-[260px]"
              data-testid="tracked-ai-draft.conflict-hint"
            >
              {t_i18n("See it in Tracked organizations")}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "state",
      header: "Review",
      hideable: true,
      sortValue: (row) => row.status,
      cell: stateBadge,
    },
    {
      key: "confidence",
      header: "Match confidence",
      hideable: true,
      sortValue: (row) => CONFIDENCE_RANK[row.confidence],
      firstSort: "desc",
      // Steps aside at 1024 so the picker and the row actions stay on screen (UX round 2, B1);
      // still a filter, a sort and the basis tooltip from 1280 up.
      hideBelow: "4xl",
      cell: (row) =>
        isDecidable(row) ? (
          <Tip content={<p className="max-w-xs content-compact">{row.basis || t_i18n("No basis given")}</p>}>
            <BadgeFocusTrigger aria-label={t_i18n("Basis for {name}", { name: row.name })}>
              <Badge variant={row.confidence === "low" ? "warning" : "outline"} className="whitespace-nowrap">
                {t_i18n(CONFIDENCE_LABELS[row.confidence])}
              </Badge>
            </BadgeFocusTrigger>
          </Tip>
        ) : (
          NOT_PENDING
        ),
    },
    {
      key: "sector",
      header: "Sector",
      hideable: true,
      sortValue: (row) => row.sector.toLowerCase(),
      // Only in a wide table (80rem), on one line with the full text as its tooltip (UX round 2, B1, B2).
      hideBelow: "7xl",
      cell: (row) => (
        <span title={row.sector || undefined} className="block max-w-[160px] truncate text-default-secondary">
          {row.sector || "-"}
        </span>
      ),
    },
    {
      key: "relationship",
      header: "Relationship",
      hideable: true,
      sortValue: (row) => (isDecidable(row) && row.relationship ? t_i18n(RELATIONSHIP_LABELS[row.relationship]) : ""),
      hideBelow: "4xl",
      cell: (row) =>
        isDecidable(row) && row.relationship ? (
          <Badge variant="secondary" className="whitespace-nowrap">
            {t_i18n(RELATIONSHIP_LABELS[row.relationship])}
          </Badge>
        ) : (
          NOT_PENDING
        ),
    },
    {
      key: "criticality",
      header: "Criticality",
      hideable: true,
      sortValue: (row) => (isDecidable(row) ? CRITICALITY_RANK[row.criticality] : -1),
      firstSort: "desc",
      hideBelow: "4xl",
      cell: (row) =>
        isDecidable(row) ? (
          <Badge variant="outline" className="whitespace-nowrap">
            {t_i18n(CRITICALITY_LABELS[row.criticality])}
          </Badge>
        ) : (
          NOT_PENDING
        ),
    },
    {
      key: "opencti",
      header: "OpenCTI organization",
      hideable: true,
      interactive: true,
      cell: openctiCell,
    },
  ];

  const filters: DataTableFilter<TrackedDraftRow>[] = [
    {
      key: "relationship",
      label: "Relationship",
      values: (row) => (isDecidable(row) ? [row.relationship] : []),
      options: (Object.keys(RELATIONSHIP_LABELS) as (keyof typeof RELATIONSHIP_LABELS)[]).map((value) => ({ value, label: t_i18n(RELATIONSHIP_LABELS[value]) })),
    },
    {
      key: "criticality",
      label: "Criticality",
      values: (row) => (isDecidable(row) ? [row.criticality] : []),
      options: (Object.keys(CRITICALITY_LABELS) as OrganizationCriticality[]).map((value) => ({ value, label: t_i18n(CRITICALITY_LABELS[value]) })),
      // Ordinal scale: low to critical, never alphabetical (#12289).
      ordered: true,
    },
    {
      key: "confidence",
      label: "Match confidence",
      values: (row) => (isDecidable(row) ? [row.confidence] : []),
      options: (Object.keys(CONFIDENCE_LABELS) as (keyof typeof CONFIDENCE_LABELS)[]).map((value) => ({ value, label: t_i18n(CONFIDENCE_LABELS[value]) })),
      // Ordinal scale: high to low confidence (#12289).
      ordered: true,
    },
  ];

  const selectable = selected.filter((name) => decidable.some((row) => row.name === name));

  return (
    <div className="space-y-6" data-testid="tracked-ai-draft.review">
      <EntityPageHeader
        backTo={BACK_TO}
        backLabel="Back to Organization"
        icon={Network}
        title={t_i18n("Review drafted organizations")}
        description={t_i18n("Accept the organizations to track. Nothing is created until you select Create accepted; each one starts with monitoring off.")}
        actions={
          !decided && rows.length > 0 ? (
            <>
              <Button variant="ghost" onClick={() => finish(false)} disabled={decide.isPending}>
                {t_i18n("Discard draft")}
              </Button>
              <Button onClick={() => finish(true)} disabled={acceptedCount === 0 || decide.isPending} aria-busy={decide.isPending || undefined}>
                {decide.isPending && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Create accepted ({count})", { count: acceptedCount })}
              </Button>
            </>
          ) : undefined
        }
      />

      {draft.data && (
        <p className="content-base" data-testid="tracked-ai-draft.count">
          {t_i18n("{count} proposals, {accepted} accepted", { count: rows.length, accepted: acceptedCount })}
          {draft.data.opencti_unavailable && (
            <span className="text-default-secondary">
              {" "}
              {t_i18n("OpenCTI was not searched: {reason}", { reason: draft.data.opencti_unavailable })}{" "}
              <a className="text-accent underline" href="/settings/integrations">
                {t_i18n("Settings > Integrations")}
              </a>
            </span>
          )}
        </p>
      )}
      {decided && (
        <p role="status" className="content-compact text-default-secondary">
          {t_i18n("This draft was already decided; nothing more can be created from it.")}
        </p>
      )}
      {batches.map((batch) => (
        <div key={batch.error} role="alert" className="flex flex-wrap items-center gap-2 content-compact text-feedback-error-primary">
          <span>
            {t_i18n("{count, plural, one {# name was not drafted: {reason}} other {# names were not drafted: {reason}}}", {
              count: batch.names.length,
              reason: t_i18n(batchErrorLabel(batch.error)),
            })}
          </span>
          {batch.error && (
            <Tip content={<p className="max-w-xs content-compact">{batch.error}</p>}>
              <BadgeFocusTrigger aria-label={t_i18n("Details")}>
                <Badge variant="outline" className="whitespace-nowrap">
                  {t_i18n("Details")}
                </Badge>
              </BadgeFocusTrigger>
            </Tip>
          )}
          <Button variant="secondary" size="sm" onClick={() => setRetryNames(batch.names)}>
            {t_i18n("Retry")}
          </Button>
        </div>
      ))}
      {decide.isError && (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The organizations were not created: {reason}", { reason: decide.error.message })}
        </p>
      )}

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.name}
        rowHref={(row) => (row.status === "conflict" && row.already_tracked_id ? BACK_TO : "")}
        storageKey="settings-tracked-organizations-ai-draft"
        rowTestId="tracked-ai-draft.row"
        defaultSort={{ key: "name", direction: "asc" }}
        searchFields={(row) => [row.name, row.sector]}
        searchPlaceholder={t_i18n("Search organizations...")}
        filters={filters}
        isLoading={draft.isLoading}
        isError={draft.isError}
        errorLabel={t_i18n("The draft could not be read. It may have been discarded.")}
        onRetry={() => draft.refetch()}
        emptyLabel={t_i18n("The file held no organization name")}
        noResultsLabel={t_i18n("No organization matches these filters")}
        selection={
          decided
            ? undefined
            : {
                selected,
                onChange: (keys) => setSelected(keys.filter((key) => decidable.some((row) => row.name === key))),
                disabledReason: (key) => {
                  const row = rows.find((r) => r.name === key);
                  if (row?.status === "conflict") return t_i18n("Already tracked");
                  if (row?.status === "error") return t_i18n("Not drafted, retry the batch");
                  if (row && !isDecidable(row)) return t_i18n("Already decided");
                  return undefined;
                },
                actions: (
                  <>
                    <Button size="sm" variant="secondary" className="gap-1.5" disabled={selectable.length === 0} onClick={() => mark(selectable, true)}>
                      <Check className="h-4 w-4" /> {t_i18n("Accept selected")}
                    </Button>
                    <Button size="sm" variant="ghost" className="gap-1.5" disabled={selectable.length === 0} onClick={() => mark(selectable, false)}>
                      <X className="h-4 w-4" /> {t_i18n("Reject selected")}
                    </Button>
                  </>
                ),
              }
        }
        rowActions={(row) =>
          !decided && isDecidable(row) ? (
            <span className="inline-flex gap-1">
              <Button variant="ghost" size="icon" aria-label={t_i18n("Accept {name}", { name: row.name })} onClick={() => mark([row.name], true)}>
                <Check className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" aria-label={t_i18n("Reject {name}", { name: row.name })} onClick={() => mark([row.name], false)}>
                <X className="h-4 w-4" />
              </Button>
            </span>
          ) : null
        }
      />
      {retryNames && (
        <TrackedOrganizationsAiDraftDialog open initialNames={retryNames} onOpenChange={(next) => !next && setRetryNames(null)} />
      )}
    </div>
  );
}
