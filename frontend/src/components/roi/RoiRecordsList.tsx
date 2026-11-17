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
 * The ROI records of one agent or fleet (#10570): the list the review-rate
 * figure opens, runs and work items alike with a Kind column, under the
 * report's window and the review filter read from the URL. The figure and
 * this list build their query from one `RoiRecordsQuery` (`lib/roi-records`),
 * so the number on the card is the number of rows here. Review is optional:
 * the filter reads "Not reviewed yet", never a backlog. The table's search,
 * sort and page live in the URL (`rec_` keys, DataTable `urlKey`), beside the
 * review filter and the window, so reload and back keep the whole view.
 */
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { DataTable, type DataTableColumn, type DataTableState } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useRoiUnits } from "@/hooks/api/useRoiCatalogApi";
import { useRoiRecordCount, useRoiRecords, type RoiRecordRow } from "@/hooks/api/useRoiRecordsApi";
import { useFormatter } from "@/i18n";
import { readTableState, writeTableState, type DataTableDefaults, type DataTableSort } from "@/lib/data-table-state";
import { readRoiRecordsQuery, roiRecordHref, ROI_RECORDS_REVIEW_PARAM, type RoiRecordsReview } from "@/lib/roi-records";

const PAGE_SIZE = 25;

/** The prefix of the table's keys in the URL (search, sort, page). */
const URL_KEY = "rec_";

/** The list's one sortable column: the date, which the API orders (server mode). */
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };
const DEFAULTS: DataTableDefaults = { sort: DEFAULT_SORT, pageSize: PAGE_SIZE };

/** The Kind cell and the review filter's options. @i18n-keys */
const KIND_LABELS = { run: "Run", work_item: "Work item" } as const;
const REVIEW_LABELS = { unreviewed: "Not reviewed yet", all: "Every record" } as const satisfies Record<RoiRecordsReview, string>;
const OUTCOME_LABELS = { accepted: "Accepted", modified: "Modified", rejected: "Rejected" } as const;

export function RoiRecordsList({ scope, id }: { scope: "agent" | "fleet"; id: string }) {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const query = readRoiRecordsQuery(scope, id, searchParams);
  const state: DataTableState = readTableState(searchParams, URL_KEY, DEFAULTS);
  const direction = state.sort?.key === "created_at" ? state.sort.direction : DEFAULT_SORT.direction;
  const records = useRoiRecords(query, { page: state.page, pageSize: state.pageSize, direction, search: state.search });
  const count = useRoiRecordCount(query, state.search);
  // Every record of the window, before the review filter and the search: "n of N" once they narrow it (#12039).
  const countAll = useRoiRecordCount({ ...query, review: "all" });
  const units = useRoiUnits();
  const unitNames = useMemo(() => new Map((units.data ?? []).map((u) => [u.key, u.name])), [units.data]);

  const setReview = (next: RoiRecordsReview) => {
    setSearchParams(
      (previous) => {
        const out = new URLSearchParams(previous);
        if (next === "unreviewed") out.set(ROI_RECORDS_REVIEW_PARAM, "unreviewed");
        else out.delete(ROI_RECORDS_REVIEW_PARAM);
        // Another filter: back to the first page, the search and sort kept.
        return writeTableState(out, URL_KEY, { ...readTableState(out, URL_KEY, DEFAULTS), page: 0 }, DEFAULTS);
      },
      { replace: true },
    );
  };

  const columns = useMemo<DataTableColumn<RoiRecordRow>[]>(
    () => [
      {
        key: "title",
        header: "Record title",
        width: 240,
        cell: (r) => (
          <span className="block truncate" title={r.title ?? undefined}>
            {r.title ?? "-"}
          </span>
        ),
        csv: (r) => r.title ?? "",
      },
      { key: "kind", header: "Kind", width: 120, cell: (r) => t_i18n(KIND_LABELS[r.kind]), csv: (r) => r.kind },
      {
        key: "unit",
        header: "Unit",
        width: 160,
        hideBelow: "4xl",
        hideable: true,
        // The unit's name as the catalog shows it; the export keeps the key.
        cell: (r) => (
          <span className="block truncate" title={unitNames.get(r.unit) ?? r.unit}>
            {unitNames.get(r.unit) ?? r.unit}
          </span>
        ),
        csv: (r) => r.unit,
      },
      {
        key: "outcome",
        header: "Outcome",
        width: 140,
        cell: (r) => (r.outcome ? t_i18n(OUTCOME_LABELS[r.outcome]) : t_i18n("Not reviewed yet")),
        csv: (r) => r.outcome ?? "",
      },
      {
        key: "created_at",
        header: "Date",
        width: 160,
        hideBelow: "3xl",
        hideable: true,
        cell: (r) => <DateTimeCell value={r.created_at} />,
        csv: (r) => r.created_at ?? "",
        sortValue: () => null,
        firstSort: "desc",
      },
    ],
    [t_i18n, unitNames],
  );

  return (
    <div data-testid="roi-records-list">
      <DataTable
        rows={records.data ?? []}
        columns={columns}
        rowKey={(r) => r.id}
        server={{ total: count.data?.total ?? 0 }}
        totalAll={countAll.data?.total}
        urlKey={URL_KEY}
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        searchPlaceholder={t_i18n("Search the records by title...")}
        noFilters="The review filter in the toolbar is the list's one dimension."
        // In the toolbar beside the search, sized to its segments like the
        // Activity tab's All / Unreviewed filter, never a full-width track.
        toolbar={
          <SegmentedFilter
            options={(Object.keys(REVIEW_LABELS) as RoiRecordsReview[]).map((key) => ({ key, label: REVIEW_LABELS[key] }))}
            value={query.review}
            onChange={setReview}
            ariaLabel={t_i18n("Review")}
            testId="roi-records-review"
            className="w-fit"
          />
        }
        layout="fixed"
        isLoading={records.isLoading || count.isLoading}
        isError={records.isError || count.isError}
        errorLabel={t_i18n("The records could not be loaded.")}
        onRetry={() => {
          void records.refetch();
          void count.refetch();
        }}
        emptyLabel={t_i18n("No record in this period.")}
        rowHref={(r) => roiRecordHref(r) ?? ""}
        rowTestId="roi-record-row"
      />
    </div>
  );
}
