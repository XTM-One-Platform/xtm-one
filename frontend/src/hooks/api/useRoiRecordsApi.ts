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
 * The ROI records list (`GET /roi/records`, `GET /roi/records/count`, #10570):
 * one page of a scope's records, runs and work items alike, and their total
 * under the same filter (the search box included). Reading never starts a run
 * nor calls a model.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { roiRecordsApiParams, type RoiRecordsQuery } from "@/lib/roi-records";
import type { RoiOutcome } from "@/types";

export interface RoiRecordRow {
  id: string;
  kind: "run" | "work_item";
  title: string | null;
  run_id: string | null;
  work_item_id: string | null;
  agent_id: string | null;
  fleet_id: string | null;
  unit: string;
  units: number;
  outcome: RoiOutcome | null;
  created_at: string | null;
}

export interface RoiRecordsPage {
  page: number;
  pageSize: number;
  /** By date: `desc` newest first (the default), `asc` oldest first. */
  direction: "asc" | "desc";
  /** The search box: a title match, sent to the list and its count alike. */
  search: string;
}

/** One page of the records under the query and the table's search. */
export function useRoiRecords(query: RoiRecordsQuery, page: RoiRecordsPage) {
  const params = roiRecordsApiParams(query, page.search);
  params.set("direction", page.direction);
  params.set("limit", String(page.pageSize));
  params.set("offset", String(page.page * page.pageSize));
  return useQuery<RoiRecordRow[]>({
    queryKey: ["roi-records", params.toString()],
    queryFn: () => api.get(`/roi/records?${params.toString()}`),
  });
}

/** How many records the list pages through: the same query and search as `useRoiRecords`. */
export function useRoiRecordCount(query: RoiRecordsQuery, search = "") {
  const params = roiRecordsApiParams(query, search);
  return useQuery<{ total: number }>({
    queryKey: ["roi-records-count", params.toString()],
    queryFn: () => api.get(`/roi/records/count?${params.toString()}`),
  });
}
