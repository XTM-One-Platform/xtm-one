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

/** ROI outcome review (#9310, #9365): mark a run's or a work item's record. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RoiOutcome } from "@/types";

export interface RoiOutcomeReview {
  /** `null` returns the record to unreviewed. */
  outcome: RoiOutcome | null;
  review_min?: number | null;
  note?: string | null;
}

/** `PUT /roi/records/{record_id}/outcome`; refreshes the run, the work item and the runs lists. */
export function useReviewRoiOutcome(recordId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RoiOutcomeReview) => api.put(`/roi/records/${recordId}/outcome`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["assignment-run"] });
      qc.invalidateQueries({ queryKey: ["work-items"] });
      qc.invalidateQueries({ queryKey: ["all-assignment-runs"] });
      qc.invalidateQueries({ queryKey: ["assignment-runs"] });
      qc.invalidateQueries({ queryKey: ["assignment-run-count"] });
    },
  });
}
