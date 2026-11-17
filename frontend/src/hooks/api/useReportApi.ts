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

import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unavailableIsOrdinary } from "@/lib/api";
import type { Report } from "@/types";

const REPORTS_KEY = ["reports"] as const;

/** Fetch one agent-generated report by id.
 *
 *  Read-only by design: reports are written exclusively through the agent-facing
 *  `generate_report` tool, which validates the spec first, so no hook here
 *  writes a report.
 *
 *  A deleted report (404) or one the viewer may not read (403) is shown as no
 *  longer available by the chat card and the viewer, never thrown: one gone
 *  report would otherwise replace the whole conversation with an error page. */
export function useReport(id: string | undefined) {
  return useQuery<Report>({
    queryKey: [...REPORTS_KEY, id],
    queryFn: () => api.get(`/reports/${id}`),
    enabled: !!id,
    ...unavailableIsOrdinary,
  });
}

/**
 * Server-side PDF of a report: the platform prints the report in its
 * own headless browser, as the viewer's print route draws it, and streams the
 * file back. Resolves with the blob and the filename the server chose
 * (`<slugified title>.pdf`). Nothing is written.
 */
export function useExportReport() {
  return useMutation({
    mutationFn: (id: string) => api.postDownload(`/reports/${id}/export`),
  });
}
