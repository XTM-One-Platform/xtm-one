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
 * The ROI records list a review-rate figure opens (#10570): one list of the
 * scope's records, runs and work items alike, under the report's window and
 * the review filter, so the number on the card is the number of rows the list
 * holds. The card's link and the list's query are built here from one
 * `RoiRecordsQuery`, so they cannot drift apart. Pure, so `yarn test:unit`
 * covers it.
 */

import { workItemPath } from "./task-routes.ts";
import type { RoiWindow } from "@/hooks/api/useRoiReportApi";
import { parseValueWindow } from "./roi-value.ts";

/** The review filter of the list: the records nobody reviewed, or every record. */
export type RoiRecordsReview = "unreviewed" | "all";

export interface RoiRecordsQuery {
  scope: "agent" | "fleet";
  id: string;
  window: RoiWindow;
  review: RoiRecordsReview;
}

/** The records list's path segment under the agent's Activity tab and the fleet's ROI tab. */
export const ROI_RECORDS_SEGMENT = "records";

/** The `review` URL value of the list; the existing review filter of the runs lists (#9365). */
export const ROI_RECORDS_REVIEW_PARAM = "review";

/** Where a scope's records list lives, with its window and review filter in the URL. */
export function roiRecordsPath(query: RoiRecordsQuery): string {
  const base =
    query.scope === "agent"
      ? `/agents/${query.id}/activity/${ROI_RECORDS_SEGMENT}`
      : `/fleets/${query.id}/roi/${ROI_RECORDS_SEGMENT}`;
  const params = new URLSearchParams();
  if (query.review === "unreviewed") params.set(ROI_RECORDS_REVIEW_PARAM, "unreviewed");
  params.set("window", query.window);
  return `${base}?${params.toString()}`;
}

/** The list's query read back from its URL: `review=unreviewed` or every record, the window as the ROI views read it. */
export function readRoiRecordsQuery(scope: RoiRecordsQuery["scope"], id: string, params: URLSearchParams): RoiRecordsQuery {
  return {
    scope,
    id,
    window: parseValueWindow(params.get("window")),
    review: params.get(ROI_RECORDS_REVIEW_PARAM) === "unreviewed" ? "unreviewed" : "all",
  };
}

/**
 * The query string `GET /roi/records` and `GET /roi/records/count` take: the
 * scope, the window and the outcome filter, never a kind filter, so runs and
 * work items are listed alike, as the report counts them. `search` (the list's
 * search box, a title match) goes to both, so the count stays the rows' count.
 */
export function roiRecordsApiParams(query: RoiRecordsQuery, search = ""): URLSearchParams {
  const params = new URLSearchParams();
  params.set(query.scope === "agent" ? "agent_id" : "fleet_id", query.id);
  params.set("window", query.window);
  if (query.review === "unreviewed") params.set("outcome", "unreviewed");
  const needle = search.trim();
  if (needle) params.set("search", needle);
  return params;
}

/** Where one record opens: its work item, else its run. */
export function roiRecordHref(record: { kind: "run" | "work_item"; run_id: string | null; work_item_id: string | null }): string | undefined {
  if (record.kind === "work_item" && record.work_item_id) return workItemPath(record.work_item_id);
  if (record.run_id) return `/operations/executions/${record.run_id}`;
  return undefined;
}

/**
 * What the review-rate figure counts: every record of the report (runs and
 * work items), of which `reviewed` a person reviewed. The card's caption and
 * the list it opens read the same total.
 */
export function roiReviewCounts(figures: { runs: number; reviewed: number; unreviewed: number }): {
  reviewed: number;
  total: number;
  unreviewed: number;
} {
  return { reviewed: figures.reviewed, total: figures.runs, unreviewed: figures.unreviewed };
}
